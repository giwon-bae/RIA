"""YouTube Data API v3 공식 검색·영상 통계 collector (video-signal, B-9).

``search.list`` 는 영상 ID를 찾는 데 쓰고, 정규화할 원본은 같은 페이지의 ID를
``videos.list`` 로 다시 조회한 공식 item이다. 관측과 절대 count 지표는 그 응답에만
근거하며 favorite/dislike, 파생 engagement, 비율, 플랫폼 간 합산 지표는 만들지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import Config, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SEARCH_ENDPOINT = f"{YOUTUBE_API_BASE}/search"
YOUTUBE_VIDEOS_ENDPOINT = f"{YOUTUBE_API_BASE}/videos"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch"

_COUNT_METRICS = (
    ("viewCount", "view_count"),
    ("likeCount", "like_count"),
    ("commentCount", "comment_count"),
)


class YouTubeCollector(GuardedCollector):
    """공식 search.list 후보를 videos.list 원본으로 검증해 저장 구조로 바꾼다."""

    source_id = "youtube_data"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        return 2 * _positive_int("max_pages", options.get("max_pages", 1))

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        _reject_unknown_options(options, {"max_pages", "max_results", "observed_at", "page_token"})
        observed_at = _observed_at(options.get("observed_at", now()))
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        max_results = _bounded_int(
            "max_results", options.get("max_results", 50), minimum=1, maximum=50
        )
        page_token = _optional_text(options.get("page_token"))

        # Policy Guard가 이 지점 전에 key를 검사했다. 여기서는 재판정 없이 허용된
        # config 값을 공식 요청 파라미터로만 쓴다.
        config = cast(Config, self._config or get_config())
        api_key = cast(str, config.credentials["RIA_YOUTUBE_API_KEY"])

        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        pages: list[dict[str, Any]] = []

        for page_number in range(1, max_pages + 1):
            search_params: dict[str, Any] = {
                "part": "snippet",
                "type": "video",
                "q": query,
                "maxResults": max_results,
                "key": api_key,
            }
            if page_token is not None:
                search_params["pageToken"] = page_token

            search_payload, search_response = self._http.get_json(
                YOUTUBE_SEARCH_ENDPOINT,
                params=search_params,
            )
            search_items, next_page_token = _response_page(search_payload, endpoint="search.list")
            search_snapshot_ref = f"snapshot:youtube:search:{page_number}"
            snapshots.append(
                CollectedSnapshot(
                    ref=search_snapshot_ref,
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=search_payload,
                        collected_at=observed_at,
                        url=search_response.url,
                        media_type="application/json",
                        query=query,
                        meta={
                            "endpoint": "search.list",
                            "page": page_number,
                            "page_token": page_token,
                            "max_results": max_results,
                        },
                    ),
                )
            )

            video_ids = _search_video_ids(search_items)
            if not video_ids:
                pages.append(
                    {
                        "page": page_number,
                        "search_items": len(search_items),
                        "video_items": 0,
                    }
                )
                break

            videos_payload, videos_response = self._http.get_json(
                YOUTUBE_VIDEOS_ENDPOINT,
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(video_ids),
                    "key": api_key,
                },
            )
            video_items, _unused_token = _response_page(
                videos_payload, endpoint="videos.list", allow_page_token=False
            )
            videos_snapshot_ref = f"snapshot:youtube:videos:{page_number}"
            snapshots.append(
                CollectedSnapshot(
                    ref=videos_snapshot_ref,
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=videos_payload,
                        collected_at=observed_at,
                        url=videos_response.url,
                        media_type="application/json",
                        query=query,
                        meta={
                            "endpoint": "videos.list",
                            "page": page_number,
                            "requested_video_ids": video_ids,
                        },
                    ),
                )
            )

            requested_ids = set(video_ids)
            seen_response_ids: set[str] = set()
            normalized_count = 0
            for raw_item in video_items:
                if not isinstance(raw_item, dict):
                    continue
                video_id = _optional_text(raw_item.get("id"))
                if (
                    video_id is None
                    or video_id not in requested_ids
                    or video_id in seen_response_ids
                ):
                    continue
                seen_response_ids.add(video_id)
                snippet = raw_item.get("snippet")
                if not isinstance(snippet, dict):
                    continue

                normalized_count += 1
                watch_url = f"{YOUTUBE_WATCH_URL}?v={video_id}"
                content_ref = f"content:youtube:{video_id}"
                observation_ref = f"observation:youtube:{page_number}:{video_id}"
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="video",
                            url=watch_url,
                            title=_optional_text(snippet.get("title"))
                            or f"YouTube video {video_id}",
                            publisher=_optional_text(snippet.get("channelTitle")) or "YouTube",
                            published_at=_published_at(snippet.get("publishedAt")),
                            language=(
                                _optional_text(snippet.get("defaultAudioLanguage"))
                                or _optional_text(snippet.get("defaultLanguage"))
                            ),
                        ),
                    ),
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="youtube",
                        platform_item_id=video_id,
                        observed_at=observed_at,
                        url=watch_url,
                        payload=_observation_payload(raw_item),
                    )
                )
                observation_snapshots[observation_ref] = videos_snapshot_ref
                metrics.extend(
                    _raw_count_metrics(
                        raw_item,
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        observed_at=observed_at,
                    )
                )

            pages.append(
                {
                    "page": page_number,
                    "search_items": len(search_items),
                    "video_items": normalized_count,
                }
            )
            if next_page_token is None or next_page_token == page_token:
                break
            page_token = next_page_token

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                pages=tuple(pages),
            ),
        )


def _response_page(
    payload: Any,
    *,
    endpoint: str,
    allow_page_token: bool = True,
) -> tuple[list[Any], str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CollectorContractError(f"YouTube {endpoint} 응답 items가 배열이 아니다")
    next_page_token = _optional_text(payload.get("nextPageToken")) if allow_page_token else None
    return payload["items"], next_page_token


def _search_video_ids(items: list[Any]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), dict):
            continue
        identifier = item["id"]
        if identifier.get("kind") not in {None, "youtube#video"}:
            continue
        video_id = _optional_text(identifier.get("videoId"))
        if video_id is not None and video_id not in seen:
            seen.add(video_id)
            result.append(video_id)
    return tuple(result)


def _raw_count_metrics(
    item: dict[str, Any],
    *,
    content_ref: str,
    observation_ref: str,
    observed_at: datetime,
) -> list[CollectedMetric]:
    statistics = item.get("statistics")
    if not isinstance(statistics, dict):
        return []

    result: list[CollectedMetric] = []
    for field, metric_name in _COUNT_METRICS:
        value = _count(statistics.get(field))
        if value is None:
            continue
        result.append(
            CollectedMetric(
                content_ref=content_ref,
                observation_ref=observation_ref,
                metric=MetricInput(
                    metric_name=metric_name,
                    value=value,
                    index_type="absolute",
                    source_id="youtube_data",
                    observed_at=observed_at,
                    unit="count",
                    denominator=None,
                    geography=None,
                    period=None,
                    population=None,
                    method="YouTube Data API v3 videos.list statistics raw count",
                    platform="youtube",
                ),
            )
        )
    return result


def _observation_payload(item: dict[str, Any]) -> dict[str, Any]:
    """허용된 원시 필드만 Observation에 남긴다; 전체 응답은 Snapshot이 보존한다."""
    statistics = item.get("statistics")
    allowed_statistics = (
        {field: statistics[field] for field, _metric_name in _COUNT_METRICS if field in statistics}
        if isinstance(statistics, dict)
        else {}
    )
    return {
        "id": item.get("id"),
        "kind": item.get("kind"),
        "etag": item.get("etag"),
        "snippet": dict(item["snippet"]) if isinstance(item.get("snippet"), dict) else {},
        "statistics": allowed_statistics,
    }


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _published_at(value: Any) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reject_unknown_options(options: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 YouTube 옵션이다: {sorted(unknown)}")
