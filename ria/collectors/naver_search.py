"""NAVER API Hub 웹 검색 collector (search-demand, B-4).

인증은 Policy Guard가 먼저 판정하며, 실제 요청은 NCP API Gateway 헤더로만
``https://naverapihub.apigw.ntruss.com``에 보낸다. 검색 응답의 ``total``은 응답
메타데이터일 뿐 Metric이 아니다.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import Config, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

NAVER_API_HUB_BASE = "https://naverapihub.apigw.ntruss.com"
NAVER_SEARCH_ENDPOINT = f"{NAVER_API_HUB_BASE}/search/v1/webkr"
NAVER_KEY_ID_HEADER = "X-NCP-APIGW-API-KEY-ID"
NAVER_KEY_HEADER = "X-NCP-APIGW-API-KEY"

_MARKUP = re.compile(r"<[^>]+>")
_SORT_VALUES = frozenset({"sim", "date"})


class NaverSearchCollector(GuardedCollector):
    """NAVER API Hub의 웹 검색 결과를 Content·Observation·Snapshot으로 만든다."""

    source_id = "naver_search"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        return _positive_int("max_pages", options.get("max_pages", 1))

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        _reject_unknown_options(
            options,
            {"display", "max_pages", "observed_at", "sort", "start"},
        )
        observed_at = _observed_at(options.get("observed_at", now()))
        display = _bounded_int("display", options.get("display", 10), minimum=1, maximum=100)
        first_start = _bounded_int("start", options.get("start", 1), minimum=1, maximum=1000)
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        sort = str(options.get("sort", "sim"))
        if sort not in _SORT_VALUES:
            raise CollectorContractError(f"NAVER Search sort가 잘못됐다: {sort!r}")

        config = cast(Config, self._config or get_config())
        headers = _api_headers(config, self.source_id)
        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        page_metadata: list[dict[str, int]] = []
        total: int | None = None

        for offset in range(max_pages):
            start = first_start + offset * display
            if start > 1000:
                break
            payload, response = self._http.get_json(
                NAVER_SEARCH_ENDPOINT,
                params={"query": query, "display": display, "start": start, "sort": sort},
                headers=headers,
            )
            response_total, items = _parse_response(payload)
            if total is None:
                total = response_total
            page_metadata.append({"start": start, "display": len(items)})

            snapshot_ref = f"snapshot:naver_search:{start}"
            snapshots.append(
                CollectedSnapshot(
                    ref=snapshot_ref,
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=payload,
                        collected_at=observed_at,
                        url=response.url,
                        media_type="application/json",
                        query=query,
                        meta={"start": start, "display": display, "sort": sort},
                    ),
                )
            )

            for item_index, raw in enumerate(items):
                if not isinstance(raw, dict):
                    continue
                link = _web_url(raw.get("link"))
                if link is None:
                    continue
                digest = hashlib.sha256(link.encode("utf-8")).hexdigest()[:20]
                rank = start + item_index
                content_ref = f"content:naver_search:{digest}"
                observation_ref = f"observation:naver_search:{rank}:{digest}"
                title = _clean_text(raw.get("title")) or link
                description = _clean_text(raw.get("description"))
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="article",
                            url=link,
                            title=title,
                            publisher=urlsplit(link).hostname,
                            language="ko",
                            metadata={
                                "description": description,
                                "naver_search_rank": rank,
                            },
                        ),
                    ),
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="naver_search",
                        platform_item_id=link,
                        observed_at=observed_at,
                        url=link,
                        payload={
                            **raw,
                            "normalized_title": title,
                            "normalized_description": description,
                            "rank": rank,
                        },
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref

            if not items or len(items) < display or start + display > min(response_total, 1000):
                break

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                total=total if total is not None else 0,
                pages=tuple(page_metadata),
            ),
        )


def _api_headers(config: Config, source_id: str) -> dict[str, str]:
    del source_id
    # Policy Guard가 이 함수 전에 두 키를 모두 검사한다. collector 내부에서
    # 자격증명을 재판정하지 않고 이미 허용된 설정값을 전송 헤더로만 사용한다.
    return {
        NAVER_KEY_ID_HEADER: cast(str, config.credentials["RIA_NAVER_CLIENT_ID"]),
        NAVER_KEY_HEADER: cast(str, config.credentials["RIA_NAVER_CLIENT_SECRET"]),
    }


def _parse_response(payload: Any) -> tuple[int, list[Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CollectorContractError("NAVER Search 응답 items가 배열이 아니다")
    total = payload.get("total", len(payload["items"]))
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise CollectorContractError("NAVER Search 응답 total이 음이 아닌 정수가 아니다")
    return total, payload["items"]


def _observed_at(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")
    return value


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorContractError(f"{name}은 양의 정수여야 한다")
    return value


def _bounded_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum or value > maximum:
        raise CollectorContractError(f"{name}은 {minimum}..{maximum} 정수여야 한다")
    return value


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(_MARKUP.sub("", str(value))).strip()
    return text or None


def _web_url(value: Any) -> str | None:
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return text


def _reject_unknown_options(options: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 NAVER Search 옵션이다: {sorted(unknown)}")
