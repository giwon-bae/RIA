"""RIA의 단일 HTTP 전송 계층.

모든 source collector는 이 모듈을 통해서만 HTTP를 사용한다. ``httpx.BaseTransport``를
주입할 수 있어 기본 pytest는 실제 소켓 없이 ``MockTransport``와 fixture로 검증한다.
오류 메시지와 저장 URL에서는 자격증명성 query parameter를 제거한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_USER_AGENT = "python:ria-core:2.1.0"

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "app_secret",
        "client_secret",
        "crtfc_key",
        "key",
        "servicekey",
    }
)


class HttpError(RuntimeError):
    """안전하게 정리된 HTTP 오류."""


class HttpTransportError(HttpError):
    """DNS·연결·타임아웃 등 응답을 받지 못한 오류."""


class HttpStatusError(HttpError):
    """2xx가 아닌 HTTP 응답."""


class HttpDecodeError(HttpError):
    """JSON 등 예상한 응답 형식을 해석하지 못한 오류."""


@dataclass(frozen=True)
class HttpResponse:
    """collector에 노출하는 비밀 제거 HTTP 응답."""

    status_code: int
    url: str
    headers: Mapping[str, str]
    content: bytes

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpDecodeError(f"JSON 응답을 해석하지 못했다: {self.url}") from error


class HttpClient:
    """httpx 기반 동기 클라이언트. transport를 주입하면 소켓을 열지 않는다."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds는 양수여야 한다")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: bytes | str | None = None,
    ) -> HttpResponse:
        request_headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        if headers:
            request_headers.update(headers)

        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = client.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=json_body,
                    content=content,
                )
        except httpx.RequestError as error:
            safe_url = redact_url(str(error.request.url)) if error.request is not None else url
            raise HttpTransportError(f"HTTP 전송 실패: {method.upper()} {safe_url}") from error

        safe_url = redact_url(str(response.url))
        if not 200 <= response.status_code < 300:
            raise HttpStatusError(f"HTTP {response.status_code}: {method.upper()} {safe_url}")
        return HttpResponse(
            status_code=response.status_code,
            url=safe_url,
            headers=dict(response.headers),
            content=response.content,
        )

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[Any, HttpResponse]:
        response = self.request("GET", url, params=params, headers=headers)
        return response.json(), response

    def post_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, HttpResponse]:
        response = self.request(
            "POST",
            url,
            params=params,
            headers=headers,
            json_body=json_body,
        )
        return response.json(), response


def redact_url(url: str) -> str:
    """URL query의 자격증명 값을 고정 마스킹한다."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = [
        (key, "REDACTED" if key.casefold() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))
