"""RIA Core 운영·디버깅 CLI (DESIGN §12.2, S2 B-11).

``source``·``query``·``snapshot``은 네트워크 없이 로컬 정책·DB만 다루고, ``collect``는 PackRunner를
통해 Policy Guard가 허용한 소스만 전송한다. 차단 판정도 정상 결과로서 gap과
query audit를 남긴다. EvidencePack export는 S3 범위다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ria import __version__
from ria.collectors.data_go_kr import DataGoKrDatasetSpec
from ria.collectors.kosis import KosisDatasetSpec
from ria.config import Config, get_config, parse_iso8601
from ria.contracts.research_brief import RESEARCH_LANES
from ria.core.metrics import get_metric_history
from ria.core.observations import list_observations
from ria.core.snapshots import get_snapshot
from ria.core.store import Store
from ria.http import redact_url
from ria.packs import PACK_MODULES, PackRunner, PackRunResult, SourceRunResult
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

    collect = commands.add_parser(
        "collect",
        help="Pack 또는 source를 Guard 통과 후 수집·적재한다",
    )
    collect.add_argument("target", help="Pack ID 또는 source_id")
    collect.add_argument("query", help="수집 질의")
    collect.add_argument("--db", type=Path, help="SQLite DB 경로")
    collect.add_argument(
        "--options-json",
        help="source 수집 옵션 JSON object (Pack에서는 사용 불가)",
    )
    collect.add_argument(
        "--source-options-json",
        help="Pack의 source별 옵션 JSON object ('{\"source_id\": {...}}')",
    )
    collect.add_argument("--as-of", help="정책 확인 TTL 기준일 (YYYY-MM-DD)")
    collect.add_argument("--lane", choices=RESEARCH_LANES, help="결과 gap에 연결할 Lane")
    collect.add_argument("--research-id", help="기존 research_runs의 research_id")
    collect.add_argument(
        "--non-commercial",
        action="store_true",
        help="commercial_context=false로 판정한다",
    )
    collect.add_argument("--json", action="store_true", help="기계 판독용 JSON 으로 출력한다")
    collect.set_defaults(handler=_cmd_collect, command="collect")

    query = commands.add_parser("query", help="적재된 관측·지표를 안전한 필터로 조회한다")
    query_commands = query.add_subparsers(dest="query_command", required=True)

    observations = query_commands.add_parser("observations", help="Observation 이력 조회")
    _add_query_common_arguments(observations)
    observations.set_defaults(handler=_cmd_query_observations, command="query")

    metrics = query_commands.add_parser("metrics", help="Metric 이력 조회")
    metrics.add_argument("metric_name", help="조회할 metric_name")
    _add_query_common_arguments(metrics)
    metrics.add_argument("--entity-id")
    metrics.set_defaults(handler=_cmd_query_metrics, command="query")

    snapshot = commands.add_parser("snapshot", help="저장된 원본 스냅샷 조회")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_get = snapshot_commands.add_parser("get", help="snapshot_id로 스냅샷 조회")
    snapshot_get.add_argument("snapshot_id")
    snapshot_get.add_argument("--db", type=Path, help="SQLite DB 경로")
    snapshot_get.add_argument(
        "--include-body",
        action="store_true",
        help="민감하거나 큰 원문 body를 명시적으로 포함한다",
    )
    snapshot_get.add_argument("--json", action="store_true", help="기계 판독용 JSON 으로 출력한다")
    snapshot_get.set_defaults(handler=_cmd_snapshot_get, command="snapshot")

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


# --- collect ---------------------------------------------------------------
def _cmd_collect(args: argparse.Namespace) -> int:
    registry = _registry(args)
    config = _config(args)
    as_of = _parse_as_of(args.as_of)

    if args.target == "web-primary":
        raise ValueError(
            "web-primary는 Core 실행 Pack이 아니다; Codex/Chrome으로 확인한 뒤 "
            "ria.collectors.web_primary.store_web_snapshot으로 저장해라"
        )

    with _store(args, config) as store:
        runner = _runner(args, store, registry, config)
        if args.target in PACK_MODULES:
            if args.options_json is not None:
                raise ValueError("Pack에는 --options-json 대신 --source-options-json을 써라")
            source_options = _parse_source_options(args.source_options_json, config=config)
            result: PackRunResult | SourceRunResult = runner.run_pack(
                args.target,
                args.query,
                source_options=source_options,
                lane=args.lane,
                research_id=args.research_id,
                as_of=as_of,
                commercial_context=not args.non_commercial,
            )
            payload = _pack_run_payload(result, config)
        else:
            if registry.find(args.target) is None:
                raise ValueError(f"등록되지 않은 Pack/source다: {args.target}")
            if args.source_options_json is not None:
                raise ValueError("source에는 --source-options-json 대신 --options-json을 써라")
            options = _coerce_source_options(
                args.target,
                _parse_json_object(args.options_json, option="--options-json"),
                config=config,
            )
            result = runner.collect_source(
                args.target,
                args.query,
                options=options,
                lane=args.lane,
                research_id=args.research_id,
                as_of=as_of,
                commercial_context=not args.non_commercial,
            )
            payload = _source_run_payload(result, config)

    _emit_collect(payload, json_output=args.json)
    if isinstance(result, PackRunResult):
        return EXIT_ERROR if any(run.status == "failed" for run in result.source_runs) else EXIT_OK
    return EXIT_ERROR if result.status == "failed" else EXIT_OK


def _parse_source_options(
    value: str | None,
    *,
    config: Config,
) -> dict[str, dict[str, Any]]:
    raw = _parse_json_object(value, option="--source-options-json")
    parsed: dict[str, dict[str, Any]] = {}
    for source_id, options in raw.items():
        if not isinstance(options, Mapping):
            raise ValueError(f"--source-options-json의 {source_id} 값은 JSON object여야 한다")
        parsed[source_id] = _coerce_source_options(source_id, dict(options), config=config)
    return parsed


def _parse_json_object(value: str | None, *, option: str) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{option} JSON 파싱 실패(문자 위치 {error.pos})") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{option}은 JSON object여야 한다")
    _reject_sensitive_options(parsed, option=option)
    return parsed


def _coerce_source_options(
    source_id: str,
    options: dict[str, Any],
    *,
    config: Config,
) -> dict[str, Any]:
    """CLI JSON의 명시적 dataset selector를 collector 계약으로 바꾼다."""
    coerced = dict(options)
    if isinstance(coerced.get("observed_at"), str):
        coerced["observed_at"] = _parse_datetime(coerced["observed_at"], "observed_at")

    dataset = coerced.get("dataset")
    if dataset is None:
        return coerced
    if not isinstance(dataset, Mapping):
        raise ValueError(f"{source_id} dataset은 JSON object여야 한다")
    # 자격증명이 없을 때는 Guard의 missing_credential이 selector 검증보다 먼저다.
    # 이 경로는 Guard에서 종료되므로 mapping을 collector에 넘겨도 _collect에 진입하지 않는다.
    if config.missing_credentials_for(source_id):
        return coerced

    values = dict(dataset)
    try:
        if source_id == "kosis":
            _validate_kosis_dataset_json(values)
            coerced["dataset"] = KosisDatasetSpec(**values)
        elif source_id == "data_go_kr":
            _validate_data_go_dataset_json(values)
            values["items_path"] = tuple(values["items_path"])
            coerced["dataset"] = DataGoKrDatasetSpec(**values)
    except TypeError as error:
        raise ValueError(f"{source_id} dataset selector 필드가 잘못됐다: {error}") from None
    return coerced


def _validate_kosis_dataset_json(values: Mapping[str, Any]) -> None:
    for field_name in ("org_id", "table_id", "object_l1", "item_id", "period_type"):
        value = values.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"KOSIS dataset.{field_name}은 비지 않은 문자열이어야 한다")
    for field_name in ("start_period", "end_period"):
        value = values.get(field_name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"KOSIS dataset.{field_name}은 문자열이어야 한다")
    latest_count = values.get("latest_count", 1)
    if latest_count is not None and (
        isinstance(latest_count, bool) or not isinstance(latest_count, int) or latest_count <= 0
    ):
        raise ValueError("KOSIS dataset.latest_count는 양의 정수여야 한다")


def _validate_data_go_dataset_json(values: Mapping[str, Any]) -> None:
    for field_name in ("dataset_id", "endpoint", "policy_url"):
        value = values.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"data.go.kr dataset.{field_name}은 비지 않은 문자열이어야 한다")
    items_path = values.get("items_path")
    if (
        not isinstance(items_path, list)
        or not items_path
        or not all(isinstance(item, str) and item for item in items_path)
    ):
        raise ValueError("data.go.kr dataset.items_path는 비지 않은 문자열 배열이어야 한다")
    for field_name in ("approved", "storage_allowed"):
        if not isinstance(values.get(field_name), bool):
            raise ValueError(f"data.go.kr dataset.{field_name}는 boolean이어야 한다")
    for field_name in ("page_size", "max_pages"):
        value = values.get(field_name, 100 if field_name == "page_size" else 1)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"data.go.kr dataset.{field_name}는 양의 정수여야 한다")


def _pack_run_payload(result: PackRunResult, config: Config) -> dict[str, Any]:
    return _safe_json_value(
        {
            "target_type": "pack",
            "pack_id": result.pack_id,
            "status": _pack_status(result),
            "registry_rows_synced": result.registry_rows_synced,
            "result_count": result.result_count,
            "gap_count": len(result.gaps),
            "source_runs": [_source_run_payload(run, config) for run in result.source_runs],
        },
        config,
    )


def _pack_status(result: PackRunResult) -> str:
    statuses = {run.status for run in result.source_runs}
    if statuses == {"completed"}:
        return "completed"
    if "failed" in statuses:
        return "completed_with_failures"
    return "completed_with_gaps"


def _source_run_payload(result: SourceRunResult, config: Config) -> dict[str, Any]:
    persisted = result.persisted
    policy: dict[str, Any] | None = None
    if result.result is not None:
        decision = result.result.policy
        policy = {"allowed": bool(decision)}
        if isinstance(decision, PolicyBlocked):
            policy.update(check=decision.check, reason=decision.reason)

    payload = {
        "target_type": "source",
        "source_id": result.source_id,
        "query_run_id": result.query_run_id,
        "status": result.status,
        "policy": policy,
        "result_count": result.result.result_count if result.result is not None else 0,
        "persisted": {
            "content": persisted.content_count if persisted is not None else 0,
            "observation": persisted.observation_count if persisted is not None else 0,
            "metric": persisted.metric_count if persisted is not None else 0,
            "snapshot": persisted.snapshot_count if persisted is not None else 0,
        },
        "gap_count": len(result.gaps),
        "gaps": [
            {
                "gap_id": gap.gap_id,
                "kind": gap.kind,
                "source_id": gap.source_id,
                "pack_id": gap.pack_id,
                "lane": gap.lane,
                "detail": gap.detail,
                "next_action": gap.next_action,
            }
            for gap in result.gaps
        ],
        "error": result.error,
    }
    return _safe_json_value(payload, config)


def _emit_collect(payload: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        _print_json(payload)
        return
    if payload["target_type"] == "pack":
        print(
            f"Pack {payload['pack_id']}: {payload['status']} | "
            f"결과 {payload['result_count']}건 | gap {payload['gap_count']}건 | "
            f"registry {payload['registry_rows_synced']}행"
        )
        for run in payload["source_runs"]:
            _print_source_run_line(run)
        return
    _print_source_run_line(payload)


def _print_source_run_line(payload: Mapping[str, Any]) -> None:
    counts = payload["persisted"]
    print(
        f"Source {payload['source_id']}: {payload['status']} | "
        f"결과 {payload['result_count']}건 | gap {payload['gap_count']}건 | "
        f"DB C/O/M/S={counts['content']}/{counts['observation']}/"
        f"{counts['metric']}/{counts['snapshot']} | query_run={payload['query_run_id']}"
    )
    for gap in payload["gaps"]:
        print(f"  - {gap['kind']}: {gap['detail']}")


# --- query -----------------------------------------------------------------
def _add_query_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, help="SQLite DB 경로")
    parser.add_argument("--source", dest="source_id")
    parser.add_argument("--platform")
    parser.add_argument("--content-id")
    parser.add_argument("--research-id")
    parser.add_argument("--since", help="ISO8601 시작 시각")
    parser.add_argument("--until", help="ISO8601 종료 시각")
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON 으로 출력한다")


def _cmd_query_observations(args: argparse.Namespace) -> int:
    config = _config(args)
    with _store(args, config) as store:
        records = list_observations(
            store,
            content_item_id=args.content_id,
            platform=args.platform,
            source_id=args.source_id,
            research_id=args.research_id,
            since=_parse_datetime(args.since, "--since") if args.since else None,
            until=_parse_datetime(args.until, "--until") if args.until else None,
            limit=args.limit,
        )
    payload = [_safe_json_value(asdict(record), config) for record in records]
    _emit_records(payload, json_output=args.json)
    return EXIT_OK


def _cmd_query_metrics(args: argparse.Namespace) -> int:
    config = _config(args)
    with _store(args, config) as store:
        records = get_metric_history(
            store,
            args.metric_name,
            source_id=args.source_id,
            platform=args.platform,
            content_item_id=args.content_id,
            entity_id=args.entity_id,
            research_id=args.research_id,
            since=_parse_datetime(args.since, "--since") if args.since else None,
            until=_parse_datetime(args.until, "--until") if args.until else None,
            limit=args.limit,
        )
    payload = [_safe_json_value(asdict(record), config) for record in records]
    _emit_records(payload, json_output=args.json)
    return EXIT_OK


def _emit_records(records: Sequence[Mapping[str, Any]], *, json_output: bool) -> None:
    if json_output:
        _print_json(records)
        return
    for record in records:
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    print(f"{len(records)}건")


# --- snapshot --------------------------------------------------------------
def _cmd_snapshot_get(args: argparse.Namespace) -> int:
    config = _config(args)
    with _store(args, config) as store:
        snapshot = get_snapshot(store, args.snapshot_id)
    if snapshot is None:
        raise ValueError(f"스냅샷을 찾을 수 없다: {args.snapshot_id}")

    payload: dict[str, Any] = {
        "snapshot_id": snapshot.snapshot_id,
        "hash": snapshot.hash,
        "source_id": snapshot.source_id,
        "url": snapshot.url,
        "media_type": snapshot.media_type,
        "body_stored": snapshot.body_stored,
        "storage_policy": snapshot.storage_policy,
        "meta": snapshot.meta,
        "query": snapshot.query,
        "collected_at": snapshot.collected_at,
        "expires_at": snapshot.expires_at,
        "deleted_at": snapshot.deleted_at,
        "is_expired_placeholder": snapshot.is_expired_placeholder,
    }
    if args.include_body:
        payload["body"] = _snapshot_body(snapshot.body, snapshot.media_type)
    safe_payload = _safe_json_value(payload, config)
    if args.json:
        _print_json(safe_payload)
    else:
        for key, value in safe_payload.items():
            if isinstance(value, dict | list):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                rendered = value
            print(f"{key}: {rendered}")
    return EXIT_OK


def _snapshot_body(body: str | None, media_type: str | None) -> Any:
    if body is None or "json" not in (media_type or "").casefold():
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


# --- 공통 -------------------------------------------------------------------
def _registry(args: argparse.Namespace) -> SourceRegistry:
    override: SourceRegistry | None = getattr(args, "registry", None)
    return override if override is not None else get_registry()


def _config(args: argparse.Namespace) -> Config:
    override: Config | None = getattr(args, "config", None)
    return override if override is not None else get_config()


@contextmanager
def _store(args: argparse.Namespace, config: Config) -> Iterator[Store]:
    override: Store | None = getattr(args, "store", None)
    if override is not None:
        yield override
        return
    db_path: Path | None = getattr(args, "db", None)
    with Store(db_path or config.db_path) as opened:
        yield opened


def _runner(
    args: argparse.Namespace,
    store: Store,
    registry: SourceRegistry,
    config: Config,
) -> PackRunner:
    override: PackRunner | None = getattr(args, "runner", None)
    if override is not None:
        if override.store is not store:
            raise ValueError("주입한 PackRunner와 Store가 다르다")
        if override.registry is not registry or override.config is not config:
            raise ValueError("주입한 PackRunner와 registry/config가 다르다")
        return override
    return PackRunner(store, registry=registry, config=config)


def _parse_as_of(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--as-of 는 YYYY-MM-DD 형식이어야 한다: {value}") from None


def _parse_datetime(value: str, option: str) -> datetime:
    try:
        return parse_iso8601(value)
    except ValueError:
        raise ValueError(f"{option}은 ISO8601 시각이어야 한다") from None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("양의 정수여야 한다") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("양의 정수여야 한다")
    return parsed


_SENSITIVE_OPTION_KEYS = frozenset(
    {
        "access_token",
        "access_key",
        "api_key",
        "apikey",
        "app_secret",
        "auth",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "key",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "service_key",
        "servicekey",
        "token",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")


def _reject_sensitive_options(value: Any, *, option: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                raise ValueError(f"{option}으로 자격증명을 받지 않는다: {key}")
            _reject_sensitive_options(item, option=option)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _reject_sensitive_options(item, option=option)


def _is_sensitive_key(key: str) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")
    return normalized in _SENSITIVE_OPTION_KEYS or normalized.endswith(
        ("_api_key", "_credential", "_key", "_password", "_secret", "_token")
    )


def _safe_json_value(value: Any, config: Config) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _safe_json_value(asdict(value), config)
    if hasattr(value, "model_dump"):
        return _safe_json_value(value.model_dump(mode="json"), config)
    if isinstance(value, Mapping):
        return {
            str(key): "REDACTED" if _is_sensitive_key(str(key)) else _safe_json_value(item, config)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_safe_json_value(item, config) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        sanitized = value
        for secret in config.credentials.values():
            if sanitized == secret:
                sanitized = "REDACTED"
            elif secret and len(secret) >= 8:
                sanitized = sanitized.replace(secret, "REDACTED")
        return _redact_urls(sanitized)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return repr(value)


def _redact_urls(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        url = raw.rstrip("),.;]")
        return f"{_redact_cli_url(url)}{raw[len(url) :]}"

    return _URL_PATTERN.sub(replace, value)


def _redact_cli_url(url: str) -> str:
    parts = urlsplit(redact_url(url))
    if not parts.query:
        return urlunsplit(parts)
    query = urlencode(
        [
            (key, "REDACTED" if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


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
