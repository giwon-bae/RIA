"""Entity 와 ContentItem 저장 (DESIGN §10.1 · §10.2).

같은 외부 URL 은 플랫폼이 달라도 ContentItem 1건으로 묶는다. 묶는 키는
`ria/core/dedup.py` 의 정규화 URL 이다. 플랫폼별 상태는 여기 넣지 않는다 —
그건 `source_observations` 의 몫이다.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ria.config import to_iso8601
from ria.core.dedup import url_key
from ria.core.store import Store

EntityType = Literal["company", "product", "topic", "market", "institution"]
ContentType = Literal["post", "video", "app", "product", "document", "article"]


def new_id(prefix: str) -> str:
    """저장소 ID. 접두사로 어느 표의 행인지 눈으로 구분되게 한다."""
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class EntityInput:
    """회사·제품·주제·시장·기관 1건."""

    entity_type: EntityType
    name: str
    canonical_key: str | None = None
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolved_key(self) -> str:
        """중복 판정 키. 주어지지 않으면 타입+이름을 정규화해서 쓴다."""
        if self.canonical_key:
            return self.canonical_key
        return f"{self.entity_type}:{' '.join(self.name.split()).casefold()}"


@dataclass(frozen=True)
class ContentItemInput:
    """글·영상·앱·상품·문서 1건."""

    content_type: ContentType
    url: str | None = None
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    entity_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContentItemRecord:
    """저장된 ContentItem 1행."""

    content_item_id: str
    content_type: str
    url_key: str | None
    canonical_url: str | None
    title: str | None
    publisher: str | None
    published_at: str | None
    language: str | None
    entity_id: str | None
    created_at: str
    updated_at: str


def upsert_entity(store: Store, entity: EntityInput, *, now: datetime) -> str:
    """엔터티를 넣거나 이미 있으면 그 ID 를 돌려준다."""
    key = entity.resolved_key()
    timestamp = to_iso8601(now)

    row = store.connection.execute(
        "SELECT entity_id FROM entities WHERE canonical_key = ?", (key,)
    ).fetchone()
    if row is not None:
        return str(row["entity_id"])

    entity_id = new_id("ent")
    store.connection.execute(
        "INSERT INTO entities (entity_id, entity_type, name, canonical_key, aliases_json,"
        " metadata_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entity_id,
            entity.entity_type,
            entity.name,
            key,
            json.dumps(list(entity.aliases), ensure_ascii=False),
            json.dumps(entity.metadata, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    return entity_id


def upsert_content_item(store: Store, item: ContentItemInput, *, now: datetime) -> str:
    """ContentItem 을 넣거나, 같은 정규화 URL 이 있으면 그 행에 붙인다.

    이미 있는 값을 덮어쓰지 않는다. 비어 있던 필드만 채운다. 두 번째 플랫폼이
    더 빈약한 메타데이터를 들고 와도 첫 관측의 제목·발행자를 지우지 않게 하기 위해서다.
    """
    key = url_key(item.url)
    timestamp = to_iso8601(now)
    published_at = to_iso8601(item.published_at) if item.published_at else None

    if key is not None:
        row = store.connection.execute(
            "SELECT content_item_id FROM content_items WHERE url_key = ?", (key,)
        ).fetchone()
        if row is not None:
            content_item_id = str(row["content_item_id"])
            store.connection.execute(
                "UPDATE content_items SET"
                " title = COALESCE(title, ?),"
                " publisher = COALESCE(publisher, ?),"
                " published_at = COALESCE(published_at, ?),"
                " language = COALESCE(language, ?),"
                " entity_id = COALESCE(entity_id, ?),"
                " updated_at = ?"
                " WHERE content_item_id = ?",
                (
                    item.title,
                    item.publisher,
                    published_at,
                    item.language,
                    item.entity_id,
                    timestamp,
                    content_item_id,
                ),
            )
            return content_item_id

    content_item_id = new_id("ci")
    store.connection.execute(
        "INSERT INTO content_items (content_item_id, content_type, url_key, canonical_url,"
        " title, publisher, published_at, language, entity_id, metadata_json,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            content_item_id,
            item.content_type,
            key,
            item.url,
            item.title,
            item.publisher,
            published_at,
            item.language,
            item.entity_id,
            json.dumps(item.metadata, ensure_ascii=False),
            timestamp,
            timestamp,
        ),
    )
    return content_item_id


def get_content_item(store: Store, content_item_id: str) -> ContentItemRecord | None:
    row = store.connection.execute(
        "SELECT * FROM content_items WHERE content_item_id = ?", (content_item_id,)
    ).fetchone()
    return _to_record(row) if row is not None else None


def find_content_item_by_url(store: Store, url: str | None) -> ContentItemRecord | None:
    """정규화 URL 로 ContentItem 을 찾는다. 정규화할 수 없으면 찾지 않는다."""
    key = url_key(url)
    if key is None:
        return None
    row = store.connection.execute(
        "SELECT * FROM content_items WHERE url_key = ?", (key,)
    ).fetchone()
    return _to_record(row) if row is not None else None


def _to_record(row: Any) -> ContentItemRecord:
    return ContentItemRecord(
        content_item_id=row["content_item_id"],
        content_type=row["content_type"],
        url_key=row["url_key"],
        canonical_url=row["canonical_url"],
        title=row["title"],
        publisher=row["publisher"],
        published_at=row["published_at"],
        language=row["language"],
        entity_id=row["entity_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
