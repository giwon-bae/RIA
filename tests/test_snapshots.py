"""A-9. immutable 스냅샷과 retention (DESIGN §10.4)."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from ria.config import KST, SOURCES_YAML_PATH
from ria.core.snapshots import (
    SnapshotInput,
    compute_hash,
    enforce_retention,
    expired_snapshots,
    find_snapshot,
    get_snapshot,
    refresh_snapshot,
    retention_days_for,
    store_snapshot,
)
from ria.core.store import Store
from ria.policy.registry import SourceRegistry

T0 = datetime(2026, 8, 27, 10, 0, tzinfo=KST)
BODY = {"items": [{"id": "abc", "views": 1200}]}


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def registry() -> SourceRegistry:
    return SourceRegistry()


@pytest.fixture
def writable_registry(tmp_path: Path) -> SourceRegistry:
    copy = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, copy)
    return SourceRegistry(copy)


def _store(store: Store, registry: SourceRegistry, **overrides: object) -> object:
    payload: dict[str, object] = {
        "source_id": "hacker_news",
        "body": BODY,
        "collected_at": T0,
        "url": "https://news.ycombinator.com/item?id=1",
    }
    payload.update(overrides)
    return store_snapshot(store, SnapshotInput(**payload), registry=registry)


# --- 해시와 immutability -----------------------------------------------------
def test_hash_is_stable_regardless_of_key_order() -> None:
    assert compute_hash({"a": 1, "b": 2}) == compute_hash({"b": 2, "a": 1})


def test_hash_differs_when_content_differs() -> None:
    assert compute_hash({"a": 1}) != compute_hash({"a": 2})


def test_snapshot_keeps_hash_and_collection_time(store: Store, registry: SourceRegistry) -> None:
    result = _store(store, registry)
    record = get_snapshot(store, result.snapshot_id)

    assert record is not None
    assert record.hash == compute_hash(BODY)
    assert record.collected_at == T0
    assert record.body_stored is True


def test_same_hash_is_deduplicated(store: Store, registry: SourceRegistry) -> None:
    """재저장 시 같은 해시면 새 행을 만들지 않는다."""
    first = _store(store, registry)
    second = _store(store, registry, collected_at=T0 + timedelta(days=1))

    assert second.deduplicated is True
    assert second.snapshot_id == first.snapshot_id

    row = store.connection.execute("SELECT COUNT(*) AS n FROM raw_snapshots").fetchone()
    assert row["n"] == 1


def test_dedup_updates_collection_time(store: Store, registry: SourceRegistry) -> None:
    first = _store(store, registry)
    _store(store, registry, collected_at=T0 + timedelta(days=1))

    record = get_snapshot(store, first.snapshot_id)

    assert record is not None
    assert record.collected_at == T0 + timedelta(days=1)


def test_different_body_creates_a_new_snapshot(store: Store, registry: SourceRegistry) -> None:
    first = _store(store, registry)
    second = _store(store, registry, body={"items": [{"id": "abc", "views": 1500}]})

    assert second.snapshot_id != first.snapshot_id
    assert second.deduplicated is False


def test_same_hash_from_a_different_source_is_a_separate_row(
    store: Store, registry: SourceRegistry
) -> None:
    first = _store(store, registry)
    second = _store(store, registry, source_id="world_bank")

    assert first.snapshot_id != second.snapshot_id
    assert first.hash == second.hash


def test_find_snapshot_by_hash(store: Store, registry: SourceRegistry) -> None:
    result = _store(store, registry)

    found = find_snapshot(store, "hacker_news", result.hash)

    assert found is not None
    assert found.snapshot_id == result.snapshot_id


def test_naive_collected_at_is_rejected(store: Store, registry: SourceRegistry) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _store(store, registry, collected_at=datetime(2026, 8, 27, 10, 0))


# --- 저장 제한 분기 ----------------------------------------------------------
def test_body_is_not_stored_when_policy_forbids_it(store: Store, registry: SourceRegistry) -> None:
    """정책상 원본 저장이 제한되면 메타데이터·URL·해시만 남긴다."""
    result = _store(store, registry, source_id="google_play")
    record = get_snapshot(store, result.snapshot_id)

    assert result.body_stored is False
    assert record is not None
    assert record.body is None
    assert record.hash == compute_hash(BODY)
    assert record.url is not None


def test_approval_gated_storage_is_metadata_only_before_approval(
    store: Store, registry: SourceRegistry
) -> None:
    result = _store(store, registry, source_id="reddit")

    assert result.body_stored is False
    assert "승인 전" in result.reason


def test_approval_gated_storage_opens_after_approval(
    store: Store, writable_registry: SourceRegistry
) -> None:
    writable_registry.set_access_status("reddit", "core", date(2026, 8, 27))

    result = store_snapshot(
        store,
        SnapshotInput(source_id="reddit", body=BODY, collected_at=T0),
        registry=writable_registry,
    )

    assert result.body_stored is True


def test_unknown_source_falls_back_to_metadata_only(store: Store, registry: SourceRegistry) -> None:
    result = _store(store, registry, source_id="mastodon")

    assert result.body_stored is False


# --- retention: YouTube 30일 -------------------------------------------------
def test_youtube_snapshot_expires_in_thirty_days(store: Store, registry: SourceRegistry) -> None:
    result = _store(store, registry, source_id="youtube_data")

    assert result.expires_at == T0 + timedelta(days=30)


def test_retention_days_are_read_from_policy(registry: SourceRegistry) -> None:
    assert retention_days_for(registry.get("youtube_data")) == 30
    assert retention_days_for(registry.get("hacker_news")) is None
    assert retention_days_for(None) is None


def test_source_without_retention_has_no_expiry(store: Store, registry: SourceRegistry) -> None:
    assert _store(store, registry, source_id="hacker_news").expires_at is None


def test_expired_snapshots_are_found_only_after_the_deadline(
    store: Store, registry: SourceRegistry
) -> None:
    _store(store, registry, source_id="youtube_data")

    assert expired_snapshots(store, T0 + timedelta(days=29)) == []
    assert len(expired_snapshots(store, T0 + timedelta(days=30))) == 1


def test_enforce_retention_deletes_the_body_but_keeps_the_trace(
    store: Store, registry: SourceRegistry
) -> None:
    """YouTube 30일 규칙이 실제로 동작해야 한다."""
    result = _store(store, registry, source_id="youtube_data")
    as_of = T0 + timedelta(days=31)

    processed = enforce_retention(store, as_of)
    record = get_snapshot(store, result.snapshot_id)

    assert processed == [result.snapshot_id]
    assert record is not None
    assert record.body is None
    assert record.body_stored is False
    assert record.deleted_at == as_of
    assert record.hash == compute_hash(BODY)
    assert record.is_expired_placeholder is True


def test_enforce_retention_is_idempotent(store: Store, registry: SourceRegistry) -> None:
    _store(store, registry, source_id="youtube_data")
    as_of = T0 + timedelta(days=31)

    first = enforce_retention(store, as_of)
    second = enforce_retention(store, as_of)

    assert len(first) == 1
    assert second == []


def test_enforce_retention_leaves_unexpired_sources_alone(
    store: Store, registry: SourceRegistry
) -> None:
    hn = _store(store, registry, source_id="hacker_news")
    _store(store, registry, source_id="youtube_data")

    enforce_retention(store, T0 + timedelta(days=31))
    record = get_snapshot(store, hn.snapshot_id)

    assert record is not None
    assert record.body is not None


def test_enforce_retention_can_be_scoped_to_one_source(
    store: Store, registry: SourceRegistry
) -> None:
    _store(store, registry, source_id="youtube_data")

    assert enforce_retention(store, T0 + timedelta(days=31), source_id="reddit") == []
    assert len(enforce_retention(store, T0 + timedelta(days=31), source_id="youtube_data")) == 1


def test_purge_removes_the_row_entirely(store: Store, registry: SourceRegistry) -> None:
    result = _store(store, registry, source_id="youtube_data")

    enforce_retention(store, T0 + timedelta(days=31), purge=True)

    assert get_snapshot(store, result.snapshot_id) is None


# --- retention: 갱신 경로 ----------------------------------------------------
def test_refresh_extends_the_deadline(store: Store, registry: SourceRegistry) -> None:
    """ "30일 이내 삭제 **또는 갱신**" 의 갱신 쪽."""
    result = _store(store, registry, source_id="youtube_data")
    refreshed_at = T0 + timedelta(days=20)

    new_expiry = refresh_snapshot(store, result.snapshot_id, refreshed_at, registry=registry)

    assert new_expiry == refreshed_at + timedelta(days=30)
    assert expired_snapshots(store, T0 + timedelta(days=31)) == []


def test_recollecting_the_same_body_refreshes_the_deadline(
    store: Store, registry: SourceRegistry
) -> None:
    _store(store, registry, source_id="youtube_data")
    _store(store, registry, source_id="youtube_data", collected_at=T0 + timedelta(days=20))

    assert expired_snapshots(store, T0 + timedelta(days=31)) == []


def test_refresh_of_unknown_snapshot_returns_none(store: Store, registry: SourceRegistry) -> None:
    assert refresh_snapshot(store, "snap_missing", T0, registry=registry) is None
