"""S2 공통 HTTP 계층은 fixture transport만 사용하고 비밀을 노출하지 않는다."""

from __future__ import annotations

import httpx
import pytest

from ria.http import HttpClient, HttpDecodeError, HttpStatusError, redact_url


def test_mock_transport_decodes_json_without_socket() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    payload, response = HttpClient(transport=httpx.MockTransport(handler)).get_json(
        "https://example.test/items", params={"q": "ria"}
    )

    assert payload == {"ok": True}
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].url.params["q"] == "ria"


def test_http_status_error_redacts_credentials_and_does_not_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="secret response", request=request)

    client = HttpClient(transport=httpx.MockTransport(handler))

    with pytest.raises(HttpStatusError) as raised:
        client.get_json("https://example.test/items", params={"serviceKey": "do-not-log"})

    assert calls == 1
    assert "do-not-log" not in str(raised.value)
    assert "REDACTED" in str(raised.value)


def test_malformed_json_is_explicit() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not-json", request=request)
    )

    with pytest.raises(HttpDecodeError, match="JSON"):
        HttpClient(transport=transport).get_json("https://example.test/items")


def test_redact_url_is_case_insensitive() -> None:
    safe = redact_url("https://example.test/?API_KEY=secret&q=public")

    assert "secret" not in safe
    assert "q=public" in safe
