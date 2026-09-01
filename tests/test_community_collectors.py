"""B-8 Reddit·Threads는 승인 전 차단되고 승인 후 fixture transport만 연다."""

from __future__ import annotations

import base64
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ria.collectors.base import CollectorContractError
from ria.collectors.reddit import RedditCollector
from ria.collectors.threads import ThreadsCollector, ThreadsQuotaCounter
from ria.config import KST, SOURCES_YAML_PATH, Config
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked
from ria.policy.registry import SourceRegistry

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 18, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")
REDDIT_UA = "python:ria-core:2.1.0 (by /u/Ambitious-Debt-8876)"
THREADS_TOKEN = "fixture-threads-token"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _registry(tmp_path: Path, *core_sources: str) -> SourceRegistry:
    target = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, target)
    registry = SourceRegistry(target)
    for source_id in core_sources:
        registry.set_access_status(source_id, "core", AS_OF, note="fixture approval")
    return registry


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "community.db",
        credentials={
            "RIA_REDDIT_CLIENT_ID": "fixture-reddit-id",
            "RIA_REDDIT_CLIENT_SECRET": "fixture-reddit-secret",
            "RIA_REDDIT_USER_AGENT": REDDIT_UA,
            "RIA_THREADS_APP_ID": "fixture-threads-app",
            "RIA_THREADS_APP_SECRET": "fixture-threads-secret",
            "RIA_THREADS_ACCESS_TOKEN": THREADS_TOKEN,
        },
    )


@pytest.mark.parametrize("source", ["reddit", "threads"])
def test_default_gate_blocks_before_transport_with_one_gap(tmp_path: Path, source: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    collector = (
        RedditCollector(
            http=HttpClient(transport=httpx.MockTransport(handler)), config=_config(tmp_path)
        )
        if source == "reddit"
        else ThreadsCollector(
            http=HttpClient(transport=httpx.MockTransport(handler)), config=_config(tmp_path)
        )
    )
    result = collector.collect("RIA", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert len(result.gaps) == 1
    assert calls == 0


def test_reddit_core_uses_oauth_user_agent_and_header_backoff(tmp_path: Path) -> None:
    payload = _fixture("reddit_search.json")
    registry = _registry(tmp_path, "reddit")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/access_token":
            expected = base64.b64encode(b"fixture-reddit-id:fixture-reddit-secret").decode()
            assert request.headers["Authorization"] == f"Basic {expected}"
            assert request.headers["User-Agent"] == REDDIT_UA
            assert request.content == b"grant_type=client_credentials"
            return httpx.Response(
                200, json={"access_token": "fixture-reddit-token"}, request=request
            )
        assert request.url.host == "oauth.reddit.com"
        assert request.url.path == "/search"
        assert request.headers["Authorization"] == "bearer fixture-reddit-token"
        assert request.headers["User-Agent"] == REDDIT_UA
        return httpx.Response(
            200,
            json=payload,
            headers={
                "X-Ratelimit-Used": "3.0",
                "X-Ratelimit-Remaining": "0.0",
                "X-Ratelimit-Reset": "17.5",
            },
            request=request,
        )

    result = RedditCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        registry=registry,
        config=_config(tmp_path),
    ).collect("RIA", max_pages=3, observed_at=OBSERVED_AT, as_of=AS_OF)

    assert result.allowed is True
    assert len(calls) == 2
    assert len(result.observations) == 1
    assert [metric.metric.value for metric in result.metrics] == [42, 7]
    assert result.metadata["backoff_seconds"] == 17.5
    assert result.metadata["rate_limit_pages"][0]["remaining"] == 0.0
    assert "fixture-reddit-token" not in repr(result)
    assert "fixture-reddit-secret" not in repr(result)


def test_threads_core_enters_mock_and_nonempty_result_debits_once(tmp_path: Path) -> None:
    payload = _fixture("threads_keyword_search.json")
    registry = _registry(tmp_path, "threads")
    counter = ThreadsQuotaCounter()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1.0/keyword_search"
        assert request.url.params["q"] == "스마트팩토리"
        assert request.url.params["search_type"] == "RECENT"
        assert request.url.params["search_mode"] == "KEYWORD"
        assert request.url.params["limit"] == "10"
        assert request.url.params["access_token"] == THREADS_TOKEN
        return httpx.Response(200, json=payload, request=request)

    result = ThreadsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        quota_counter=counter,
        registry=registry,
        config=_config(tmp_path),
    ).collect(
        "스마트팩토리",
        search_type="RECENT",
        limit=10,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert len(result.observations) == 1
    assert [metric.metric.value for metric in result.metrics] == [12, 3, 120]
    assert result.metadata["charged_calls"] == 1
    assert result.metadata["quota_used"] == 1
    assert result.metadata["representativeness_warning"] is None
    assert THREADS_TOKEN not in repr(result)


def test_threads_zero_results_do_not_debit_quota(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "threads")
    counter = ThreadsQuotaCounter()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": []}, request=request)
    )
    result = ThreadsCollector(
        http=HttpClient(transport=transport),
        quota_counter=counter,
        registry=registry,
        config=_config(tmp_path),
    ).collect("없는 질의", observed_at=OBSERVED_AT, as_of=AS_OF)

    assert result.metadata["charged_calls"] == 0
    assert result.metadata["zero_result_calls"] == 1
    assert result.metadata["quota_used"] == 0


def test_threads_limit_and_exhausted_counter_block_before_http(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "threads")
    config = _config(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": []}, request=request)

    collector = ThreadsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        registry=registry,
        config=config,
    )
    with pytest.raises(CollectorContractError, match="1..100"):
        collector.collect("RIA", limit=101, observed_at=OBSERVED_AT, as_of=AS_OF)
    assert calls == 0

    counter = ThreadsQuotaCounter()
    quota = config.quota_for("threads")
    assert quota is not None and quota.limit is not None
    counter.record_result(
        counter.user_key("ria-single-configured-threads-user"),
        OBSERVED_AT,
        result_count=1,
        calls=quota.limit,
    )
    exhausted = ThreadsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        quota_counter=counter,
        registry=registry,
        config=config,
    )
    with pytest.raises(CollectorContractError, match="쿼터"):
        exhausted.collect("RIA", observed_at=OBSERVED_AT, as_of=AS_OF)
    assert calls == 0


def test_threads_refresh_uses_new_token_without_exposing_either_token(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "threads")
    refresh_payload = _fixture("threads_refresh_token.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/refresh_access_token":
            assert request.url.params["access_token"] == THREADS_TOKEN
            return httpx.Response(200, json=refresh_payload, request=request)
        assert request.url.params["access_token"] == "fixture-refreshed-token"
        return httpx.Response(200, json={"data": []}, request=request)

    result = ThreadsCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        registry=registry,
        config=_config(tmp_path),
    ).collect(
        "RIA",
        refresh_access_token=True,
        token_expires_at=OBSERVED_AT + timedelta(days=3),
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert len(calls) == 2
    assert result.metadata["access_token_refreshed"] is True
    assert result.metadata["refreshed_expires_in"] == 5184000
    assert "만료 임박" in result.metadata["token_expiry_warning"]
    assert THREADS_TOKEN not in repr(result)
    assert "fixture-refreshed-token" not in repr(result)


def test_threads_quota_survives_collector_and_token_rotation(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "threads")
    counter = ThreadsQuotaCounter()
    subject = "stable-fixture-user"
    config = _config(tmp_path)
    responses = iter(
        (
            {"data": [_fixture("threads_keyword_search.json")["data"][0]]},
            _fixture("threads_refresh_token.json"),
            {"data": [_fixture("threads_keyword_search.json")["data"][0]]},
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses), request=request)

    http = HttpClient(transport=httpx.MockTransport(handler))
    first = ThreadsCollector(
        http=http,
        quota_counter=counter,
        quota_user_subject=subject,
        registry=registry,
        config=config,
    ).collect("RIA", observed_at=OBSERVED_AT, as_of=AS_OF)
    second = ThreadsCollector(
        http=http,
        quota_counter=counter,
        quota_user_subject=subject,
        registry=registry,
        config=config,
    ).collect(
        "RIA",
        refresh_access_token=True,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert first.metadata["quota_used"] == 1
    assert second.metadata["quota_used"] == 2


def test_threads_snapshot_redacts_nested_access_token(tmp_path: Path) -> None:
    registry = _registry(tmp_path, "threads")
    payload = {
        "data": [],
        "paging": {
            "next": "https://graph.threads.net/v1.0/keyword_search?access_token=leak",
            "access_token": "leak",
        },
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    result = ThreadsCollector(
        http=HttpClient(transport=transport),
        quota_counter=ThreadsQuotaCounter(),
        registry=registry,
        config=_config(tmp_path),
    ).collect("RIA", observed_at=OBSERVED_AT, as_of=AS_OF)

    assert "leak" not in repr(result)
    assert "REDACTED" in repr(result)
