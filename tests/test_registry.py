"""A-5. Source Registry 로드 · 조회 · 상태 전환 (DESIGN §8.1 · §21)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from ria.config import SOURCES_YAML_PATH
from ria.policy.registry import (
    RegistryError,
    SourceRegistry,
    UnknownSourceError,
    get_registry,
    override_registry,
)


@pytest.fixture
def registry() -> SourceRegistry:
    """읽기 전용 검사는 시드 파일을 그대로 쓴다."""
    return SourceRegistry()


@pytest.fixture
def writable(tmp_path: Path) -> SourceRegistry:
    """상태 전환 검사는 시드 사본에 한다. 원본을 건드리지 않는다."""
    copy = tmp_path / "sources.yaml"
    shutil.copyfile(SOURCES_YAML_PATH, copy)
    return SourceRegistry(copy)


def test_loads_all_twenty_sources(registry: SourceRegistry) -> None:
    assert len(registry.list_sources()) == 20
    assert len(registry) == 20


def test_get_returns_typed_record(registry: SourceRegistry) -> None:
    reddit = registry.get("reddit")

    assert reddit.access_status == "blocked"
    assert reddit.commercial_use == "separate_agreement_required"
    assert reddit.rate_limit_model == "server_headers"
    assert reddit.last_verified_at == date(2026, 8, 27)
    assert reddit.official is True


def test_get_unknown_source_raises(registry: SourceRegistry) -> None:
    with pytest.raises(UnknownSourceError):
        registry.get("mastodon")


def test_find_unknown_source_returns_none(registry: SourceRegistry) -> None:
    assert registry.find("mastodon") is None
    assert "mastodon" not in registry
    assert "reddit" in registry


def test_list_sources_filters_by_pack(registry: SourceRegistry) -> None:
    ids = [s.source_id for s in registry.list_sources(pack_id="community-signal")]

    assert ids == ["reddit", "threads", "x_twitter"]


def test_list_sources_filters_by_status(registry: SourceRegistry) -> None:
    blocked = {s.source_id for s in registry.list_sources(access_status="blocked")}

    assert {"reddit", "product_hunt", "naver_shopping_search"} <= blocked


def test_list_sources_preserves_yaml_order(registry: SourceRegistry) -> None:
    raw = yaml.safe_load(SOURCES_YAML_PATH.read_text(encoding="utf-8"))

    assert [s.source_id for s in registry.list_sources()] == [
        s["source_id"] for s in raw["sources"]
    ]


def test_callable_status_flag(registry: SourceRegistry) -> None:
    assert registry.get("hacker_news").is_callable_status is True
    assert registry.get("threads").is_callable_status is True
    assert registry.get("reddit").is_callable_status is False
    assert registry.get("google_play").is_callable_status is False


def test_threads_quota_is_loaded(registry: SourceRegistry) -> None:
    quota = registry.get("threads").quota

    assert quota is not None
    assert (quota.limit, quota.window_hours, quota.scope) == (2200, 24, "per_user")


def test_sources_without_declared_quota_have_none(registry: SourceRegistry) -> None:
    assert registry.get("reddit").quota is None


# --- 상태 전환 --------------------------------------------------------------
def test_set_access_status_updates_yaml_not_code(writable: SourceRegistry) -> None:
    """승인이 나면 이 함수 한 번으로 반영돼야 한다 (DESIGN §21 원칙 1)."""
    updated = writable.set_access_status(
        "reddit", "core", date(2026, 9, 1), note="Data Access Request 승인"
    )

    assert updated.access_status == "core"
    assert updated.last_verified_at == date(2026, 9, 1)
    assert updated.access_status_note == "Data Access Request 승인"

    reloaded = SourceRegistry(writable.path)
    assert reloaded.get("reddit").access_status == "core"


def test_set_access_status_preserves_seed_comments(writable: SourceRegistry) -> None:
    """주석에 담긴 정책 근거가 재직렬화로 사라지면 정본 가치가 없다."""
    before = writable.path.read_text(encoding="utf-8")
    writable.set_access_status("reddit", "core", date(2026, 9, 1))
    after = writable.path.read_text(encoding="utf-8")

    assert "# 승인 신청 진행 중 — 승인 시 이 필드만 전환" in before
    assert "# 승인 신청 진행 중 — 승인 시 이 필드만 전환" in after
    assert "# Source Registry — 플랫폼 접근·상업 이용·보관 정책의 정본" in after


def test_set_access_status_does_not_touch_other_sources(writable: SourceRegistry) -> None:
    writable.set_access_status("reddit", "core", date(2026, 9, 1))
    reloaded = SourceRegistry(writable.path)

    assert reloaded.get("threads").access_status == "conditional"
    assert reloaded.get("threads").last_verified_at == date(2026, 8, 27)
    assert len(reloaded) == 20


def test_set_access_status_does_not_overwrite_seed_notes(writable: SourceRegistry) -> None:
    writable.set_access_status("reddit", "core", date(2026, 9, 1), note="승인 완료")

    record = SourceRegistry(writable.path).get("reddit")
    assert "승인 신청 진행 중" in record.notes
    assert record.access_status_note == "승인 완료"


def test_set_access_status_records_rejection_reason(writable: SourceRegistry) -> None:
    """거절 시 사유를 남긴다 (DESIGN §21 원칙 4)."""
    writable.set_access_status("reddit", "blocked", date(2026, 9, 1), note="거절 — use case 부적합")

    assert SourceRegistry(writable.path).get("reddit").access_status_note.startswith("거절")


def test_set_access_status_rejects_undefined_status(writable: SourceRegistry) -> None:
    with pytest.raises(RegistryError):
        writable.set_access_status("reddit", "approved", date(2026, 9, 1))


def test_set_access_status_rejects_unknown_source(writable: SourceRegistry) -> None:
    with pytest.raises(UnknownSourceError):
        writable.set_access_status("mastodon", "core", date(2026, 9, 1))


def test_repeated_transition_does_not_duplicate_fields(writable: SourceRegistry) -> None:
    writable.set_access_status("reddit", "core", date(2026, 9, 1), note="첫 승인")
    writable.set_access_status("reddit", "conditional", date(2026, 9, 2), note="조건부로 조정")

    raw = yaml.safe_load(writable.path.read_text(encoding="utf-8"))
    reddit = next(s for s in raw["sources"] if s["source_id"] == "reddit")

    assert reddit["access_status"] == "conditional"
    assert reddit["access_status_note"] == "조건부로 조정"
    assert writable.path.read_text(encoding="utf-8").count("    access_status_note:") == 1


def test_note_with_yaml_special_characters_survives(writable: SourceRegistry) -> None:
    note = 'approved: yes — "2026-09-01" #1'
    writable.set_access_status("reddit", "core", date(2026, 9, 1), note=note)

    assert SourceRegistry(writable.path).get("reddit").access_status_note == note


# --- 무결성 ----------------------------------------------------------------
def test_unknown_pack_reference_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "packs": ["authority-stats"],
                "sources": [_record(pack_ids=["does-not-exist"])],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="pack"):
        SourceRegistry(path)


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        yaml.safe_dump(
            {"packs": ["authority-stats"], "sources": [_record(), _record()]},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="중복"):
        SourceRegistry(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        SourceRegistry(tmp_path / "nope.yaml")


def test_override_registry_swaps_global(writable: SourceRegistry) -> None:
    override_registry(writable)
    try:
        assert get_registry() is writable
    finally:
        override_registry(None)


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source_id": "sample",
        "name": "Sample",
        "pack_ids": ["authority-stats"],
        "access_status": "core",
        "access_method": "official_api",
        "official": True,
        "commercial_use": "allowed_with_conditions",
        "auth_type": "none",
        "rate_limit_model": "unknown",
        "storage_policy": "retain_allowed",
        "deletion_policy": "none_required",
        "allowed_data_types": [],
        "blocked_data_types": ["personal_sensitive_data"],
        "last_verified_at": date(2026, 8, 11),
        "verify_before_use": False,
        "fallback_sources": [],
        "policy_urls": [],
    }
    base.update(overrides)
    return base
