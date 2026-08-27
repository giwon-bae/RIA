"""테스트 공통 안전장치.

두 가지를 전역으로 막는다.

1. 네트워크 — 소켓 연결 자체를 차단한다. S1 범위에는 실호출이 없고,
   S2 이후에도 단위 테스트는 fixture JSON 으로만 검증한다.
2. `.env` 폴백 — v1 에서 테스트가 `.env` 값을 주워 라이브 호출이 나갈 뻔했다.
   전역 config 를 자격증명 없는 것으로 갈아끼워 `.env` 유무와 무관하게 통과시킨다.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ria import config as config_module
from ria.config import Config


class NetworkAccessDenied(RuntimeError):
    """테스트에서 네트워크를 타려 했을 때."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트 중 소켓 연결을 차단한다."""

    def deny(*args: Any, **kwargs: Any) -> None:
        raise NetworkAccessDenied("테스트는 네트워크를 타지 않는다. fixture 로 검증해라.")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(autouse=True)
def _blocked_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """전역 config 를 자격증명 0개 · 임시 DB 로 고정한다."""
    blocked = Config(db_path=tmp_path / "ria-test.db", credentials={})
    config_module.override_config(blocked)
    monkeypatch.delenv("RIA_DB_PATH", raising=False)
    yield blocked
    config_module.override_config(None)


@pytest.fixture
def blocked_config(_blocked_config: Config) -> Config:
    """자격증명이 차단된 테스트용 config."""
    return _blocked_config
