"""A-12. CLI 뼈대 — source list / source check."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ria.cli import build_parser, main
from ria.config import Config
from ria.config import override_config as set_global_config
from ria.policy.registry import SourceRegistry

CONFIG = Config(db_path=Path("/tmp/ria-cli-test.db"), credentials={})


@pytest.fixture(autouse=True)
def _clean_global_config() -> None:
    """CLI 는 전역 config 를 쓴다. 자격증명 0개로 고정한다."""
    set_global_config(CONFIG)
    yield
    set_global_config(None)


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


def test_parser_exposes_only_the_stage_one_commands() -> None:
    """수집·export 는 S2·S3 범위다. 지금 열어 두지 않는다."""
    parser = build_parser()

    for argv in (["collect", "authority-stats"], ["query", "x"], ["evidence", "export", "r-1"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


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
