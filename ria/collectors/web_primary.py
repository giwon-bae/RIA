"""Codex/Chrome이 확인한 웹 원문을 짧은 스냅샷으로 저장한다 (B-6).

``web-primary``는 Source Registry의 source가 아니라 의도적으로 비대칭인 Pack ID다.
Core가 웹을 탐색하거나 HTTP를 호출하지 않고, 외부에서 확인한 짧은 발췌만 받아
Content·metadata-only raw snapshot·Observation으로 원자 저장한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from ria.collectors.base import CollectorContractError
from ria.config import Config, get_config, to_iso8601
from ria.core.entities import ContentItemInput, upsert_content_item
from ria.core.observations import ObservationInput, record_observation
from ria.core.snapshots import SnapshotInput, store_snapshot
from ria.core.store import Store

WEB_PRIMARY_ID = "web-primary"


@dataclass(frozen=True)
class StoredWebSnapshot:
    """web-primary 저장 결과의 실제 DB 식별자."""

    content_item_id: str
    observation_id: str
    snapshot_id: str
    body_stored: bool
    excerpt_chars: int


def store_web_snapshot(
    url: str,
    title: str,
    publisher: str,
    published_at: datetime,
    excerpt: str,
    query: str,
    observed_at: datetime,
    *,
    store: Store | None = None,
    config: Config | None = None,
) -> StoredWebSnapshot:
    """이미 확인된 웹 원문의 짧은 발췌와 해시만 저장한다.

    ``store``를 생략하면 설정의 DB를 열고 이 호출 안에서 닫는다. HTTP client나
    네트워크 진입점은 의도적으로 존재하지 않는다.
    """
    resolved_config = config if config is not None else get_config()
    clean_url = _web_url(url)
    clean_title = _required_text("title", title)
    clean_publisher = _required_text("publisher", publisher)
    clean_excerpt = _required_text("excerpt", excerpt)
    clean_query = _required_text("query", query)
    _aware("published_at", published_at)
    _aware("observed_at", observed_at)
    limit = resolved_config.web_snapshot_max_excerpt_chars
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise CollectorContractError("web snapshot excerpt 한도는 양의 정수여야 한다")
    if len(clean_excerpt) > limit:
        raise CollectorContractError(
            f"excerpt가 짧은 발췌 한도({limit}자)를 넘었다: {len(clean_excerpt)}자"
        )

    owns_store = store is None
    target = store if store is not None else Store(resolved_config.db_path)
    try:
        return _persist_web_snapshot(
            target,
            url=clean_url,
            title=clean_title,
            publisher=clean_publisher,
            published_at=published_at,
            excerpt=clean_excerpt,
            query=clean_query,
            observed_at=observed_at,
        )
    finally:
        if owns_store:
            target.close()


def _persist_web_snapshot(
    store: Store,
    *,
    url: str,
    title: str,
    publisher: str,
    published_at: datetime,
    excerpt: str,
    query: str,
    observed_at: datetime,
) -> StoredWebSnapshot:
    canonical_body = {
        "url": url,
        "title": title,
        "publisher": publisher,
        "published_at": to_iso8601(published_at),
        "excerpt": excerpt,
        "query": query,
    }
    with store.transaction():
        content_item_id = upsert_content_item(
            store,
            ContentItemInput(
                content_type="article",
                url=url,
                title=title,
                publisher=publisher,
                published_at=published_at,
                metadata={"ingest_path": WEB_PRIMARY_ID},
            ),
            now=observed_at,
        )
        snapshot = store_snapshot(
            store,
            SnapshotInput(
                source_id=WEB_PRIMARY_ID,
                body=canonical_body,
                collected_at=observed_at,
                url=url,
                media_type="application/json",
                query=query,
                meta={
                    "publisher": publisher,
                    "published_at": to_iso8601(published_at),
                    "excerpt_chars": len(excerpt),
                },
            ),
        )
        observation_id = record_observation(
            store,
            ObservationInput(
                content_item_id=content_item_id,
                source_id=WEB_PRIMARY_ID,
                platform="web",
                platform_item_id=url,
                observed_at=observed_at,
                url=url,
                payload={
                    "title": title,
                    "publisher": publisher,
                    "published_at": to_iso8601(published_at),
                    "excerpt": excerpt,
                    "query": query,
                },
                snapshot_id=snapshot.snapshot_id,
            ),
            now=observed_at,
        )

    return StoredWebSnapshot(
        content_item_id=content_item_id,
        observation_id=observation_id,
        snapshot_id=snapshot.snapshot_id,
        body_stored=snapshot.body_stored,
        excerpt_chars=len(excerpt),
    )


def _web_url(value: str) -> str:
    text = _required_text("url", value)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CollectorContractError("url은 host가 있는 http/https URL이어야 한다")
    if parsed.username is not None or parsed.password is not None:
        raise CollectorContractError("url에 자격증명을 포함할 수 없다")
    return text


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise CollectorContractError(f"{name}은 비어 있지 않은 문자열이어야 한다")
    return text


def _aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectorContractError(f"{name}은 timezone-aware datetime이어야 한다")
