"""A-4. Source Registry 시드 (DESIGN §7 판정표 20건 · §8.1 스키마)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import yaml

from ria.config import SOURCE_CREDENTIAL_KEYS, SOURCE_QUOTAS, SOURCES_YAML_PATH

# DESIGN §8.1 이 필수로 못 박은 필드.
REQUIRED_FIELDS = (
    "source_id",
    "pack_ids",
    "access_status",
    "access_method",
    "official",
    "commercial_use",
    "auth_type",
    "rate_limit_model",
    "storage_policy",
    "deletion_policy",
    "allowed_data_types",
    "blocked_data_types",
    "last_verified_at",
    "verify_before_use",
    "fallback_sources",
    "policy_urls",
)

ACCESS_STATUSES = {"core", "conditional", "experimental", "blocked"}


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return yaml.safe_load(SOURCES_YAML_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sources(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["sources"]


@pytest.fixture(scope="module")
def by_id(sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {source["source_id"]: source for source in sources}


def test_seed_has_twenty_sources(sources: list[dict[str, Any]]) -> None:
    """DESIGN §7 판정표는 20행이다."""
    assert len(sources) == 20


def test_source_ids_are_unique(sources: list[dict[str, Any]]) -> None:
    ids = [source["source_id"] for source in sources]

    assert len(set(ids)) == len(ids)


def test_pack_list_has_ten_entries(document: dict[str, Any]) -> None:
    """DESIGN §5.1 의 Pack 은 10종이다."""
    assert len(document["packs"]) == 10
    assert len(set(document["packs"])) == 10


def test_every_source_has_every_required_field(sources: list[dict[str, Any]]) -> None:
    missing = {
        source["source_id"]: [f for f in REQUIRED_FIELDS if f not in source] for source in sources
    }

    assert {sid: fields for sid, fields in missing.items() if fields} == {}


def test_access_status_uses_defined_vocabulary(sources: list[dict[str, Any]]) -> None:
    assert {source["access_status"] for source in sources} <= ACCESS_STATUSES


def test_pack_ids_reference_declared_packs(document: dict[str, Any]) -> None:
    declared = set(document["packs"])
    used = {pack for source in document["sources"] for pack in source["pack_ids"]}

    assert used <= declared


def test_last_verified_at_parses_as_date(sources: list[dict[str, Any]]) -> None:
    assert all(isinstance(source["last_verified_at"], date) for source in sources)


def test_reddit_matches_design_v21_revision(by_id: dict[str, Any]) -> None:
    """지시서 A-4 가 못 박은 Reddit 필드값."""
    reddit = by_id["reddit"]

    assert reddit["access_status"] == "blocked"
    assert reddit["commercial_use"] == "separate_agreement_required"
    assert reddit["rate_limit_model"] == "server_headers"
    assert reddit["last_verified_at"] == date(2026, 8, 27)
    assert reddit["pack_ids"] == ["community-signal"]
    assert "승인 신청 진행 중" in reddit["notes"]
    assert "승인 시 이 필드만 전환" in reddit["notes"]


def test_threads_matches_design_v21_revision(by_id: dict[str, Any]) -> None:
    """지시서 A-4 가 못 박은 Threads 필드값."""
    threads = by_id["threads"]

    assert threads["access_status"] == "conditional"
    assert threads["auth_type"] == "oauth"
    assert threads["last_verified_at"] == date(2026, 8, 27)
    assert threads["quota"] == {
        "limit": 2200,
        "window_hours": 24,
        "scope": "per_user",
        "note": "앱 간 합산. 0건 응답은 미차감. limit 파라미터 최대 100.",
    }
    assert "미승인 시 본인 게시물만 검색" in threads["notes"]
    assert "App Review 신청 진행 중" in threads["notes"]


def test_naver_shopping_search_is_blocked(by_id: dict[str, Any]) -> None:
    """2026-07-31 서비스 종료. 대체 API 가 없다."""
    source = by_id["naver_shopping_search"]

    assert source["access_status"] == "blocked"
    assert source["fallback_sources"] == []
    assert "2026-07-31" in source["notes"]


def test_relative_index_sources_block_absolute_volume(by_id: dict[str, Any]) -> None:
    """상대 지수를 절대 검색량으로 표현하는 경로를 데이터 타입 수준에서 막는다 (DESIGN §6.3)."""
    assert "absolute_search_volume" in by_id["naver_datalab"]["blocked_data_types"]
    assert "absolute_search_volume" in by_id["google_trends"]["blocked_data_types"]
    assert "absolute_click_volume" in by_id["naver_shopping_insight"]["blocked_data_types"]


def test_youtube_declares_thirty_day_retention(by_id: dict[str, Any]) -> None:
    youtube = by_id["youtube_data"]

    assert youtube["storage_policy"] == "refresh_or_delete_30d"
    assert youtube["deletion_policy"] == "delete_or_refresh_30d"
    assert "cross_platform_merged_metric" in youtube["blocked_data_types"]
    assert "derived_metric" in youtube["blocked_data_types"]


def test_registered_only_sources_are_not_callable(by_id: dict[str, Any]) -> None:
    """App Store · Google Play · Google Trends 는 등록만 하고 실호출 경로를 열지 않는다."""
    for source_id in ("app_store", "google_play", "google_trends"):
        assert by_id[source_id]["access_status"] == "experimental"


def test_every_source_blocks_personal_sensitive_data(sources: list[dict[str, Any]]) -> None:
    assert all("personal_sensitive_data" in s["blocked_data_types"] for s in sources)


def test_fallback_sources_reference_known_source_or_pack(document: dict[str, Any]) -> None:
    """DESIGN §8.1 예시가 source_id 와 pack_id 를 섞어 쓴다. 둘 중 하나여야 한다."""
    known = {s["source_id"] for s in document["sources"]} | set(document["packs"])

    for source in document["sources"]:
        assert set(source["fallback_sources"]) <= known, source["source_id"]


def test_source_ids_match_config_credential_table(sources: list[dict[str, Any]]) -> None:
    """config 의 자격증명 표와 레지스트리가 어긋나면 키를 못 찾는다."""
    assert {s["source_id"] for s in sources} == set(SOURCE_CREDENTIAL_KEYS)


def test_declared_quota_agrees_with_config_default(by_id: dict[str, Any]) -> None:
    """레지스트리와 config 기본값이 어긋나면 쿼터가 두 벌이 된다."""
    for source_id, quota in SOURCE_QUOTAS.items():
        declared = by_id[source_id].get("quota")
        if declared is None:
            continue
        assert declared["limit"] == quota.limit
        assert declared["window_hours"] == quota.window_hours
        assert declared["scope"] == quota.scope


# 지시서 §8 키 표에 없는 소스. collector 를 만들지 않고 등록만 하므로 env 키도 없다.
# 실호출 경로를 열게 되면 키를 먼저 §8 표와 .env.example 에 추가해야 한다.
CREDENTIAL_EXEMPT = {"coupang_partners", "google_trends"}


def test_auth_required_sources_have_credential_keys(sources: list[dict[str, Any]]) -> None:
    """호출 가능한 상태인데 인증이 필요하면 env 키가 등록돼 있어야 한다."""
    for source in sources:
        if source["source_id"] in CREDENTIAL_EXEMPT:
            continue
        if source["access_status"] in {"core", "conditional"} and source["auth_type"] != "none":
            assert SOURCE_CREDENTIAL_KEYS[source["source_id"]], source["source_id"]


def test_policy_urls_are_https(sources: list[dict[str, Any]]) -> None:
    for source in sources:
        assert all(url.startswith("https://") for url in source["policy_urls"])
