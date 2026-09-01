"""Source Pack 오케스트레이션 (DESIGN §5, B-10).

Pack은 등록 소스를 무차별 호출하는 목록이 아니라 우선순위·정책·수집 전략을 함께
고정한다. 실행 직전 YAML 정본을 SQLite ``source_registry``에 스냅샷하고, 한 소스가
차단되거나 실패해도 gap을 남긴 뒤 다음 소스를 계속한다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast

from ria.collectors.base import CollectResult, GuardedCollector
from ria.collectors.data_go_kr import DataGoKrCollector
from ria.collectors.hacker_news import HackerNewsCollector, HNAlgoliaCollector
from ria.collectors.kosis import KosisCollector
from ria.collectors.naver_datalab import NaverDataLabCollector
from ria.collectors.naver_search import NaverSearchCollector
from ria.collectors.naver_shopping_insight import NaverShoppingInsightCollector
from ria.collectors.opendart import OpenDartCollector
from ria.collectors.persistence import PersistedCollectResult, persist_collect_result
from ria.collectors.reddit import RedditCollector
from ria.collectors.threads import ThreadsCollector
from ria.collectors.world_bank import WorldBankCollector
from ria.collectors.youtube import YouTubeCollector
from ria.config import Config, get_config, now, to_iso8601
from ria.contracts.evidence_pack import Gap
from ria.contracts.research_brief import RESEARCH_LANES, ResearchBrief, ResearchLane
from ria.core.entities import new_id
from ria.core.store import Store
from ria.http import redact_url
from ria.policy.guard import PolicyBlocked, check_for_brief, check_source
from ria.policy.registry import SourceRecord, SourceRegistry, get_registry

PackRunStatus = Literal["completed", "blocked", "not_attempted", "failed"]
_RESERVED_SOURCE_OPTIONS = frozenset(
    {"as_of", "brief", "commercial_context", "gap_id", "lane", "research_id"}
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


class PackError(RuntimeError):
    """Pack 정의 또는 실행 요청이 정본 계약과 맞지 않을 때."""


@dataclass(frozen=True)
class SourceStrategy:
    """한 Pack 안에서 소스를 다루는 우선순위와 실행 경로."""

    source_id: str
    collector_type: type[GuardedCollector] | None
    priority: int
    strategy: str
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.strategy:
            raise ValueError("SourceStrategy source_id/strategy는 비어 있으면 안 된다")
        if self.priority <= 0:
            raise ValueError("SourceStrategy priority는 양수여야 한다")
        if self.collector_type is not None and self.collector_type.source_id != self.source_id:
            raise ValueError(
                "SourceStrategy source_id와 collector_type.source_id가 다르다: "
                f"{self.source_id} != {self.collector_type.source_id}"
            )
        if self.collector_type is None and not self.unavailable_reason:
            raise ValueError("collector가 없는 전략은 unavailable_reason이 필요하다")


@dataclass(frozen=True)
class PackDefinition:
    """정책 정본의 Pack과 결정론적 실행 전략."""

    pack_id: str
    purpose: str
    sources: tuple[SourceStrategy, ...]

    def __post_init__(self) -> None:
        if not self.pack_id or not self.purpose or not self.sources:
            raise ValueError("PackDefinition pack_id/purpose/sources는 비어 있으면 안 된다")
        source_ids = [source.source_id for source in self.sources]
        priorities = [source.priority for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError(f"Pack source_id가 중복됐다: {self.pack_id}")
        if len(priorities) != len(set(priorities)):
            raise ValueError(f"Pack priority가 중복됐다: {self.pack_id}")

    @property
    def ordered_sources(self) -> tuple[SourceStrategy, ...]:
        return tuple(sorted(self.sources, key=lambda source: source.priority))


@dataclass(frozen=True)
class LanePackSelection:
    required: tuple[str, ...]
    optional: tuple[str, ...]


@dataclass(frozen=True)
class SourceRunResult:
    query_run_id: str
    source_id: str
    status: PackRunStatus
    result: CollectResult | None = None
    persisted: PersistedCollectResult | None = None
    extra_gaps: tuple[Gap, ...] = ()
    error: str | None = None

    @property
    def gaps(self) -> tuple[Gap, ...]:
        result_gaps = self.result.gaps if self.result is not None else ()
        return (*result_gaps, *self.extra_gaps)


@dataclass(frozen=True)
class PackRunResult:
    pack_id: str
    query: str
    source_runs: tuple[SourceRunResult, ...]
    registry_rows_synced: int

    @property
    def gaps(self) -> tuple[Gap, ...]:
        return tuple(gap for run in self.source_runs for gap in run.gaps)

    @property
    def result_count(self) -> int:
        return sum(run.result.result_count for run in self.source_runs if run.result is not None)


PACK_MODULES: Mapping[str, str] = {
    "authority-stats": "ria.packs.authority_stats",
    "company-market": "ria.packs.company_market",
    "search-demand": "ria.packs.search_demand",
    "community-signal": "ria.packs.community_signal",
    "tech-launch": "ria.packs.tech_launch",
    "video-signal": "ria.packs.video_signal",
    "app-market": "ria.packs.app_market",
    "commerce-signal": "ria.packs.commerce_signal",
    "regulation-policy": "ria.packs.regulation_policy",
}

# web-primary는 선택 결과에는 들어가지만 Core 실행 모듈은 의도적으로 없다.
LANE_PACKS: Mapping[ResearchLane, LanePackSelection] = {
    "market_size": LanePackSelection(
        required=("authority-stats",), optional=("company-market", "web-primary")
    ),
    "demand": LanePackSelection(required=("search-demand",), optional=("video-signal",)),
    "customer_pain": LanePackSelection(
        required=("community-signal", "web-primary"), optional=("app-market",)
    ),
    "competitors": LanePackSelection(
        required=("company-market", "web-primary"), optional=("tech-launch", "app-market")
    ),
    "technology": LanePackSelection(
        required=("tech-launch", "web-primary"), optional=("video-signal",)
    ),
    "regulation": LanePackSelection(
        required=("regulation-policy", "web-primary"), optional=("authority-stats",)
    ),
    "economics": LanePackSelection(
        required=("company-market", "authority-stats"), optional=("commerce-signal",)
    ),
    "distribution": LanePackSelection(
        required=("search-demand", "web-primary"),
        optional=("community-signal", "video-signal"),
    ),
}

_COLLECTOR_TYPES: Mapping[str, type[GuardedCollector]] = {
    "world_bank": WorldBankCollector,
    "kosis": KosisCollector,
    "data_go_kr": DataGoKrCollector,
    "opendart": OpenDartCollector,
    "naver_search": NaverSearchCollector,
    "naver_datalab": NaverDataLabCollector,
    "naver_shopping_insight": NaverShoppingInsightCollector,
    "hacker_news": HackerNewsCollector,
    "hn_algolia": HNAlgoliaCollector,
    "reddit": RedditCollector,
    "threads": ThreadsCollector,
    "youtube_data": YouTubeCollector,
}


def get_pack(pack_id: str, *, registry: SourceRegistry | None = None) -> PackDefinition:
    """9개 Core Pack 정의를 가져오고 Registry 구성과 어긋나지 않는지 검사한다."""
    if pack_id == "web-primary":
        raise PackError(
            "web-primary는 Core 실행 모듈이 없다; Codex/Chrome 확인 후 store_web_snapshot을 써라"
        )
    try:
        module_name = PACK_MODULES[pack_id]
    except KeyError:
        raise PackError(f"정의되지 않은 Core Pack이다: {pack_id}") from None
    definition = getattr(import_module(module_name), "PACK", None)
    if not isinstance(definition, PackDefinition) or definition.pack_id != pack_id:
        raise PackError(f"Pack 모듈의 PACK 정의가 잘못됐다: {module_name}")

    resolved_registry = registry or get_registry()
    registered = {record.source_id for record in resolved_registry.list_sources(pack_id=pack_id)}
    declared = {strategy.source_id for strategy in definition.sources}
    if registered != declared:
        raise PackError(
            f"Pack 정의가 Source Registry와 어긋난다: {pack_id}; "
            f"missing={sorted(registered - declared)}, extra={sorted(declared - registered)}"
        )
    return definition


def select_packs(
    lanes: Sequence[ResearchLane], *, include_optional: bool = False
) -> tuple[str, ...]:
    """Lane 순서와 최초 등장을 보존하며 필요한 최소 Pack을 고른다."""
    unknown = [lane for lane in lanes if lane not in RESEARCH_LANES]
    if unknown:
        raise PackError(f"정의되지 않은 Research Lane이다: {unknown}")
    selected: list[str] = []
    for lane in lanes:
        mapping = LANE_PACKS[lane]
        candidates = (
            (*mapping.required, *mapping.optional) if include_optional else mapping.required
        )
        for pack_id in candidates:
            if pack_id not in selected:
                selected.append(pack_id)
    return tuple(selected)


def sync_source_registry(
    store: Store,
    registry: SourceRegistry | None = None,
    *,
    synced_at: datetime | None = None,
) -> int:
    """YAML 정본 20행을 DB의 최신 사용시점 정책 스냅샷으로 동기화한다."""
    resolved_registry = registry or get_registry()
    timestamp = synced_at or now()
    if timestamp.tzinfo is None:
        raise PackError("source_registry synced_at은 timezone-aware여야 한다")
    records = list(resolved_registry)
    source_ids = [record.source_id for record in records]

    with store.transaction() as connection:
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            connection.execute(
                f"DELETE FROM source_registry WHERE source_id NOT IN ({placeholders})", source_ids
            )
        else:
            connection.execute("DELETE FROM source_registry")
        for record in records:
            connection.execute(_SOURCE_REGISTRY_UPSERT, _registry_values(record, timestamp))
    return len(records)


class PackRunner:
    """Pack/소스 실행, query log, 정규화 결과 적재를 한 경로로 묶는다."""

    def __init__(
        self,
        store: Store,
        *,
        registry: SourceRegistry | None = None,
        config: Config | None = None,
        collectors: Mapping[str, GuardedCollector] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry or get_registry()
        self.config = config or get_config()
        self.collectors = dict(collectors or {})
        for source_id, collector in self.collectors.items():
            if not isinstance(collector, GuardedCollector):
                raise PackError(f"주입 collector는 GuardedCollector여야 한다: {source_id}")
            if collector.source_id != source_id:
                raise PackError(
                    f"주입 collector source_id가 mapping key와 다르다: "
                    f"{collector.source_id} != {source_id}"
                )
            if collector._registry is not self.registry or collector._config is not self.config:
                raise PackError(
                    f"주입 collector는 PackRunner와 동일한 registry/config를 써야 한다: {source_id}"
                )

    def run_pack(
        self,
        pack_id: str,
        query: str,
        *,
        source_options: Mapping[str, Mapping[str, Any]] | None = None,
        brief: ResearchBrief | None = None,
        lane: ResearchLane | None = None,
        research_id: str | None = None,
        as_of: date | None = None,
        commercial_context: bool = True,
        stored_at: datetime | None = None,
    ) -> PackRunResult:
        definition = get_pack(pack_id, registry=self.registry)
        options_by_source = dict(source_options or {})
        declared_sources = {source.source_id for source in definition.sources}
        if unknown := set(options_by_source) - declared_sources:
            raise PackError(f"{pack_id}에 속하지 않은 source_options다: {sorted(unknown)}")
        for source_id, options in options_by_source.items():
            _reject_reserved_source_options(source_id, options)
        resolved_research_id = self._resolve_research_id(brief, research_id)
        resolved_commercial_context = _resolve_commercial_context(brief, commercial_context)
        timestamp = stored_at or now()
        synced = sync_source_registry(self.store, self.registry, synced_at=timestamp)
        runs: list[SourceRunResult] = []
        for strategy in definition.ordered_sources:
            options = dict(options_by_source.get(strategy.source_id, {}))
            if pack_id == "tech-launch" and strategy.source_id == "hacker_news":
                candidates = _verified_hn_candidate_ids(runs)
                if candidates and "item_ids" not in options:
                    options["item_ids"] = candidates
            runs.append(
                self._run_source(
                    strategy,
                    query,
                    pack_id=pack_id,
                    options=options,
                    brief=brief,
                    lane=lane,
                    research_id=resolved_research_id,
                    as_of=as_of,
                    commercial_context=resolved_commercial_context,
                    stored_at=timestamp,
                )
            )
        return PackRunResult(
            pack_id=pack_id,
            query=query,
            source_runs=tuple(runs),
            registry_rows_synced=synced,
        )

    def collect_source(
        self,
        source_id: str,
        query: str,
        *,
        options: Mapping[str, Any] | None = None,
        brief: ResearchBrief | None = None,
        lane: ResearchLane | None = None,
        research_id: str | None = None,
        as_of: date | None = None,
        commercial_context: bool = True,
        stored_at: datetime | None = None,
    ) -> SourceRunResult:
        self.registry.get(source_id)
        resolved_options = dict(options or {})
        _reject_reserved_source_options(source_id, resolved_options)
        resolved_research_id = self._resolve_research_id(brief, research_id)
        resolved_commercial_context = _resolve_commercial_context(brief, commercial_context)
        timestamp = stored_at or now()
        sync_source_registry(self.store, self.registry, synced_at=timestamp)
        collector_type = _COLLECTOR_TYPES.get(source_id)
        strategy = SourceStrategy(
            source_id=source_id,
            collector_type=collector_type,
            priority=1,
            strategy="직접 source 수집; Policy Guard 선행",
            unavailable_reason=(
                None if collector_type is not None else f"{source_id} collector 실행 경로가 없다"
            ),
        )
        return self._run_source(
            strategy,
            query,
            pack_id=None,
            options=resolved_options,
            brief=brief,
            lane=lane,
            research_id=resolved_research_id,
            as_of=as_of,
            commercial_context=resolved_commercial_context,
            stored_at=timestamp,
        )

    def _resolve_research_id(
        self, brief: ResearchBrief | None, research_id: str | None
    ) -> str | None:
        if brief is not None and research_id is not None and brief.research_id != research_id:
            raise PackError(
                "brief.research_id와 명시한 research_id가 다르다: "
                f"{brief.research_id} != {research_id}"
            )
        resolved = brief.research_id if brief is not None else research_id
        if resolved is None:
            return None
        row = self.store.connection.execute(
            "SELECT 1 FROM research_runs WHERE research_id = ?", (resolved,)
        ).fetchone()
        if row is None:
            raise PackError(f"research_runs에 없는 research_id다: {resolved}")
        return resolved

    def _run_source(
        self,
        strategy: SourceStrategy,
        query: str,
        *,
        pack_id: str | None,
        options: dict[str, Any],
        brief: ResearchBrief | None,
        lane: ResearchLane | None,
        research_id: str | None,
        as_of: date | None,
        commercial_context: bool,
        stored_at: datetime,
    ) -> SourceRunResult:
        query_run_id = new_id("qry")
        logged_options = dict(options)
        if as_of is not None:
            logged_options["as_of"] = as_of
        logged_options["commercial_context"] = commercial_context
        self._start_query_run(
            query_run_id,
            source_id=strategy.source_id,
            pack_id=pack_id,
            query=query,
            options=logged_options,
            research_id=research_id,
            started_at=stored_at,
        )

        try:
            if strategy.collector_type is None and strategy.source_id not in self.collectors:
                return self._unavailable_source(
                    strategy,
                    query_run_id=query_run_id,
                    query=query,
                    pack_id=pack_id,
                    brief=brief,
                    lane=lane,
                    as_of=as_of,
                    commercial_context=commercial_context,
                    research_id=research_id,
                    finished_at=stored_at,
                )

            collector = self.collectors.get(strategy.source_id)
            if collector is None:
                collector_type = cast(type[GuardedCollector], strategy.collector_type)
                collector = collector_type(registry=self.registry, config=self.config)
            call_options = dict(options)
            call_options["commercial_context"] = commercial_context
            if brief is not None:
                call_options["brief"] = brief
            if lane is not None:
                call_options["lane"] = lane
            if as_of is not None:
                call_options["as_of"] = as_of
            result = collector.collect(query, **call_options)
            result = _bind_query_context(result, query_run_id, research_id)
            persisted = persist_collect_result(
                self.store,
                result,
                stored_at=stored_at,
                registry=self.registry,
            )
            status: PackRunStatus = "completed" if result.allowed else "blocked"
            extra_gaps = _no_data_gap(result, pack_id=pack_id, lane=lane)
            query_error = (
                _policy_blocked_detail(result.policy)
                if isinstance(result.policy, PolicyBlocked)
                else None
            )
            self._finish_query_run(
                query_run_id,
                status=status,
                result_count=result.result_count,
                error=query_error,
                finished_at=stored_at,
            )
            run = SourceRunResult(
                query_run_id=query_run_id,
                source_id=strategy.source_id,
                status=status,
                result=_annotate_result_gaps(result, pack_id=pack_id, lane=lane),
                persisted=persisted,
                extra_gaps=extra_gaps,
                error=query_error,
            )
            self._persist_gaps(run.gaps, research_id=research_id, created_at=stored_at)
            return run
        except Exception as error:  # Pack은 한 source 실패 뒤 다음 source를 계속한다.
            safe_error = _safe_error(error, self.config, options)
            self._finish_query_run(
                query_run_id,
                status="failed",
                result_count=0,
                error=safe_error,
                finished_at=stored_at,
            )
            gap = Gap(
                gap_id=new_id("gap"),
                kind="source_error",
                detail=f"{strategy.source_id} 수집 실패: {safe_error}",
                lane=lane,
                source_id=strategy.source_id,
                pack_id=pack_id,
                next_action="입력·응답·공식 정책을 확인한 뒤 해당 source만 재시도",
            )
            self._persist_gaps((gap,), research_id=research_id, created_at=stored_at)
            return SourceRunResult(
                query_run_id=query_run_id,
                source_id=strategy.source_id,
                status="failed",
                extra_gaps=(gap,),
                error=safe_error,
            )

    def _unavailable_source(
        self,
        strategy: SourceStrategy,
        *,
        query_run_id: str,
        query: str,
        pack_id: str | None,
        brief: ResearchBrief | None,
        lane: ResearchLane | None,
        as_of: date | None,
        commercial_context: bool,
        research_id: str | None,
        finished_at: datetime,
    ) -> SourceRunResult:
        if brief is not None:
            decision = check_for_brief(
                brief,
                strategy.source_id,
                requested_calls=1,
                as_of=as_of,
                registry=self.registry,
                config=self.config,
            )
        else:
            decision = check_source(
                strategy.source_id,
                commercial_context=commercial_context,
                requested_calls=1,
                as_of=as_of,
                registry=self.registry,
                config=self.config,
            )
        if isinstance(decision, PolicyBlocked):
            gap = decision.to_gap(new_id("gap"), lane=lane).model_copy(update={"pack_id": pack_id})
            result = CollectResult(
                source_id=strategy.source_id,
                query=query,
                policy=decision,
                gaps=(gap,),
            )
            status: PackRunStatus = "blocked"
        else:
            gap = Gap(
                gap_id=new_id("gap"),
                kind="not_attempted",
                detail=cast(str, strategy.unavailable_reason),
                lane=lane,
                source_id=strategy.source_id,
                pack_id=pack_id,
                next_action="정본 판정과 명시적 구현 지시 전에는 비공식 경로로 대체하지 않음",
            )
            result = None
            status = "not_attempted"
        query_error = (
            _policy_blocked_detail(decision)
            if isinstance(decision, PolicyBlocked)
            else cast(str, strategy.unavailable_reason)
        )
        self._finish_query_run(
            query_run_id,
            status=status,
            result_count=0,
            error=query_error,
            finished_at=finished_at,
        )
        run = SourceRunResult(
            query_run_id=query_run_id,
            source_id=strategy.source_id,
            status=status,
            result=result,
            extra_gaps=() if result is not None else (gap,),
            error=query_error,
        )
        self._persist_gaps(run.gaps, research_id=research_id, created_at=finished_at)
        return run

    def _start_query_run(
        self,
        query_run_id: str,
        *,
        source_id: str,
        pack_id: str | None,
        query: str,
        options: Mapping[str, Any],
        research_id: str | None,
        started_at: datetime,
    ) -> None:
        self.store.connection.execute(
            "INSERT INTO query_runs (query_run_id, research_id, pack_id, source_id, query,"
            " options_json, status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                query_run_id,
                research_id,
                pack_id,
                source_id,
                query,
                json.dumps(_json_safe(options), ensure_ascii=False, sort_keys=True),
                to_iso8601(started_at),
            ),
        )

    def _finish_query_run(
        self,
        query_run_id: str,
        *,
        status: PackRunStatus,
        result_count: int,
        error: str | None,
        finished_at: datetime,
    ) -> None:
        self.store.connection.execute(
            "UPDATE query_runs SET status = ?, result_count = ?, error = ?, finished_at = ?"
            " WHERE query_run_id = ?",
            (status, result_count, error, to_iso8601(finished_at), query_run_id),
        )

    def _persist_gaps(
        self,
        gaps: Sequence[Gap],
        *,
        research_id: str | None,
        created_at: datetime,
    ) -> None:
        if research_id is None:
            return
        for gap in gaps:
            self.store.connection.execute(
                "INSERT OR IGNORE INTO research_gaps (gap_id, research_id, kind, detail, lane,"
                " source_id, pack_id, next_action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gap.gap_id,
                    research_id,
                    gap.kind,
                    gap.detail,
                    gap.lane,
                    gap.source_id,
                    gap.pack_id,
                    gap.next_action,
                    to_iso8601(created_at),
                ),
            )


_SOURCE_REGISTRY_UPSERT = """
INSERT INTO source_registry (
    source_id, name, pack_ids_json, access_status, access_method, official,
    commercial_use, auth_type, rate_limit_model, storage_policy, deletion_policy,
    allowed_data_types_json, blocked_data_types_json, last_verified_at,
    verify_before_use, fallback_sources_json, policy_urls_json, notes, synced_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source_id) DO UPDATE SET
    name = excluded.name,
    pack_ids_json = excluded.pack_ids_json,
    access_status = excluded.access_status,
    access_method = excluded.access_method,
    official = excluded.official,
    commercial_use = excluded.commercial_use,
    auth_type = excluded.auth_type,
    rate_limit_model = excluded.rate_limit_model,
    storage_policy = excluded.storage_policy,
    deletion_policy = excluded.deletion_policy,
    allowed_data_types_json = excluded.allowed_data_types_json,
    blocked_data_types_json = excluded.blocked_data_types_json,
    last_verified_at = excluded.last_verified_at,
    verify_before_use = excluded.verify_before_use,
    fallback_sources_json = excluded.fallback_sources_json,
    policy_urls_json = excluded.policy_urls_json,
    notes = excluded.notes,
    synced_at = excluded.synced_at
"""


def _registry_values(record: SourceRecord, synced_at: datetime) -> tuple[Any, ...]:
    encode = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731
    return (
        record.source_id,
        record.name,
        encode(record.pack_ids),
        record.access_status,
        record.access_method,
        int(record.official),
        record.commercial_use,
        record.auth_type,
        record.rate_limit_model,
        record.storage_policy,
        record.deletion_policy,
        encode(record.allowed_data_types),
        encode(record.blocked_data_types),
        record.last_verified_at.isoformat(),
        int(record.verify_before_use),
        encode(record.fallback_sources),
        encode(record.policy_urls),
        record.notes,
        to_iso8601(synced_at),
    )


def _bind_query_context(
    result: CollectResult, query_run_id: str, research_id: str | None
) -> CollectResult:
    observations = tuple(
        replace(item, query_run_id=query_run_id, research_id=research_id)
        for item in result.observations
    )
    metrics = tuple(
        replace(item, metric=replace(item.metric, research_id=research_id))
        for item in result.metrics
    )
    return replace(result, observations=observations, metrics=metrics)


def _annotate_result_gaps(
    result: CollectResult, *, pack_id: str | None, lane: ResearchLane | None
) -> CollectResult:
    gaps = tuple(
        gap.model_copy(
            update={
                "pack_id": pack_id if gap.pack_id is None else gap.pack_id,
                "lane": lane if gap.lane is None else gap.lane,
            }
        )
        for gap in result.gaps
    )
    return replace(result, gaps=gaps)


def _no_data_gap(
    result: CollectResult, *, pack_id: str | None, lane: ResearchLane | None
) -> tuple[Gap, ...]:
    if not result.allowed or result.contents or result.observations or result.metrics:
        return ()
    candidates = result.metadata.get("candidate_ids", ())
    if isinstance(candidates, Sequence) and not isinstance(candidates, str | bytes) and candidates:
        return ()
    return (
        Gap(
            gap_id=new_id("gap"),
            kind="no_data",
            detail=f"{result.source_id}가 query에 대한 저장 가능 결과를 반환하지 않았다",
            lane=lane,
            source_id=result.source_id,
            pack_id=pack_id,
            next_action="query·기간·공식 소스 상태를 확인",
        ),
    )


def _verified_hn_candidate_ids(runs: Sequence[SourceRunResult]) -> tuple[int, ...]:
    for run in runs:
        if run.source_id != "hn_algolia" or run.result is None or not run.result.allowed:
            continue
        raw = run.result.metadata.get("candidate_ids", ())
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
            return tuple(
                item
                for item in raw
                if isinstance(item, int) and not isinstance(item, bool) and item > 0
            )
    return ()


def _reject_reserved_source_options(source_id: str, options: Mapping[str, Any]) -> None:
    if reserved := set(options) & _RESERVED_SOURCE_OPTIONS:
        raise PackError(
            f"{source_id} source_options에서 Pack 공통 정책 옵션을 덮어쓸 수 없다: "
            f"{sorted(reserved)}"
        )


def _resolve_commercial_context(brief: ResearchBrief | None, commercial_context: bool) -> bool:
    if not isinstance(commercial_context, bool):
        raise PackError("commercial_context는 bool이어야 한다")
    return brief.constraints.commercial_context if brief is not None else commercial_context


def _policy_blocked_detail(decision: PolicyBlocked) -> str:
    return f"[{decision.check}/{decision.reason}] {decision.detail}"


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): ("REDACTED" if _is_sensitive_option_key(str(key)) else _json_safe(item))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return redact_url(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def _is_sensitive_option_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in {
        "access_token",
        "api_key",
        "apikey",
        "app_secret",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "key",
        "password",
        "refresh_token",
        "service_key",
        "servicekey",
        "token",
    } or normalized.endswith(("_api_key", "_credential", "_password", "_secret", "_token"))


def _safe_error(error: Exception, config: Config, options: Mapping[str, Any]) -> str:
    message = f"{type(error).__name__}: {error}"
    for secret in (*config.credentials.values(), *_sensitive_values(options)):
        if secret:
            message = message.replace(secret, "REDACTED")
    return _redact_urls(message)


def _sensitive_values(value: Any, *, sensitive: bool = False) -> tuple[str, ...]:
    if is_dataclass(value) and not isinstance(value, type):
        return _sensitive_values(asdict(value), sensitive=sensitive)
    if isinstance(value, Mapping):
        values: list[str] = []
        for key, item in value.items():
            values.extend(
                _sensitive_values(
                    item,
                    sensitive=sensitive or _is_sensitive_option_key(str(key)),
                )
            )
        return tuple(values)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(
            secret for item in value for secret in _sensitive_values(item, sensitive=sensitive)
        )
    if sensitive and value is not None:
        return (str(value),)
    return ()


def _redact_urls(message: str) -> str:
    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        url = raw.rstrip("),.;]")
        return f"{redact_url(url)}{raw[len(url) :]}"

    return _URL_PATTERN.sub(replace_url, message)


__all__ = [
    "LANE_PACKS",
    "PACK_MODULES",
    "LanePackSelection",
    "PackDefinition",
    "PackError",
    "PackRunResult",
    "PackRunner",
    "SourceRunResult",
    "SourceStrategy",
    "get_pack",
    "select_packs",
    "sync_source_registry",
]
