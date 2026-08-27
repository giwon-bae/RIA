"""A-6. Policy Guard 6단 검사 (DESIGN §8.2)."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from ria.config import SOURCES_YAML_PATH, Config
from ria.contracts.research_brief import ResearchBrief
from ria.policy.guard import (
    PolicyAllowed,
    PolicyBlocked,
    check_for_brief,
    check_source,
)
from ria.policy.registry import SourceRegistry

AS_OF = date(2026, 8, 27)

REDDIT_CREDENTIALS = {
    "RIA_REDDIT_CLIENT_ID": "id",
    "RIA_REDDIT_CLIENT_SECRET": "secret",
    "RIA_REDDIT_USER_AGENT": "python:ria:2.1 (test)",
}


@pytest.fixture
def registry() -> SourceRegistry:
    return SourceRegistry()


@pytest.fixture
def writable_registry(tmp_path: Path) -> SourceRegistry:
    copy = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, copy)
    return SourceRegistry(copy)


def _config(**credentials: str) -> Config:
    """자격증명은 항상 테스트가 명시한 것만 쓴다. `.env` 를 절대 보지 않는다."""
    return Config(db_path=Path("/tmp/ria-guard-test.db"), credentials=dict(credentials))


def _check(source_id: str, registry: SourceRegistry, **kwargs: object) -> object:
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("config", _config())
    return check_source(source_id, registry=registry, **kwargs)


# --- 필수 검증 (지시서 A-6) --------------------------------------------------
def test_commercial_context_blocks_reddit(registry: SourceRegistry) -> None:
    """commercial_context=True + reddit → 반드시 차단."""
    decision = _check("reddit", registry, commercial_context=True)

    assert isinstance(decision, PolicyBlocked)
    assert bool(decision) is False
    assert decision.source_id == "reddit"


def test_reddit_passes_once_access_status_becomes_core(writable_registry: SourceRegistry) -> None:
    """승인은 access_status 전환만으로 반영돼야 한다 (DESIGN §21 원칙 1)."""
    blocked = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=AS_OF,
        commercial_context=True,
    )
    assert isinstance(blocked, PolicyBlocked)

    writable_registry.set_access_status("reddit", "core", AS_OF, note="Data Access Request 승인")

    allowed = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=AS_OF,
        commercial_context=True,
    )

    assert isinstance(allowed, PolicyAllowed)
    assert bool(allowed) is True


def test_expired_policy_ttl_is_blocked(writable_registry: SourceRegistry) -> None:
    """TTL 초과 소스는 차단한다."""
    writable_registry.set_access_status("reddit", "core", date(2026, 1, 1))

    decision = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=AS_OF,
        commercial_context=True,
    )

    assert isinstance(decision, PolicyBlocked)
    assert decision.reason == "policy_verification_expired"
    assert decision.check == "policy_freshness"


def test_ttl_boundary_is_inclusive(writable_registry: SourceRegistry) -> None:
    """만료 당일까지는 통과하고 다음 날부터 막힌다."""
    writable_registry.set_access_status("reddit", "core", AS_OF)
    expires_on = AS_OF + timedelta(days=30)

    on_time = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=expires_on,
        commercial_context=True,
    )
    late = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=expires_on + timedelta(days=1),
        commercial_context=True,
    )

    assert isinstance(on_time, PolicyAllowed)
    assert isinstance(late, PolicyBlocked)


# --- 단계 순서 --------------------------------------------------------------
def test_guard_never_raises_for_unknown_source(registry: SourceRegistry) -> None:
    """예외로 죽지 않는다. 값으로 돌려준다."""
    decision = _check("mastodon", registry)

    assert isinstance(decision, PolicyBlocked)
    assert decision.reason == "unknown_source"


def test_access_status_is_checked_first(registry: SourceRegistry) -> None:
    """reddit 은 상업 이용·인증도 문제지만 1단에서 먼저 걸린다."""
    decision = _check("reddit", registry, commercial_context=True)

    assert isinstance(decision, PolicyBlocked)
    assert decision.check == "access_status"
    assert decision.reason == "access_status_not_allowed"


def test_experimental_status_is_not_callable(registry: SourceRegistry) -> None:
    """Google Trends · App Store · Google Play 는 실행 차단 상태를 유지한다."""
    for source_id in ("google_trends", "app_store", "google_play"):
        decision = _check(source_id, registry)
        assert isinstance(decision, PolicyBlocked), source_id
        assert decision.check == "access_status"


def test_commercial_use_is_checked_before_authentication(registry: SourceRegistry) -> None:
    """threads 는 자격증명도 없지만 2단에서 먼저 걸린다."""
    decision = _check("threads", registry, commercial_context=True)

    assert isinstance(decision, PolicyBlocked)
    assert decision.check == "commercial_use"


def test_missing_credential_is_blocked(registry: SourceRegistry) -> None:
    decision = _check("kosis", registry)

    assert isinstance(decision, PolicyBlocked)
    assert decision.check == "authentication"
    assert decision.reason == "missing_credential"
    assert "RIA_KOSIS_API_KEY" in decision.detail


def test_credentialed_source_passes_authentication(registry: SourceRegistry) -> None:
    decision = check_source(
        "kosis",
        registry=registry,
        config=_config(RIA_KOSIS_API_KEY="key"),
        as_of=AS_OF,
    )

    assert isinstance(decision, PolicyAllowed)


def test_storage_needing_approval_is_blocked_before_approval(
    writable_registry: SourceRegistry,
) -> None:
    """5단 — 승인 범위 밖의 보관은 하지 않는다."""
    writable_registry.set_access_status("threads", "conditional", AS_OF)

    decision = check_source(
        "threads",
        registry=writable_registry,
        config=_config(
            RIA_THREADS_APP_ID="id",
            RIA_THREADS_APP_SECRET="secret",
            RIA_THREADS_ACCESS_TOKEN="token",
        ),
        as_of=AS_OF,
        commercial_context=False,
    )

    assert isinstance(decision, PolicyBlocked)
    assert decision.check == "retention"
    assert decision.reason == "retention_not_enforceable"


def test_request_volume_over_quota_is_blocked(writable_registry: SourceRegistry) -> None:
    """6단 — Threads 쿼터는 사용자당 24시간 2,200 쿼리다."""
    writable_registry.set_access_status("threads", "core", AS_OF)
    config = _config(
        RIA_THREADS_APP_ID="id",
        RIA_THREADS_APP_SECRET="secret",
        RIA_THREADS_ACCESS_TOKEN="token",
    )

    ok = check_source(
        "threads", registry=writable_registry, config=config, as_of=AS_OF, requested_calls=2200
    )
    too_many = check_source(
        "threads", registry=writable_registry, config=config, as_of=AS_OF, requested_calls=2201
    )

    assert isinstance(ok, PolicyAllowed)
    assert isinstance(too_many, PolicyBlocked)
    assert too_many.check == "rate_limit"


def test_source_without_rate_limit_has_no_ceiling(registry: SourceRegistry) -> None:
    """HN 공식 API 에는 명시된 rate limit 이 없다."""
    decision = _check("hacker_news", registry, requested_calls=10_000)

    assert isinstance(decision, PolicyAllowed)
    assert decision.max_calls is None


def test_unknown_rate_limit_falls_back_to_config_default(registry: SourceRegistry) -> None:
    config = _config()
    decision = check_source("world_bank", registry=registry, config=config, as_of=AS_OF)

    assert isinstance(decision, PolicyAllowed)
    assert decision.max_calls == config.default_max_calls_per_run


# --- 허용 결과가 들려 보내는 것 ----------------------------------------------
def test_allowed_decision_carries_retention_obligation(registry: SourceRegistry) -> None:
    decision = check_source(
        "youtube_data",
        registry=registry,
        config=_config(RIA_YOUTUBE_API_KEY="key"),
        as_of=AS_OF,
        commercial_context=True,
    )

    assert isinstance(decision, PolicyAllowed)
    assert decision.storage_policy == "refresh_or_delete_30d"
    assert decision.deletion_policy == "delete_or_refresh_30d"
    assert any("30일" in note for note in decision.notes)


def test_server_header_sources_are_told_to_read_headers(
    writable_registry: SourceRegistry,
) -> None:
    writable_registry.set_access_status("reddit", "core", AS_OF)

    decision = check_source(
        "reddit",
        registry=writable_registry,
        config=_config(**REDDIT_CREDENTIALS),
        as_of=AS_OF,
    )

    assert isinstance(decision, PolicyAllowed)
    assert any("응답 헤더" in note for note in decision.notes)


# --- brief 연동 -------------------------------------------------------------
def test_non_commercial_context_relaxes_commercial_check(registry: SourceRegistry) -> None:
    decision = _check("hn_algolia", registry, commercial_context=False)

    assert isinstance(decision, PolicyAllowed)


def test_commercial_context_blocks_unclear_commercial_use(registry: SourceRegistry) -> None:
    decision = _check("hn_algolia", registry, commercial_context=True)

    assert isinstance(decision, PolicyBlocked)
    assert decision.check == "commercial_use"


def test_brief_blocklist_is_honoured(registry: SourceRegistry) -> None:
    brief = ResearchBrief(
        research_id="r-1",
        decision_question="q",
        business_domain="d",
        research_lanes=["technology"],
        constraints={"blocked_sources": ["hacker_news"]},
    )

    decision = check_for_brief(
        brief, "hacker_news", registry=registry, config=_config(), as_of=AS_OF
    )

    assert isinstance(decision, PolicyBlocked)
    assert decision.reason == "blocked_by_brief"


def test_brief_commercial_context_default_blocks_reddit(registry: SourceRegistry) -> None:
    brief = ResearchBrief(
        research_id="r-1",
        decision_question="q",
        business_domain="d",
        research_lanes=["customer_pain"],
    )

    decision = check_for_brief(brief, "reddit", registry=registry, config=_config(), as_of=AS_OF)

    assert isinstance(decision, PolicyBlocked)


# --- gap 기록 ---------------------------------------------------------------
def test_blocked_decision_becomes_policy_blocked_gap(registry: SourceRegistry) -> None:
    decision = _check("reddit", registry)

    assert isinstance(decision, PolicyBlocked)
    gap = decision.to_gap("g-1", lane="customer_pain")

    assert gap.kind == "policy_blocked"
    assert gap.source_id == "reddit"
    assert gap.lane == "customer_pain"
    assert "access_status_not_allowed" in gap.detail


def test_missing_credential_becomes_its_own_gap_kind(registry: SourceRegistry) -> None:
    decision = _check("kosis", registry)

    assert isinstance(decision, PolicyBlocked)
    assert decision.to_gap("g-2").kind == "missing_credential"


def test_no_source_is_callable_without_any_credentials_except_open_ones(
    registry: SourceRegistry,
) -> None:
    """자격증명 0개 · 상업 조사 기본값에서 열리는 소스는 무인증 공개 소스뿐이다."""
    allowed = {
        s.source_id
        for s in registry.list_sources()
        if isinstance(_check(s.source_id, registry, commercial_context=True), PolicyAllowed)
    }

    assert allowed == {"world_bank", "hacker_news"}
