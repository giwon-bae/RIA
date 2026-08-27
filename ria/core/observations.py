"""플랫폼 관측 저장 (DESIGN §10.2).

**Observation 은 절대 덮어쓰지 않는다.** 같은 (source, platform_item_id, observed_at)
을 다시 수집해도 새 행으로 남긴다. 그래야 절대값뿐 아니라 변화 속도와 지속성을
분석할 수 있고, 플랫폼이 값을 되돌렸다는 사실도 보존된다.

그래서 이 모듈에는 UPDATE 나 DELETE 함수가 없다. 보관 정책에 따른 삭제는
`ria/core/snapshots.py` 의 retention 경로가 담당한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ria.config import parse_iso8601, to_iso8601
from ria.core.entities import new_id
from ria.core.store import Store


@dataclass(frozen=True)
class ObservationInput:
    """특정 플랫폼에서 특정 시점에 본 상태 1건."""

    content_item_id: str
    source_id: str
    platform: str
    observed_at: datetime
    platform_item_id: str | None = None
    url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    snapshot_id: str | None = None
    query_run_id: str | None = None
    research_id: str | None = None


@dataclass(frozen=True)
class ObservationRecord:
    """저장된 관측 1행."""

    observation_id: str
    content_item_id: str
    source_id: str
    platform: str
    platform_item_id: str | None
    observed_at: datetime
    url: str | None
    payload: dict[str, Any]
    snapshot_id: str | None
    query_run_id: str | None
    research_id: str | None
    created_at: str


def record_observation(store: Store, observation: ObservationInput, *, now: datetime) -> str:
    """관측을 새 행으로 남긴다. 기존 행을 갱신하지 않는다."""
    if observation.observed_at.tzinfo is None:
        raise ValueError("observed_at 은 timezone-aware 여야 한다")

    observation_id = new_id("obs")
    store.connection.execute(
        "INSERT INTO source_observations (observation_id, content_item_id, source_id, platform,"
        " platform_item_id, observed_at, url, payload_json, snapshot_id, query_run_id,"
        " research_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            observation_id,
            observation.content_item_id,
            observation.source_id,
            observation.platform,
            observation.platform_item_id,
            to_iso8601(observation.observed_at),
            observation.url,
            json.dumps(observation.payload, ensure_ascii=False),
            observation.snapshot_id,
            observation.query_run_id,
            observation.research_id,
            to_iso8601(now),
        ),
    )
    return observation_id


def list_observations(
    store: Store,
    *,
    content_item_id: str | None = None,
    platform: str | None = None,
    source_id: str | None = None,
    research_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[ObservationRecord]:
    """관측을 시간순으로 돌려준다. 플랫폼별로 따로 보는 것이 기본 사용법이다."""
    clauses: list[str] = []
    params: list[Any] = []

    for column, value in (
        ("content_item_id", content_item_id),
        ("platform", platform),
        ("source_id", source_id),
        ("research_id", research_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)

    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(to_iso8601(since))
    if until is not None:
        clauses.append("observed_at <= ?")
        params.append(to_iso8601(until))

    sql = "SELECT * FROM source_observations"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY observed_at ASC, created_at ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = store.connection.execute(sql, params).fetchall()
    return [_to_record(row) for row in rows]


def count_observations(store: Store, content_item_id: str) -> int:
    row = store.connection.execute(
        "SELECT COUNT(*) AS n FROM source_observations WHERE content_item_id = ?",
        (content_item_id,),
    ).fetchone()
    return int(row["n"])


def platforms_for(store: Store, content_item_id: str) -> list[str]:
    """같은 ContentItem 을 어느 플랫폼에서 봤는가."""
    rows = store.connection.execute(
        "SELECT DISTINCT platform FROM source_observations WHERE content_item_id = ?"
        " ORDER BY platform",
        (content_item_id,),
    ).fetchall()
    return [row["platform"] for row in rows]


def _to_record(row: Any) -> ObservationRecord:
    return ObservationRecord(
        observation_id=row["observation_id"],
        content_item_id=row["content_item_id"],
        source_id=row["source_id"],
        platform=row["platform"],
        platform_item_id=row["platform_item_id"],
        observed_at=parse_iso8601(row["observed_at"]),
        url=row["url"],
        payload=json.loads(row["payload_json"]) if row["payload_json"] else {},
        snapshot_id=row["snapshot_id"],
        query_run_id=row["query_run_id"],
        research_id=row["research_id"],
        created_at=row["created_at"],
    )
