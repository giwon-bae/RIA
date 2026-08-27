"""A-2. ResearchBrief 계약 (DESIGN §4.1)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ria.contracts.research_brief import (
    RESEARCH_LANES,
    Budget,
    Constraints,
    ResearchBrief,
    TimeRange,
)


def _minimal() -> dict[str, object]:
    return {
        "research_id": "r-2026-08-27-001",
        "decision_question": "한국 중소 제조업 AI 비전검사 SaaS 가 가능한가",
        "business_domain": "manufacturing-ai",
        "research_lanes": ["market_size"],
    }


def test_lane_literal_has_exactly_eight_values() -> None:
    """DESIGN §4.1 의 Research Lane 은 8종이다."""
    assert len(RESEARCH_LANES) == 8
    assert set(RESEARCH_LANES) == {
        "market_size",
        "demand",
        "customer_pain",
        "competitors",
        "technology",
        "regulation",
        "economics",
        "distribution",
    }


@pytest.mark.parametrize("lane", RESEARCH_LANES)
def test_every_defined_lane_is_accepted(lane: str) -> None:
    brief = ResearchBrief(**{**_minimal(), "research_lanes": [lane]})

    assert brief.research_lanes == [lane]


def test_undefined_lane_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ResearchBrief(**{**_minimal(), "research_lanes": ["market_sizing"]})


def test_commercial_context_defaults_to_true() -> None:
    """비상업 전용 API 를 자동 허용하지 않기 위한 기본값이다."""
    assert ResearchBrief(**_minimal()).constraints.commercial_context is True
    assert Constraints().commercial_context is True


def test_personal_data_defaults_to_exclude() -> None:
    assert ResearchBrief(**_minimal()).constraints.personal_data == "exclude"


def test_allow_paid_sources_defaults_to_false() -> None:
    assert ResearchBrief(**_minimal()).budget.allow_paid_sources is False
    assert Budget().allow_paid_sources is False


def test_max_codex_usage_defaults_to_normal() -> None:
    assert Budget().max_codex_usage == "normal"


def test_time_range_accepts_from_and_to_aliases() -> None:
    time_range = TimeRange.model_validate({"from": "2025-01-01", "to": "2026-08-27"})

    assert time_range.from_date == date(2025, 1, 1)
    assert time_range.to_date == date(2026, 8, 27)


def test_time_range_rejects_reversed_order() -> None:
    with pytest.raises(ValidationError):
        TimeRange.model_validate({"from": "2026-08-27", "to": "2025-01-01"})


def test_time_range_allows_open_ends() -> None:
    assert TimeRange().from_date is None
    assert TimeRange.model_validate({"from": "2025-01-01"}).to_date is None


def test_empty_lane_list_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchBrief(**{**_minimal(), "research_lanes": []})


def test_duplicate_lanes_are_deduped_but_order_is_kept() -> None:
    brief = ResearchBrief(**{**_minimal(), "research_lanes": ["demand", "market_size", "demand"]})

    assert brief.research_lanes == ["demand", "market_size"]


def test_unknown_top_level_field_is_rejected() -> None:
    """계약 밖 필드를 조용히 흘려보내지 않는다."""
    with pytest.raises(ValidationError):
        ResearchBrief(**{**_minimal(), "max_documents": 100})


def test_blocked_sources_are_reported() -> None:
    brief = ResearchBrief(
        **{**_minimal(), "constraints": {"blocked_sources": ["reddit", "reddit"]}}
    )

    assert brief.constraints.blocked_sources == ["reddit"]
    assert brief.is_source_blocked("reddit") is True
    assert brief.is_source_blocked("hacker_news") is False


def test_freshness_rejects_undefined_value() -> None:
    with pytest.raises(ValidationError):
        ResearchBrief(**{**_minimal(), "freshness": "90d"})


def test_budget_rejects_non_positive_minutes() -> None:
    with pytest.raises(ValidationError):
        Budget(max_minutes=0)


def test_roundtrip_json_keeps_aliases() -> None:
    brief = ResearchBrief(
        **{**_minimal(), "time_range": {"from": "2025-01-01", "to": "2026-08-27"}}
    )

    dumped = brief.model_dump(mode="json", by_alias=True)

    assert dumped["time_range"] == {"from": "2025-01-01", "to": "2026-08-27"}
    assert ResearchBrief.model_validate(dumped) == brief
