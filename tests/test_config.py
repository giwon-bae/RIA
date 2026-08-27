"""A-11. `.env` 로드 · DB 경로 · 자격증명 · TTL · 쿼터 기본값."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from ria.config import (
    ALL_CREDENTIAL_KEYS,
    DEFAULT_DB_PATH,
    KST,
    SOURCE_CREDENTIAL_KEYS,
    Config,
    ConfigError,
    MissingCredentialError,
    load_config,
    parse_iso8601,
    to_iso8601,
)


def test_env_file_is_not_read_when_dotenv_disabled(tmp_path: Path) -> None:
    """테스트 경로에서는 .env 값이 절대 새어 들어오지 않는다."""
    env_file = tmp_path / ".env"
    env_file.write_text("RIA_KOSIS_API_KEY=leaked\n", encoding="utf-8")

    config = load_config(env_file=env_file, environ={}, use_dotenv=False)

    assert config.credential("RIA_KOSIS_API_KEY") is None


def test_env_file_is_read_when_enabled(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RIA_KOSIS_API_KEY=from-file\n", encoding="utf-8")

    config = load_config(env_file=env_file, environ={})

    assert config.credential("RIA_KOSIS_API_KEY") == "from-file"


def test_process_env_wins_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("RIA_KOSIS_API_KEY=from-file\n", encoding="utf-8")

    config = load_config(env_file=env_file, environ={"RIA_KOSIS_API_KEY": "from-process"})

    assert config.credential("RIA_KOSIS_API_KEY") == "from-process"


def test_blank_credential_is_treated_as_missing(tmp_path: Path) -> None:
    """`.env.example` 을 그대로 복사해 값이 비어 있는 경우를 미설정으로 본다."""
    env_file = tmp_path / ".env"
    env_file.write_text("RIA_KOSIS_API_KEY=   \n", encoding="utf-8")

    config = load_config(env_file=env_file, environ={})

    assert config.credential("RIA_KOSIS_API_KEY") is None


def test_db_path_defaults_to_repo_root(tmp_path: Path) -> None:
    config = load_config(env_file=tmp_path / "missing.env", environ={}, use_dotenv=False)

    assert config.db_path == DEFAULT_DB_PATH


def test_db_path_from_env_is_absolute(tmp_path: Path) -> None:
    config = load_config(
        environ={"RIA_DB_PATH": str(tmp_path / "custom.db")},
        use_dotenv=False,
    )

    assert config.db_path == tmp_path / "custom.db"
    assert config.db_path.is_absolute()


def test_relative_db_path_is_resolved_against_repo_root() -> None:
    config = load_config(environ={"RIA_DB_PATH": "./local.db"}, use_dotenv=False)

    assert config.db_path.is_absolute()
    assert config.db_path.name == "local.db"


def test_require_credential_fails_early_with_key_name() -> None:
    config = Config(db_path=Path("/tmp/x.db"), credentials={})

    with pytest.raises(MissingCredentialError) as excinfo:
        config.require_credential("RIA_KOSIS_API_KEY")

    assert "RIA_KOSIS_API_KEY" in str(excinfo.value)


def test_require_credentials_for_source_reports_every_missing_key() -> None:
    config = Config(db_path=Path("/tmp/x.db"), credentials={"RIA_NAVER_CLIENT_ID": "id"})

    with pytest.raises(MissingCredentialError) as excinfo:
        config.require_credentials_for("naver_search")

    assert excinfo.value.keys == ("RIA_NAVER_CLIENT_SECRET",)
    assert excinfo.value.source_id == "naver_search"


def test_sources_without_credentials_are_always_satisfied() -> None:
    config = Config(db_path=Path("/tmp/x.db"), credentials={})

    assert config.has_credentials("world_bank") is True
    assert config.has_credentials("hacker_news") is True
    assert config.require_credentials_for("world_bank") == {}


def test_credential_keys_cover_every_env_example_entry() -> None:
    """`.env.example` 과 SOURCE_CREDENTIAL_KEYS 가 어긋나면 키 목록이 소실된다."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    declared = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }

    assert set(ALL_CREDENTIAL_KEYS) | {"RIA_DB_PATH"} == declared


def test_no_ai_credential_keys_are_declared() -> None:
    """RIA Core 는 모델을 호출하지 않으므로 AI 호출용 키가 있어서는 안 된다."""
    forbidden = ("OPENAI", "ANTHROPIC", "GEMINI", "CLAUDE")
    assert not [k for k in ALL_CREDENTIAL_KEYS if any(f in k.upper() for f in forbidden)]


@pytest.mark.parametrize(
    ("source_id", "expected"),
    [
        ("reddit", 30),
        ("threads", 30),
        ("naver_datalab", 30),
        ("product_hunt", 30),
        ("youtube_data", 90),
        ("google_play", 90),
        ("app_store", 90),
        ("coupang_seller", 90),
        ("kosis", 180),
        ("opendart", 180),
        ("data_go_kr", 180),
    ],
)
def test_policy_ttl_matches_design_recommendation(source_id: str, expected: int) -> None:
    config = Config(db_path=Path("/tmp/x.db"))

    assert config.policy_ttl_for(source_id) == expected


def test_unknown_source_falls_back_to_default_ttl() -> None:
    config = Config(db_path=Path("/tmp/x.db"))

    assert config.policy_ttl_for("does_not_exist") == config.default_policy_ttl_days


def test_policy_expires_on_adds_ttl() -> None:
    config = Config(db_path=Path("/tmp/x.db"))

    assert config.policy_expires_on("reddit", date(2026, 8, 27)) == date(2026, 8, 27) + timedelta(
        days=30
    )


def test_threads_quota_is_2200_per_user_per_24h() -> None:
    config = Config(db_path=Path("/tmp/x.db"))
    quota = config.quota_for("threads")

    assert quota is not None
    assert (quota.limit, quota.window_hours, quota.scope) == (2200, 24, "per_user")


def test_reddit_quota_has_no_documented_number() -> None:
    """공식 문서에 수치가 없다. 추측값을 넣으면 안 된다."""
    config = Config(db_path=Path("/tmp/x.db"))
    quota = config.quota_for("reddit")

    assert quota is not None
    assert quota.limit is None


def test_naver_shopping_search_needs_no_credentials() -> None:
    """2026-07-31 서비스 종료. collector 를 만들지 않으므로 키도 없다."""
    assert SOURCE_CREDENTIAL_KEYS["naver_shopping_search"] == ()


def test_to_iso8601_rejects_naive_datetime() -> None:
    with pytest.raises(ConfigError):
        to_iso8601(datetime(2026, 8, 27, 10, 0))


def test_to_iso8601_roundtrip_keeps_timezone() -> None:
    moment = datetime(2026, 8, 27, 10, 0, tzinfo=KST)

    assert parse_iso8601(to_iso8601(moment)) == moment


def test_parse_iso8601_assumes_kst_when_offset_missing() -> None:
    assert parse_iso8601("2026-08-27T10:00:00").tzinfo == KST
