"""A-7. SQLite 저장소 스키마 (DESIGN §10.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ria.config import Config
from ria.config import override_config as set_global_config
from ria.core.store import (
    BASELINE_SCHEMA_VERSION,
    CORE_TABLES,
    Store,
    StoreError,
    open_store,
)


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


def test_core_entities_are_eleven() -> None:
    """DESIGN §10.1 의 핵심 엔터티는 11종이다."""
    assert len(CORE_TABLES) == 11
    assert len(set(CORE_TABLES)) == 11


def test_all_core_tables_exist(store: Store) -> None:
    assert set(CORE_TABLES) <= store.table_names()


def test_schema_version_table_exists(store: Store) -> None:
    assert "schema_version" in store.table_names()
    assert store.schema_version() == BASELINE_SCHEMA_VERSION


def test_required_indexes_exist(store: Store) -> None:
    """지시서 A-7 이 못 박은 3개 인덱스."""
    indexes = store.index_names()

    assert "idx_observations_content_platform_time" in indexes
    assert "idx_metrics_name_time" in indexes
    assert "idx_snapshots_hash" in indexes


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ria.db"

    with Store(path) as first:
        first.connection.execute(
            "INSERT INTO entities (entity_id, entity_type, name, canonical_key,"
            " created_at, updated_at) VALUES ('e-1','company','A','a','t','t')"
        )

    with Store(path) as second:
        rows = second.connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()

        assert rows["n"] == 1
        assert second.schema_version() == BASELINE_SCHEMA_VERSION


def test_foreign_keys_are_enforced(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO source_observations (observation_id, content_item_id, source_id,"
            " platform, observed_at, created_at)"
            " VALUES ('o-1','missing','hacker_news','hacker_news','t','t')"
        )


def test_observations_allow_repeated_identical_rows(store: Store) -> None:
    """같은 (source, item, observed_at) 재수집도 새 행이다 — UNIQUE 제약을 두지 않는다."""
    _content(store, "c-1", "https://example.com/a")

    for observation_id in ("o-1", "o-2"):
        store.connection.execute(
            "INSERT INTO source_observations (observation_id, content_item_id, source_id,"
            " platform, platform_item_id, observed_at, created_at)"
            " VALUES (?, 'c-1', 'hacker_news', 'hacker_news', '42', '2026-08-27T10:00:00+09:00',"
            " '2026-08-27T10:00:00+09:00')",
            (observation_id,),
        )

    row = store.connection.execute("SELECT COUNT(*) AS n FROM source_observations").fetchone()
    assert row["n"] == 2


def test_metrics_require_index_type(store: Store) -> None:
    """상대 지수를 절대 수치로 표현하는 경로를 스키마에서 막는다 (DESIGN §6.3)."""
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO metrics (metric_row_id, metric_name, value_num, index_type,"
            " source_id, observed_at, created_at)"
            " VALUES ('m-1','votes',1,'guess','hacker_news','t','t')"
        )


def test_metrics_require_a_value(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO metrics (metric_row_id, metric_name, index_type, source_id,"
            " observed_at, created_at)"
            " VALUES ('m-1','votes','absolute','hacker_news','t','t')"
        )


def test_content_url_key_is_unique(store: Store) -> None:
    _content(store, "c-1", "https://example.com/a")

    with pytest.raises(sqlite3.IntegrityError):
        _content(store, "c-2", "https://example.com/a")


def test_snapshot_hash_is_unique_per_source(store: Store) -> None:
    _snapshot(store, "s-1", "hacker_news", "abc")
    _snapshot(store, "s-2", "world_bank", "abc")  # 다른 소스면 같은 해시를 허용한다

    with pytest.raises(sqlite3.IntegrityError):
        _snapshot(store, "s-3", "hacker_news", "abc")


def test_transaction_rolls_back_on_failure(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as connection:
        _content(store, "c-1", "https://example.com/a")
        connection.execute(
            "INSERT INTO source_observations (observation_id, content_item_id, source_id,"
            " platform, observed_at, created_at)"
            " VALUES ('o-1','missing','x','x','t','t')"
        )

    row = store.connection.execute("SELECT COUNT(*) AS n FROM content_items").fetchone()
    assert row["n"] == 0


# --- 마이그레이션 -----------------------------------------------------------
def test_migration_files_are_applied_in_order(store: Store, tmp_path: Path) -> None:
    (tmp_path / "002_add_note.sql").write_text(
        "ALTER TABLE entities ADD COLUMN note TEXT;", encoding="utf-8"
    )
    (tmp_path / "003_add_flag.sql").write_text(
        "ALTER TABLE entities ADD COLUMN flag INTEGER;", encoding="utf-8"
    )

    applied = store.apply_migrations(tmp_path)

    assert applied == [2, 3]
    assert store.schema_version() == 3
    assert store.apply_migrations(tmp_path) == []


def test_migration_below_baseline_is_rejected(store: Store, tmp_path: Path) -> None:
    (tmp_path / "001_conflict.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(StoreError, match="기준 스키마"):
        store.apply_migrations(tmp_path)


def test_migration_with_bad_name_is_rejected(store: Store, tmp_path: Path) -> None:
    (tmp_path / "add_note.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(StoreError, match="이름"):
        store.apply_migrations(tmp_path)


def test_missing_migration_directory_is_not_an_error(store: Store, tmp_path: Path) -> None:
    assert store.apply_migrations(tmp_path / "nope") == []


def test_open_store_uses_configured_db_path(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ria.db"
    set_global_config(Config(db_path=path, credentials={}))
    try:
        with open_store() as store:
            assert store.db_path == path
        assert path.is_file()
    finally:
        set_global_config(None)


# --- 헬퍼 -------------------------------------------------------------------
def _content(store: Store, content_item_id: str, url: str) -> None:
    store.connection.execute(
        "INSERT INTO content_items (content_item_id, content_type, url_key, canonical_url,"
        " created_at, updated_at) VALUES (?, 'article', ?, ?, 't', 't')",
        (content_item_id, url, url),
    )


def _snapshot(store: Store, snapshot_id: str, source_id: str, digest: str) -> None:
    store.connection.execute(
        "INSERT INTO raw_snapshots (snapshot_id, hash, source_id, body_stored, collected_at)"
        " VALUES (?, ?, ?, 1, '2026-08-27T10:00:00+09:00')",
        (snapshot_id, digest, source_id),
    )
