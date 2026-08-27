"""Collector 공통 계약과 Policy Guard 단일 진입점 (DESIGN §8 · §10).

collector 구현은 :class:`GuardedCollector` 를 상속하고 ``_collect`` 만 구현한다.
공개 ``collect`` 는 재정의할 수 없으며, 항상 Policy Guard를 먼저 통과한다. 이 구조로
네트워크 호출 로직이 guard를 우회하는 공개 경로를 만들지 않는다.

수집 결과는 아직 DB ID가 없는 저장 전 구조다. ``ref`` 로 Content·Observation·Metric의
관계를 표현하고, 저장 계층이 실제 ID로 치환할 수 있게 한다.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, ClassVar, Protocol, final, runtime_checkable

from ria.config import Config
from ria.contracts.evidence_pack import Gap
from ria.contracts.research_brief import ResearchBrief
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.observations import ObservationInput
from ria.policy.guard import (
    PolicyAllowed,
    PolicyBlocked,
    PolicyDecision,
    check_for_brief,
    check_source,
)
from ria.policy.registry import SourceRegistry


class CollectorContractError(ValueError):
    """collector가 저장 가능한 정규화 계약을 위반했을 때."""


@dataclass(frozen=True)
class CollectedContent:
    """저장 전 ContentItem과 결과 내부 참조."""

    ref: str
    item: ContentItemInput


@dataclass(frozen=True)
class CollectedObservation:
    """저장 전 Observation.

    ``content_ref`` 는 같은 결과의 :class:`CollectedContent.ref` 를 가리킨다. 실제
    ``content_item_id`` 는 저장 시점에 채운다.
    """

    ref: str
    content_ref: str
    source_id: str
    platform: str
    observed_at: datetime
    platform_item_id: str | None = None
    url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    query_run_id: str | None = None
    research_id: str | None = None

    def to_input(
        self,
        content_item_id: str,
        *,
        snapshot_id: str | None = None,
    ) -> ObservationInput:
        """결과 내부 참조를 실제 DB ID로 바꾼다."""
        return ObservationInput(
            content_item_id=content_item_id,
            source_id=self.source_id,
            platform=self.platform,
            observed_at=self.observed_at,
            platform_item_id=self.platform_item_id,
            url=self.url,
            payload=self.payload,
            snapshot_id=snapshot_id,
            query_run_id=self.query_run_id,
            research_id=self.research_id,
        )


@dataclass(frozen=True)
class CollectedMetric:
    """저장 전 Metric과 선택적 Content·Observation 참조."""

    metric: MetricInput
    content_ref: str | None = None
    observation_ref: str | None = None

    def to_input(
        self,
        *,
        content_item_id: str | None = None,
        observation_id: str | None = None,
    ) -> MetricInput:
        """결과 내부 참조를 실제 DB ID로 바꾼다."""
        return replace(
            self.metric,
            content_item_id=content_item_id,
            observation_id=observation_id,
        )


@dataclass(frozen=True)
class CollectedBatch:
    """guard 통과 뒤 collector가 돌려주는 정규화 묶음."""

    contents: tuple[CollectedContent, ...] = ()
    observations: tuple[CollectedObservation, ...] = ()
    metrics: tuple[CollectedMetric, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CollectResult:
    """공개 collector 반환값."""

    source_id: str
    query: str
    policy: PolicyDecision
    contents: tuple[CollectedContent, ...] = ()
    observations: tuple[CollectedObservation, ...] = ()
    metrics: tuple[CollectedMetric, ...] = ()
    gaps: tuple[Gap, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return isinstance(self.policy, PolicyAllowed)

    @property
    def result_count(self) -> int:
        """수집된 플랫폼 관측 수. Content 중복 제거와 무관한 실제 관측 건수다."""
        return len(self.observations)


@runtime_checkable
class Collector(Protocol):
    """모든 collector의 공개 계약 (지시서 B-1)."""

    source_id: str

    def collect(self, query: str, **options: Any) -> CollectResult:
        """Policy Guard 판정을 포함해 수집한다."""
        ...


class GuardedCollector(ABC):
    """Policy Guard를 우회할 수 없는 collector 기반 클래스."""

    source_id: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "collect" in cls.__dict__:
            raise TypeError("collector는 공개 collect()를 재정의할 수 없다; _collect()만 구현해라")

    def __init__(
        self,
        *,
        registry: SourceRegistry | None = None,
        config: Config | None = None,
    ) -> None:
        if not getattr(type(self), "source_id", ""):
            raise CollectorContractError("collector source_id가 비어 있다")
        self._registry = registry
        self._config = config

    @final
    def collect(self, query: str, **options: Any) -> CollectResult:
        """guard를 먼저 검사하고 허용된 경우에만 내부 수집 로직을 실행한다.

        공통 옵션은 ``brief``, ``commercial_context``, ``requested_calls``, ``as_of``,
        ``gap_id``, ``lane`` 이다. 나머지는 collector별 옵션으로 ``_collect`` 에 전달한다.
        """
        if not isinstance(query, str) or not query.strip():
            raise CollectorContractError("query는 비어 있지 않은 문자열이어야 한다")

        collector_options = dict(options)
        brief = collector_options.pop("brief", None)
        commercial_context = collector_options.pop("commercial_context", True)
        declared_calls = collector_options.pop("requested_calls", 1)
        as_of = collector_options.pop("as_of", None)
        gap_id = collector_options.pop("gap_id", None)
        lane = collector_options.pop("lane", None)

        if brief is not None and not isinstance(brief, ResearchBrief):
            raise CollectorContractError("brief는 ResearchBrief여야 한다")
        if not isinstance(commercial_context, bool):
            raise CollectorContractError("commercial_context는 bool이어야 한다")
        if not isinstance(declared_calls, int) or isinstance(declared_calls, bool):
            raise CollectorContractError("requested_calls는 양의 정수여야 한다")
        if declared_calls <= 0:
            raise CollectorContractError("requested_calls는 양의 정수여야 한다")
        if as_of is not None and not isinstance(as_of, date):
            raise CollectorContractError("as_of는 date여야 한다")

        estimated_calls = self._estimate_requested_calls(query, collector_options)
        if not isinstance(estimated_calls, int) or isinstance(estimated_calls, bool):
            raise CollectorContractError("collector 호출량 추정값은 양의 정수여야 한다")
        if estimated_calls <= 0:
            raise CollectorContractError("collector 호출량 추정값은 양의 정수여야 한다")
        # 호출자가 pagination 비용을 작게 신고해 Guard를 우회할 수 없게 collector의
        # 추정값을 하한으로 쓴다. 더 큰 값의 사전 점검은 허용한다.
        requested_calls = max(declared_calls, estimated_calls)

        if brief is not None:
            decision = check_for_brief(
                brief,
                self.source_id,
                requested_calls=requested_calls,
                as_of=as_of,
                registry=self._registry,
                config=self._config,
            )
        else:
            decision = check_source(
                self.source_id,
                commercial_context=commercial_context,
                requested_calls=requested_calls,
                as_of=as_of,
                registry=self._registry,
                config=self._config,
            )

        if isinstance(decision, PolicyBlocked):
            resolved_gap_id = gap_id or f"gap_{self.source_id}_{uuid.uuid4().hex}"
            return CollectResult(
                source_id=self.source_id,
                query=query,
                policy=decision,
                gaps=(decision.to_gap(resolved_gap_id, lane=lane),),
            )

        batch = self._collect(query, policy=decision, **collector_options)
        _validate_batch(self.source_id, batch)
        return CollectResult(
            source_id=self.source_id,
            query=query,
            policy=decision,
            contents=batch.contents,
            observations=batch.observations,
            metrics=batch.metrics,
            metadata=batch.metadata,
        )

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        """수집 옵션으로 실제 외부 호출 수를 계산한다. 기본 collector는 1회다."""
        return 1

    @abstractmethod
    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        """guard 통과 뒤 실행되는 내부 구현. 외부 호출은 이 안에서만 한다."""
        raise NotImplementedError


def _validate_batch(source_id: str, batch: CollectedBatch) -> None:
    """저장 전에 참조 무결성·소스·시각 계약을 검사한다."""
    if not isinstance(batch, CollectedBatch):
        raise CollectorContractError("_collect()는 CollectedBatch를 반환해야 한다")

    content_refs = _unique_refs("content", (item.ref for item in batch.contents))
    observation_refs = _unique_refs(
        "observation", (observation.ref for observation in batch.observations)
    )

    for observation in batch.observations:
        if observation.content_ref not in content_refs:
            raise CollectorContractError(
                f"observation {observation.ref}가 없는 content_ref를 가리킨다: "
                f"{observation.content_ref}"
            )
        if observation.source_id != source_id:
            raise CollectorContractError(
                f"observation source_id가 collector와 다르다: "
                f"{observation.source_id} != {source_id}"
            )
        if observation.observed_at.tzinfo is None:
            raise CollectorContractError("observation observed_at은 timezone-aware여야 한다")

    for metric in batch.metrics:
        if metric.metric.source_id != source_id:
            raise CollectorContractError(
                f"metric source_id가 collector와 다르다: {metric.metric.source_id} != {source_id}"
            )
        if metric.metric.observed_at.tzinfo is None:
            raise CollectorContractError("metric observed_at은 timezone-aware여야 한다")
        if metric.content_ref is not None and metric.content_ref not in content_refs:
            raise CollectorContractError(
                f"metric이 없는 content_ref를 가리킨다: {metric.content_ref}"
            )
        if metric.observation_ref is not None and metric.observation_ref not in observation_refs:
            raise CollectorContractError(
                f"metric이 없는 observation_ref를 가리킨다: {metric.observation_ref}"
            )


def _unique_refs(kind: str, refs: Any) -> set[str]:
    result: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            raise CollectorContractError(f"{kind} ref는 비어 있지 않은 문자열이어야 한다")
        if ref in result:
            raise CollectorContractError(f"{kind} ref가 중복됐다: {ref}")
        result.add(ref)
    return result
