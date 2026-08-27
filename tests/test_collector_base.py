"""B-1. Collector 계약과 Policy Guard 단일 진입점."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    Collector,
    CollectorContractError,
    CollectResult,
    GuardedCollector,
)
from ria.config import KST
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.policy.guard import PolicyAllowed, PolicyBlocked

AS_OF = date(2026, 8, 27)
OBSERVED_AT = datetime(2026, 8, 27, 12, 0, tzinfo=KST)


class DummyHackerNewsCollector(GuardedCollector):
    source_id = "hacker_news"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.internal_calls = 0

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        self.internal_calls += 1
        return CollectedBatch(
            contents=(
                CollectedContent(
                    ref="content:1",
                    item=ContentItemInput(
                        content_type="article",
                        url="https://example.com/ria",
                        title=query,
                    ),
                ),
            ),
            observations=(
                CollectedObservation(
                    ref="observation:1",
                    content_ref="content:1",
                    source_id=self.source_id,
                    platform="hacker_news",
                    observed_at=OBSERVED_AT,
                    payload={"score": 10},
                ),
            ),
            metrics=(
                CollectedMetric(
                    content_ref="content:1",
                    observation_ref="observation:1",
                    metric=MetricInput(
                        metric_name="score",
                        value=10,
                        index_type="absolute",
                        source_id=self.source_id,
                        observed_at=OBSERVED_AT,
                    ),
                ),
            ),
            metadata={"policy_notes": list(policy.notes)},
        )


class BlockedRedditCollector(DummyHackerNewsCollector):
    source_id = "reddit"


def test_collector_protocol_and_normalized_result() -> None:
    collector = DummyHackerNewsCollector()

    assert isinstance(collector, Collector)

    result = collector.collect("RIA", as_of=AS_OF)

    assert isinstance(result, CollectResult)
    assert result.allowed is True
    assert result.result_count == 1
    assert collector.internal_calls == 1
    assert result.observations[0].content_ref == result.contents[0].ref
    assert result.metrics[0].observation_ref == result.observations[0].ref


def test_policy_blocked_result_never_executes_internal_collector() -> None:
    collector = BlockedRedditCollector()

    result = collector.collect("approved-only query", as_of=AS_OF, gap_id="gap_reddit")

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "access_status_not_allowed"
    assert collector.internal_calls == 0
    assert not result.contents
    assert result.gaps[0].gap_id == "gap_reddit"
    assert result.gaps[0].kind == "policy_blocked"


def test_public_collect_cannot_be_overridden() -> None:
    with pytest.raises(TypeError, match=r"collect\(\)를 재정의할 수 없다"):

        class UnsafeCollector(GuardedCollector):
            source_id = "hacker_news"

            def collect(self, query: str, **options: Any) -> CollectResult:
                raise AssertionError("guard bypass")

            def _collect(
                self,
                query: str,
                *,
                policy: PolicyAllowed,
                **options: Any,
            ) -> CollectedBatch:
                return CollectedBatch()


def test_unknown_content_reference_is_rejected_before_storage() -> None:
    class BrokenCollector(GuardedCollector):
        source_id = "hacker_news"

        def _collect(
            self,
            query: str,
            *,
            policy: PolicyAllowed,
            **options: Any,
        ) -> CollectedBatch:
            return CollectedBatch(
                observations=(
                    CollectedObservation(
                        ref="observation:orphan",
                        content_ref="content:missing",
                        source_id=self.source_id,
                        platform="hacker_news",
                        observed_at=OBSERVED_AT,
                    ),
                )
            )

    with pytest.raises(CollectorContractError, match="없는 content_ref"):
        BrokenCollector().collect("RIA", as_of=AS_OF)


def test_collector_estimate_is_the_guard_floor() -> None:
    class ExpensiveCollector(DummyHackerNewsCollector):
        source_id = "world_bank"

        def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
            return 51

    collector = ExpensiveCollector()
    result = collector.collect("NY.GDP.MKTP.CD", as_of=AS_OF, requested_calls=1)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert collector.internal_calls == 0


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(CollectorContractError, match="query"):
        DummyHackerNewsCollector().collect(query, as_of=AS_OF)
