"""Collector 결과를 SQLite 정본으로 적재한다.

고정된 B-1 계약에는 snapshot 필드가 없으므로 ``CollectResult.metadata``의 확장점에
``CollectedSnapshot``과 observation→snapshot 참조를 싣는다. 그 외 Content·Observation·
Metric 참조는 base.py의 검증을 그대로 따른다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ria.collectors.base import CollectorContractError, CollectResult
from ria.core.entities import upsert_content_item
from ria.core.metrics import record_metric
from ria.core.observations import record_observation
from ria.core.snapshots import SnapshotInput, store_snapshot
from ria.core.store import Store
from ria.policy.registry import SourceRegistry

SNAPSHOTS_METADATA_KEY = "snapshots"
OBSERVATION_SNAPSHOTS_METADATA_KEY = "observation_snapshot_refs"


@dataclass(frozen=True)
class CollectedSnapshot:
    """결과 내부 ref가 붙은 immutable snapshot 입력."""

    ref: str
    snapshot: SnapshotInput


@dataclass(frozen=True)
class PersistedCollectResult:
    """적재 결과와 실측 행 수."""

    content_ids: Mapping[str, str]
    observation_ids: Mapping[str, str]
    metric_ids: tuple[str, ...]
    snapshot_ids: Mapping[str, str]

    @property
    def content_count(self) -> int:
        return len(set(self.content_ids.values()))

    @property
    def observation_count(self) -> int:
        return len(self.observation_ids)

    @property
    def metric_count(self) -> int:
        return len(self.metric_ids)

    @property
    def snapshot_count(self) -> int:
        return len(set(self.snapshot_ids.values()))


def persist_collect_result(
    store: Store,
    result: CollectResult,
    *,
    stored_at: datetime,
    registry: SourceRegistry | None = None,
) -> PersistedCollectResult:
    """허용된 수집 결과를 snapshot→content→observation→metric 순서로 원자 적재한다."""
    if stored_at.tzinfo is None:
        raise CollectorContractError("stored_at은 timezone-aware여야 한다")
    if not result.allowed:
        return PersistedCollectResult({}, {}, (), {})

    snapshots = _snapshots(result.metadata)
    observation_snapshot_refs = _observation_snapshot_refs(result.metadata)
    snapshot_ids: dict[str, str] = {}
    content_ids: dict[str, str] = {}
    observation_ids: dict[str, str] = {}
    metric_ids: list[str] = []

    with store.transaction():
        for collected in snapshots:
            if collected.snapshot.source_id != result.source_id:
                raise CollectorContractError(
                    "snapshot source_id가 collector 결과와 다르다: "
                    f"{collected.snapshot.source_id} != {result.source_id}"
                )
            if not collected.ref or collected.ref in snapshot_ids:
                raise CollectorContractError(f"snapshot ref가 비었거나 중복됐다: {collected.ref}")
            saved = store_snapshot(store, collected.snapshot, registry=registry)
            snapshot_ids[collected.ref] = saved.snapshot_id

        for collected in result.contents:
            content_ids[collected.ref] = upsert_content_item(store, collected.item, now=stored_at)

        observation_refs = {item.ref for item in result.observations}
        if unknown := set(observation_snapshot_refs) - observation_refs:
            raise CollectorContractError(
                f"snapshot 연결이 없는 observation ref를 가리킨다: {sorted(unknown)}"
            )
        if unknown := set(observation_snapshot_refs.values()) - set(snapshot_ids):
            raise CollectorContractError(
                f"observation이 없는 snapshot ref를 가리킨다: {sorted(unknown)}"
            )

        for collected in result.observations:
            snapshot_ref = observation_snapshot_refs.get(collected.ref)
            observation_ids[collected.ref] = record_observation(
                store,
                collected.to_input(
                    content_ids[collected.content_ref],
                    snapshot_id=snapshot_ids.get(snapshot_ref) if snapshot_ref else None,
                ),
                now=stored_at,
            )

        for collected in result.metrics:
            metric_ids.append(
                record_metric(
                    store,
                    collected.to_input(
                        content_item_id=(
                            content_ids[collected.content_ref]
                            if collected.content_ref is not None
                            else None
                        ),
                        observation_id=(
                            observation_ids[collected.observation_ref]
                            if collected.observation_ref is not None
                            else None
                        ),
                    ),
                    now=stored_at,
                )
            )

    return PersistedCollectResult(
        content_ids=content_ids,
        observation_ids=observation_ids,
        metric_ids=tuple(metric_ids),
        snapshot_ids=snapshot_ids,
    )


def snapshot_metadata(
    snapshots: Sequence[CollectedSnapshot],
    observation_snapshot_refs: Mapping[str, str],
    **metadata: Any,
) -> dict[str, Any]:
    """collector가 공통 snapshot metadata 규약을 만들 때 쓰는 헬퍼."""
    return {
        **metadata,
        SNAPSHOTS_METADATA_KEY: tuple(snapshots),
        OBSERVATION_SNAPSHOTS_METADATA_KEY: dict(observation_snapshot_refs),
    }


def _snapshots(metadata: Mapping[str, Any]) -> tuple[CollectedSnapshot, ...]:
    raw = metadata.get(SNAPSHOTS_METADATA_KEY, ())
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise CollectorContractError("metadata.snapshots는 CollectedSnapshot sequence여야 한다")
    if not all(isinstance(item, CollectedSnapshot) for item in raw):
        raise CollectorContractError("metadata.snapshots 항목 형식이 잘못됐다")
    return tuple(raw)


def _observation_snapshot_refs(metadata: Mapping[str, Any]) -> dict[str, str]:
    raw = metadata.get(OBSERVATION_SNAPSHOTS_METADATA_KEY, {})
    if not isinstance(raw, Mapping):
        raise CollectorContractError("observation_snapshot_refs는 mapping이어야 한다")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
        raise CollectorContractError("observation_snapshot_refs는 str→str mapping이어야 한다")
    return dict(raw)
