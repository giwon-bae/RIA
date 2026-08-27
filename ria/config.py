"""설정 로드 — `.env` · DB 경로 · 소스별 자격증명 · TTL · 쿼터 기본값.

경로·한도·TTL 을 코드 곳곳에 하드코딩하지 않는다. 전부 이 모듈을 경유한다.

API 키는 `.env` 에서만 로드한다 (지시서 §3). AI 호출용 키는 존재하지 않는다 —
RIA Core 는 모델을 호출하지 않는다 (DESIGN §3.4).

테스트 주의: v1 에서 테스트가 `.env` 값으로 폴백해 라이브 호출이 나갈 수 있었다.
그래서 `load_config()` 는 `environ` 을 인자로 받고 `override_config()` 로 전역 캐시를
갈아끼울 수 있게 해 둔다. 테스트는 이 경로로 config 를 차단해 `.env` 유무와 무관하게 통과한다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

# --- 기준 타임존 -----------------------------------------------------------
# 시각은 전부 timezone-aware 로 다루고 저장은 ISO8601 로 한다 (지시서 §3).
TIMEZONE_NAME = "Asia/Seoul"
KST = ZoneInfo(TIMEZONE_NAME)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "ria.db"
SOURCES_YAML_PATH = PACKAGE_ROOT / "policy" / "sources.yaml"
MIGRATIONS_DIR = PACKAGE_ROOT / "migrations"

ENV_DB_PATH = "RIA_DB_PATH"

# --- 소스별 자격증명 env 키 (지시서 §8 표가 정본) ---------------------------
# source_id -> 그 소스를 호출하는 데 필요한 env 키 전부.
# 값이 비어 있는 소스(hacker_news · world_bank 등)는 키가 필요 없다는 뜻이다.
SOURCE_CREDENTIAL_KEYS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "kosis": ("RIA_KOSIS_API_KEY",),
        "data_go_kr": ("RIA_DATA_GO_KR_KEY",),
        "opendart": ("RIA_OPENDART_API_KEY",),
        "world_bank": (),
        "naver_search": ("RIA_NAVER_CLIENT_ID", "RIA_NAVER_CLIENT_SECRET"),
        "naver_datalab": ("RIA_NAVER_CLIENT_ID", "RIA_NAVER_CLIENT_SECRET"),
        "naver_shopping_insight": ("RIA_NAVER_CLIENT_ID", "RIA_NAVER_CLIENT_SECRET"),
        "naver_shopping_search": (),
        "google_trends": (),
        "hacker_news": (),
        "hn_algolia": (),
        "product_hunt": (),
        "reddit": (
            "RIA_REDDIT_CLIENT_ID",
            "RIA_REDDIT_CLIENT_SECRET",
            "RIA_REDDIT_USER_AGENT",
        ),
        "threads": (
            "RIA_THREADS_APP_ID",
            "RIA_THREADS_APP_SECRET",
            "RIA_THREADS_ACCESS_TOKEN",
        ),
        "youtube_data": ("RIA_YOUTUBE_API_KEY",),
        "google_play": (),
        "app_store": (),
        "coupang_seller": (),
        "coupang_partners": (),
        "x_twitter": (),
    }
)

ALL_CREDENTIAL_KEYS: tuple[str, ...] = tuple(
    sorted({key for keys in SOURCE_CREDENTIAL_KEYS.values() for key in keys})
)

# --- 정책 확인일(last_verified_at) TTL 기본값 (DESIGN §8.2 권장값) ----------
DEFAULT_POLICY_TTL_DAYS = 30

POLICY_TTL_DAYS: Mapping[str, int] = MappingProxyType(
    {
        # 30일 또는 주요 작업 착수 전
        "reddit": 30,
        "threads": 30,
        "naver_search": 30,
        "naver_datalab": 30,
        "naver_shopping_insight": 30,
        "naver_shopping_search": 30,
        "product_hunt": 30,
        # 90일 또는 구현 착수 전
        "youtube_data": 90,
        "google_play": 90,
        "app_store": 90,
        "coupang_seller": 90,
        "coupang_partners": 90,
        # 180일 (개별 데이터셋 약관은 호출 전 확인)
        "kosis": 180,
        "opendart": 180,
        "data_go_kr": 180,
    }
)

# --- 소스별 쿼터 모델 기본값 ------------------------------------------------
# 공식 문서에 수치가 있는 것만 적는다. 수치가 없으면 None 이고, 그때는
# 응답 헤더(rate_limit_model: server_headers)를 따른다. 추측값을 넣지 않는다.
DEFAULT_MAX_CALLS_PER_RUN = 50


@dataclass(frozen=True)
class SourceQuota:
    """소스별 쿼터 기본값. `limit` 이 None 이면 공식 수치가 공개되지 않은 것이다."""

    limit: int | None
    window_hours: int | None
    scope: str
    note: str = ""


SOURCE_QUOTAS: Mapping[str, SourceQuota] = MappingProxyType(
    {
        # 사용자당 24시간 2,200 쿼리. 앱 단위가 아니라 앱 간 합산이며
        # 0건 응답은 차감되지 않는다 (DESIGN §7 Threads 행).
        "threads": SourceQuota(
            limit=2200,
            window_hours=24,
            scope="per_user",
            note="앱 간 합산. 0건 응답은 미차감. limit 파라미터 최대 100.",
        ),
        # 공식 문서에 수치가 없다. X-Ratelimit-* 응답 헤더를 읽어 처리한다.
        "reddit": SourceQuota(
            limit=None,
            window_hours=None,
            scope="per_client",
            note="공식 수치 미기재 — X-Ratelimit-* 응답 헤더 기준.",
        ),
        # 공식 API 자체에는 현재 rate limit 이 없다 (DESIGN §7 Hacker News 행).
        "hacker_news": SourceQuota(
            limit=None,
            window_hours=None,
            scope="none",
            note="공식 Firebase API 에 명시된 rate limit 없음. 예의상 호출량만 제한한다.",
        ),
    }
)


class ConfigError(RuntimeError):
    """설정 자체가 잘못됐을 때."""


class MissingCredentialError(ConfigError):
    """필요한 자격증명이 `.env` 에 없을 때. 호출 전에 조기 실패시킨다."""

    def __init__(self, keys: tuple[str, ...], source_id: str | None = None) -> None:
        self.keys = keys
        self.source_id = source_id
        where = f" (source: {source_id})" if source_id else ""
        joined = ", ".join(keys)
        super().__init__(
            f"자격증명이 설정되지 않았다{where}: {joined}. "
            f".env 에 값을 채워라 (.env.example 참고). 키를 로그·EvidencePack 에 넣지 않는다."
        )


@dataclass(frozen=True)
class Config:
    """RIA Core 런타임 설정. 불변이며 테스트에서는 교체해서 쓴다."""

    db_path: Path
    sources_yaml_path: Path = SOURCES_YAML_PATH
    migrations_dir: Path = MIGRATIONS_DIR
    timezone_name: str = TIMEZONE_NAME
    default_policy_ttl_days: int = DEFAULT_POLICY_TTL_DAYS
    default_max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    policy_ttl_days: Mapping[str, int] = field(default_factory=lambda: POLICY_TTL_DAYS)
    quotas: Mapping[str, SourceQuota] = field(default_factory=lambda: SOURCE_QUOTAS)
    credentials: Mapping[str, str] = field(default_factory=dict)

    # -- 자격증명 --------------------------------------------------------
    def credential(self, key: str) -> str | None:
        """env 키 하나를 읽는다. 없으면 None."""
        value = self.credentials.get(key)
        return value or None

    def require_credential(self, key: str) -> str:
        """env 키 하나를 읽되 없으면 `MissingCredentialError` 로 조기 실패한다."""
        value = self.credential(key)
        if value is None:
            raise MissingCredentialError((key,))
        return value

    def credentials_for(self, source_id: str) -> dict[str, str | None]:
        """소스가 요구하는 env 키를 전부 읽는다. 없는 값은 None 으로 남긴다."""
        return {key: self.credential(key) for key in SOURCE_CREDENTIAL_KEYS.get(source_id, ())}

    def missing_credentials_for(self, source_id: str) -> tuple[str, ...]:
        """소스가 요구하지만 비어 있는 env 키 목록."""
        return tuple(key for key, value in self.credentials_for(source_id).items() if value is None)

    def has_credentials(self, source_id: str) -> bool:
        """소스 호출에 필요한 키가 전부 있는가. 키가 필요 없는 소스는 항상 True."""
        return not self.missing_credentials_for(source_id)

    def require_credentials_for(self, source_id: str) -> dict[str, str]:
        """소스가 요구하는 키를 전부 읽되 하나라도 없으면 조기 실패한다."""
        missing = self.missing_credentials_for(source_id)
        if missing:
            raise MissingCredentialError(missing, source_id=source_id)
        return {k: v for k, v in self.credentials_for(source_id).items() if v is not None}

    # -- 정책 TTL --------------------------------------------------------
    def policy_ttl_for(self, source_id: str) -> int:
        """소스별 `last_verified_at` TTL(일). 표에 없으면 기본값."""
        return self.policy_ttl_days.get(source_id, self.default_policy_ttl_days)

    def policy_expires_on(self, source_id: str, last_verified_at: date) -> date:
        """정책 확인일 + TTL. 이 날짜를 지나면 재확인 없이 호출하지 않는다."""
        return last_verified_at + timedelta(days=self.policy_ttl_for(source_id))

    # -- 쿼터 ------------------------------------------------------------
    def quota_for(self, source_id: str) -> SourceQuota | None:
        """소스별 쿼터 기본값. 공식 수치가 없으면 None 이거나 limit=None 이다."""
        return self.quotas.get(source_id)


# --- 시각 헬퍼 -------------------------------------------------------------
def now() -> datetime:
    """기준 타임존(Asia/Seoul) 기준 현재 시각. timezone-aware 다."""
    return datetime.now(tz=KST)


def today() -> date:
    """기준 타임존 기준 오늘 날짜."""
    return now().date()


def to_iso8601(value: datetime) -> str:
    """저장용 ISO8601 문자열. naive datetime 은 거부한다."""
    if value.tzinfo is None:
        raise ConfigError(f"naive datetime 은 저장하지 않는다: {value!r}")
    return value.isoformat()


def parse_iso8601(value: str) -> datetime:
    """ISO8601 문자열을 timezone-aware datetime 으로. 오프셋이 없으면 기준 타임존으로 본다."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed


# --- 로드 ------------------------------------------------------------------
def load_config(
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    use_dotenv: bool = True,
) -> Config:
    """설정을 만든다.

    우선순위는 프로세스 환경변수 > `.env` 파일이다. `.env` 는 프로세스 환경을
    덮어쓰지 않는다.

    `use_dotenv=False` 이면 `.env` 를 아예 읽지 않는다. 테스트는 이 경로를 쓴다.
    `os.environ` 을 전역으로 오염시키지 않으려고 `dotenv_values` 를 쓰고
    `load_dotenv` 는 쓰지 않는다.
    """
    process_env: Mapping[str, str] = os.environ if environ is None else environ

    file_env: dict[str, str | None] = {}
    if use_dotenv:
        path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
        if path.is_file():
            file_env = dotenv_values(path)

    def read(key: str) -> str | None:
        value = process_env.get(key)
        if value is None:
            value = file_env.get(key)
        if value is None:
            return None
        value = value.strip()
        return value or None

    raw_db_path = read(ENV_DB_PATH)
    db_path = Path(raw_db_path).expanduser() if raw_db_path else DEFAULT_DB_PATH
    if not db_path.is_absolute():
        db_path = (PROJECT_ROOT / db_path).resolve()

    credentials = {key: value for key in ALL_CREDENTIAL_KEYS if (value := read(key)) is not None}

    return Config(db_path=db_path, credentials=credentials)


_config: Config | None = None


def get_config() -> Config:
    """전역 설정. 처음 호출할 때 한 번 로드하고 캐시한다."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def override_config(config: Config | None) -> None:
    """전역 설정을 갈아끼운다. 테스트가 `.env` 를 차단하는 경로다.

    `None` 을 주면 캐시를 비워 다음 `get_config()` 에서 다시 로드한다.
    """
    global _config
    _config = config
