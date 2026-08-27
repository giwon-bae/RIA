"""SQLite 저장소 — 핵심 엔터티 11개 테이블 (DESIGN §10.1).

표준 `sqlite3` 만 쓴다. ORM 을 넣지 않는다. 스키마가 곧 데이터 모델 문서이고,
SQL 을 직접 읽는 것이 정책 검증에 유리하기 때문이다.

v1 DB 는 마이그레이션하지 않는다. 원본이 소실됐고 DESIGN §17 에서 개발 데이터로만
취급한다.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

from ria.config import MIGRATIONS_DIR, get_config, now, to_iso8601

# store.py 가 부트스트랩하는 기준 스키마 번호. 이후 변경은 ria/migrations/ 의 SQL 로 올린다.
BASELINE_SCHEMA_VERSION = 1

# DESIGN §10.1 핵심 엔터티 11종.
CORE_TABLES: tuple[str, ...] = (
    "research_runs",
    "query_runs",
    "source_registry",
    "entities",
    "content_items",
    "source_observations",
    "metrics",
    "raw_snapshots",
    "evidence_claims",
    "claim_evidence",
    "research_gaps",
)

_SCHEMA_SQL = """
-- 조사 작업과 ResearchBrief
CREATE TABLE IF NOT EXISTS research_runs (
    research_id         TEXT PRIMARY KEY,
    decision_question   TEXT NOT NULL,
    business_domain     TEXT NOT NULL,
    brief_json          TEXT NOT NULL,
    status              TEXT NOT NULL,
    termination_reason  TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT
);

-- Pack·소스별 query, 옵션, 결과, 오류, 비용
CREATE TABLE IF NOT EXISTS query_runs (
    query_run_id    TEXT PRIMARY KEY,
    research_id     TEXT REFERENCES research_runs(research_id) ON DELETE CASCADE,
    pack_id         TEXT,
    source_id       TEXT NOT NULL,
    query           TEXT NOT NULL,
    options_json    TEXT,
    status          TEXT NOT NULL,
    result_count    INTEGER,
    error           TEXT,
    cost_note       TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- 접근·상업 이용·보관·정책 상태의 사용 시점 스냅샷.
-- 정본은 ria/policy/sources.yaml 이고 이 표는 "그때 무슨 정책으로 호출했는가"의 기록이다.
CREATE TABLE IF NOT EXISTS source_registry (
    source_id               TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    pack_ids_json           TEXT NOT NULL,
    access_status           TEXT NOT NULL,
    access_method           TEXT NOT NULL,
    official                INTEGER NOT NULL,
    commercial_use          TEXT NOT NULL,
    auth_type               TEXT NOT NULL,
    rate_limit_model        TEXT NOT NULL,
    storage_policy          TEXT NOT NULL,
    deletion_policy         TEXT NOT NULL,
    allowed_data_types_json TEXT NOT NULL,
    blocked_data_types_json TEXT NOT NULL,
    last_verified_at        TEXT NOT NULL,
    verify_before_use       INTEGER NOT NULL,
    fallback_sources_json   TEXT NOT NULL,
    policy_urls_json        TEXT NOT NULL,
    notes                   TEXT,
    synced_at               TEXT NOT NULL
);

-- 회사·제품·주제·시장·기관
CREATE TABLE IF NOT EXISTS entities (
    entity_id     TEXT PRIMARY KEY,
    entity_type   TEXT NOT NULL,
    name          TEXT NOT NULL,
    canonical_key TEXT NOT NULL UNIQUE,
    aliases_json  TEXT,
    metadata_json TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 글·영상·앱·상품·문서. 같은 외부 URL 은 플랫폼이 달라도 여기서 1건으로 묶인다.
CREATE TABLE IF NOT EXISTS content_items (
    content_item_id TEXT PRIMARY KEY,
    content_type    TEXT NOT NULL,
    url_key         TEXT UNIQUE,
    canonical_url   TEXT,
    title           TEXT,
    publisher       TEXT,
    published_at    TEXT,
    language        TEXT,
    entity_id       TEXT REFERENCES entities(entity_id) ON DELETE SET NULL,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 특정 플랫폼에서 특정 시점에 본 상태.
-- 절대 덮어쓰지 않는다. 같은 (source_id, platform_item_id, observed_at) 재수집도 새 행이다.
-- 그래서 UNIQUE 제약을 두지 않는다 (DESIGN §10.2).
CREATE TABLE IF NOT EXISTS source_observations (
    observation_id   TEXT PRIMARY KEY,
    content_item_id  TEXT NOT NULL REFERENCES content_items(content_item_id) ON DELETE CASCADE,
    source_id        TEXT NOT NULL,
    platform         TEXT NOT NULL,
    platform_item_id TEXT,
    observed_at      TEXT NOT NULL,
    url              TEXT,
    payload_json     TEXT,
    snapshot_id      TEXT REFERENCES raw_snapshots(snapshot_id) ON DELETE SET NULL,
    query_run_id     TEXT REFERENCES query_runs(query_run_id) ON DELETE SET NULL,
    research_id      TEXT,
    created_at       TEXT NOT NULL
);

-- 값·단위·기간·지역·모집단·측정 방법. append-only 다 (DESIGN §10.3).
-- 같은 지표의 후속 관측은 UPDATE 가 아니라 INSERT 다.
CREATE TABLE IF NOT EXISTS metrics (
    metric_row_id   TEXT PRIMARY KEY,
    metric_name     TEXT NOT NULL,
    value_num       REAL,
    value_text      TEXT,
    unit            TEXT,
    denominator     TEXT,
    geography       TEXT,
    period          TEXT,
    population      TEXT,
    method          TEXT,
    -- 상대 지수를 절대 수치로 표현하는 것을 막기 위한 필수 표기 (DESIGN §6.3).
    index_type      TEXT NOT NULL CHECK (index_type IN ('absolute', 'relative')),
    source_id       TEXT NOT NULL,
    platform        TEXT,
    content_item_id TEXT REFERENCES content_items(content_item_id) ON DELETE SET NULL,
    entity_id       TEXT REFERENCES entities(entity_id) ON DELETE SET NULL,
    observation_id  TEXT REFERENCES source_observations(observation_id) ON DELETE SET NULL,
    research_id     TEXT,
    observed_at     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    CHECK (value_num IS NOT NULL OR value_text IS NOT NULL)
);

-- 불변 원본 응답·페이지 메타데이터·해시 (DESIGN §10.4).
-- 정책상 원본 저장이 제한되면 body 를 비우고 메타데이터·URL·해시만 남긴다.
CREATE TABLE IF NOT EXISTS raw_snapshots (
    snapshot_id  TEXT PRIMARY KEY,
    hash         TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    url          TEXT,
    media_type   TEXT,
    body         TEXT,
    body_stored  INTEGER NOT NULL,
    storage_policy TEXT,
    meta_json    TEXT,
    query        TEXT,
    collected_at TEXT NOT NULL,
    expires_at   TEXT,
    deleted_at   TEXT,
    UNIQUE (source_id, hash)
);

-- Evidence Pack 의 사실 주장
CREATE TABLE IF NOT EXISTS evidence_claims (
    claim_id         TEXT PRIMARY KEY,
    research_id      TEXT NOT NULL REFERENCES research_runs(research_id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    evidence_class   TEXT NOT NULL,
    corroboration    TEXT NOT NULL,
    scope            TEXT,
    limitations_json TEXT,
    created_at       TEXT NOT NULL
);

-- 주장과 근거의 연결
CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id      TEXT NOT NULL REFERENCES evidence_claims(claim_id) ON DELETE CASCADE,
    evidence_id   TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id)
);

-- 누락·차단·충돌·추가 조사 항목. 차단된 소스를 숨기지 않는다 (DESIGN §13.7).
CREATE TABLE IF NOT EXISTS research_gaps (
    gap_id      TEXT PRIMARY KEY,
    research_id TEXT NOT NULL REFERENCES research_runs(research_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL,
    lane        TEXT,
    source_id   TEXT,
    pack_id     TEXT,
    next_action TEXT,
    created_at  TEXT NOT NULL
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_observations_content_platform_time
    ON source_observations (content_item_id, platform, observed_at);
CREATE INDEX IF NOT EXISTS idx_metrics_name_time
    ON metrics (metric_name, observed_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_hash
    ON raw_snapshots (hash);

CREATE INDEX IF NOT EXISTS idx_observations_source_time
    ON source_observations (source_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_query_runs_research
    ON query_runs (research_id, source_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_expiry
    ON raw_snapshots (expires_at, deleted_at);
CREATE INDEX IF NOT EXISTS idx_gaps_research
    ON research_gaps (research_id, kind);
"""

_SCHEMA_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
"""

_MIGRATION_NAME = re.compile(r"^(\d{3,})_(.+)\.sql$")


class StoreError(RuntimeError):
    """저장소 초기화·마이그레이션 실패."""


class Store:
    """SQLite 연결 한 벌. 컨텍스트 매니저로 쓴다."""

    def __init__(self, db_path: Path | str | None = None, *, initialize: bool = True) -> None:
        self.db_path = Path(db_path) if db_path is not None else get_config().db_path
        if self.db_path.parent and str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(self.db_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

        if initialize:
            self.initialize()

    # -- 수명 -----------------------------------------------------------
    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """실패하면 통째로 되돌린다. 반쯤 적재된 관측을 남기지 않는다."""
        self.connection.execute("BEGIN")
        try:
            yield self.connection
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    # -- 스키마 ---------------------------------------------------------
    def initialize(self) -> None:
        """기준 스키마를 만들고 migrations 디렉터리의 SQL 을 순서대로 올린다."""
        self.connection.executescript(_SCHEMA_VERSION_SQL)
        self.connection.executescript(_SCHEMA_SQL)
        self.connection.executescript(_INDEX_SQL)
        self._record_version(BASELINE_SCHEMA_VERSION, "baseline schema (ria/core/store.py)")
        self.apply_migrations()

    def apply_migrations(self, directory: Path | str | None = None) -> list[int]:
        """`NNN_요약.sql` 을 번호 순으로 적용한다. 이미 적용된 번호는 건너뛴다."""
        path = Path(directory) if directory is not None else MIGRATIONS_DIR
        if not path.is_dir():
            return []

        applied = self.applied_versions()
        new_versions: list[int] = []

        for file in sorted(path.glob("*.sql")):
            match = _MIGRATION_NAME.match(file.name)
            if match is None:
                raise StoreError(f"마이그레이션 파일 이름이 규칙에 맞지 않는다: {file.name}")
            version = int(match.group(1))
            if version <= BASELINE_SCHEMA_VERSION:
                raise StoreError(
                    f"{file.name} 의 번호가 기준 스키마 번호({BASELINE_SCHEMA_VERSION}) 이하다"
                )
            if version in applied:
                continue
            # executescript 는 대기 중인 트랜잭션을 먼저 커밋한다. 그래서 BEGIN 으로
            # 감싸지 않고, 각 마이그레이션 SQL 이 자체적으로 원자성을 갖게 둔다.
            self.connection.executescript(file.read_text(encoding="utf-8"))
            self._record_version(version, match.group(2))
            new_versions.append(version)

        return new_versions

    def applied_versions(self) -> set[int]:
        rows = self.connection.execute("SELECT version FROM schema_version").fetchall()
        return {row["version"] for row in rows}

    def schema_version(self) -> int:
        """적용된 가장 높은 스키마 번호."""
        row = self.connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row["name"] for row in rows}

    def index_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IS NOT NULL"
        ).fetchall()
        return {row["name"] for row in rows}

    def _record_version(self, version: int, description: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_version (version, description, applied_at) "
            "VALUES (?, ?, ?)",
            (version, description, to_iso8601(now())),
        )


def open_store(db_path: Path | str | None = None) -> Store:
    """설정된 경로의 저장소를 연다."""
    return Store(db_path)
