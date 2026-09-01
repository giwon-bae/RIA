"""B-3 OpenDART 공시·재무 fixture 수집."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ria.collectors.base import CollectorContractError
from ria.collectors.opendart import ORIGINAL_DOCUMENT_NOTE, OpenDartCollector
from ria.collectors.persistence import persist_collect_result
from ria.config import KST, Config
from ria.core.store import Store
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 14, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(tmp_path: Path, *, keyed: bool) -> Config:
    credentials = {"RIA_OPENDART_API_KEY": "fixture-secret"} if keyed else {}
    return Config(db_path=tmp_path / "opendart.db", credentials=credentials)


def test_missing_key_is_guarded_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=False),
    ).collect("테스트 주식회사", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "missing_credential"
    assert calls == 0


def test_disclosure_fixture_maps_official_viewer_and_snapshot(tmp_path: Path) -> None:
    payload = _fixture("opendart_disclosures.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/api/list.json"
        assert request.url.params["crtfc_key"] == "fixture-secret"
        assert request.url.params["corp_code"] == "fixture-corp"
        return httpx.Response(200, json=payload, request=request)

    result = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "테스트 주식회사",
        corp_code="fixture-corp",
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert len(calls) == 1
    assert len(result.contents) == 2
    assert len(result.observations) == 2
    assert not result.metrics
    assert result.contents[0].item.url == (
        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260331000001"
    )
    assert result.metadata["original_document_note"] == ORIGINAL_DOCUMENT_NOTE
    assert "fixture-secret" not in repr(result)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (2, 2, 0, 1)


def test_financial_fixture_emits_only_raw_absolute_account_metrics(tmp_path: Path) -> None:
    payload = _fixture("opendart_financials.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fnlttSinglAcntAll.json"
        assert request.url.params["fs_div"] == "CFS"
        return httpx.Response(200, json=payload, request=request)

    result = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "테스트 주식회사",
        mode="financials",
        corp_code="fixture-corp",
        bsns_year="2025",
        reprt_code="11011",
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert len(result.contents) == 1
    assert len(result.observations) == 1
    assert [item.metric.value for item in result.metrics] == [1234567, 987654]
    assert all(item.metric.index_type == "absolute" for item in result.metrics)
    assert all(item.metric.unit == "KRW" for item in result.metrics)
    assert all(item.metric.metric_name.startswith("opendart_account:") for item in result.metrics)


def test_document_zip_is_reversibly_stored_as_base64(tmp_path: Path) -> None:
    archive = b"PK\x03\x04fixture zip bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/document.xml"
        assert request.url.params["crtfc_key"] == "fixture-secret"
        return httpx.Response(200, content=archive, request=request)

    result = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect(
        "테스트 공시 원문",
        mode="document",
        rcept_no="20260331000001",
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        row = store.connection.execute(
            "SELECT body, meta_json FROM raw_snapshots WHERE snapshot_id = ?",
            (persisted.snapshot_ids["snapshot:opendart:document:20260331000001"],),
        ).fetchone()
        assert base64.b64decode(row["body"]) == archive
        assert json.loads(row["meta_json"])["encoding"] == "base64"
        assert "fixture-secret" not in repr(result)


def test_status_013_is_allowed_empty_batch(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"status": "013", "message": "조회된 데이터가 없습니다."}, request=request
        )
    )
    result = OpenDartCollector(
        http=HttpClient(transport=transport), config=_config(tmp_path, keyed=True)
    ).collect("없는 회사", observed_at=OBSERVED_AT, as_of=AS_OF)

    assert result.allowed is True
    assert not result.contents
    assert not result.observations
    assert not result.metrics


def test_non_success_status_is_response_error_not_new_guard_reason(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"status": "010", "message": "등록되지 않은 키"}, request=request
        )
    )
    collector = OpenDartCollector(
        http=HttpClient(transport=transport), config=_config(tmp_path, keyed=True)
    )

    with pytest.raises(CollectorContractError, match="응답 오류 010"):
        collector.collect("테스트", observed_at=OBSERVED_AT, as_of=AS_OF)


def test_disclosure_pagination_estimate_cannot_bypass_guard(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    ).collect("테스트", max_pages=51, requested_calls=1, as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert calls == 0


def test_financial_identifiers_are_required_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    collector = OpenDartCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, keyed=True),
    )
    with pytest.raises(CollectorContractError, match="corp_code"):
        collector.collect("테스트", mode="financials", as_of=AS_OF)
    assert calls == 0
