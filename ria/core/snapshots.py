"""원본 스냅샷 저장과 retention (DESIGN §10.4 · §15).

원본 응답은 해시와 수집 시점을 포함한 **immutable** 스냅샷으로 저장한다. 같은 소스에서
같은 해시가 다시 들어오면 새 행을 만들지 않고 기존 행의 수집 시점·만료를 갱신한다.

정책상 원본 저장이 제한되면 body 를 저장하지 않고 URL·해시·메타데이터만 남기는 경로로
분기한다. 이 분기는 Policy Guard 가 아니라 `storage_policy` 가 결정한다 — guard 는
"호출해도 되는가"를 보고, 여기서는 "무엇을 남겨도 되는가"를 본다.

retention 은 `deletion_policy` 를 따른다. YouTube 의 30일 규칙은 여기서 실제로 동작한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ria.config import parse_iso8601, to_iso8601
from ria.core.entities import new_id
from ria.core.store import Store
from ria.policy.registry import SourceRecord, SourceRegistry, get_registry

# 보관 기간을 정책 이름에서 읽는다. 코드에 30 을 흩뿌리지 않는다.
RETENTION_DAYS: dict[str, int] = {
    "delete_or_refresh_30d": 30,
}

STORAGE_RETENTION_DAYS: dict[str, int] = {
    "refresh_or_delete_30d": 30,
}

# 원본 body 를 저장할 수 없는 보관 정책.
BODY_FORBIDDEN_POLICIES = frozenset({"no_storage", "metadata_only"})

# 승인 범위 안에서만 원본을 보관할 수 있는 정책. 승인은 access_status=core 로 표현된다.
BODY_NEEDS_APPROVAL_POLICIES = frozenset({"approved_use_only"})


@dataclass(frozen=True)
class SnapshotInput:
    """저장할 원본 1건."""

    source_id: str
    body: str | bytes | dict[str, Any] | list[Any] | None
    collected_at: datetime
    url: str | None = None
    media_type: str | None = None
    query: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotRecord:
    """저장된 스냅샷 1행."""

    snapshot_id: str
    hash: str
    source_id: str
    url: str | None
    media_type: str | None
    body: str | None
    body_stored: bool
    storage_policy: str | None
    meta: dict[str, Any]
    query: str | None
    collected_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None

    @property
    def is_expired_placeholder(self) -> bool:
        """retention 으로 원본이 지워지고 메타데이터만 남은 행인가."""
        return self.deleted_at is not None


@dataclass(frozen=True)
class SnapshotResult:
    """저장 결과. 무엇이 실제로 일어났는지 호출부가 알아야 한다."""

    snapshot_id: str
    hash: str
    deduplicated: bool
    body_stored: bool
    expires_at: datetime | None
    reason: str


def compute_hash(body: str | bytes | dict[str, Any] | list[Any] | None) -> str:
    """원본의 내용 해시. dict·list 는 키 순서에 흔들리지 않게 정렬해서 직렬화한다."""
    if body is None:
        payload = b""
    elif isinstance(body, bytes):
        payload = body
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    else:
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def store_snapshot(
    store: Store,
    snapshot: SnapshotInput,
    *,
    registry: SourceRegistry | None = None,
) -> SnapshotResult:
    """원본을 저장한다. 같은 소스·같은 해시면 새 행을 만들지 않는다."""
    if snapshot.collected_at.tzinfo is None:
        raise ValueError("collected_at 은 timezone-aware 여야 한다")

    registry = registry if registry is not None else get_registry()
    record = registry.find(snapshot.source_id)

    digest = compute_hash(snapshot.body)
    store_body, reason = _body_decision(record)
    expires_at = _expiry(record, snapshot.collected_at)

    existing = store.connection.execute(
        "SELECT snapshot_id, body_stored FROM raw_snapshots WHERE source_id = ? AND hash = ?",
        (snapshot.source_id, digest),
    ).fetchone()

    if existing is not None:
        # 같은 원본을 다시 봤다. 새 행을 만들지 않고 수집 시점과 만료를 갱신한다.
        # 이 갱신이 곧 "30일 이내 삭제 또는 갱신"의 갱신 쪽 경로다.
        store.connection.execute(
            "UPDATE raw_snapshots SET collected_at = ?, expires_at = ?, deleted_at = NULL"
            " WHERE snapshot_id = ?",
            (
                to_iso8601(snapshot.collected_at),
                to_iso8601(expires_at) if expires_at else None,
                existing["snapshot_id"],
            ),
        )
        return SnapshotResult(
            snapshot_id=str(existing["snapshot_id"]),
            hash=digest,
            deduplicated=True,
            body_stored=bool(existing["body_stored"]),
            expires_at=expires_at,
            reason="같은 해시가 이미 있어 수집 시점만 갱신했다",
        )

    snapshot_id = new_id("snap")
    store.connection.execute(
        "INSERT INTO raw_snapshots (snapshot_id, hash, source_id, url, media_type, body,"
        " body_stored, storage_policy, meta_json, query, collected_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            digest,
            snapshot.source_id,
            snapshot.url,
            snapshot.media_type,
            _serialize_body(snapshot.body) if store_body else None,
            int(store_body),
            record.storage_policy if record else None,
            json.dumps(snapshot.meta, ensure_ascii=False),
            snapshot.query,
            to_iso8601(snapshot.collected_at),
            to_iso8601(expires_at) if expires_at else None,
        ),
    )
    return SnapshotResult(
        snapshot_id=snapshot_id,
        hash=digest,
        deduplicated=False,
        body_stored=store_body,
        expires_at=expires_at,
        reason=reason,
    )


def get_snapshot(store: Store, snapshot_id: str) -> SnapshotRecord | None:
    row = store.connection.execute(
        "SELECT * FROM raw_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    return _to_record(row) if row is not None else None


def find_snapshot(store: Store, source_id: str, digest: str) -> SnapshotRecord | None:
    row = store.connection.execute(
        "SELECT * FROM raw_snapshots WHERE source_id = ? AND hash = ?", (source_id, digest)
    ).fetchone()
    return _to_record(row) if row is not None else None


# --- retention --------------------------------------------------------------
def expired_snapshots(
    store: Store, as_of: datetime, *, source_id: str | None = None
) -> list[SnapshotRecord]:
    """보관 기한이 지났는데 아직 정리되지 않은 스냅샷."""
    clauses = ["expires_at IS NOT NULL", "expires_at <= ?", "deleted_at IS NULL"]
    params: list[Any] = [to_iso8601(as_of)]
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(source_id)

    rows = store.connection.execute(
        "SELECT * FROM raw_snapshots WHERE " + " AND ".join(clauses) + " ORDER BY expires_at ASC",
        params,
    ).fetchall()
    return [_to_record(row) for row in rows]


def enforce_retention(
    store: Store,
    as_of: datetime,
    *,
    source_id: str | None = None,
    purge: bool = False,
) -> list[str]:
    """만료된 스냅샷을 정리한다. 처리한 snapshot_id 목록을 돌려준다.

    기본 동작은 원본 body 만 지우고 해시·URL·수집 시점은 남기는 것이다. 그래야
    "이 자료를 봤고 지금은 정책에 따라 지웠다"는 사실이 추적 가능하게 남는다.
    YouTube는 같은 원본에 연결된 관측 payload도 비우고 그 관측에서 파생 없이 복사한
    metric 행도 지운다. 관측 trace 자체와 ContentItem은 남긴다.
    `purge=True` 면 행 자체를 지운다 — 메타데이터도 남길 수 없을 때만 쓴다.
    """
    targets = expired_snapshots(store, as_of, source_id=source_id)
    if not targets:
        return []

    ids = [snapshot.snapshot_id for snapshot in targets]
    placeholders = ", ".join("?" for _ in ids)

    # purge가 raw snapshot FK를 먼저 끊어 버리기 전에 연결된 YouTube observation을
    # 확보한다. 다른 source의 snapshot·observation·metric에는 이 정리를 적용하지 않는다.
    youtube_snapshot_ids = [
        snapshot.snapshot_id for snapshot in targets if snapshot.source_id == "youtube_data"
    ]
    if youtube_snapshot_ids:
        youtube_placeholders = ", ".join("?" for _ in youtube_snapshot_ids)
        rows = store.connection.execute(
            "SELECT observation_id FROM source_observations"
            f" WHERE source_id = 'youtube_data'"
            f" AND snapshot_id IN ({youtube_placeholders})",
            youtube_snapshot_ids,
        ).fetchall()
        observation_ids = [str(row["observation_id"]) for row in rows]
        if observation_ids:
            observation_placeholders = ", ".join("?" for _ in observation_ids)
            store.connection.execute(
                f"DELETE FROM metrics WHERE observation_id IN ({observation_placeholders})",
                observation_ids,
            )
            store.connection.execute(
                "UPDATE source_observations SET payload_json = NULL"
                f" WHERE observation_id IN ({observation_placeholders})",
                observation_ids,
            )

    if purge:
        store.connection.execute(
            f"DELETE FROM raw_snapshots WHERE snapshot_id IN ({placeholders})", ids
        )
    else:
        store.connection.execute(
            f"UPDATE raw_snapshots SET body = NULL, body_stored = 0, deleted_at = ?"
            f" WHERE snapshot_id IN ({placeholders})",
            [to_iso8601(as_of), *ids],
        )
    return ids


def refresh_snapshot(
    store: Store,
    snapshot_id: str,
    collected_at: datetime,
    *,
    registry: SourceRegistry | None = None,
) -> datetime | None:
    """스냅샷을 다시 확인했다고 표시하고 만료를 미룬다. 새 만료 시각을 돌려준다."""
    row = store.connection.execute(
        "SELECT source_id FROM raw_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return None

    registry = registry if registry is not None else get_registry()
    expires_at = _expiry(registry.find(row["source_id"]), collected_at)
    store.connection.execute(
        "UPDATE raw_snapshots SET collected_at = ?, expires_at = ?, deleted_at = NULL"
        " WHERE snapshot_id = ?",
        (
            to_iso8601(collected_at),
            to_iso8601(expires_at) if expires_at else None,
            snapshot_id,
        ),
    )
    return expires_at


def retention_days_for(record: SourceRecord | None) -> int | None:
    """소스의 보관 기간(일). 기한이 없으면 None."""
    if record is None:
        return None
    if record.deletion_policy in RETENTION_DAYS:
        return RETENTION_DAYS[record.deletion_policy]
    return STORAGE_RETENTION_DAYS.get(record.storage_policy)


# --- 내부 ------------------------------------------------------------------
def _body_decision(record: SourceRecord | None) -> tuple[bool, str]:
    """원본 body 를 저장해도 되는가."""
    if record is None:
        return False, "레지스트리에 없는 소스다 — 메타데이터·해시만 남긴다"
    if record.storage_policy in BODY_FORBIDDEN_POLICIES:
        return False, f"storage_policy={record.storage_policy} — 메타데이터·URL·해시만 남긴다"
    if record.storage_policy in BODY_NEEDS_APPROVAL_POLICIES and record.access_status != "core":
        return (
            False,
            f"storage_policy={record.storage_policy} 인데 승인 전(access_status="
            f"{record.access_status})이다 — 메타데이터·해시만 남긴다",
        )
    return True, f"storage_policy={record.storage_policy} — 원본을 보관한다"


def _expiry(record: SourceRecord | None, collected_at: datetime) -> datetime | None:
    days = retention_days_for(record)
    return collected_at + timedelta(days=days) if days is not None else None


def _serialize_body(body: str | bytes | dict[str, Any] | list[Any] | None) -> str | None:
    if body is None:
        return None
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _to_record(row: Any) -> SnapshotRecord:
    return SnapshotRecord(
        snapshot_id=row["snapshot_id"],
        hash=row["hash"],
        source_id=row["source_id"],
        url=row["url"],
        media_type=row["media_type"],
        body=row["body"],
        body_stored=bool(row["body_stored"]),
        storage_policy=row["storage_policy"],
        meta=json.loads(row["meta_json"]) if row["meta_json"] else {},
        query=row["query"],
        collected_at=parse_iso8601(row["collected_at"]),
        expires_at=parse_iso8601(row["expires_at"]) if row["expires_at"] else None,
        deleted_at=parse_iso8601(row["deleted_at"]) if row["deleted_at"] else None,
    )
