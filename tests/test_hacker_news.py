"""B-5 Hacker News 공식 원본 검증과 Algolia 후보 격리."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from ria.collectors.hacker_news import (
    HN_REPRESENTATIVENESS_WARNING,
    HackerNewsCollector,
    HNAlgoliaCollector,
)
from ria.collectors.persistence import persist_collect_result
from ria.config import KST, SOURCES_YAML_PATH, Config
from ria.core.store import Store
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked
from ria.policy.registry import SourceRegistry

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 16, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(tmp_path: Path) -> Config:
    return Config(db_path=tmp_path / "hacker-news.db", credentials={})


def _row_count(store: Store, table: str) -> int:
    allowed = {"content_items", "source_observations", "metrics", "raw_snapshots"}
    assert table in allowed
    row = store.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def test_firebase_direct_items_normalize_story_comment_metrics_and_snapshots(
    tmp_path: Path,
) -> None:
    story = _fixture("hn_story.json")
    comment = _fixture("hn_comment.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        payload = {
            "/v0/item/1001.json": story,
            "/v0/item/2001.json": comment,
        }[request.url.path]
        return httpx.Response(200, json=payload, request=request)

    collector = HackerNewsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    )
    result = collector.collect(
        "RIA evidence collector",
        item_ids=[1001, 2001, 1001],
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert calls == ["/v0/item/1001.json", "/v0/item/2001.json"]
    assert len(result.contents) == 2
    assert len(result.observations) == 2
    assert result.contents[0].item.title == "RIA launches an evidence collector"
    assert result.contents[0].item.url == "https://example.com/ria?utm_source=hackernews"
    assert result.contents[1].item.content_type == "post"
    assert result.contents[1].item.published_at is not None
    assert result.contents[1].item.published_at.tzinfo is not None
    assert [metric.metric.metric_name for metric in result.metrics] == [
        "hn_score",
        "hn_comment_count",
    ]
    assert [metric.metric.value for metric in result.metrics] == [321, 42]
    assert all(metric.metric.index_type == "absolute" for metric in result.metrics)
    assert all(
        "시장 수요·시장 규모가 아님" in str(metric.metric.method) for metric in result.metrics
    )
    assert result.metadata["representativeness_warning"] == HN_REPRESENTATIVENESS_WARNING
    assert all(
        observation.payload["representativeness_warning"] == HN_REPRESENTATIVENESS_WARNING
        for observation in result.observations
    )

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (2, 2, 2, 2)
        assert _row_count(store, "raw_snapshots") == 2
        assert _row_count(store, "source_observations") == 2


def test_feed_scan_discards_dead_deleted_null_and_id_mismatch(tmp_path: Path) -> None:
    feed = _fixture("hn_feed.json")
    story = _fixture("hn_story.json")
    invalid = _fixture("hn_invalid_items.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v0/topstories.json":
            payload = feed
        else:
            item_id = request.url.path.removeprefix("/v0/item/").removesuffix(".json")
            payload = story if item_id == "1001" else invalid[item_id]
        if payload is None:
            return httpx.Response(200, content=b"null", request=request)
        return httpx.Response(200, json=payload, request=request)

    collector = HackerNewsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    )

    assert collector._estimate_requested_calls("RIA", {"scan_limit": 5}) == 6
    result = collector.collect(
        "RIA",
        feed="topstories",
        scan_limit=5,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert len(calls) == 6
    assert len(result.observations) == 1
    assert result.observations[0].platform_item_id == "1001"
    assert result.metadata["discarded_item_ids"] == (1002, 1003, 1004, 1005)
    assert len(result.metadata["snapshots"]) == 6


def test_blocked_hacker_news_policy_never_enters_http(tmp_path: Path) -> None:
    source_path = tmp_path / "sources.yaml"
    source_path.write_text(SOURCES_YAML_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    registry = SourceRegistry(source_path)
    registry.set_access_status("hacker_news", "blocked", AS_OF, note="fixture block")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    result = HackerNewsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        registry=registry,
        config=_config(tmp_path),
    ).collect("RIA", item_ids=[1001], as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "access_status_not_allowed"
    assert len(result.gaps) == 1
    assert calls == 0


def test_algolia_is_blocked_in_commercial_context_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = HNAlgoliaCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    ).collect("RIA", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "commercial_use_not_permitted"
    assert calls == 0


def test_algolia_max_pages_estimate_cannot_bypass_guard(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = HNAlgoliaCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    ).collect(
        "RIA",
        max_pages=51,
        requested_calls=1,
        commercial_context=False,
        as_of=AS_OF,
    )

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert calls == 0


def test_algolia_candidates_are_reverified_and_only_firebase_values_normalize(
    tmp_path: Path,
) -> None:
    algolia_payload = _fixture("hn_algolia_search.json")
    official_story = _fixture("hn_story.json")
    algolia_calls: list[httpx.Request] = []

    def algolia_handler(request: httpx.Request) -> httpx.Response:
        algolia_calls.append(request)
        assert request.url.path == "/api/v1/search"
        assert request.url.params["query"] == "RIA"
        assert request.url.params["tags"] == "story"
        return httpx.Response(200, json=algolia_payload, request=request)

    discovery = HNAlgoliaCollector(
        http=HttpClient(transport=httpx.MockTransport(algolia_handler)),
        config=_config(tmp_path),
    ).collect("RIA", commercial_context=False, as_of=AS_OF)

    assert discovery.allowed is True
    assert discovery.metadata["candidate_ids"] == (1001,)
    assert not discovery.contents
    assert not discovery.observations
    assert not discovery.metrics
    assert "MALICIOUS" not in repr(discovery)
    assert "999999999" not in repr(discovery)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, discovery, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (0, 0, 0, 0)
        assert all(
            _row_count(store, table) == 0
            for table in ("content_items", "source_observations", "metrics", "raw_snapshots")
        )

    firebase_calls: list[httpx.Request] = []

    def firebase_handler(request: httpx.Request) -> httpx.Response:
        firebase_calls.append(request)
        return httpx.Response(200, json=official_story, request=request)

    verified = HackerNewsCollector(
        http=HttpClient(transport=httpx.MockTransport(firebase_handler)),
        config=_config(tmp_path),
    ).collect(
        "RIA",
        item_ids=discovery.metadata["candidate_ids"],
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert len(algolia_calls) == 1
    assert len(firebase_calls) == 1
    assert verified.contents[0].item.title == "RIA launches an evidence collector"
    assert verified.metrics[0].metric.value == 321
    assert "MALICIOUS" not in repr(verified)
    assert "999999999" not in repr(verified)
