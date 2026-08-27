"""Policy Guard — collector 호출 전 6단 검사 (DESIGN §8.2).

DESIGN §8.2 가 정한 순서 그대로 검사한다.

1. `access_status` 가 허용 상태인가
2. `commercial_context` 와 상업 이용 조건이 맞는가
3. 인증이 유효한가
4. 정책 확인일이 TTL 안에 있는가
5. 보관 기간과 삭제 작업을 지킬 수 있는가
6. 요청량이 API 의 실제 rate limit 모델과 맞는가

**예외로 죽지 않는다.** 조건을 만족하지 않으면 호출하지 않고 `PolicyBlocked` 를
돌려주고, 호출부는 그것을 `policy_blocked` gap 으로 남긴 뒤 다른 Pack 을 계속 진행한다
(DESIGN §14). 승인이 나지 않은 소스가 조용히 호출되는 것보다 조사가 partial 로
끝나는 편이 낫다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ria.config import Config, get_config, today
from ria.contracts.evidence_pack import Gap
from ria.contracts.research_brief import ResearchBrief, ResearchLane
from ria.policy.registry import SourceRecord, SourceRegistry, UnknownSourceError, get_registry

# 검사 단계 이름. 실패한 단계를 그대로 기록에 남긴다.
PolicyCheck = Literal[
    "brief_blocklist",
    "access_status",
    "commercial_use",
    "authentication",
    "policy_freshness",
    "retention",
    "rate_limit",
]

# 차단 사유 코드. 사람이 읽는 문장은 detail 에 따로 담는다.
BlockReason = Literal[
    "unknown_source",
    "blocked_by_brief",
    "access_status_not_allowed",
    "commercial_use_not_permitted",
    "missing_credential",
    "policy_verification_expired",
    "retention_not_enforceable",
    "request_exceeds_rate_limit",
]

# 호출을 시도해 볼 수 있는 접근 상태.
# `experimental` 은 비공식·낡은 인터페이스라 핵심 근거로 쓰지 않는다 (DESIGN §7).
# Google Trends · App Store · Google Play 를 실행 차단 상태로 유지하는 근거이기도 하다.
ALLOWED_ACCESS_STATUSES = frozenset({"core", "conditional"})

# 상업 이용이 무조건 막히는 조건.
COMMERCIAL_PROHIBITED = frozenset({"prohibited", "not_applicable"})

# 별도 합의가 필요한 조건. 합의 확보는 access_status 가 `core` 로 올라가는 것으로 표현된다.
COMMERCIAL_NEEDS_APPROVAL = frozenset({"separate_agreement_required", "unclear"})

# 승인 범위 안에서만 보관할 수 있는 정책. 승인(=`core`) 전에는 저장할 수 없다.
STORAGE_NEEDS_APPROVAL = frozenset({"approved_use_only"})

# Core 가 실제로 이행할 수 있는 삭제 정책. 여기 없는 정책을 만나면 호출하지 않는다.
# `delete_or_refresh_30d` 는 ria/core/snapshots.py 의 retention 함수가 이행한다.
ENFORCEABLE_DELETION_POLICIES = frozenset(
    {"none_required", "source_specific", "delete_or_refresh_30d", "on_request"}
)


@dataclass(frozen=True)
class PolicyAllowed:
    """호출해도 되는 상태. 이행해야 할 보관·삭제 조건을 함께 들려 보낸다."""

    source_id: str
    checked_at: date
    storage_policy: str
    deletion_policy: str
    max_calls: int | None
    notes: tuple[str, ...] = ()

    allowed: bool = field(default=True, init=False)

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class PolicyBlocked:
    """호출하면 안 되는 상태. 예외가 아니라 값이다."""

    reason: BlockReason
    source_id: str
    detail: str
    check: PolicyCheck
    checked_at: date

    allowed: bool = field(default=False, init=False)

    def __bool__(self) -> bool:
        return False

    def to_gap(self, gap_id: str, lane: ResearchLane | None = None) -> Gap:
        """EvidencePack 에 남길 gap. 차단된 소스를 숨기지 않는다 (DESIGN §13.7)."""
        kind = "missing_credential" if self.reason == "missing_credential" else "policy_blocked"
        return Gap(
            gap_id=gap_id,
            kind=kind,
            source_id=self.source_id,
            lane=lane,
            detail=f"[{self.check}/{self.reason}] {self.detail}",
        )


PolicyDecision = PolicyAllowed | PolicyBlocked


def check_source(
    source_id: str,
    *,
    commercial_context: bool = True,
    requested_calls: int = 1,
    as_of: date | None = None,
    registry: SourceRegistry | None = None,
    config: Config | None = None,
    brief: ResearchBrief | None = None,
) -> PolicyDecision:
    """소스 하나에 대해 6단 검사를 순서대로 돌린다. 첫 실패에서 멈춘다."""
    registry = registry if registry is not None else get_registry()
    config = config if config is not None else get_config()
    checked_at = as_of if as_of is not None else today()

    try:
        record = registry.get(source_id)
    except UnknownSourceError as error:
        return PolicyBlocked(
            reason="unknown_source",
            source_id=source_id,
            detail=str(error),
            check="access_status",
            checked_at=checked_at,
        )

    # 0. brief 가 명시적으로 막은 소스 (DESIGN §4.1 constraints.blocked_sources).
    #    6단 검사 이전 단계다. 정책이 허용해도 조사 요청이 막으면 호출하지 않는다.
    if brief is not None and brief.is_source_blocked(source_id):
        return _block(
            "blocked_by_brief",
            record,
            "ResearchBrief 의 constraints.blocked_sources 가 이 소스를 막았다",
            "brief_blocklist",
            checked_at,
        )

    notes: list[str] = []

    # 1. access_status 가 허용 상태인가.
    if record.access_status not in ALLOWED_ACCESS_STATUSES:
        return _block(
            "access_status_not_allowed",
            record,
            _access_status_detail(record),
            "access_status",
            checked_at,
        )

    # 2. commercial_context 와 상업 이용 조건이 맞는가.
    commercial = _check_commercial(record, commercial_context)
    if commercial is not None:
        return _block(
            "commercial_use_not_permitted", record, commercial, "commercial_use", checked_at
        )

    # 3. 인증이 유효한가.
    if record.auth_type != "none":
        missing = config.missing_credentials_for(record.source_id)
        if missing:
            return _block(
                "missing_credential",
                record,
                f"auth_type={record.auth_type} 인데 자격증명이 없다: {', '.join(missing)}",
                "authentication",
                checked_at,
            )

    # 4. 정책 확인일이 TTL 안에 있는가.
    expires_on = config.policy_expires_on(record.source_id, record.last_verified_at)
    if checked_at > expires_on:
        return _block(
            "policy_verification_expired",
            record,
            (
                f"정책 확인일 {record.last_verified_at.isoformat()} + "
                f"{config.policy_ttl_for(record.source_id)}일 = "
                f"{expires_on.isoformat()} 을 지났다. "
                f"공식 문서를 재확인하고 last_verified_at 을 갱신해라"
            ),
            "policy_freshness",
            checked_at,
        )
    if record.verify_before_use:
        notes.append(
            f"verify_before_use=true — 호출 전 정책 재확인 (만료 {expires_on.isoformat()})"
        )

    # 5. 보관 기간과 삭제 작업을 지킬 수 있는가.
    retention = _check_retention(record)
    if retention is not None:
        return _block("retention_not_enforceable", record, retention, "retention", checked_at)
    if record.deletion_policy == "delete_or_refresh_30d":
        notes.append("보관 30일 — 만료 스냅샷을 삭제하거나 갱신해야 한다")

    # 6. 요청량이 API 의 실제 rate limit 모델과 맞는가.
    max_calls = _max_calls(record, config)
    if max_calls is not None and requested_calls > max_calls:
        return _block(
            "request_exceeds_rate_limit",
            record,
            (
                f"요청 {requested_calls}회가 한도 {max_calls}회를 넘는다 "
                f"(rate_limit_model={record.rate_limit_model})"
            ),
            "rate_limit",
            checked_at,
        )
    if record.rate_limit_model == "server_headers":
        notes.append("rate limit 수치가 공식 문서에 없다 — 응답 헤더를 읽어 처리해야 한다")

    return PolicyAllowed(
        source_id=record.source_id,
        checked_at=checked_at,
        storage_policy=record.storage_policy,
        deletion_policy=record.deletion_policy,
        max_calls=max_calls,
        notes=tuple(notes),
    )


def check_for_brief(
    brief: ResearchBrief,
    source_id: str,
    *,
    requested_calls: int = 1,
    as_of: date | None = None,
    registry: SourceRegistry | None = None,
    config: Config | None = None,
) -> PolicyDecision:
    """brief 의 제약(commercial_context · blocked_sources)을 적용해 검사한다."""
    return check_source(
        source_id,
        commercial_context=brief.constraints.commercial_context,
        requested_calls=requested_calls,
        as_of=as_of,
        registry=registry,
        config=config,
        brief=brief,
    )


# --- 단계별 판정 ------------------------------------------------------------
def _access_status_detail(record: SourceRecord) -> str:
    base = f"access_status={record.access_status} 는 호출 허용 상태가 아니다"
    if record.access_status == "experimental":
        base += " (비공식·낡은 인터페이스 — 핵심 근거로 쓰지 않는다)"
    if record.notes:
        base += f". {record.notes}"
    if record.fallback_sources:
        base += f" 대체 소스: {', '.join(record.fallback_sources)}"
    return base


def _check_commercial(record: SourceRecord, commercial_context: bool) -> str | None:
    """상업 이용 조건 불일치를 문장으로 돌려준다. 문제 없으면 None."""
    if record.commercial_use in COMMERCIAL_PROHIBITED:
        return f"commercial_use={record.commercial_use} — 이 목적으로 사용할 수 없다"

    if not commercial_context:
        return None

    if record.commercial_use in COMMERCIAL_NEEDS_APPROVAL and record.access_status != "core":
        return (
            f"commercial_context=true 인데 commercial_use={record.commercial_use} 이고 "
            f"access_status={record.access_status} 다. 별도 합의·승인이 확인되면 "
            f"access_status 를 core 로 전환해라"
        )
    return None


def _check_retention(record: SourceRecord) -> str | None:
    """보관·삭제 이행 가능성. 지킬 수 없으면 문장을 돌려준다."""
    if record.deletion_policy not in ENFORCEABLE_DELETION_POLICIES:
        return f"deletion_policy={record.deletion_policy} 를 이행할 수단이 없다"

    if record.storage_policy in STORAGE_NEEDS_APPROVAL and record.access_status != "core":
        return (
            f"storage_policy={record.storage_policy} 인데 access_status={record.access_status} 다. "
            f"승인 범위 밖의 보관은 하지 않는다"
        )
    return None


def _max_calls(record: SourceRecord, config: Config) -> int | None:
    """이번 실행에서 허용할 최대 호출 수. None 이면 상한이 없다."""
    if record.quota is not None:
        return record.quota.limit
    if record.rate_limit_model == "none":
        return None
    if record.rate_limit_model == "server_headers":
        # 공식 수치가 없다. 사전 상한을 두되 실제 제어는 응답 헤더로 한다.
        return config.default_max_calls_per_run
    return config.default_max_calls_per_run


def _block(
    reason: BlockReason,
    record: SourceRecord,
    detail: str,
    check: PolicyCheck,
    checked_at: date,
) -> PolicyBlocked:
    return PolicyBlocked(
        reason=reason,
        source_id=record.source_id,
        detail=detail,
        check=check,
        checked_at=checked_at,
    )
