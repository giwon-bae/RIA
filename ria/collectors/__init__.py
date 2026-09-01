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
from ria.collectors.hacker_news import HackerNewsCollector, HNAlgoliaCollector
from ria.collectors.kosis import KosisCollector, KosisDatasetSpec
from ria.collectors.naver_datalab import NaverDataLabCollector
from ria.collectors.naver_search import NaverSearchCollector
from ria.collectors.naver_shopping_insight import NaverShoppingInsightCollector
from ria.collectors.opendart import OpenDartCollector
from ria.collectors.persistence import (
    CollectedSnapshot,
    PersistedCollectResult,
    persist_collect_result,
    snapshot_metadata,
)
from ria.collectors.web_primary import StoredWebSnapshot, store_web_snapshot
from ria.collectors.world_bank import WorldBankCollector
from ria.collectors.youtube import YouTubeCollector

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
    "HNAlgoliaCollector",
    "HackerNewsCollector",
    "KosisCollector",
    "KosisDatasetSpec",
    "NaverDataLabCollector",
    "NaverSearchCollector",
    "NaverShoppingInsightCollector",
    "OpenDartCollector",
    "PersistedCollectResult",
    "StoredWebSnapshot",
    "WorldBankCollector",
    "YouTubeCollector",
    "persist_collect_result",
    "snapshot_metadata",
    "store_web_snapshot",
]
