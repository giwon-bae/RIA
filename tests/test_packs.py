"""B-10 Pack 정의·정책 스냅샷·실패 격리 오케스트레이션."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    GuardedCollector,
)
from ria.collectors.hacker_news import HackerNewsCollector, HNAlgoliaCollector
from ria.collectors.reddit import RedditCollector
from ria.collectors.threads import ThreadsCollector
from ria.config import KST, SOURCES_YAML_PATH, Config
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.store import Store
from ria.http import HttpClient
from ria.packs import (
    LANE_PACKS,
    PACK_MODULES,
    PackError,
    PackRunner,
    get_pack,
    select_packs,
    sync_source_registry,
)
from ria.policy.guard import PolicyAllowed
from ria.policy.registry import SourceRegistry

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 20, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FailingWorldBankCollector(GuardedCollector):
    source_id = "world_bank"

    def _collect(self, query: str, *, policy: PolicyAllowed, **options: Any) -> CollectedBatch:
        del query, policy
        authorization = options.get("authorization", "fixture world-bank failure")
        raise RuntimeError(
            f"{authorization} https://api.example.test/items?access_token=url-secret"
        )


class _FixtureKosisCollector(GuardedCollector):
    source_id = "kosis"

    def _collect(self, query: str, *, policy: PolicyAllowed, **options: Any) -> CollectedBatch:
        del query, policy, options
        content_ref = "content:kosis:fixture"
        observation_ref = "observation:kosis:fixture"
        return CollectedBatch(
            contents=(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        url="https://kosis.kr/statHtml/fixture",
                        title="fixture statistic",
                        publisher="KOSIS",
                    ),
                ),
            ),
            observations=(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id="kosis",
                    platform="kosis",
                    observed_at=OBSERVED_AT,
                    platform_item_id="fixture",
                ),
            ),
            metrics=(
                CollectedMetric(
                    content_ref=content_ref,
                    observation_ref=observation_ref,
                    metric=MetricInput(
                        metric_name="fixture_count",
                        value=7,
                        index_type="absolute",
                        source_id="kosis",
                        observed_at=OBSERVED_AT,
                        unit="count",
                        denominator=None,
                        geography="KR",
                        period="2026",
                        population="fixture",
                        method="fixture",
                        platform="kosis",
                    ),
                ),
            ),
        )


def _config(tmp_path: Path, **credentials: str) -> Config:
    return Config(db_path=tmp_path / "packs.db", credentials=credentials)


def _writable_registry(tmp_path: Path) -> SourceRegistry:
    path = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, path)
    return SourceRegistry(path)


def test_exact_nine_core_modules_and_no_web_primary_module() -> None:
    assert tuple(PACK_MODULES) == (
        "authority-stats",
        "company-market",
        "search-demand",
        "community-signal",
        "tech-launch",
        "video-signal",
        "app-market",
        "commerce-signal",
        "regulation-policy",
    )
    packs_dir = Path(__file__).parents[1] / "ria" / "packs"
    assert not (packs_dir / "web_primary.py").exists()
    assert {path.stem for path in packs_dir.glob("*.py")} == {
        "__init__",
        "authority_stats",
        "company_market",
        "search_demand",
        "community_signal",
        "tech_launch",
        "video_signal",
        "app_market",
        "commerce_signal",
        "regulation_policy",
    }


def test_every_pack_definition_matches_registry() -> None:
    registry = SourceRegistry()
    for pack_id in PACK_MODULES:
        definition = get_pack(pack_id, registry=registry)
        assert {item.source_id for item in definition.sources} == {
            item.source_id for item in registry.list_sources(pack_id=pack_id)
        }


def test_lane_mapping_matches_design_and_dedupes_in_order() -> None:
    assert {
        lane: (selection.required, selection.optional) for lane, selection in LANE_PACKS.items()
    } == {
        "market_size": (("authority-stats",), ("company-market", "web-primary")),
        "demand": (("search-demand",), ("video-signal",)),
        "customer_pain": (("community-signal", "web-primary"), ("app-market",)),
        "competitors": (
            ("company-market", "web-primary"),
            ("tech-launch", "app-market"),
        ),
        "technology": (("tech-launch", "web-primary"), ("video-signal",)),
        "regulation": (("regulation-policy", "web-primary"), ("authority-stats",)),
        "economics": (
            ("company-market", "authority-stats"),
            ("commerce-signal",),
        ),
        "distribution": (
            ("search-demand", "web-primary"),
            ("community-signal", "video-signal"),
        ),
    }
    assert select_packs(("market_size", "economics")) == (
        "authority-stats",
        "company-market",
    )
    assert select_packs(("demand",), include_optional=True) == (
        "search-demand",
        "video-signal",
    )


def test_web_primary_has_no_core_runner() -> None:
    with pytest.raises(PackError, match="store_web_snapshot"):
        get_pack("web-primary")


def test_sync_source_registry_writes_all_rows_and_updates_snapshot(tmp_path: Path) -> None:
    registry = _writable_registry(tmp_path)
    with Store(":memory:") as store:
        assert sync_source_registry(store, registry, synced_at=OBSERVED_AT) == 20
        first = store.connection.execute(
            "SELECT access_status, synced_at FROM source_registry WHERE source_id = 'reddit'"
        ).fetchone()
        assert first["access_status"] == "blocked"

        registry.set_access_status("reddit", "core", AS_OF, note="fixture approval")
        later = datetime(2026, 9, 1, 21, 0, tzinfo=KST)
        assert sync_source_registry(store, registry, synced_at=later) == 20
        second = store.connection.execute(
            "SELECT access_status, synced_at FROM source_registry WHERE source_id = 'reddit'"
        ).fetchone()

        assert second["access_status"] == "core"
        assert second["synced_at"] != first["synced_at"]
        count = store.connection.execute("SELECT COUNT(*) AS n FROM source_registry").fetchone()
        assert count["n"] == 20


def test_community_pack_blocks_without_entering_transport_and_keeps_going(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    config = _config(
        tmp_path,
        RIA_REDDIT_CLIENT_ID="fixture-id",
        RIA_REDDIT_CLIENT_SECRET="fixture-secret",
        RIA_REDDIT_USER_AGENT="python:ria-core:2.1.0 (by /u/Ambitious-Debt-8876)",
        RIA_THREADS_APP_ID="fixture-app",
        RIA_THREADS_APP_SECRET="fixture-secret",
        RIA_THREADS_ACCESS_TOKEN="fixture-token",
    )
    registry = SourceRegistry()
    transport = httpx.MockTransport(handler)
    collectors = {
        "reddit": RedditCollector(
            http=HttpClient(transport=transport), registry=registry, config=config
        ),
        "threads": ThreadsCollector(
            http=HttpClient(transport=transport), registry=registry, config=config
        ),
    }
    with Store(":memory:") as store:
        result = PackRunner(
            store, registry=registry, config=config, collectors=collectors
        ).run_pack("community-signal", "RIA", as_of=AS_OF, stored_at=OBSERVED_AT)

        assert [run.source_id for run in result.source_runs] == ["reddit", "threads", "x_twitter"]
        assert [run.status for run in result.source_runs] == ["blocked", "blocked", "blocked"]
        assert len(result.gaps) == 3
        assert result.registry_rows_synced == 20
        assert calls == 0
        rows = store.connection.execute("SELECT status FROM query_runs ORDER BY rowid").fetchall()
        assert [row["status"] for row in rows] == ["blocked", "blocked", "blocked"]
        errors = store.connection.execute("SELECT error FROM query_runs ORDER BY rowid").fetchall()
        assert all(row["error"] for row in errors)


def test_source_failure_does_not_stop_later_source_and_query_links_are_stored(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, RIA_KOSIS_API_KEY="fixture-key")
    registry = SourceRegistry()
    collectors = {
        "world_bank": _FailingWorldBankCollector(registry=registry, config=config),
        "kosis": _FixtureKosisCollector(registry=registry, config=config),
    }
    with Store(":memory:") as store:
        result = PackRunner(
            store, registry=registry, config=config, collectors=collectors
        ).run_pack(
            "authority-stats",
            "fixture",
            source_options={"world_bank": {"authorization": "Bearer option-secret"}},
            as_of=AS_OF,
            stored_at=OBSERVED_AT,
        )

        assert [run.status for run in result.source_runs] == [
            "failed",
            "completed",
            "blocked",
        ]
        assert result.source_runs[1].persisted is not None
        assert result.source_runs[1].persisted.observation_count == 1
        assert "option-secret" not in result.source_runs[0].error
        assert "url-secret" not in result.source_runs[0].error
        row = store.connection.execute(
            "SELECT query_run_id FROM source_observations WHERE source_id = 'kosis'"
        ).fetchone()
        assert row["query_run_id"] == result.source_runs[1].query_run_id
        statuses = store.connection.execute(
            "SELECT status FROM query_runs ORDER BY rowid"
        ).fetchall()
        assert [item["status"] for item in statuses] == ["failed", "completed", "blocked"]


def test_source_options_cannot_override_pack_policy_context(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with Store(":memory:") as store:
        runner = PackRunner(store, config=config)
        with pytest.raises(PackError, match="덮어쓸 수 없다"):
            runner.run_pack(
                "authority-stats",
                "SP.POP.TOTL",
                source_options={"world_bank": {"as_of": date(2020, 1, 1)}},
                as_of=AS_OF,
                stored_at=OBSERVED_AT,
            )
        count = store.connection.execute("SELECT COUNT(*) AS n FROM query_runs").fetchone()
        assert count["n"] == 0


def test_collector_injection_must_keep_guard_and_runner_policy_objects(tmp_path: Path) -> None:
    config = _config(tmp_path)
    registry = SourceRegistry()
    with Store(":memory:") as store:
        with pytest.raises(PackError, match="GuardedCollector"):
            PackRunner(
                store,
                registry=registry,
                config=config,
                collectors={"world_bank": object()},  # type: ignore[dict-item]
            )

        other_registry = _writable_registry(tmp_path)
        collector = _FailingWorldBankCollector(registry=other_registry, config=config)
        with pytest.raises(PackError, match="동일한 registry/config"):
            PackRunner(
                store,
                registry=registry,
                config=config,
                collectors={"world_bank": collector},
            )


def test_unknown_research_id_is_rejected_before_any_source_run(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        runner = PackRunner(store, config=_config(tmp_path))
        with pytest.raises(PackError, match="research_runs에 없는"):
            runner.run_pack(
                "authority-stats",
                "SP.POP.TOTL",
                research_id="missing",
                as_of=AS_OF,
                stored_at=OBSERVED_AT,
            )
        count = store.connection.execute("SELECT COUNT(*) AS n FROM query_runs").fetchone()
        assert count["n"] == 0


def test_tech_pack_passes_algolia_candidates_to_official_hn_before_storage(
    tmp_path: Path,
) -> None:
    algolia = _fixture("hn_algolia_search.json")
    official = _fixture("hn_story.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json=algolia, request=request)
        assert request.url.path == "/v0/item/1001.json"
        return httpx.Response(200, json=official, request=request)

    config = _config(tmp_path)
    registry = SourceRegistry()
    transport = httpx.MockTransport(handler)
    collectors = {
        "hn_algolia": HNAlgoliaCollector(
            http=HttpClient(transport=transport), registry=registry, config=config
        ),
        "hacker_news": HackerNewsCollector(
            http=HttpClient(transport=transport), registry=registry, config=config
        ),
    }
    with Store(":memory:") as store:
        result = PackRunner(
            store, registry=registry, config=config, collectors=collectors
        ).run_pack(
            "tech-launch",
            "RIA",
            source_options={
                "hacker_news": {"observed_at": OBSERVED_AT},
            },
            commercial_context=False,
            as_of=AS_OF,
            stored_at=OBSERVED_AT,
        )

        assert [run.source_id for run in result.source_runs] == [
            "hn_algolia",
            "hacker_news",
            "product_hunt",
        ]
        assert result.source_runs[0].result is not None
        assert result.source_runs[0].result.metadata["candidate_ids"] == (1001,)
        assert result.source_runs[0].persisted is not None
        assert result.source_runs[0].persisted.content_count == 0
        assert result.source_runs[1].persisted is not None
        assert result.source_runs[1].persisted.observation_count == 1
        assert calls == [
            "hn.algolia.com/api/v1/search",
            "hacker-news.firebaseio.com/v0/item/1001.json",
        ]
        logged = store.connection.execute(
            "SELECT options_json FROM query_runs WHERE source_id = 'hacker_news'"
        ).fetchone()
        assert json.loads(logged["options_json"])["item_ids"] == [1001]
        snapshot = store.connection.execute("SELECT body FROM raw_snapshots").fetchone()
        assert "MALICIOUS" not in snapshot["body"]


def test_regulation_pack_does_not_open_b7_data_go_path(tmp_path: Path) -> None:
    config = _config(tmp_path, RIA_DATA_GO_KR_KEY="fixture-key")
    with Store(":memory:") as store:
        result = PackRunner(store, config=config).run_pack(
            "regulation-policy", "산업안전", as_of=AS_OF, stored_at=OBSERVED_AT
        )

        assert len(result.source_runs) == 1
        assert result.source_runs[0].source_id == "data_go_kr"
        assert result.source_runs[0].status == "not_attempted"
        assert result.gaps[0].kind == "not_attempted"


def test_registered_only_sources_have_no_collector_path() -> None:
    for pack_id, source_ids in {
        "app-market": {"google_play", "app_store"},
        "commerce-signal": {
            "naver_shopping_search",
            "coupang_seller",
            "coupang_partners",
        },
    }.items():
        definition = get_pack(pack_id)
        strategies = {item.source_id: item for item in definition.sources}
        assert all(strategies[source_id].collector_type is None for source_id in source_ids)


def test_query_log_redacts_secrets_without_losing_keyword_groups(tmp_path: Path) -> None:
    config = _config(tmp_path, RIA_KOSIS_API_KEY="fixture-key")
    registry = SourceRegistry()
    collector = _FixtureKosisCollector(registry=registry, config=config)
    with Store(":memory:") as store:
        PackRunner(
            store,
            registry=registry,
            config=config,
            collectors={"kosis": collector},
        ).collect_source(
            "kosis",
            "fixture",
            options={
                "access_token": "must-not-persist",
                "keyword_groups": [{"group_name": "RIA", "keywords": ["RIA"]}],
            },
            as_of=AS_OF,
            stored_at=OBSERVED_AT,
        )
        row = store.connection.execute(
            "SELECT options_json FROM query_runs WHERE source_id = 'kosis'"
        ).fetchone()
        logged = json.loads(row["options_json"])

        assert logged["access_token"] == "REDACTED"
        assert logged["keyword_groups"] == [{"group_name": "RIA", "keywords": ["RIA"]}]
