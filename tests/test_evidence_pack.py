"""A-3. EvidencePack 계약 (DESIGN §4.2)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from ria.config import KST
from ria.contracts.evidence_pack import (
    CORROBORATION_VALUES,
    EVIDENCE_CLASSES,
    QUALITY_DIMENSIONS,
    Claim,
    Conflict,
    EvidencePack,
    Gap,
    Metric,
    PolicyLogEntry,
    QueryLogEntry,
    Signal,
    SourceRef,
)
from ria.contracts.research_brief import ResearchBrief

OBSERVED_AT = datetime(2026, 8, 27, 10, 0, tzinfo=KST)


def _metric(**overrides: object) -> Metric:
    base: dict[str, object] = {
        "metric_id": "m-1",
        "name": "사업체 수",
        "value": 1234,
        "unit": "개",
        "denominator": None,
        "geography": "KR",
        "period": "2024",
        "population": "제조업 중소기업",
        "method": "전수조사",
        "source_id": "kosis",
    }
    base.update(overrides)
    return Metric(**base)


def test_evidence_class_has_exactly_four_values() -> None:
    assert set(EVIDENCE_CLASSES) == {"authority", "behavior", "signal", "context"}
    assert len(EVIDENCE_CLASSES) == 4


def test_corroboration_has_exactly_four_values() -> None:
    assert set(CORROBORATION_VALUES) == {
        "confirmed",
        "single_source",
        "conflicting",
        "insufficient",
    }


def test_quality_dimensions_has_exactly_seven_values() -> None:
    """DESIGN §6.2 의 7개 차원. 단일 신뢰도 점수로 뭉개지 않는다."""
    assert len(QUALITY_DIMENSIONS) == 7


def test_claim_rejects_undefined_evidence_class() -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c-1",
            statement="x",
            evidence_class="opinion",
            corroboration="confirmed",
        )


def test_claim_rejects_undefined_corroboration() -> None:
    with pytest.raises(ValidationError):
        Claim(
            claim_id="c-1",
            statement="x",
            evidence_class="authority",
            corroboration="probably",
        )


@pytest.mark.parametrize(
    "field",
    ["unit", "denominator", "geography", "period", "population", "method"],
)
def test_metric_field_cannot_be_omitted(field: str) -> None:
    """nullable 은 허용하지만 생략은 허용하지 않는다 (DESIGN §13.2)."""
    payload = {
        "metric_id": "m-1",
        "name": "사업체 수",
        "value": 1234,
        "unit": "개",
        "denominator": None,
        "geography": "KR",
        "period": "2024",
        "population": "제조업",
        "method": "전수조사",
        "source_id": "kosis",
    }
    del payload[field]

    with pytest.raises(ValidationError):
        Metric(**payload)


def test_metric_accepts_explicit_none() -> None:
    metric = _metric(denominator=None, method=None)

    assert metric.denominator is None
    assert metric.method is None


def test_metric_serialization_keeps_null_fields() -> None:
    """직렬화에서도 필드가 사라지지 않아야 사후 복원이 가능하다."""
    dumped = _metric(method=None).model_dump(mode="json")

    assert "method" in dumped
    assert dumped["method"] is None


def test_metric_accepts_string_value() -> None:
    assert _metric(value="비공개").value == "비공개"


def test_signal_requires_representativeness_warning() -> None:
    """플랫폼 신호는 대표성 한계를 반드시 동반한다 (DESIGN §13.4)."""
    with pytest.raises(ValidationError):
        Signal(
            signal_id="s-1",
            platform="hacker_news",
            observed_at=OBSERVED_AT,
            representativeness_warning="",
        )


def test_signal_rejects_naive_observed_at() -> None:
    with pytest.raises(ValidationError):
        Signal(
            signal_id="s-1",
            platform="hacker_news",
            observed_at=datetime(2026, 8, 27, 10, 0),
            representativeness_warning="HN 이용자 표본에 한정된다",
        )


def test_from_brief_snapshots_the_brief_and_seeds_coverage() -> None:
    brief = ResearchBrief(
        research_id="r-1",
        decision_question="q",
        business_domain="d",
        research_lanes=["market_size", "demand"],
    )

    pack = EvidencePack.from_brief(brief)

    assert pack.research_id == "r-1"
    assert pack.brief_snapshot["decision_question"] == "q"
    assert pack.coverage.requested_lanes == ["market_size", "demand"]
    assert pack.coverage.missing_lanes == ["market_size", "demand"]
    assert pack.coverage.completed_lanes == []


def test_empty_pack_serializes_to_json_and_markdown() -> None:
    pack = EvidencePack(research_id="r-1")

    assert json.loads(pack.to_json())["research_id"] == "r-1"
    assert pack.to_markdown().startswith("# EvidencePack — r-1")


def test_json_roundtrip_is_lossless() -> None:
    pack = _full_pack()

    assert EvidencePack.model_validate(json.loads(pack.to_json())) == pack


def test_markdown_separates_facts_signals_and_gaps() -> None:
    """확인된 사실과 신호·빈칸이 같은 절에 섞이지 않는다 (DESIGN §13.10)."""
    markdown = _full_pack().to_markdown()

    for heading in ("## 주장", "## 수치", "## 플랫폼 신호", "## 충돌", "## 빈칸"):
        assert heading in markdown

    assert markdown.index("## 주장") < markdown.index("## 플랫폼 신호")
    assert markdown.index("## 플랫폼 신호") < markdown.index("## 빈칸")


def test_markdown_shows_null_metric_fields_as_dash() -> None:
    pack = EvidencePack(research_id="r-1", metrics=[_metric(denominator=None)])

    assert "분모: —" in pack.to_markdown()


def test_markdown_marks_empty_sections_explicitly() -> None:
    """빈칸이 없다는 것과 절이 통째로 빠진 것은 다르다."""
    markdown = EvidencePack(research_id="r-1").to_markdown()

    # 10개 절 중 9개가 비었다고 표시된다. Lane 커버리지는 항상 3줄을 출력한다.
    assert markdown.count("_기록 없음_") == 9
    assert "- 요청: —" in markdown


def test_blocked_source_is_recorded_as_gap_not_hidden() -> None:
    pack = EvidencePack(
        research_id="r-1",
        gaps=[
            Gap(
                gap_id="g-1",
                kind="policy_blocked",
                source_id="reddit",
                detail="Data API 승인 전 — 실호출하지 않는다",
            )
        ],
    )

    assert "policy_blocked" in pack.to_markdown()


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidencePack(research_id="r-1", confidence_score=0.8)


def _full_pack() -> EvidencePack:
    return EvidencePack(
        research_id="r-1",
        executive_facts=["국내 제조 중소기업은 2024년 기준 N개다"],
        claims=[
            Claim(
                claim_id="c-1",
                statement="시장은 공식 통계로 확인된다",
                evidence_class="authority",
                evidence_ids=["e-1"],
                scope="KR / 2024",
                limitations=["업종 분류가 조사 목적과 완전히 일치하지 않는다"],
                corroboration="single_source",
            )
        ],
        metrics=[_metric()],
        signals=[
            Signal(
                signal_id="s-1",
                platform="hacker_news",
                observed_at=OBSERVED_AT,
                metric_snapshot={"score": 120, "comments": 34},
                representativeness_warning="HN 이용자 표본에 한정되며 시장 규모로 환산하지 않는다",
            )
        ],
        sources=[
            SourceRef(
                evidence_id="e-1",
                source_id="kosis",
                url="https://kosis.kr/example",
                title="전국사업체조사",
                publisher="통계청",
                retrieved_at=OBSERVED_AT,
                query="사업체 수",
                quality={"authority": "high", "recency": "2024"},
            )
        ],
        conflicts=[
            Conflict(
                conflict_id="x-1",
                subject="사업체 수",
                claim_ids=["c-1"],
                description="두 통계의 모집단 정의가 다르다",
            )
        ],
        gaps=[
            Gap(
                gap_id="g-1",
                kind="policy_blocked",
                source_id="reddit",
                lane="customer_pain",
                detail="Data API 승인 전",
            )
        ],
        query_log=[
            QueryLogEntry(
                query_id="q-1",
                source_id="kosis",
                query="사업체 수",
                executed_at=OBSERVED_AT,
                result_count=12,
            )
        ],
        policy_log=[
            PolicyLogEntry(
                source_id="reddit",
                decision="blocked",
                checked_at=OBSERVED_AT,
                reason="access_status=blocked",
            )
        ],
    )
