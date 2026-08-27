"""EvidencePack — RIA → 판단 에이전트 출력 계약 (DESIGN §4.2).

RIA 의 최종 산출물은 데이터 덩어리가 아니라 판단 가능한 근거 패키지다 (DESIGN §20).

이 모듈은 **직렬화만** 한다. Markdown 서술 문장을 생성하는 것은 상위 계층(Codex/RIA
subagent)의 몫이고, 여기서는 구조를 그대로 절로 옮긴다. 확인된 사실과 추정을 섞지
않기 위해 Markdown 도 필드 구조를 그대로 따른다 (DESIGN §13.10).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ria.contracts.research_brief import ResearchBrief, ResearchLane

# --- Evidence Class (DESIGN §6.1 — 4종) ------------------------------------
EvidenceClass = Literal["authority", "behavior", "signal", "context"]

EVIDENCE_CLASSES: tuple[EvidenceClass, ...] = ("authority", "behavior", "signal", "context")

# 독립 출처 대조 결과 (DESIGN §4.2)
Corroboration = Literal["confirmed", "single_source", "conflicting", "insufficient"]

CORROBORATION_VALUES: tuple[Corroboration, ...] = (
    "confirmed",
    "single_source",
    "conflicting",
    "insufficient",
)

# 품질 차원 (DESIGN §6.2 — 7종). 단일 신뢰도 점수 하나로 뭉개지 않는다.
QualityDimension = Literal[
    "authority",
    "directness",
    "recency",
    "scope_fit",
    "method_clarity",
    "corroboration",
    "representativeness",
]

QUALITY_DIMENSIONS: tuple[QualityDimension, ...] = (
    "authority",
    "directness",
    "recency",
    "scope_fit",
    "method_clarity",
    "corroboration",
    "representativeness",
)

# gap 종류 (DESIGN §8.2 · §14)
GapKind = Literal[
    "policy_blocked",
    "missing_credential",
    "waiting_for_login",
    "rate_limited",
    "no_data",
    "source_error",
    "not_attempted",
]

PolicyDecision = Literal["allowed", "blocked"]


class _Strict(BaseModel):
    """계약 밖 필드를 조용히 흘려보내지 않는다."""

    model_config = ConfigDict(extra="forbid")


class Claim(_Strict):
    """사실 주장 1건. 모든 주장은 근거 ID 로 원문까지 추적 가능해야 한다 (DESIGN §13.1)."""

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_class: EvidenceClass
    evidence_ids: list[str] = Field(default_factory=list)
    scope: str | None = None
    limitations: list[str] = Field(default_factory=list)
    corroboration: Corroboration


class Metric(_Strict):
    """수치 1건.

    값·단위·분모·지역·기간·모집단·측정방법·출처를 **전부 필드로** 갖는다.
    적용되지 않으면 `None` 을 명시하되 **생략하지 않는다** (DESIGN §13.2).
    필드가 통째로 빠지면 그 수치가 무엇에 대한 것인지 사후에 복원할 수 없다.
    """

    metric_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: int | float | str
    unit: str | None
    denominator: str | None
    geography: str | None
    period: str | None
    population: str | None
    method: str | None
    source_id: str = Field(min_length=1)


class Signal(_Strict):
    """플랫폼 내부 신호 1건.

    플랫폼 지표는 해당 플랫폼 내부 반응으로만 해석한다 (DESIGN §5.3). 그래서
    `representativeness_warning` 이 필수다 — 비우고 넘어갈 수 없다 (DESIGN §13.4).
    """

    signal_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    observed_at: datetime
    metric_snapshot: dict[str, Any] = Field(default_factory=dict)
    representativeness_warning: str = Field(min_length=1)

    @field_validator("observed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at 은 timezone-aware 여야 한다")
        return value


class SourceRef(_Strict):
    """인용된 원문 1건.

    검색 결과가 아니라 실제 원문 URL 을 인용한다 (DESIGN §13.5).
    필드 구성은 DESIGN §9.4 의 보존 목록(URL·제목·발행자·발행일·확인일·조사 query)을 따른다.
    """

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    url: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    query: str | None = None
    snapshot_hash: str | None = None
    quality: dict[QualityDimension, str] = Field(default_factory=dict)


class Conflict(_Strict):
    """상충하는 근거. 임의로 하나를 고르지 않고 병기한다 (DESIGN §13.8 · §14)."""

    conflict_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)


class Gap(_Strict):
    """확인하지 못한 것. 차단된 소스를 숨기지 않고 여기에 남긴다 (DESIGN §13.7)."""

    gap_id: str = Field(min_length=1)
    kind: GapKind
    detail: str = Field(min_length=1)
    lane: ResearchLane | None = None
    source_id: str | None = None
    pack_id: str | None = None
    next_action: str | None = None


class QueryLogEntry(_Strict):
    """수집 query 와 실행 시점. 재현 가능해야 한다 (DESIGN §13.6)."""

    query_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    query: str
    executed_at: datetime
    pack_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    result_count: int | None = None
    error: str | None = None


class PolicyLogEntry(_Strict):
    """Policy Guard 판정 기록. 보관·갱신·삭제 요구사항이 등록돼 있어야 한다 (DESIGN §13.9)."""

    source_id: str = Field(min_length=1)
    decision: PolicyDecision
    checked_at: datetime
    reason: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    storage_policy: str | None = None
    deletion_policy: str | None = None


class Coverage(_Strict):
    """요청한 lane 중 무엇이 충족됐는가."""

    requested_lanes: list[ResearchLane] = Field(default_factory=list)
    completed_lanes: list[ResearchLane] = Field(default_factory=list)
    missing_lanes: list[ResearchLane] = Field(default_factory=list)


class EvidencePack(_Strict):
    """조사 1건의 근거 패키지."""

    research_id: str = Field(min_length=1)
    brief_snapshot: dict[str, Any] = Field(default_factory=dict)
    executive_facts: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    query_log: list[QueryLogEntry] = Field(default_factory=list)
    policy_log: list[PolicyLogEntry] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)

    @classmethod
    def from_brief(cls, brief: ResearchBrief) -> EvidencePack:
        """brief 를 스냅샷으로 박아 빈 팩을 연다. 요청 lane 은 그대로 coverage 로 옮긴다."""
        return cls(
            research_id=brief.research_id,
            brief_snapshot=brief.model_dump(mode="json", by_alias=True),
            coverage=Coverage(
                requested_lanes=list(brief.research_lanes),
                missing_lanes=list(brief.research_lanes),
            ),
        )

    # -- 산출 -----------------------------------------------------------
    def to_json(self, *, indent: int | None = 2) -> str:
        """기계 판독용 JSON."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            indent=indent,
            sort_keys=False,
        )

    def to_markdown(self) -> str:
        """사람이 읽는 요약.

        문장을 생성하지 않는다. 필드를 절 구조로 옮기기만 한다. 확인된 사실
        (`claims`·`metrics`)과 플랫폼 신호(`signals`), 확인하지 못한 것(`conflicts`·`gaps`)을
        서로 다른 절로 분리해 둔다 (DESIGN §11.5 · §13.10).
        """
        lines: list[str] = [f"# EvidencePack — {self.research_id}", ""]

        lines += _section("확인된 사실", _executive_lines(self.executive_facts))
        lines += _section("주장", [_claim_line(c) for c in self.claims])
        lines += _section("수치", [_metric_line(m) for m in self.metrics])
        lines += _section("플랫폼 신호", [_signal_line(s) for s in self.signals])
        lines += _section("출처", [_source_line(s) for s in self.sources])
        lines += _section("충돌", [_conflict_line(c) for c in self.conflicts])
        lines += _section("빈칸", [_gap_line(g) for g in self.gaps])
        lines += _section("정책 판정", [_policy_line(p) for p in self.policy_log])
        lines += _section("수집 query", [_query_line(q) for q in self.query_log])
        lines += _section("Lane 커버리지", _coverage_lines(self.coverage))

        return "\n".join(lines).rstrip() + "\n"


# --- Markdown 직렬화 헬퍼 ---------------------------------------------------
_EMPTY = "_기록 없음_"


def _section(title: str, body: list[str]) -> list[str]:
    return [f"## {title}", "", *(body or [_EMPTY]), ""]


def _executive_lines(facts: list[str]) -> list[str]:
    return [f"- {fact}" for fact in facts]


def _claim_line(claim: Claim) -> str:
    parts = [
        f"- **{claim.claim_id}** ({claim.evidence_class} · {claim.corroboration}) {claim.statement}"
    ]
    if claim.scope:
        parts.append(f"  - 범위: {claim.scope}")
    for limitation in claim.limitations:
        parts.append(f"  - 한계: {limitation}")
    if claim.evidence_ids:
        parts.append(f"  - 근거: {', '.join(claim.evidence_ids)}")
    return "\n".join(parts)


def _metric_line(metric: Metric) -> str:
    fields = [
        ("단위", metric.unit),
        ("분모", metric.denominator),
        ("지역", metric.geography),
        ("기간", metric.period),
        ("모집단", metric.population),
        ("측정방법", metric.method),
    ]
    rendered = " · ".join(
        f"{label}: {value if value is not None else '—'}" for label, value in fields
    )
    return f"- **{metric.name}** = {metric.value} ({rendered}) [출처: {metric.source_id}]"


def _signal_line(signal: Signal) -> str:
    return (
        f"- **{signal.platform}** @ {signal.observed_at.isoformat()} "
        f"{json.dumps(signal.metric_snapshot, ensure_ascii=False, sort_keys=True)}\n"
        f"  - 대표성 한계: {signal.representativeness_warning}"
    )


def _source_line(source: SourceRef) -> str:
    label = source.title or source.url or source.source_id
    suffix = f" <{source.url}>" if source.url else ""
    return f"- **{source.evidence_id}** [{source.source_id}] {label}{suffix}"


def _conflict_line(conflict: Conflict) -> str:
    return f"- **{conflict.subject}** — {conflict.description}"


def _gap_line(gap: Gap) -> str:
    where = gap.source_id or gap.pack_id or gap.lane or "-"
    return f"- **{gap.kind}** [{where}] {gap.detail}"


def _policy_line(entry: PolicyLogEntry) -> str:
    reason = f" — {entry.reason}" if entry.reason else ""
    return f"- **{entry.source_id}** {entry.decision} @ {entry.checked_at.isoformat()}{reason}"


def _query_line(entry: QueryLogEntry) -> str:
    count = "-" if entry.result_count is None else str(entry.result_count)
    return f"- **{entry.source_id}** `{entry.query}` @ {entry.executed_at.isoformat()} → {count}건"


def _coverage_lines(coverage: Coverage) -> list[str]:
    return [
        f"- 요청: {', '.join(coverage.requested_lanes) or '—'}",
        f"- 충족: {', '.join(coverage.completed_lanes) or '—'}",
        f"- 미충족: {', '.join(coverage.missing_lanes) or '—'}",
    ]
