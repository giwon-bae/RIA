"""Policy Guard가 적용된 결정론적 source collector (DESIGN §11.2)."""

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
from ria.collectors.data_go_kr import DataGoKrCollector, DataGoKrDatasetSpec
from ria.collectors.kosis import KosisCollector, KosisDatasetSpec
from ria.collectors.persistence import (
    CollectedSnapshot,
    PersistedCollectResult,
    persist_collect_result,
    snapshot_metadata,
)
from ria.collectors.world_bank import WorldBankCollector

__all__ = [
    "CollectResult",
    "CollectedBatch",
    "CollectedContent",
    "CollectedMetric",
    "CollectedObservation",
    "Collector",
    "CollectorContractError",
    "CollectedSnapshot",
    "DataGoKrCollector",
    "DataGoKrDatasetSpec",
    "GuardedCollector",
    "KosisCollector",
    "KosisDatasetSpec",
    "PersistedCollectResult",
    "WorldBankCollector",
    "persist_collect_result",
    "snapshot_metadata",
]
