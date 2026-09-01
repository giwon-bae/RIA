"""B-4 NAVER API Hub 3종 fixture 수집 계약."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ria.collectors.base import CollectorContractError
from ria.collectors.naver_datalab import NaverDataLabCollector
from ria.collectors.naver_search import NaverSearchCollector
from ria.collectors.naver_shopping_insight import NaverShoppingInsightCollector
from ria.collectors.persistence import persist_collect_result
from ria.config import KST, Config
from ria.core.store import Store
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 15, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")
CLIENT_ID = "fixture-client-id"
CLIENT_SECRET = "fixture-client-secret"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(tmp_path: Path, *, keyed: bool) -> Config:
    credentials = (
        {
            "RIA_NAVER_CLIENT_ID": CLIENT_ID,
            "RIA_NAVER_CLIENT_SECRET": CLIENT_SECRET,
        }
        if keyed
        else {}
    )
    return Config(db_path=tmp_path / "naver.db", credentials=credentials)


@pytest.mark.parametrize(
    "collector_type",
    [NaverSearchCollector, NaverDataLabCollector, NaverShoppingInsightCollector],
)
def test_missing_credentials_are_guarded_before_http(
    tmp_path: Path,
    collector_type: type[
        NaverSearchCollector | NaverDataLabCollector | NaverShoppingInsightCollector
    ],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = collector_type(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=False),
    ).collect("테스트", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "missing_credential"
    assert calls == 0


def test_search_uses_api_hub_and_normalizes_snapshot_without_total_metric(
    tmp_path: Path,
) -> None:
    payload = _fixture("naver_search_webkr.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "naverapihub.apigw.ntruss.com"
        assert request.url.path == "/search/v1/webkr"
        assert dict(request.url.params) == {
            "query": "스마트팩토리",
            "display": "2",
            "start": "1",
            "sort": "sim",
        }
        assert request.headers["X-NCP-APIGW-API-KEY-ID"] == CLIENT_ID
        assert request.headers["X-NCP-APIGW-API-KEY"] == CLIENT_SECRET
        return httpx.Response(200, json=payload, request=request)

    result = NaverSearchCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "스마트팩토리",
        display=2,
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert len(calls) == 1
    assert len(result.contents) == 2
    assert len(result.observations) == 2
    assert not result.metrics
    assert result.metadata["total"] == 243
    assert result.contents[0].item.title == "스마트팩토리 도입 가이드"
    assert all(item.observed_at.tzinfo is not None for item in result.observations)
    assert CLIENT_ID not in repr(result)
    assert CLIENT_SECRET not in repr(result)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (2, 2, 0, 1)


def test_search_pagination_estimate_cannot_bypass_guard(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = NaverSearchCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect("테스트", max_pages=51, requested_calls=1, as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert calls == 0


def test_datalab_posts_exact_body_and_emits_only_relative_search_metrics(
    tmp_path: Path,
) -> None:
    payload = _fixture("naver_datalab_search.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "naverapihub.apigw.ntruss.com"
        assert request.url.path == "/search-trend/v1/search"
        assert request.headers["X-NCP-APIGW-API-KEY-ID"] == CLIENT_ID
        assert request.headers["X-NCP-APIGW-API-KEY"] == CLIENT_SECRET
        assert json.loads(request.content) == {
            "startDate": "2026-08-01",
            "endDate": "2026-08-02",
            "timeUnit": "date",
            "keywordGroups": [
                {
                    "groupName": "스마트팩토리",
                    "keywords": ["스마트팩토리", "스마트 공장"],
                },
                {"groupName": "제조 AI", "keywords": ["제조 AI"]},
            ],
            "device": "pc",
            "ages": ["3", "4"],
        }
        return httpx.Response(200, json=payload, request=request)

    result = NaverDataLabCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "스마트팩토리",
        start_date="2026-08-01",
        end_date="2026-08-02",
        keyword_groups=[
            {
                "groupName": "스마트팩토리",
                "keywords": ["스마트팩토리", "스마트 공장"],
            },
            {"groupName": "제조 AI", "keywords": ["제조 AI"]},
        ],
        device="pc",
        ages=["3", "4"],
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    _assert_relative_metrics(result, "search_interest_index")
    assert [item.metric.value for item in result.metrics] == [73.25, 100, 42.5, 61.75]
    assert CLIENT_ID not in repr(result)
    assert CLIENT_SECRET not in repr(result)


def test_shopping_posts_exact_body_and_emits_only_relative_click_metrics(
    tmp_path: Path,
) -> None:
    payload = _fixture("naver_shopping_keywords.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "naverapihub.apigw.ntruss.com"
        assert request.url.path == "/shopping/v1/category/keywords"
        assert request.headers["X-NCP-APIGW-API-KEY-ID"] == CLIENT_ID
        assert request.headers["X-NCP-APIGW-API-KEY"] == CLIENT_SECRET
        assert json.loads(request.content) == {
            "startDate": "2026-08-01",
            "endDate": "2026-08-02",
            "timeUnit": "date",
            "category": "50000008",
            "keyword": [
                {"name": "산업용 센서", "param": ["산업용 센서"]},
                {"name": "온도 센서", "param": ["온도 센서"]},
            ],
            "gender": "m",
        }
        return httpx.Response(200, json=payload, request=request)

    result = NaverShoppingInsightCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "센서",
        start_date="2026-08-01",
        end_date="2026-08-02",
        category="50000008",
        keywords=[
            {"name": "산업용 센서", "param": ["산업용 센서"]},
            {"name": "온도 센서", "param": ["온도 센서"]},
        ],
        gender="m",
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    _assert_relative_metrics(result, "shopping_click_index")
    assert [item.metric.value for item in result.metrics] == [88.4, 100, 55.2, 63.1]
    assert CLIENT_ID not in repr(result)
    assert CLIENT_SECRET not in repr(result)


@pytest.mark.parametrize(
    ("collector_type", "options", "payload"),
    [
        (
            NaverDataLabCollector,
            {"start_date": "2026-08-01", "end_date": "2026-08-02"},
            {
                "results": [
                    {
                        "title": "테스트",
                        "keywords": ["테스트"],
                        "data": [{"period": "2026-08-01", "ratio": 101}],
                    }
                ]
            },
        ),
        (
            NaverShoppingInsightCollector,
            {"start_date": "2026-08-01", "end_date": "2026-08-02", "category": "fixture"},
            {
                "results": [
                    {
                        "title": "테스트",
                        "keyword": "테스트",
                        "data": [{"period": "2026-08-01", "ratio": -1}],
                    }
                ]
            },
        ),
    ],
)
def test_relative_ratio_must_stay_in_zero_to_one_hundred(
    tmp_path: Path,
    collector_type: type[NaverDataLabCollector | NaverShoppingInsightCollector],
    options: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    collector = collector_type(
        http=HttpClient(transport=transport), config=_config(tmp_path, keyed=True)
    )

    with pytest.raises(CollectorContractError, match="0..100"):
        collector.collect(
            "테스트",
            **options,
            observed_at=OBSERVED_AT,
            as_of=AS_OF,
        )


def _assert_relative_metrics(result: Any, metric_name: str) -> None:
    assert len(result.contents) == 2
    assert len(result.observations) == 4
    assert len(result.metrics) == 4
    assert all(item.payload["index_type"] == "relative" for item in result.observations)
    assert all(item.payload["unit"] == "relative_index_0_100" for item in result.observations)
    assert all(
        item.payload["denominator"] == "요청 범위 최대값=100" for item in result.observations
    )
    assert all(item.observed_at.tzinfo is not None for item in result.observations)
    assert all(item.metric.metric_name == metric_name for item in result.metrics)
    assert all(item.metric.index_type == "relative" for item in result.metrics)
    assert all(item.metric.unit == "relative_index_0_100" for item in result.metrics)
    assert all(item.metric.denominator == "요청 범위 최대값=100" for item in result.metrics)
    assert all(item.metric.observed_at.tzinfo is not None for item in result.metrics)
