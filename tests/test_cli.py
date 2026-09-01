"""A-12 + B-11. 정책·수집·조회 CLI."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from ria.cli import build_parser, main
from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    GuardedCollector,
)
from ria.config import KST, Config
from ria.core.entities import ContentItemInput, upsert_content_item
from ria.core.metrics import MetricInput, record_metric
from ria.core.observations import ObservationInput, record_observation
from ria.core.snapshots import SnapshotInput, store_snapshot
from ria.core.store import Store
from ria.packs import PackRunner
from ria.policy.guard import PolicyAllowed
from ria.policy.registry import SourceRegistry

OBSERVED_AT = datetime(2026, 9, 1, 21, 0, tzinfo=KST)


class _FixtureWorldBankCollector(GuardedCollector):
    source_id = "world_bank"

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy, options
        content_ref = "content:world_bank:cli"
        observation_ref = "observation:world_bank:cli"
        return CollectedBatch(
            contents=(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        url="https://data.worldbank.org/indicator/fixture",
                        title=query,
                        publisher="World Bank",
                    ),
                ),
            ),
            observations=(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id=self.source_id,
                    platform="world_bank",
                    platform_item_id="KOR:2026",
                    observed_at=OBSERVED_AT,
                ),
            ),
            metrics=(
                CollectedMetric(
                    content_ref=content_ref,
                    observation_ref=observation_ref,
                    metric=MetricInput(
                        metric_name="fixture_metric",
                        value=42,
                        index_type="absolute",
                        source_id=self.source_id,
                        observed_at=OBSERVED_AT,
                    ),
                ),
            ),
        )


def run(argv: list[str]) -> int:
    return main(argv)


# --- source list ------------------------------------------------------------
def test_source_list_prints_twenty_rows(capsys: pytest.CaptureFixture[str]) -> None:
    """S1 종료 게이트: `ria source list` → 20건."""
    assert run(["source", "list"]) == 0

    output = capsys.readouterr().out

    assert "20건" in output
    assert "reddit" in output
    assert "naver_shopping_search" in output


def test_source_list_json_has_twenty_entries(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["source", "list", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)

    assert len(payload) == 20
    assert {entry["source_id"] for entry in payload} >= {"reddit", "threads", "hacker_news"}


def test_source_list_json_shows_policy_expiry_and_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run(["source", "list", "--json"])
    payload = {e["source_id"]: e for e in json.loads(capsys.readouterr().out)}

    assert payload["reddit"]["policy_ttl_days"] == 30
    assert payload["reddit"]["policy_expires_on"] == "2026-09-26"
    assert payload["reddit"]["credentials_present"] is False
    assert "RIA_REDDIT_CLIENT_ID" in payload["reddit"]["missing_credentials"]
    assert payload["hacker_news"]["credentials_present"] is True


def test_source_list_filters_by_pack(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "list", "--pack", "community-signal", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert [entry["source_id"] for entry in payload] == ["reddit", "threads", "x_twitter"]


def test_source_list_filters_by_status(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "list", "--status", "experimental", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert {entry["source_id"] for entry in payload} == {
        "google_trends",
        "google_play",
        "app_store",
    }


def test_table_columns_align_with_wide_characters(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "list"])
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("SOURCE_ID")
    assert set(lines[1]) <= {"-", " "}


# --- source check -----------------------------------------------------------
def test_source_check_reddit_reports_blocked_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """S1 종료 게이트: `ria source check reddit` → blocked 사유 출력, 실호출 0."""
    assert run(["source", "check", "reddit"]) == 0

    output = capsys.readouterr().out

    assert "BLOCKED" in output
    assert "access_status_not_allowed" in output
    assert "실호출은 없었다" in output
    assert "https://redditinc.com/policies/data-api-terms" in output


def test_source_check_reddit_json(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "check", "reddit", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["allowed"] is False
    assert payload["reason"] == "access_status_not_allowed"
    assert payload["check"] == "access_status"
    assert payload["source_id"] == "reddit"


def test_source_check_allows_open_source(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "check", "hacker_news", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["allowed"] is True
    assert payload["max_calls"] is None


def test_source_check_honours_non_commercial_flag(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "check", "hn_algolia", "--json"])
    commercial = json.loads(capsys.readouterr().out)

    run(["source", "check", "hn_algolia", "--non-commercial", "--json"])
    non_commercial = json.loads(capsys.readouterr().out)

    assert commercial["allowed"] is False
    assert non_commercial["allowed"] is True


def test_source_check_honours_as_of(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "check", "hacker_news", "--as-of", "2027-01-01", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["allowed"] is False
    assert payload["reason"] == "policy_verification_expired"


def test_source_check_honours_call_count(capsys: pytest.CaptureFixture[str]) -> None:
    run(["source", "check", "world_bank", "--calls", "10000", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["allowed"] is False
    assert payload["reason"] == "request_exceeds_rate_limit"


def test_source_check_unknown_source_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["source", "check", "mastodon", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["reason"] == "unknown_source"


def test_bad_as_of_is_reported_as_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["source", "check", "reddit", "--as-of", "2026/08/27"]) == 1

    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert run([]) == 0

    assert "usage: ria" in capsys.readouterr().out


def test_parser_exposes_s2_commands_but_not_s3_export() -> None:
    parser = build_parser()

    assert parser.parse_args(["collect", "authority-stats", "population"]).command == "collect"
    assert parser.parse_args(["query", "observations"]).query_command == "observations"
    assert parser.parse_args(["query", "metrics", "views"]).query_command == "metrics"
    assert parser.parse_args(["snapshot", "get", "snap_1"]).snapshot_command == "get"
    with pytest.raises(SystemExit):
        parser.parse_args(["evidence", "export", "r-1"])


def test_cli_uses_injected_registry(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """테스트가 레지스트리를 갈아끼울 수 있어야 한다."""
    import shutil

    from ria.config import SOURCES_YAML_PATH

    copy = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, copy)
    registry = SourceRegistry(copy)
    registry.set_access_status("reddit", "core", date(2026, 8, 27))

    parser = build_parser()
    args = parser.parse_args(["source", "check", "reddit", "--json"])
    args.registry = registry
    args.config = Config(
        db_path=Path("/tmp/x.db"),
        credentials={
            "RIA_REDDIT_CLIENT_ID": "id",
            "RIA_REDDIT_CLIENT_SECRET": "secret",
            "RIA_REDDIT_USER_AGENT": "ua",
        },
    )

    assert args.handler(args) == 0
    assert json.loads(capsys.readouterr().out)["allowed"] is True


# --- collect ---------------------------------------------------------------
def test_collect_source_uses_runner_and_reports_persisted_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["collect", "world_bank", "fixture", "--json"])
    registry = SourceRegistry()
    config = Config(db_path=Path(":memory:"), credentials={})

    with Store(":memory:") as store:
        collector = _FixtureWorldBankCollector(registry=registry, config=config)
        args.registry = registry
        args.config = config
        args.store = store
        args.runner = PackRunner(
            store,
            registry=registry,
            config=config,
            collectors={"world_bank": collector},
        )

        assert args.handler(args) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["status"] == "completed"
        assert payload["result_count"] == 1
        assert payload["persisted"] == {
            "content": 1,
            "metric": 1,
            "observation": 1,
            "snapshot": 0,
        }
        assert payload["query_run_id"].startswith("qry_")
        assert store.connection.execute("SELECT COUNT(*) FROM query_runs").fetchone()[0] == 1


def test_collect_rejects_injected_runner_with_different_registry() -> None:
    parser = build_parser()
    args = parser.parse_args(["collect", "world_bank", "fixture", "--json"])
    registry = SourceRegistry()
    other_registry = SourceRegistry()
    config = Config(db_path=Path(":memory:"), credentials={})

    with Store(":memory:") as store:
        args.registry = registry
        args.config = config
        args.store = store
        args.runner = PackRunner(store, registry=other_registry, config=config)
        with pytest.raises(ValueError, match="registry/config"):
            args.handler(args)


def test_collect_pack_continues_after_credential_gaps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["collect", "authority-stats", "fixture", "--json"])
    registry = SourceRegistry()
    config = Config(db_path=Path(":memory:"), credentials={})

    with Store(":memory:") as store:
        collector = _FixtureWorldBankCollector(registry=registry, config=config)
        args.registry = registry
        args.config = config
        args.store = store
        args.runner = PackRunner(
            store,
            registry=registry,
            config=config,
            collectors={"world_bank": collector},
        )

        assert args.handler(args) == 0
        payload = json.loads(capsys.readouterr().out)

        assert payload["pack_id"] == "authority-stats"
        assert payload["result_count"] == 1
        assert payload["registry_rows_synced"] == 20
        assert [run["status"] for run in payload["source_runs"]] == [
            "completed",
            "blocked",
            "blocked",
        ]
        assert payload["gap_count"] == 2


@pytest.mark.parametrize("source_id", ["reddit", "threads"])
def test_collect_gated_platform_is_blocked_with_one_gap_and_no_network(
    source_id: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / f"{source_id}.db"
    default_db_path = tmp_path / "ria-test.db"
    research_id = f"research_{source_id}"
    with Store(db_path) as store:
        store.connection.execute(
            "INSERT INTO research_runs (research_id, decision_question, business_domain,"
            " brief_json, status, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (
                research_id,
                "fixture",
                "fixture",
                "running",
                OBSERVED_AT.isoformat(),
                OBSERVED_AT.isoformat(),
            ),
        )

    assert (
        run(
            [
                "collect",
                source_id,
                "fixture",
                "--db",
                str(db_path),
                "--research-id",
                research_id,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "blocked"
    assert payload["result_count"] == 0
    assert payload["gap_count"] == 1
    assert payload["gaps"][0]["kind"] == "policy_blocked"
    with Store(db_path) as store:
        assert store.connection.execute("SELECT COUNT(*) FROM research_gaps").fetchone()[0] == 1
        assert store.connection.execute("SELECT status FROM query_runs").fetchone()[0] == "blocked"
    assert not default_db_path.exists()


def test_collect_web_primary_explains_storage_only_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["collect", "web-primary", "fixture"]) == 1

    error = capsys.readouterr().err
    assert "Core 실행 Pack이 아니다" in error
    assert "store_web_snapshot" in error


def test_collect_rejects_credentials_in_options_without_echoing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "DO-NOT-PRINT-CLI-SECRET"

    assert (
        run(
            [
                "collect",
                "world_bank",
                "fixture",
                "--options-json",
                json.dumps({"api_key": sentinel}),
            ]
        )
        == 1
    )

    error = capsys.readouterr().err
    assert "자격증명을 받지 않는다" in error
    assert sentinel not in error


def test_collect_rejects_wrong_option_shape_and_unknown_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["collect", "world_bank", "fixture", "--options-json", "[]"]) == 1
    assert "JSON object" in capsys.readouterr().err

    assert run(["collect", "unknown", "fixture"]) == 1
    assert "등록되지 않은 Pack/source" in capsys.readouterr().err


def test_collect_failed_source_and_pack_return_nonzero_after_writing_audit(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "failed-source.db"
    bad_options = json.dumps({"max_pages": 0})

    assert (
        run(
            [
                "collect",
                "world_bank",
                "SP.POP.TOTL",
                "--db",
                str(source_db),
                "--options-json",
                bad_options,
                "--json",
            ]
        )
        == 1
    )
    source_payload = json.loads(capsys.readouterr().out)
    assert source_payload["status"] == "failed"
    with Store(source_db) as store:
        assert store.connection.execute("SELECT status FROM query_runs").fetchone()[0] == "failed"

    assert (
        run(
            [
                "collect",
                "authority-stats",
                "SP.POP.TOTL",
                "--db",
                str(tmp_path / "failed-pack.db"),
                "--source-options-json",
                json.dumps({"world_bank": {"max_pages": 0}}),
                "--json",
            ]
        )
        == 1
    )
    pack_payload = json.loads(capsys.readouterr().out)
    assert pack_payload["status"] == "completed_with_failures"
    assert [run["status"] for run in pack_payload["source_runs"]] == [
        "failed",
        "blocked",
        "blocked",
    ]


@pytest.mark.parametrize(
    ("source_id", "dataset"),
    [
        (
            "kosis",
            {
                "org_id": "official-org",
                "table_id": "official-table",
                "object_l1": "official-object",
                "item_id": "official-item",
                "period_type": "Y",
            },
        ),
        (
            "data_go_kr",
            {
                "dataset_id": "approved-dataset",
                "endpoint": "https://apis.data.go.kr/example",
                "policy_url": "https://www.data.go.kr/example-policy",
                "items_path": ["response", "body", "items"],
                "approved": True,
                "storage_allowed": True,
            },
        ),
    ],
)
def test_collect_accepts_explicit_dataset_specs_but_guard_runs_before_http(
    source_id: str,
    dataset: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert (
        run(
            [
                "collect",
                source_id,
                "fixture",
                "--db",
                str(tmp_path / f"{source_id}.db"),
                "--options-json",
                json.dumps({"dataset": dataset}),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["policy"]["reason"] == "missing_credential"
    assert payload["gap_count"] == 1


@pytest.mark.parametrize(
    ("source_id", "credentials", "dataset", "message"),
    [
        (
            "kosis",
            {"RIA_KOSIS_API_KEY": "fixture-key"},
            {
                "org_id": "org",
                "table_id": "table",
                "object_l1": "object",
                "item_id": "item",
                "period_type": "Y",
                "latest_count": True,
            },
            "latest_count",
        ),
        (
            "data_go_kr",
            {"RIA_DATA_GO_KR_KEY": "fixture-key"},
            {
                "dataset_id": "dataset",
                "endpoint": "https://apis.data.go.kr/example",
                "policy_url": "https://www.data.go.kr/policy",
                "items_path": ["response", "items"],
                "approved": "false",
                "storage_allowed": "false",
            },
            "approved",
        ),
        (
            "data_go_kr",
            {"RIA_DATA_GO_KR_KEY": "fixture-key"},
            {
                "dataset_id": "dataset",
                "endpoint": "https://apis.data.go.kr/example",
                "policy_url": "https://www.data.go.kr/policy",
                "items_path": "response.items",
                "approved": True,
                "storage_allowed": True,
            },
            "items_path",
        ),
    ],
)
def test_collect_strictly_validates_dataset_json_before_http(
    source_id: str,
    credentials: dict[str, str],
    dataset: dict[str, Any],
    message: str,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "collect",
            source_id,
            "fixture",
            "--options-json",
            json.dumps({"dataset": dataset}),
            "--json",
        ]
    )
    registry = SourceRegistry()
    config = Config(db_path=Path(":memory:"), credentials=credentials)
    args.registry = registry
    args.config = config

    with Store(":memory:") as store:
        args.store = store
        with pytest.raises(ValueError, match=message):
            args.handler(args)


# --- query / snapshot ------------------------------------------------------
def test_query_observations_and_metrics_use_safe_filters(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "query.db"
    with Store(db_path) as store:
        content_id = upsert_content_item(
            store,
            ContentItemInput(
                content_type="article",
                url="https://example.test/item",
                title="fixture",
                publisher="fixture",
            ),
            now=OBSERVED_AT,
        )
        observation_id = record_observation(
            store,
            ObservationInput(
                content_item_id=content_id,
                source_id="hacker_news",
                platform="hacker_news",
                observed_at=OBSERVED_AT,
                url=("https://example.test/item?token=URL-TOKEN&password=URL-PASSWORD&id=1"),
                payload={
                    "access_token": "BODY-SECRET",
                    "clientSecret": "CAMEL-SECRET",
                    "private_key": "PRIVATE-SECRET",
                    "secret": "PLAIN-SECRET",
                    "score": 10,
                },
            ),
            now=OBSERVED_AT,
        )
        record_metric(
            store,
            MetricInput(
                metric_name="score",
                value=10,
                index_type="absolute",
                source_id="hacker_news",
                observed_at=OBSERVED_AT,
                platform="hacker_news",
                content_item_id=content_id,
                observation_id=observation_id,
            ),
            now=OBSERVED_AT,
        )

    assert (
        run(
            [
                "query",
                "observations",
                "--db",
                str(db_path),
                "--source",
                "hacker_news",
                "--since",
                "2026-09-01T20:00:00+09:00",
                "--limit",
                "1",
                "--json",
            ]
        )
        == 0
    )
    observations = json.loads(capsys.readouterr().out)
    assert len(observations) == 1
    assert observations[0]["payload"]["access_token"] == "REDACTED"
    assert observations[0]["payload"]["clientSecret"] == "REDACTED"
    assert observations[0]["payload"]["private_key"] == "REDACTED"
    assert observations[0]["payload"]["secret"] == "REDACTED"
    assert "URL-TOKEN" not in observations[0]["url"]
    assert "URL-PASSWORD" not in observations[0]["url"]

    assert (
        run(
            [
                "query",
                "metrics",
                "score",
                "--db",
                str(db_path),
                "--platform",
                "hacker_news",
                "--until",
                "2026-09-01T22:00:00+09:00",
                "--json",
            ]
        )
        == 0
    )
    metrics = json.loads(capsys.readouterr().out)
    assert len(metrics) == 1
    assert metrics[0]["metric_name"] == "score"
    assert metrics[0]["observed_at"] == OBSERVED_AT.isoformat()


def test_query_limit_must_be_positive() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["query", "observations", "--limit", "0"])


def test_snapshot_body_is_opt_in_and_sensitive_fields_are_redacted(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.db"
    with Store(db_path) as store:
        saved = store_snapshot(
            store,
            SnapshotInput(
                source_id="hacker_news",
                body={"token": "BODY-SECRET", "value": 3},
                collected_at=OBSERVED_AT,
                url="https://example.test/item?serviceKey=URL-SECRET&id=1",
                media_type="application/json",
                meta={
                    "client_secret": "META-SECRET",
                    "accessToken": "META-CAMEL-SECRET",
                    "private_key": "META-PRIVATE-SECRET",
                },
                query="fixture",
            ),
        )

    base = ["snapshot", "get", saved.snapshot_id, "--db", str(db_path), "--json"]
    assert run(base) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert "body" not in metadata
    assert metadata["meta"]["client_secret"] == "REDACTED"
    assert metadata["meta"]["accessToken"] == "REDACTED"
    assert metadata["meta"]["private_key"] == "REDACTED"
    assert "URL-SECRET" not in metadata["url"]

    assert run([*base, "--include-body"]) == 0
    with_body = json.loads(capsys.readouterr().out)
    assert with_body["body"] == {"token": "REDACTED", "value": 3}


def test_snapshot_get_missing_id_is_an_error(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert run(["snapshot", "get", "snap_missing", "--db", str(tmp_path / "empty.db")]) == 1
    assert "스냅샷을 찾을 수 없다" in capsys.readouterr().err


def test_readme_marks_s2_complete_and_links_privacy_once() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert (
        "| S2 Pack & Collector | 수집 → 정규화 → 저장 관통 | "
        "구현 완료 · 종료 게이트 검증 중 |" in readme
    )
    assert readme.count("[`PRIVACY.md`](PRIVACY.md)") == 1
    assert "collect reddit demand --json" in readme
    assert "fixture + 소켓 차단" in readme
