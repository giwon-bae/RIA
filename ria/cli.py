"""RIA Core 운영·디버깅 CLI (DESIGN §12.2).

이번 스테이지 범위는 `ria source list` 와 `ria source check <source>` 두 개다.
수집·조회·export 는 S2·S3 에서 붙인다.

**이 CLI 는 네트워크를 타지 않는다.** `source check` 는 Policy Guard 판정만 보여준다.
차단된 소스를 "확인해 보려고" 호출하는 경로를 만들지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from ria import __version__
from ria.config import Config, get_config
from ria.policy.guard import PolicyAllowed, PolicyBlocked, check_source
from ria.policy.registry import SourceRecord, SourceRegistry, get_registry

EXIT_OK = 0
EXIT_ERROR = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    handler = args.handler
    try:
        return int(handler(args))
    except Exception as error:  # noqa: BLE001 — CLI 는 스택트레이스 대신 문장을 보여준다
        print(f"오류: {error}", file=sys.stderr)
        return EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ria",
        description="RIA Core — 근거 수집 엔진의 운영 CLI. 모델을 호출하지 않는다.",
    )
    parser.add_argument("--version", action="version", version=f"ria {__version__}")
    parser.set_defaults(command=None)

    commands = parser.add_subparsers(dest="command")

    source = commands.add_parser("source", help="Source Registry 조회와 정책 판정")
    source_commands = source.add_subparsers(dest="source_command", required=True)

    listing = source_commands.add_parser("list", help="등록된 소스를 나열한다")
    listing.add_argument("--pack", help="Source Pack 으로 거른다 (예: community-signal)")
    listing.add_argument(
        "--status",
        choices=["core", "conditional", "experimental", "blocked"],
        help="접근 상태로 거른다",
    )
    listing.add_argument("--json", action="store_true", help="기계 판독용 JSON 으로 출력한다")
    listing.set_defaults(handler=_cmd_source_list, command="source")

    check = source_commands.add_parser(
        "check",
        help="Policy Guard 6단 검사를 돌린다. 네트워크를 타지 않는다.",
    )
    check.add_argument("source_id", help="검사할 source_id (예: reddit)")
    check.add_argument(
        "--non-commercial",
        action="store_true",
        help="commercial_context=false 로 검사한다. 기본값은 true 다.",
    )
    check.add_argument("--calls", type=int, default=1, help="요청 예정 호출 수 (기본 1)")
    check.add_argument("--as-of", help="정책 확인일 TTL 을 계산할 기준일 (YYYY-MM-DD)")
    check.add_argument("--json", action="store_true", help="기계 판독용 JSON 으로 출력한다")
    check.set_defaults(handler=_cmd_source_check, command="source")

    return parser


# --- source list ------------------------------------------------------------
_LIST_HEADERS = ("SOURCE_ID", "STATUS", "PACKS", "AUTH", "COMMERCIAL", "VERIFIED", "EXPIRES")


def _cmd_source_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    config = _config(args)
    records = registry.list_sources(pack_id=args.pack, access_status=args.status)

    if args.json:
        payload = [_source_json(record, config) for record in records]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    rows = [
        (
            record.source_id,
            record.access_status,
            ",".join(record.pack_ids),
            record.auth_type,
            record.commercial_use,
            record.last_verified_at.isoformat(),
            config.policy_expires_on(record.source_id, record.last_verified_at).isoformat(),
        )
        for record in records
    ]
    print(_render_table(_LIST_HEADERS, rows))
    print(f"\n{len(records)}건")
    return EXIT_OK


def _source_json(record: SourceRecord, config: Config) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["policy_expires_on"] = config.policy_expires_on(
        record.source_id, record.last_verified_at
    ).isoformat()
    payload["policy_ttl_days"] = config.policy_ttl_for(record.source_id)
    payload["credentials_present"] = config.has_credentials(record.source_id)
    payload["missing_credentials"] = list(config.missing_credentials_for(record.source_id))
    return payload


# --- source check -----------------------------------------------------------
def _cmd_source_check(args: argparse.Namespace) -> int:
    registry = _registry(args)
    config = _config(args)
    as_of = _parse_as_of(args.as_of)

    decision = check_source(
        args.source_id,
        commercial_context=not args.non_commercial,
        requested_calls=args.calls,
        as_of=as_of,
        registry=registry,
        config=config,
    )

    if args.json:
        print(json.dumps(_decision_json(decision), ensure_ascii=False, indent=2))
        return EXIT_OK

    print(_render_decision(args.source_id, decision, registry, config))
    return EXIT_OK


def _decision_json(decision: PolicyAllowed | PolicyBlocked) -> dict[str, Any]:
    payload = asdict(decision)
    payload["allowed"] = bool(decision)
    payload["checked_at"] = decision.checked_at.isoformat()
    if isinstance(decision, PolicyAllowed):
        payload["notes"] = list(decision.notes)
    return payload


def _render_decision(
    source_id: str,
    decision: PolicyAllowed | PolicyBlocked,
    registry: SourceRegistry,
    config: Config,
) -> str:
    record = registry.find(source_id)
    lines = [f"소스: {source_id}"]

    if record is not None:
        lines += [
            f"이름: {record.name}",
            f"접근 상태: {record.access_status}   상업 이용: {record.commercial_use}",
            f"인증: {record.auth_type}   rate limit 모델: {record.rate_limit_model}",
            f"보관: {record.storage_policy}   삭제: {record.deletion_policy}",
            (
                f"정책 확인일: {record.last_verified_at.isoformat()}"
                f"  (TTL {config.policy_ttl_for(source_id)}일 → "
                f"{config.policy_expires_on(source_id, record.last_verified_at).isoformat()})"
            ),
        ]

    lines.append("")

    if isinstance(decision, PolicyBlocked):
        lines += [
            "판정: BLOCKED — 호출하지 않는다",
            f"실패 단계: {decision.check}",
            f"사유: {decision.reason}",
            f"상세: {decision.detail}",
        ]
        if record is not None and record.policy_urls:
            lines.append("정책 근거:")
            lines += [f"  - {url}" for url in record.policy_urls]
        lines += [
            "",
            "이 판정은 EvidencePack 에 policy_blocked gap 으로 기록된다. 실호출은 없었다.",
        ]
    else:
        ceiling = "없음" if decision.max_calls is None else str(decision.max_calls)
        lines += [
            "판정: ALLOWED — 정책상 호출 가능",
            f"보관 의무: {decision.storage_policy} / {decision.deletion_policy}",
            f"이번 실행 호출 상한: {ceiling}",
        ]
        if decision.notes:
            lines.append("유의:")
            lines += [f"  - {note}" for note in decision.notes]

    return "\n".join(lines)


# --- 공통 -------------------------------------------------------------------
def _registry(args: argparse.Namespace) -> SourceRegistry:
    override: SourceRegistry | None = getattr(args, "registry", None)
    return override if override is not None else get_registry()


def _config(args: argparse.Namespace) -> Config:
    override: Config | None = getattr(args, "config", None)
    return override if override is not None else get_config()


def _parse_as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--as-of 는 YYYY-MM-DD 형식이어야 한다: {value}") from None


def _display_width(text: str) -> int:
    """한글·한자는 터미널에서 두 칸을 차지한다. 표 정렬에 반영한다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(cell))

    lines = ["  ".join(_pad(h, w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines += [
        "  ".join(_pad(cell, w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
