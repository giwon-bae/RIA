"""B-9 YouTube 공식 API 수집과 observation·metric 30일 retention."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ria.collectors.persistence import persist_collect_result
from ria.collectors.youtube import YouTubeCollector
from ria.config import KST, Config
from ria.core.entities import ContentItemInput, upsert_content_item
from ria.core.metrics import MetricInput, record_metric
from ria.core.observations import ObservationInput, record_observation
from ria.core.snapshots import SnapshotInput, enforce_retention, get_snapshot, store_snapshot
from ria.core.store import Store
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 17, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")
API_KEY = "fixture-youtube-secret"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(tmp_path: Path, *, keyed: bool) -> Config:
    credentials = {"RIA_YOUTUBE_API_KEY": API_KEY} if keyed else {}
    return Config(db_path=tmp_path / "youtube.db", credentials=credentials)


def _fixture_transport() -> httpx.MockTransport:
    search = _fixture("youtube_search.json")
    videos = _fixture("youtube_videos.json")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = search if request.url.path == "/youtube/v3/search" else videos
        return httpx.Response(200, json=payload, request=request)

    return httpx.MockTransport(handler)


def _collect(tmp_path: Path) -> Any:
    return YouTubeCollector(
        http=HttpClient(transport=_fixture_transport()),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "스마트팩토리 AI",
        max_results=2,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )


def test_missing_key_is_guarded_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = YouTubeCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=False),
    ).collect("테스트", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "missing_credential"
    assert calls == 0


def test_search_then_videos_uses_exact_params_and_only_raw_count_metrics(
    tmp_path: Path,
) -> None:
    search = _fixture("youtube_search.json")
    videos = _fixture("youtube_videos.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "www.googleapis.com"
        if request.url.path == "/youtube/v3/search":
            assert dict(request.url.params) == {
                "part": "snippet",
                "type": "video",
                "q": "스마트팩토리 AI",
                "maxResults": "2",
                "key": API_KEY,
            }
            return httpx.Response(200, json=search, request=request)
        assert request.url.path == "/youtube/v3/videos"
        assert dict(request.url.params) == {
            "part": "snippet,statistics",
            "id": "abc123XYZ01,def456UVW02",
            "key": API_KEY,
        }
        return httpx.Response(200, json=videos, request=request)

    result = YouTubeCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "스마트팩토리 AI",
        max_results=2,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert [request.url.path for request in calls] == [
        "/youtube/v3/search",
        "/youtube/v3/videos",
    ]
    assert [item.item.url for item in result.contents] == [
        "https://www.youtube.com/watch?v=abc123XYZ01",
        "https://www.youtube.com/watch?v=def456UVW02",
    ]
    assert result.contents[0].item.title == "스마트팩토리 AI 도입 사례"
    assert result.observations[0].payload["snippet"] == videos["items"][0]["snippet"]
    assert result.observations[0].payload["statistics"] == {
        "viewCount": "1200",
        "likeCount": "87",
        "commentCount": "12",
    }
    assert [item.metric.metric_name for item in result.metrics] == [
        "view_count",
        "like_count",
        "comment_count",
        "view_count",
        "like_count",
        "comment_count",
    ]
    assert [item.metric.value for item in result.metrics] == [1200, 87, 12, 250, 19, 3]
    assert all(item.metric.index_type == "absolute" for item in result.metrics)
    assert all(item.metric.unit == "count" for item in result.metrics)
    assert all(item.metric.platform == "youtube" for item in result.metrics)
    assert {item.metric.metric_name for item in result.metrics} == {
        "view_count",
        "like_count",
        "comment_count",
    }
    assert "favorite" not in repr(result.metrics).lower()
    assert "dislike" not in repr(result.metrics).lower()
    assert "favorite" not in repr(result.observations).lower()
    assert "dislike" not in repr(result.observations).lower()
    assert "ratio" not in repr(result.metrics).lower()
    assert API_KEY not in repr(result)


def test_pagination_runs_search_and_videos_once_per_page(tmp_path: Path) -> None:
    first_search = _fixture("youtube_search.json")
    first_videos = _fixture("youtube_videos.json")
    second_search = copy.deepcopy(first_search)
    second_search.pop("nextPageToken")
    second_search["items"] = [
        {
            "kind": "youtube#searchResult",
            "id": {"kind": "youtube#video", "videoId": "ghi789RST03"},
            "snippet": {"title": "third candidate"},
        }
    ]
    second_videos = copy.deepcopy(first_videos)
    second_videos["items"] = [copy.deepcopy(first_videos["items"][0])]
    second_videos["items"][0]["id"] = "ghi789RST03"
    second_videos["items"][0]["snippet"]["title"] = "세 번째 공식 영상"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        token = request.url.params.get("pageToken")
        if request.url.path == "/youtube/v3/search":
            if token is None:
                return httpx.Response(200, json=first_search, request=request)
            assert token == "NEXT_PAGE"
            return httpx.Response(200, json=second_search, request=request)
        if token is None and request.url.params["id"] == "abc123XYZ01,def456UVW02":
            return httpx.Response(200, json=first_videos, request=request)
        assert request.url.params["id"] == "ghi789RST03"
        return httpx.Response(200, json=second_videos, request=request)

    result = YouTubeCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "스마트팩토리 AI",
        max_pages=2,
        max_results=2,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert [request.url.path for request in calls] == [
        "/youtube/v3/search",
        "/youtube/v3/videos",
        "/youtube/v3/search",
        "/youtube/v3/videos",
    ]
    assert "pageToken" not in calls[0].url.params
    assert calls[2].url.params["pageToken"] == "NEXT_PAGE"
    assert len(result.observations) == 3
    assert len(result.metadata["snapshots"]) == 4


def test_max_pages_estimate_cannot_bypass_guard(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    collector = YouTubeCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    )
    assert collector._estimate_requested_calls("테스트", {"max_pages": 26}) == 52

    result = collector.collect(
        "테스트",
        max_pages=26,
        requested_calls=1,
        as_of=AS_OF,
    )

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert calls == 0


def test_persisted_youtube_retention_clears_payload_and_metrics_only_after_expiry(
    tmp_path: Path,
) -> None:
    result = _collect(tmp_path)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (2, 2, 6, 2)

        linked = store.connection.execute(
            "SELECT s.meta_json FROM source_observations o"
            " JOIN raw_snapshots s ON s.snapshot_id = o.snapshot_id"
            " WHERE o.source_id = 'youtube_data'"
        ).fetchall()
        assert len(linked) == 2
        assert all(json.loads(row["meta_json"])["endpoint"] == "videos.list" for row in linked)

        hn_snapshot = store_snapshot(
            store,
            SnapshotInput(
                source_id="hacker_news",
                body={"id": 9001, "score": 5},
                collected_at=OBSERVED_AT,
            ),
        )
        hn_content_id = upsert_content_item(
            store,
            ContentItemInput(
                content_type="article",
                url="https://news.ycombinator.com/item?id=9001",
                title="HN trace",
                publisher="Hacker News",
            ),
            now=OBSERVED_AT,
        )
        hn_observation_id = record_observation(
            store,
            ObservationInput(
                content_item_id=hn_content_id,
                source_id="hacker_news",
                platform="hacker_news",
                platform_item_id="9001",
                observed_at=OBSERVED_AT,
                payload={"id": 9001, "score": 5},
                snapshot_id=hn_snapshot.snapshot_id,
            ),
            now=OBSERVED_AT,
        )
        hn_metric_id = record_metric(
            store,
            MetricInput(
                metric_name="hn_score",
                value=5,
                index_type="absolute",
                source_id="hacker_news",
                observed_at=OBSERVED_AT,
                unit="points",
                platform="hacker_news",
                content_item_id=hn_content_id,
                observation_id=hn_observation_id,
            ),
            now=OBSERVED_AT,
        )

        assert enforce_retention(store, OBSERVED_AT + timedelta(days=29)) == []
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM metrics WHERE source_id = 'youtube_data'"
            ).fetchone()["n"]
            == 6
        )

        processed = enforce_retention(store, OBSERVED_AT + timedelta(days=31))
        assert set(processed) == set(persisted.snapshot_ids.values())
        youtube_rows = store.connection.execute(
            "SELECT payload_json, snapshot_id FROM source_observations"
            " WHERE source_id = 'youtube_data'"
        ).fetchall()
        assert len(youtube_rows) == 2
        assert all(row["payload_json"] is None for row in youtube_rows)
        assert all(row["snapshot_id"] is not None for row in youtube_rows)
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM metrics WHERE source_id = 'youtube_data'"
            ).fetchone()["n"]
            == 0
        )
        assert all(
            get_snapshot(store, snapshot_id).is_expired_placeholder
            for snapshot_id in persisted.snapshot_ids.values()
        )

        hn_observation = store.connection.execute(
            "SELECT payload_json, snapshot_id FROM source_observations WHERE observation_id = ?",
            (hn_observation_id,),
        ).fetchone()
        assert json.loads(hn_observation["payload_json"])["score"] == 5
        assert hn_observation["snapshot_id"] == hn_snapshot.snapshot_id
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM metrics WHERE metric_row_id = ?", (hn_metric_id,)
            ).fetchone()["n"]
            == 1
        )
        assert get_snapshot(store, hn_snapshot.snapshot_id).body is not None

        content = store.connection.execute(
            "SELECT title, publisher FROM content_items"
            " WHERE canonical_url = 'https://www.youtube.com/watch?v=abc123XYZ01'"
        ).fetchone()
        assert tuple(content) == ("스마트팩토리 AI 도입 사례", "Factory Lab")
        assert enforce_retention(store, OBSERVED_AT + timedelta(days=31)) == []


def test_purge_cleans_linked_data_before_snapshot_fk_is_cleared(tmp_path: Path) -> None:
    result = _collect(tmp_path)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        processed = enforce_retention(store, OBSERVED_AT + timedelta(days=31), purge=True)

        assert set(processed) == set(persisted.snapshot_ids.values())
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM raw_snapshots WHERE source_id = 'youtube_data'"
            ).fetchone()["n"]
            == 0
        )
        observations = store.connection.execute(
            "SELECT payload_json, snapshot_id FROM source_observations"
            " WHERE source_id = 'youtube_data'"
        ).fetchall()
        assert len(observations) == 2
        assert all(row["payload_json"] is None for row in observations)
        assert all(row["snapshot_id"] is None for row in observations)
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM metrics WHERE source_id = 'youtube_data'"
            ).fetchone()["n"]
            == 0
        )
