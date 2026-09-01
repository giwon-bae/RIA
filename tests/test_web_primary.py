"""B-6 web-primary는 네트워크 없이 짧은 발췌와 해시만 저장한다."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ria.collectors.base import CollectorContractError
from ria.collectors.web_primary import store_web_snapshot
from ria.config import KST, Config
from ria.core.store import Store

PUBLISHED_AT = datetime(2026, 8, 31, 8, 0, tzinfo=KST)
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=KST)
URL = "https://example.com/research/ria"


def _config(tmp_path: Path, *, limit: int = 80) -> Config:
    return Config(
        db_path=tmp_path / "web-primary.db",
        credentials={},
        web_snapshot_max_excerpt_chars=limit,
    )


def _store(store: Store, config: Config, **overrides: object) -> object:
    values: dict[str, object] = {
        "url": URL,
        "title": "RIA 원문",
        "publisher": "Example Research",
        "published_at": PUBLISHED_AT,
        "excerpt": "공식 원문에서 확인한 짧은 사실 발췌.",
        "query": "RIA evidence",
        "observed_at": OBSERVED_AT,
        "store": store,
        "config": config,
    }
    values.update(overrides)
    return store_web_snapshot(**values)


def _counts(store: Store) -> tuple[int, int, int, int]:
    return tuple(
        int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("content_items", "source_observations", "metrics", "raw_snapshots")
    )


def test_store_web_snapshot_keeps_short_excerpt_and_hash_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with Store(":memory:") as store:
        result = _store(store, config)
        snapshot = store.connection.execute(
            "SELECT * FROM raw_snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
        ).fetchone()
        observation = store.connection.execute(
            "SELECT * FROM source_observations WHERE observation_id = ?",
            (result.observation_id,),
        ).fetchone()

        assert _counts(store) == (1, 1, 0, 1)
        assert result.body_stored is False
        assert snapshot["source_id"] == "web-primary"
        assert snapshot["body"] is None
        assert snapshot["hash"]
        assert snapshot["snapshot_id"] == observation["snapshot_id"]
        assert json.loads(observation["payload_json"])["excerpt"].startswith("공식 원문")


def test_excerpt_at_limit_is_allowed_but_full_page_is_rejected_before_write(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, limit=12)
    with Store(":memory:") as store:
        _store(store, config, excerpt="x" * 12)
        assert _counts(store) == (1, 1, 0, 1)

    with Store(":memory:") as store:
        marker = "FULL_PAGE_MARKER_" + "x" * 30
        with pytest.raises(CollectorContractError, match="한도"):
            _store(store, config, excerpt=marker)
        assert _counts(store) == (0, 0, 0, 0)
        dump = "\n".join(
            str(value)
            for table in ("content_items", "source_observations", "raw_snapshots")
            for row in store.connection.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
        assert "FULL_PAGE_MARKER" not in dump


def test_same_url_deduplicates_content_and_snapshot_but_appends_observation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with Store(":memory:") as store:
        first = _store(store, config)
        second = _store(store, config)

        assert first.content_item_id == second.content_item_id
        assert first.snapshot_id == second.snapshot_id
        assert _counts(store) == (1, 2, 0, 1)


def test_same_excerpt_on_different_url_has_distinct_snapshot_hash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with Store(":memory:") as store:
        first = _store(store, config)
        second = _store(store, config, url="https://example.org/research/ria")

        assert first.snapshot_id != second.snapshot_id
        assert _counts(store) == (2, 2, 0, 2)


@pytest.mark.parametrize("field", ["published_at", "observed_at"])
def test_naive_datetime_is_rejected_before_write(tmp_path: Path, field: str) -> None:
    config = _config(tmp_path)
    with Store(":memory:") as store:
        with pytest.raises(CollectorContractError, match="timezone-aware"):
            _store(store, config, **{field: datetime(2026, 9, 1, 10, 0)})
        assert _counts(store) == (0, 0, 0, 0)


def test_omitted_store_uses_configured_database(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = store_web_snapshot(
        URL,
        "RIA 원문",
        "Example Research",
        PUBLISHED_AT,
        "짧은 발췌",
        "RIA evidence",
        OBSERVED_AT,
        config=config,
    )

    with Store(config.db_path) as store:
        assert result.body_stored is False
        assert _counts(store) == (1, 1, 0, 1)
