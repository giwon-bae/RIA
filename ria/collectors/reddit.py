"""승인 후 열리는 Reddit OAuth search collector (community-signal, B-8).

기본 registry의 ``blocked`` 상태에서는 Guard가 이 모듈의 HTTP 경로에 진입시키지
않는다. 승인 후 ``core``로 전환된 경우에도 공식 응답 헤더의 rate 상태만 따르며,
추측한 수치·retry·sleep을 넣지 않는다.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

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

REDDIT_TOKEN_ENDPOINT = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_ENDPOINT = "https://oauth.reddit.com/search"
REDDIT_ORIGIN = "https://www.reddit.com"
REDDIT_SIGNAL_WARNING = (
    "Reddit 점수와 댓글 수는 해당 플랫폼 내부 반응이며 시장 수요·시장 규모가 아니다."
)
_SORTS = frozenset({"comments", "hot", "new", "relevance", "top"})
_TIME_FILTERS = frozenset({"all", "day", "hour", "month", "week", "year"})


class RedditCollector(GuardedCollector):
    source_id = "reddit"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        return 1 + _positive_int("max_pages", options.get("max_pages", 1))

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        allowed = {
            "after",
            "limit",
            "max_pages",
            "observed_at",
            "sort",
            "subreddit",
            "time_filter",
        }
        _reject_unknown_options(options, allowed)
        observed_at = _aware_datetime("observed_at", options.get("observed_at", now()))
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        limit = _bounded_int("limit", options.get("limit", 25), minimum=1, maximum=100)
        sort = str(options.get("sort", "relevance"))
        time_filter = str(options.get("time_filter", "all"))
        if sort not in _SORTS:
            raise CollectorContractError(f"Reddit sort가 잘못됐다: {sort!r}")
        if time_filter not in _TIME_FILTERS:
            raise CollectorContractError(f"Reddit time_filter가 잘못됐다: {time_filter!r}")

        config = cast(Config, self._config or get_config())
        client_id = cast(str, config.credentials["RIA_REDDIT_CLIENT_ID"])
        client_secret = cast(str, config.credentials["RIA_REDDIT_CLIENT_SECRET"])
        user_agent = cast(str, config.credentials["RIA_REDDIT_USER_AGENT"])
        token = self._access_token(client_id, client_secret, user_agent)

        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        rate_pages: list[dict[str, float | None]] = []
        backoff_seconds: float | None = None
        after = _optional_text(options.get("after"))

        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "q": query,
                "sort": sort,
                "t": time_filter,
                "type": "link",
                "limit": limit,
                "raw_json": 1,
            }
            if (subreddit := _optional_text(options.get("subreddit"))) is not None:
                params.update(restrict_sr=1, subreddit=subreddit)
            if after is not None:
                params["after"] = after
            payload, response = self._http.get_json(
                REDDIT_SEARCH_ENDPOINT,
                params=params,
                headers={"Authorization": f"bearer {token}", "User-Agent": user_agent},
            )
            children, next_after = _listing(payload)
            snapshot_ref = f"snapshot:reddit:search:{page}"
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
                        meta={"page": page, "sort": sort, "time_filter": time_filter},
                    ),
                )
            )

            for index, child in enumerate(children):
                raw = child.get("data") if isinstance(child, dict) else None
                if not isinstance(raw, dict) or not (item_id := _optional_text(raw.get("id"))):
                    continue
                permalink = _optional_text(raw.get("permalink"))
                discussion_url = (
                    f"{REDDIT_ORIGIN}{permalink}"
                    if permalink and permalink.startswith("/")
                    else None
                )
                if discussion_url is None:
                    continue
                external_url = _http_url(raw.get("url_overridden_by_dest"))
                content_url = external_url or discussion_url
                content_ref = f"content:reddit:{item_id}"
                observation_ref = f"observation:reddit:{page}:{index}:{item_id}"
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="article" if external_url else "post",
                            url=content_url,
                            title=_optional_text(raw.get("title")) or f"Reddit post {item_id}",
                            publisher=(
                                f"r/{raw['subreddit']}"
                                if _optional_text(raw.get("subreddit"))
                                else "Reddit"
                            ),
                            published_at=_unix_time(raw.get("created_utc")),
                            language=None,
                            metadata={"reddit_post_id": item_id},
                        ),
                    ),
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="reddit",
                        platform_item_id=item_id,
                        observed_at=observed_at,
                        url=discussion_url,
                        payload={
                            **raw,
                            "representativeness_warning": REDDIT_SIGNAL_WARNING,
                        },
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref
                metrics.extend(
                    _reddit_metrics(
                        raw,
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        observed_at=observed_at,
                    )
                )

            rate = _rate_headers(response.headers)
            rate_pages.append(rate)
            remaining = rate["remaining"]
            if remaining is not None and remaining <= 0:
                backoff_seconds = rate["reset"]
                break
            if next_after is None or next_after == after:
                break
            after = next_after

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                rate_limit_pages=tuple(rate_pages),
                backoff_seconds=backoff_seconds,
                representativeness_warning=REDDIT_SIGNAL_WARNING,
            ),
        )

    def _access_token(self, client_id: str, client_secret: str, user_agent: str) -> str:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        response = self._http.request(
            "POST",
            REDDIT_TOKEN_ENDPOINT,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": user_agent,
            },
            content=urlencode({"grant_type": "client_credentials"}),
        )
        payload = response.json()
        if not isinstance(payload, dict) or not (
            token := _optional_text(payload.get("access_token"))
        ):
            raise CollectorContractError("Reddit OAuth 응답에 access_token이 없다")
        return token


def _listing(payload: Any) -> tuple[list[Any], str | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("children"), list):
        raise CollectorContractError("Reddit listing 응답 형식이 잘못됐다")
    return data["children"], _optional_text(data.get("after"))


def _rate_headers(headers: Any) -> dict[str, float | None]:
    normalized = {str(key).casefold(): value for key, value in dict(headers).items()}
    return {
        name: _float_or_none(normalized.get(f"x-ratelimit-{name}"))
        for name in ("used", "remaining", "reset")
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _reddit_metrics(
    raw: dict[str, Any],
    *,
    content_ref: str,
    observation_ref: str,
    observed_at: datetime,
) -> list[CollectedMetric]:
    result: list[CollectedMetric] = []
    for field, metric_name in (("score", "reddit_score"), ("num_comments", "reddit_comment_count")):
        value = raw.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            continue
        result.append(
            CollectedMetric(
                content_ref=content_ref,
                observation_ref=observation_ref,
                metric=MetricInput(
                    metric_name=metric_name,
                    value=value,
                    index_type="absolute",
                    source_id="reddit",
                    observed_at=observed_at,
                    unit="count",
                    denominator=None,
                    geography=None,
                    period=None,
                    population="Reddit 플랫폼 반응",
                    method="Reddit OAuth API raw platform count; 시장 수요·규모가 아님",
                    platform="reddit",
                ),
            )
        )
    return result


def _http_url(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parsed = urlsplit(text)
    return text if parsed.scheme in {"http", "https"} and parsed.hostname else None


def _unix_time(value: Any) -> datetime | None:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _aware_datetime(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectorContractError(f"{name}은 timezone-aware datetime이어야 한다")
    return value


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorContractError(f"{name}은 양의 정수여야 한다")
    return value


def _bounded_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CollectorContractError(f"{name}은 {minimum}..{maximum} 정수여야 한다")
    return value


def _reject_unknown_options(options: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 Reddit 옵션이다: {sorted(unknown)}")
