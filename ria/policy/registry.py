"""Source Registry — `sources.yaml` 로드 · 조회 · 상태 전환 (DESIGN §8.1).

승인·거절 같은 상태 변화는 **yaml 을 갱신해서** 반영한다. 코드 상수를 고치지 않는다.
승인이 나면 `set_access_status()` 한 번으로 collector 가 열려야 한다 (DESIGN §21 원칙 1).

yaml 을 통째로 재직렬화하지 않고 해당 소스 블록의 해당 줄만 바꾼다. 주석에 담긴
정책 근거("승인 신청 진행 중 — 승인 시 이 필드만 전환" 등)가 재직렬화로 사라지면
정본으로서의 가치가 없어지기 때문이다.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ria.config import SOURCES_YAML_PATH

AccessStatus = Literal["core", "conditional", "experimental", "blocked"]
ACCESS_STATUSES: tuple[AccessStatus, ...] = ("core", "conditional", "experimental", "blocked")

AccessMethod = Literal["official_api", "official_feed", "third_party_api", "none"]

CommercialUse = Literal[
    "allowed",
    "allowed_with_conditions",
    "separate_agreement_required",
    "prohibited",
    "unclear",
    "not_applicable",
]

AuthType = Literal["none", "api_key", "oauth"]

RateLimitModel = Literal[
    "none",
    "server_headers",
    "daily_quota",
    "dataset_specific",
    "paid_tier",
    "unknown",
]

StoragePolicy = Literal[
    "retain_allowed",
    "metadata_only",
    "approved_use_only",
    "refresh_or_delete_30d",
    "no_storage",
]

DeletionPolicy = Literal[
    "none_required",
    "source_specific",
    "delete_or_refresh_30d",
    "on_request",
]


class RegistryError(RuntimeError):
    """레지스트리 자체가 잘못됐거나 없는 소스를 찾을 때."""


class UnknownSourceError(RegistryError):
    """등록되지 않은 source_id."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        super().__init__(f"등록되지 않은 소스다: {source_id}")


class QuotaSpec(BaseModel):
    """소스가 공식 문서로 명시한 쿼터. 수치가 없으면 이 블록 자체를 두지 않는다."""

    model_config = ConfigDict(extra="forbid")

    limit: int
    window_hours: int
    scope: str
    note: str = ""


class SourceRecord(BaseModel):
    """레지스트리 한 행. 필드 구성은 DESIGN §8.1 필수 필드 목록을 따른다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    pack_ids: list[str]
    access_status: AccessStatus
    access_method: AccessMethod
    official: bool
    commercial_use: CommercialUse
    auth_type: AuthType
    rate_limit_model: RateLimitModel
    storage_policy: StoragePolicy
    deletion_policy: DeletionPolicy
    allowed_data_types: list[str]
    blocked_data_types: list[str]
    last_verified_at: date
    verify_before_use: bool
    fallback_sources: list[str]
    policy_urls: list[str]
    notes: str = ""
    quota: QuotaSpec | None = None
    # 상태 전환 사유. 승인 결과는 last_verified_at 과 함께 기록하고 거절 시 사유를
    # 남긴다 (DESIGN §21 승인 대기 원칙 4). 시드 주석(`notes`)을 덮어쓰지 않는다.
    access_status_note: str | None = None

    @property
    def is_callable_status(self) -> bool:
        """호출을 시도해 볼 수 있는 상태인가. 실제 허용 여부는 Policy Guard 가 정한다."""
        return self.access_status in {"core", "conditional"}


class SourceRegistry:
    """`sources.yaml` 한 벌."""

    def __init__(self, path: Path | str = SOURCES_YAML_PATH) -> None:
        self.path = Path(path)
        self._packs: tuple[str, ...] = ()
        self._records: dict[str, SourceRecord] = {}
        self.reload()

    # -- 로드 ------------------------------------------------------------
    def reload(self) -> None:
        """디스크에서 다시 읽는다. 상태 전환 뒤 자동으로 호출된다."""
        if not self.path.is_file():
            raise RegistryError(f"Source Registry 파일이 없다: {self.path}")

        document = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        raw_sources = document.get("sources") or []

        self._packs = tuple(document.get("packs") or ())

        records: dict[str, SourceRecord] = {}
        for raw in raw_sources:
            record = SourceRecord.model_validate(raw)
            if record.source_id in records:
                raise RegistryError(f"source_id 가 중복됐다: {record.source_id}")
            unknown_packs = set(record.pack_ids) - set(self._packs)
            if unknown_packs:
                raise RegistryError(
                    f"{record.source_id} 가 등록되지 않은 pack 을 참조한다: {sorted(unknown_packs)}"
                )
            records[record.source_id] = record

        self._records = records

    # -- 조회 ------------------------------------------------------------
    @property
    def packs(self) -> tuple[str, ...]:
        """선언된 Source Pack 목록 (DESIGN §5.1)."""
        return self._packs

    def list_sources(
        self,
        pack_id: str | None = None,
        access_status: AccessStatus | None = None,
    ) -> list[SourceRecord]:
        """등록된 소스를 yaml 순서 그대로 돌려준다. 필요하면 pack·상태로 거른다."""
        records = list(self._records.values())
        if pack_id is not None:
            records = [r for r in records if pack_id in r.pack_ids]
        if access_status is not None:
            records = [r for r in records if r.access_status == access_status]
        return records

    def get(self, source_id: str) -> SourceRecord:
        """소스 하나. 없으면 `UnknownSourceError`."""
        try:
            return self._records[source_id]
        except KeyError:
            raise UnknownSourceError(source_id) from None

    def find(self, source_id: str) -> SourceRecord | None:
        """소스 하나. 없으면 None."""
        return self._records.get(source_id)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[SourceRecord]:
        return iter(self._records.values())

    # -- 상태 전환 -------------------------------------------------------
    def set_access_status(
        self,
        source_id: str,
        status: AccessStatus,
        verified_at: date,
        note: str | None = None,
    ) -> SourceRecord:
        """접근 상태를 바꾸고 yaml 에 기록한다.

        승인이 나면 이 함수 한 번으로 반영돼야 한다. 코드 상수를 고치지 않는다.
        `note` 는 `access_status_note` 에 들어간다 — 시드 주석(`notes`)을 덮어쓰지 않는다.
        """
        if status not in ACCESS_STATUSES:
            raise RegistryError(f"정의되지 않은 access_status 다: {status}")
        self.get(source_id)  # 없는 소스면 여기서 실패한다.

        text = self.path.read_text(encoding="utf-8")
        text = _set_field(text, source_id, "access_status", status)
        text = _set_field(text, source_id, "last_verified_at", verified_at.isoformat())
        if note is not None:
            text = _set_field(
                text, source_id, "access_status_note", json.dumps(note, ensure_ascii=False)
            )

        _atomic_write(self.path, text)
        self.reload()
        return self.get(source_id)


# --- yaml 블록 편집 ---------------------------------------------------------
# 주석을 보존해야 하므로 재직렬화하지 않고 해당 줄만 바꾼다.
_ITEM_INDENT = "  "
_FIELD_INDENT = "    "


def _source_block(lines: Sequence[str], source_id: str) -> tuple[int, int]:
    """`- source_id: X` 로 시작하는 블록의 [시작, 끝) 줄 범위."""
    start = None
    for index, line in enumerate(lines):
        if re.fullmatch(rf"{_ITEM_INDENT}- source_id:\s+{re.escape(source_id)}\s*", line):
            start = index
            break
    if start is None:
        raise RegistryError(f"yaml 에서 {source_id} 블록을 찾지 못했다")

    for index in range(start + 1, len(lines)):
        if lines[index].startswith(f"{_ITEM_INDENT}- "):
            return start, index
    return start, len(lines)


def _set_field(text: str, source_id: str, key: str, value: str) -> str:
    """소스 블록 안의 `key: ...` 줄을 바꾸거나 없으면 추가한다."""
    lines = text.splitlines()
    start, end = _source_block(lines, source_id)
    replacement = f"{_FIELD_INDENT}{key}: {value}"

    for index in range(start, end):
        if re.match(rf"{_FIELD_INDENT}{re.escape(key)}:", lines[index]):
            lines[index] = replacement
            break
    else:
        lines.insert(start + 1, replacement)

    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """부분 기록된 레지스트리를 남기지 않는다."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# --- 전역 인스턴스 ----------------------------------------------------------
_registry: SourceRegistry | None = None


def get_registry() -> SourceRegistry:
    """기본 경로의 레지스트리. 처음 호출할 때 한 번 로드하고 캐시한다."""
    global _registry
    if _registry is None:
        _registry = SourceRegistry()
    return _registry


def override_registry(registry: SourceRegistry | None) -> None:
    """전역 레지스트리를 갈아끼운다. 테스트가 임시 yaml 을 쓰는 경로다."""
    global _registry
    _registry = registry
