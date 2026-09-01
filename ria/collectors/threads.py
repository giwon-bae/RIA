"""승인 후 열리는 Threads keyword search collector와 24시간 사용자 쿼터 (B-8)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast
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
from ria.config import Config, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient, redact_url
from ria.policy.guard import PolicyAllowed
from ria.policy.registry import get_registry

THREADS_KEYWORD_ENDPOINT = "https://graph.threads.net/v1.0/keyword_search"
THREADS_REFRESH_ENDPOINT = "https://graph.threads.net/refresh_access_token"
THREADS_FIELDS = "id,text,media_type,permalink,username,timestamp,like_count,replies_count,views"
THREADS_REPRESENTATIVENESS_WARNING = (
    "App Review 미승인 상태에서는 Threads keyword search가 본인 게시물로 한정되어 "
    "전체 이용자 표본을 대표하지 않는다."
)
THREADS_SIGNAL_WARNING = (
    "Threads 반응 수치는 해당 플랫폼 내부 반응이며 시장 수요·시장 규모가 아니다."
)
_SEARCH_TYPES = frozenset({"RECENT", "TOP"})
_SEARCH_MODES = frozenset({"KEYWORD", "TAG"})
_COUNT_METRICS = (
    ("like_count", "threads_like_count"),
    ("replies_count", "threads_reply_count"),
    ("views", "threads_view_count"),
)


@dataclass
class ThreadsQuotaCounter:
    """access-token hash별 성공(1건 이상) 검색 시각을 24시간 window로 센다."""

    _events: dict[str, list[datetime]] = field(default_factory=dict)

    @staticmethod
    def user_key(stable_user_subject: str) -> str:
        """토큰 갱신과 무관한 로컬 사용자 식별자를 비가역 키로 바꾼다."""
        return hashlib.sha256(stable_user_subject.encode()).hexdigest()

    def used(self, user_key: str, at: datetime, *, window_hours: int) -> int:
        cutoff = at - timedelta(hours=window_hours)
        active = [event for event in self._events.get(user_key, ()) if event > cutoff]
        self._events[user_key] = active
        return len(active)

    def can_consume(
        self,
        user_key: str,
        at: datetime,
        *,
        limit: int,
        window_hours: int,
    ) -> bool:
        return self.used(user_key, at, window_hours=window_hours) < limit

    def record_result(
        self,
        user_key: str,
        at: datetime,
        *,
        result_count: int,
        calls: int = 1,
    ) -> None:
        if result_count <= 0:
            return
        if calls <= 0:
            raise CollectorContractError("Threads quota calls는 양수여야 한다")
        self._events.setdefault(user_key, []).extend([at] * calls)


_DEFAULT_THREADS_QUOTA_COUNTER = ThreadsQuotaCounter()
_DEFAULT_THREADS_QUOTA_SUBJECT = "ria-single-configured-threads-user"


class ThreadsCollector(GuardedCollector):
    source_id = "threads"

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        quota_counter: ThreadsQuotaCounter | None = None,
        quota_user_subject: str = _DEFAULT_THREADS_QUOTA_SUBJECT,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not quota_user_subject.strip():
            raise CollectorContractError("Threads quota_user_subject는 비어 있으면 안 된다")
        self._http = http or HttpClient()
        self._quota_counter = quota_counter or _DEFAULT_THREADS_QUOTA_COUNTER
        self._quota_user_key = self._quota_counter.user_key(quota_user_subject)

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        pages = _positive_int("max_pages", options.get("max_pages", 1))
        return pages + int(options.get("refresh_access_token", False) is True)

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
            "author_username",
            "limit",
            "max_pages",
            "media_type",
            "observed_at",
            "refresh_access_token",
            "search_mode",
            "search_type",
            "since",
            "token_expires_at",
            "until",
        }
        _reject_unknown_options(options, allowed)
        observed_at = _aware_datetime("observed_at", options.get("observed_at", now()))
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        limit = _bounded_int("limit", options.get("limit", 25), minimum=1, maximum=100)
        search_type = str(options.get("search_type", "TOP")).upper()
        search_mode = str(options.get("search_mode", "KEYWORD")).upper()
        if search_type not in _SEARCH_TYPES:
            raise CollectorContractError(f"Threads search_type이 잘못됐다: {search_type!r}")
        if search_mode not in _SEARCH_MODES:
            raise CollectorContractError(f"Threads search_mode가 잘못됐다: {search_mode!r}")
        refresh = options.get("refresh_access_token", False)
        if not isinstance(refresh, bool):
            raise CollectorContractError("refresh_access_token은 bool이어야 한다")

        config = cast(Config, self._config or get_config())
        token = cast(str, config.credentials["RIA_THREADS_ACCESS_TOKEN"])
        refreshed = False
        refresh_expires_in: int | None = None
        if refresh:
            refresh_payload, _response = self._http.get_json(
                THREADS_REFRESH_ENDPOINT,
                params={"grant_type": "th_refresh_token", "access_token": token},
            )
            if not isinstance(refresh_payload, dict) or not (
                next_token := _optional_text(refresh_payload.get("access_token"))
            ):
                raise CollectorContractError("Threads refresh 응답에 access_token이 없다")
            token = next_token
            refreshed = True
            raw_expires = refresh_payload.get("expires_in")
            refresh_expires_in = raw_expires if isinstance(raw_expires, int) else None

        quota = config.quota_for(self.source_id)
        if quota is None or quota.limit is None or quota.window_hours is None:
            raise CollectorContractError("Threads per-user quota 설정이 없다")
        # 이 설치는 기본적으로 한 명의 configured user를 대상으로 한다. 토큰이나 앱이
        # 바뀌어도 같은 사용자 버킷을 유지하며, 다중 사용자 호출자는 생성자에서 안정적인
        # quota_user_subject를 사용자별로 주입한다.
        user_key = self._quota_user_key
        token_warning = _token_warning(options.get("token_expires_at"), observed_at, config)
        registry = self._registry or get_registry()
        representativeness_warning = (
            THREADS_REPRESENTATIVENESS_WARNING
            if registry.get(self.source_id).access_status != "core"
            else None
        )

        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        after = _optional_text(options.get("after"))
        charged_calls = 0

        for page in range(1, max_pages + 1):
            if not self._quota_counter.can_consume(
                user_key,
                observed_at,
                limit=quota.limit,
                window_hours=quota.window_hours,
            ):
                raise CollectorContractError("Threads 사용자 24시간 쿼터가 소진돼 호출하지 않았다")
            params: dict[str, Any] = {
                "q": query,
                "search_type": search_type,
                "search_mode": search_mode,
                "limit": limit,
                "fields": THREADS_FIELDS,
                "access_token": token,
            }
            for key in ("media_type", "since", "until", "author_username"):
                if (value := options.get(key)) is not None:
                    params[key] = value
            if after is not None:
                params["after"] = after
            payload, response = self._http.get_json(THREADS_KEYWORD_ENDPOINT, params=params)
            payload = _redact_payload_secrets(payload)
            rows, next_after = _threads_page(payload)
            self._quota_counter.record_result(
                user_key,
                observed_at,
                result_count=len(rows),
            )
            if rows:
                charged_calls += 1

            snapshot_ref = f"snapshot:threads:keyword:{page}"
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
                        meta={"page": page, "search_type": search_type, "search_mode": search_mode},
                    ),
                )
            )
            for index, raw in enumerate(rows):
                if not isinstance(raw, dict) or not (item_id := _optional_text(raw.get("id"))):
                    continue
                permalink = _http_url(raw.get("permalink"))
                if permalink is None:
                    continue
                content_ref = f"content:threads:{item_id}"
                observation_ref = f"observation:threads:{page}:{index}:{item_id}"
                username = _optional_text(raw.get("username"))
                text = _optional_text(raw.get("text")) or f"Threads post {item_id}"
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="post",
                            url=permalink,
                            title=text[:160],
                            publisher=f"@{username}" if username else "Threads",
                            published_at=_iso_datetime(raw.get("timestamp")),
                            language=None,
                            metadata={"threads_media_type": _optional_text(raw.get("media_type"))},
                        ),
                    ),
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="threads",
                        platform_item_id=item_id,
                        observed_at=observed_at,
                        url=permalink,
                        payload={
                            **raw,
                            "representativeness_warning": representativeness_warning,
                            "signal_warning": THREADS_SIGNAL_WARNING,
                        },
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref
                metrics.extend(
                    _threads_metrics(
                        raw,
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        observed_at=observed_at,
                    )
                )
            if next_after is None or next_after == after:
                break
            after = next_after

        used = self._quota_counter.used(user_key, observed_at, window_hours=quota.window_hours)
        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                quota_used=used,
                quota_limit=quota.limit,
                quota_window_hours=quota.window_hours,
                charged_calls=charged_calls,
                zero_result_calls=len(snapshots) - charged_calls,
                representativeness_warning=representativeness_warning,
                token_expiry_warning=token_warning,
                access_token_refreshed=refreshed,
                refreshed_expires_in=refresh_expires_in,
                required_permissions=("threads_basic", "threads_keyword_search"),
            ),
        )


def _threads_page(payload: Any) -> tuple[list[Any], str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CollectorContractError("Threads keyword search 응답 data가 배열이 아니다")
    paging = payload.get("paging")
    cursors = paging.get("cursors") if isinstance(paging, dict) else None
    after = _optional_text(cursors.get("after")) if isinstance(cursors, dict) else None
    return payload["data"], after


def _redact_payload_secrets(value: Any) -> Any:
    """응답 body의 토큰 필드와 토큰이 든 URL을 snapshot 전에 제거한다."""
    if isinstance(value, dict):
        return {
            key: (
                "REDACTED"
                if str(key).casefold() in {"access_token", "app_secret", "client_secret"}
                else _redact_payload_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload_secrets(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return redact_url(value)
    return value


def _threads_metrics(
    raw: dict[str, Any],
    *,
    content_ref: str,
    observation_ref: str,
    observed_at: datetime,
) -> list[CollectedMetric]:
    result: list[CollectedMetric] = []
    for source_field, metric_name in _COUNT_METRICS:
        value = raw.get(source_field)
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
                    source_id="threads",
                    observed_at=observed_at,
                    unit="count",
                    denominator=None,
                    geography=None,
                    period=None,
                    population="Threads 플랫폼 반응",
                    method="Threads API raw platform count; 시장 수요·규모가 아님",
                    platform="threads",
                ),
            )
        )
    return result


def _token_warning(value: Any, observed_at: datetime, config: Config) -> str | None:
    if value is None:
        return None
    expires_at = _aware_datetime("token_expires_at", value)
    if expires_at <= observed_at + timedelta(days=config.threads_token_warning_days):
        return f"Threads 장기 토큰 만료 임박: {expires_at.isoformat()}"
    return None


def _iso_datetime(value: Any) -> datetime | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _http_url(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    parsed = urlsplit(text)
    return text if parsed.scheme in {"http", "https"} and parsed.hostname else None


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
        raise CollectorContractError(f"지원하지 않는 Threads 옵션이다: {sorted(unknown)}")
