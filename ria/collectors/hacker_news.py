"""Hacker News 공식 Firebase 수집과 Algolia 보조 검색 (tech-launch, B-5).

정규화 가능한 근거는 공식 Firebase item 응답뿐이다. 제3자 Algolia 응답은 후보
``item id``를 찾는 데만 쓰며, 제목·점수·댓글 수 등 Algolia 필드는 저장 구조로
옮기지 않는다. 후보는 :class:`HackerNewsCollector`로 다시 조회해야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

HN_FIREBASE_API = "https://hacker-news.firebaseio.com/v0"
HN_ALGOLIA_API = "https://hn.algolia.com/api/v1/search"
HN_DISCUSSION_URL = "https://news.ycombinator.com/item"
HN_FEEDS = frozenset(
    {
        "askstories",
        "beststories",
        "jobstories",
        "newstories",
        "showstories",
        "topstories",
    }
)
HN_ITEM_TYPES = frozenset({"story", "comment"})
HN_REPRESENTATIVENESS_WARNING = (
    "Hacker News 점수와 댓글 수는 해당 플랫폼 내부 반응이며 시장 수요·시장 규모가 아니다."
)
HN_ALGOLIA_WARNING = (
    "HN Algolia는 제3자 후보 탐색용이다. 후보 ID를 공식 Hacker News Firebase item으로 "
    "재조회한 결과만 정규화할 수 있다."
)


class HackerNewsCollector(GuardedCollector):
    """공식 Firebase item을 story/comment Content·Observation·Metric으로 바꾼다."""

    source_id = "hacker_news"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        item_ids = options.get("item_ids")
        if item_ids is not None:
            return len(_item_ids(item_ids))
        # feed 목록 1회 + 목록에서 실제 조회할 item 수. 실제 목록이 더 짧아도
        # 과소 신고하지 않도록 가능한 호출 수를 Guard 하한으로 전달한다.
        return 1 + _positive_int("scan_limit", options.get("scan_limit", 20))

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
            {"feed", "item_ids", "observed_at", "scan_limit"},
            collector="Hacker News",
        )
        observed_at = _observed_at(options.get("observed_at", now()))

        snapshots: list[CollectedSnapshot] = []
        item_ids_option = options.get("item_ids")
        if item_ids_option is not None:
            item_ids = _item_ids(item_ids_option)
            feed: str | None = None
        else:
            feed = str(options.get("feed", "topstories"))
            if feed not in HN_FEEDS:
                raise CollectorContractError(
                    f"지원하지 않는 Hacker News feed다: {feed!r}; {sorted(HN_FEEDS)}"
                )
            scan_limit = _positive_int("scan_limit", options.get("scan_limit", 20))
            feed_payload, feed_response = self._http.get_json(f"{HN_FIREBASE_API}/{feed}.json")
            snapshots.append(
                CollectedSnapshot(
                    ref=f"snapshot:hacker_news:feed:{feed}",
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=feed_payload,
                        collected_at=observed_at,
                        url=feed_response.url,
                        media_type="application/json",
                        query=query,
                        meta={"kind": "feed", "feed": feed},
                    ),
                )
            )
            item_ids = _feed_item_ids(feed_payload, scan_limit=scan_limit)

        contents: list[CollectedContent] = []
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        observation_snapshots: dict[str, str] = {}
        discarded_item_ids: list[int] = []

        for item_id in item_ids:
            payload, response = self._http.get_json(f"{HN_FIREBASE_API}/item/{item_id}.json")
            snapshot_ref = f"snapshot:hacker_news:item:{item_id}"
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
                        meta={"kind": "item", "requested_item_id": item_id},
                    ),
                )
            )

            item = _verified_item(payload, requested_id=item_id)
            if item is None:
                discarded_item_ids.append(item_id)
                continue

            discussion_url = f"{HN_DISCUSSION_URL}?id={item_id}"
            content_url = _external_url(item.get("url")) or discussion_url
            item_type = str(item["type"])
            content_ref = f"content:hacker_news:{item_id}"
            observation_ref = f"observation:hacker_news:{item_id}"
            contents.append(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="article" if item_type == "story" else "post",
                        url=content_url,
                        title=_item_title(item, item_id=item_id),
                        publisher=_publisher(content_url),
                        published_at=_published_at(item.get("time")),
                        language=None,
                        metadata={
                            "hacker_news_item_id": item_id,
                            "hacker_news_item_type": item_type,
                            "hacker_news_author": _optional_text(item.get("by")),
                        },
                    ),
                )
            )
            observations.append(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id=self.source_id,
                    platform="hacker_news",
                    platform_item_id=str(item_id),
                    observed_at=observed_at,
                    url=discussion_url,
                    payload={
                        **item,
                        "representativeness_warning": HN_REPRESENTATIVENESS_WARNING,
                    },
                )
            )
            observation_snapshots[observation_ref] = snapshot_ref
            metrics.extend(
                _item_metrics(
                    item,
                    content_ref=content_ref,
                    observation_ref=observation_ref,
                    observed_at=observed_at,
                )
            )

        return CollectedBatch(
            contents=tuple(contents),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                feed=feed,
                requested_item_ids=tuple(item_ids),
                discarded_item_ids=tuple(discarded_item_ids),
                representativeness_warning=HN_REPRESENTATIVENESS_WARNING,
            ),
        )


class HNAlgoliaCollector(GuardedCollector):
    """제3자 Algolia 검색에서 검증 전 candidate item ID만 돌려준다."""

    source_id = "hn_algolia"

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
            {"hits_per_page", "max_pages", "page", "tags"},
            collector="HN Algolia",
        )
        first_page = _non_negative_int("page", options.get("page", 0))
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        hits_per_page = _positive_int("hits_per_page", options.get("hits_per_page", 20))
        tags = str(options.get("tags", "story")).strip()
        if not tags:
            raise CollectorContractError("HN Algolia tags는 비어 있지 않아야 한다")

        candidate_ids: list[int] = []
        seen: set[int] = set()
        pages: list[dict[str, int]] = []
        for page_offset in range(max_pages):
            page = first_page + page_offset
            payload, _response = self._http.get_json(
                HN_ALGOLIA_API,
                params={
                    "query": query,
                    "tags": tags,
                    "page": page,
                    "hitsPerPage": hits_per_page,
                },
            )
            hits, total_pages = _algolia_page(payload)
            pages.append({"page": page, "hits": len(hits), "total_pages": total_pages})
            for hit in hits:
                candidate_id = _candidate_id(hit.get("objectID"))
                if candidate_id is not None and candidate_id not in seen:
                    seen.add(candidate_id)
                    candidate_ids.append(candidate_id)
            if page + 1 >= total_pages:
                break

        # Algolia의 제목·본문·점수는 의도적으로 반환하지 않는다. 이 결과를 적재해도
        # Content·Observation·Metric·Snapshot은 모두 0건이다.
        return CollectedBatch(
            metadata={
                "candidate_ids": tuple(candidate_ids),
                "pages": tuple(pages),
                "verification_source_id": "hacker_news",
                "verification_required": True,
                "warning": HN_ALGOLIA_WARNING,
            }
        )


def _item_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise CollectorContractError("Hacker News item_ids는 list 또는 tuple이어야 한다")
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        item_id = _positive_int("item_id", raw)
        if item_id not in seen:
            seen.add(item_id)
            result.append(item_id)
    if not result:
        raise CollectorContractError("Hacker News item_ids는 하나 이상이어야 한다")
    return tuple(result)


def _feed_item_ids(payload: Any, *, scan_limit: int) -> tuple[int, ...]:
    if not isinstance(payload, list):
        raise CollectorContractError("Hacker News feed 응답은 item ID 배열이어야 한다")
    result: list[int] = []
    seen: set[int] = set()
    for raw in payload:
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0 or raw in seen:
            continue
        seen.add(raw)
        result.append(raw)
        if len(result) >= scan_limit:
            break
    return tuple(result)


def _verified_item(payload: Any, *, requested_id: int) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    item_id = payload.get("id")
    if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id != requested_id:
        return None
    if payload.get("dead") is True or payload.get("deleted") is True:
        return None
    if payload.get("type") not in HN_ITEM_TYPES:
        return None
    return dict(payload)


def _item_metrics(
    item: dict[str, Any],
    *,
    content_ref: str,
    observation_ref: str,
    observed_at: datetime,
) -> list[CollectedMetric]:
    result: list[CollectedMetric] = []
    for field, metric_name, unit in (
        ("score", "hn_score", "points"),
        ("descendants", "hn_comment_count", "comments"),
    ):
        value = item.get(field)
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            continue
        result.append(
            CollectedMetric(
                content_ref=content_ref,
                observation_ref=observation_ref,
                metric=MetricInput(
                    metric_name=metric_name,
                    value=value,
                    index_type="absolute",
                    source_id="hacker_news",
                    observed_at=observed_at,
                    unit=unit,
                    denominator=None,
                    geography=None,
                    period=None,
                    population="Hacker News 사용자 반응",
                    method=(
                        "Hacker News Firebase API 관측; 플랫폼 내부 반응이며 "
                        "시장 수요·시장 규모가 아님"
                    ),
                    platform="hacker_news",
                ),
            )
        )
    return result


def _algolia_page(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        raise CollectorContractError("HN Algolia 응답은 hits 배열을 포함해야 한다")
    hits = [hit for hit in payload["hits"] if isinstance(hit, dict)]
    total_pages = payload.get("nbPages", 1)
    if not isinstance(total_pages, int) or isinstance(total_pages, bool) or total_pages < 0:
        raise CollectorContractError("HN Algolia nbPages는 0 이상의 정수여야 한다")
    return hits, total_pages


def _candidate_id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _published_at(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _item_title(item: dict[str, Any], *, item_id: int) -> str:
    title = _optional_text(item.get("title"))
    if title is not None:
        return title
    item_type = str(item.get("type") or "item")
    return f"Hacker News {item_type} {item_id}"


def _external_url(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return text


def _publisher(url: str) -> str:
    hostname = urlsplit(url).hostname
    if hostname in {"news.ycombinator.com", None}:
        return "Hacker News"
    return hostname.lower()


def _observed_at(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")
    return value


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorContractError(f"{name}은 양의 정수여야 한다")
    return value


def _non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CollectorContractError(f"{name}은 0 이상의 정수여야 한다")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reject_unknown_options(options: dict[str, Any], allowed: set[str], *, collector: str) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 {collector} 옵션이다: {sorted(unknown)}")
