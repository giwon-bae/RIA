"""OpenDART 공시검색·재무제표 collector (company-market, B-3).

API key는 Policy Guard만 검사한다. 공시 원문 ``document.xml`` ZIP은 bytes를 직접
넘기지 않고 base64 문자열로 가역 저장해 현재 Snapshot 계약을 바꾸지 않는다.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from datetime import datetime
from typing import Any, cast

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import KST, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

OPENDART_API = "https://opendart.fss.or.kr/api"
OPENDART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do"
DISCLOSURES_MODE = "disclosures"
FINANCIALS_MODE = "financials"
DOCUMENT_MODE = "document"
REPORT_CODES = frozenset({"11011", "11012", "11013", "11014"})
FINANCIAL_DIVISIONS = frozenset({"CFS", "OFS"})
ORIGINAL_DOCUMENT_NOTE = (
    "OpenDART document.xml ZIP은 Snapshot 계약을 바꾸지 않고 base64 문자열로 가역 저장한다."
)


class OpenDartCollector(GuardedCollector):
    """공시 목록 또는 단일회사 전체 재무계정을 공식 API에서 정규화한다."""

    source_id = "opendart"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        del query
        if options.get("mode", DISCLOSURES_MODE) == FINANCIALS_MODE:
            return 1
        if options.get("mode", DISCLOSURES_MODE) == DOCUMENT_MODE:
            return 1
        return _positive_int("max_pages", options.get("max_pages", 1))

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        mode = str(options.get("mode", DISCLOSURES_MODE))
        if mode == DISCLOSURES_MODE:
            return self._collect_disclosures(query, options)
        if mode == FINANCIALS_MODE:
            return self._collect_financials(query, options)
        if mode == DOCUMENT_MODE:
            return self._collect_document(query, options)
        raise CollectorContractError(f"지원하지 않는 OpenDART mode다: {mode!r}")

    def _collect_disclosures(self, query: str, options: dict[str, Any]) -> CollectedBatch:
        allowed = {
            "bgn_de",
            "corp_code",
            "end_de",
            "last_reprt_at",
            "max_pages",
            "mode",
            "observed_at",
            "page_count",
            "page_no",
            "pblntf_detail_ty",
            "pblntf_ty",
            "sort",
            "sort_mth",
        }
        _reject_unknown_options(options, allowed)
        observed_at = _observed_at(options.get("observed_at", now()))
        first_page = _positive_int("page_no", options.get("page_no", 1))
        page_count = _positive_int("page_count", options.get("page_count", 100))
        if page_count > 100:
            raise CollectorContractError("OpenDART page_count는 100 이하여야 한다")
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        api_key = cast(str, (self._config or get_config()).credentials["RIA_OPENDART_API_KEY"])

        base_params: dict[str, Any] = {"crtfc_key": api_key, "page_count": page_count}
        for key in (
            "corp_code",
            "bgn_de",
            "end_de",
            "last_reprt_at",
            "pblntf_ty",
            "pblntf_detail_ty",
            "sort",
            "sort_mth",
        ):
            if (value := options.get(key)) is not None:
                base_params[key] = value

        contents: list[CollectedContent] = []
        observations: list[CollectedObservation] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        pages: list[dict[str, int]] = []

        for offset in range(max_pages):
            page = first_page + offset
            payload, _response = self._http.get_json(
                f"{OPENDART_API}/list.json", params={**base_params, "page_no": page}
            )
            status = _response_status(payload)
            snapshot_ref = f"snapshot:opendart:disclosures:{page}"
            snapshots.append(_snapshot(snapshot_ref, payload, observed_at, query, page=page))
            if status == "013":
                break
            rows = payload.get("list")
            if not isinstance(rows, list):
                raise CollectorContractError("OpenDART 공시 응답 list가 배열이 아니다")
            total_pages = _positive_int("total_page", payload.get("total_page", 1))
            pages.append({"page": page, "total_pages": total_pages, "rows": len(rows)})

            for index, row in enumerate(rows):
                if not isinstance(row, dict) or not (receipt := _text(row.get("rcept_no"))):
                    continue
                viewer_url = f"{OPENDART_VIEWER}?rcpNo={receipt}"
                content_ref = f"content:opendart:disclosure:{receipt}"
                observation_ref = f"observation:opendart:disclosure:{receipt}:{page}:{index}"
                contents.append(
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="document",
                            url=viewer_url,
                            title=_disclosure_title(row, receipt),
                            publisher="금융감독원 OpenDART",
                            published_at=_receipt_date(row.get("rcept_dt")),
                            language="ko",
                            metadata={
                                "corp_code": _text(row.get("corp_code")),
                                "rcept_no": receipt,
                                "original_document_supported": True,
                            },
                        ),
                    )
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="opendart",
                        platform_item_id=receipt,
                        observed_at=observed_at,
                        url=viewer_url,
                        payload=dict(row),
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref
            if page >= total_pages:
                break

        return CollectedBatch(
            contents=tuple(contents),
            observations=tuple(observations),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                mode=DISCLOSURES_MODE,
                pages=tuple(pages),
                original_document_note=ORIGINAL_DOCUMENT_NOTE,
            ),
        )

    def _collect_financials(self, query: str, options: dict[str, Any]) -> CollectedBatch:
        allowed = {
            "bsns_year",
            "corp_code",
            "fs_div",
            "mode",
            "observed_at",
            "reprt_code",
            "unit",
        }
        _reject_unknown_options(options, allowed)
        observed_at = _observed_at(options.get("observed_at", now()))
        corp_code = _required_text("corp_code", options.get("corp_code"))
        business_year = _required_text("bsns_year", options.get("bsns_year"))
        report_code = _required_text("reprt_code", options.get("reprt_code"))
        if report_code not in REPORT_CODES:
            raise CollectorContractError(f"OpenDART reprt_code가 잘못됐다: {report_code}")
        fs_div = str(options.get("fs_div", "CFS"))
        if fs_div not in FINANCIAL_DIVISIONS:
            raise CollectorContractError(f"OpenDART fs_div가 잘못됐다: {fs_div}")
        unit = _required_text("unit", options.get("unit", "KRW"))
        api_key = cast(str, (self._config or get_config()).credentials["RIA_OPENDART_API_KEY"])

        payload, _response = self._http.get_json(
            f"{OPENDART_API}/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bsns_year": business_year,
                "reprt_code": report_code,
                "fs_div": fs_div,
            },
        )
        status = _response_status(payload)
        snapshot_ref = f"snapshot:opendart:financials:{corp_code}:{business_year}:{report_code}"
        snapshots = (_snapshot(snapshot_ref, payload, observed_at, query),)
        if status == "013":
            return CollectedBatch(
                metadata=snapshot_metadata(
                    snapshots,
                    {},
                    mode=FINANCIALS_MODE,
                    original_document_note=ORIGINAL_DOCUMENT_NOTE,
                )
            )
        rows = payload.get("list")
        if not isinstance(rows, list):
            raise CollectorContractError("OpenDART 재무 응답 list가 배열이 아니다")

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in rows:
            if isinstance(raw, dict) and (receipt := _text(raw.get("rcept_no"))):
                grouped[receipt].append(raw)

        contents: list[CollectedContent] = []
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        observation_snapshots: dict[str, str] = {}
        for receipt, accounts in grouped.items():
            first = accounts[0]
            viewer_url = f"{OPENDART_VIEWER}?rcpNo={receipt}"
            content_ref = f"content:opendart:financial:{receipt}"
            observation_ref = f"observation:opendart:financial:{receipt}"
            corp_name = _text(first.get("corp_name")) or query
            contents.append(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        url=viewer_url,
                        title=f"{corp_name} {business_year} 재무제표",
                        publisher="금융감독원 OpenDART",
                        language="ko",
                        metadata={
                            "corp_code": corp_code,
                            "rcept_no": receipt,
                            "fs_div": fs_div,
                        },
                    ),
                )
            )
            observations.append(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id=self.source_id,
                    platform="opendart",
                    platform_item_id=receipt,
                    observed_at=observed_at,
                    url=viewer_url,
                    payload={"accounts": accounts, "fs_div": fs_div},
                )
            )
            observation_snapshots[observation_ref] = snapshot_ref

            for account in accounts:
                if (raw_amount := account.get("thstrm_amount")) is None:
                    continue
                account_id = _text(account.get("account_id"))
                account_name = _text(account.get("account_nm"))
                if account_id is None and account_name is None:
                    continue
                metrics.append(
                    CollectedMetric(
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        metric=MetricInput(
                            metric_name=f"opendart_account:{account_id or account_name}",
                            value=_amount(raw_amount),
                            index_type="absolute",
                            source_id=self.source_id,
                            observed_at=observed_at,
                            unit=unit,
                            denominator=None,
                            geography=None,
                            period=_text(account.get("thstrm_nm")) or business_year,
                            population=corp_name,
                            method=f"OpenDART fnlttSinglAcntAll.json ({fs_div})",
                            platform="opendart",
                        ),
                    )
                )

        return CollectedBatch(
            contents=tuple(contents),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                mode=FINANCIALS_MODE,
                original_document_note=ORIGINAL_DOCUMENT_NOTE,
            ),
        )

    def _collect_document(self, query: str, options: dict[str, Any]) -> CollectedBatch:
        allowed = {"corp_name", "mode", "observed_at", "rcept_no", "title"}
        _reject_unknown_options(options, allowed)
        observed_at = _observed_at(options.get("observed_at", now()))
        receipt = _required_text("rcept_no", options.get("rcept_no"))
        api_key = cast(str, (self._config or get_config()).credentials["RIA_OPENDART_API_KEY"])
        response = self._http.request(
            "GET",
            f"{OPENDART_API}/document.xml",
            params={"crtfc_key": api_key, "rcept_no": receipt},
        )
        if not response.content.startswith(b"PK"):
            raise CollectorContractError("OpenDART 공시 원문 응답이 ZIP 형식이 아니다")

        encoded = base64.b64encode(response.content).decode("ascii")
        viewer_url = f"{OPENDART_VIEWER}?rcpNo={receipt}"
        content_ref = f"content:opendart:document:{receipt}"
        observation_ref = f"observation:opendart:document:{receipt}"
        snapshot_ref = f"snapshot:opendart:document:{receipt}"
        return CollectedBatch(
            contents=(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        url=viewer_url,
                        title=_text(options.get("title")) or f"OpenDART 공시 원문 {receipt}",
                        publisher="금융감독원 OpenDART",
                        language="ko",
                        metadata={
                            "corp_name": _text(options.get("corp_name")),
                            "rcept_no": receipt,
                            "archive_encoding": "base64",
                        },
                    ),
                ),
            ),
            observations=(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id=self.source_id,
                    platform="opendart",
                    platform_item_id=receipt,
                    observed_at=observed_at,
                    url=viewer_url,
                    payload={"rcept_no": receipt, "archive_encoding": "base64"},
                ),
            ),
            metadata=snapshot_metadata(
                (
                    CollectedSnapshot(
                        ref=snapshot_ref,
                        snapshot=SnapshotInput(
                            source_id=self.source_id,
                            body=encoded,
                            collected_at=observed_at,
                            url=f"{OPENDART_API}/document.xml",
                            media_type="application/zip",
                            query=query,
                            meta={"rcept_no": receipt, "encoding": "base64"},
                        ),
                    ),
                ),
                {observation_ref: snapshot_ref},
                mode=DOCUMENT_MODE,
                original_document_note=ORIGINAL_DOCUMENT_NOTE,
            ),
        )


def _response_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise CollectorContractError("OpenDART 응답은 JSON object여야 한다")
    status = str(payload.get("status") or "")
    if status not in {"000", "013"}:
        raise CollectorContractError(
            f"OpenDART 응답 오류 {status or 'missing'}: {payload.get('message') or 'unknown'}"
        )
    return status


def _snapshot(
    ref: str,
    payload: dict[str, Any],
    observed_at: datetime,
    query: str,
    *,
    page: int | None = None,
) -> CollectedSnapshot:
    return CollectedSnapshot(
        ref=ref,
        snapshot=SnapshotInput(
            source_id="opendart",
            body=payload,
            collected_at=observed_at,
            url=f"{OPENDART_API}/{'list.json' if page is not None else 'fnlttSinglAcntAll.json'}",
            media_type="application/json",
            query=query,
            meta={"page": page} if page is not None else {},
        ),
    )


def _disclosure_title(row: dict[str, Any], receipt: str) -> str:
    corp_name = _text(row.get("corp_name"))
    report_name = _text(row.get("report_nm"))
    return " — ".join(part for part in (corp_name, report_name) if part) or receipt


def _receipt_date(value: Any) -> datetime | None:
    text = _text(value)
    if text is None or len(text) != 8 or not text.isdecimal():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=KST)
    except ValueError:
        return None


def _amount(value: Any) -> int | float | str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return value
    text = str(value).strip()
    normalized = text.replace(",", "")
    try:
        number = float(normalized)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _required_text(name: str, value: Any) -> str:
    if (text := _text(value)) is None:
        raise CollectorContractError(f"OpenDART {name}이 필요하다")
    return text


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorContractError(f"OpenDART {name}은 양의 정수여야 한다")
    return value


def _observed_at(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")
    return value


def _reject_unknown_options(options: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 OpenDART 옵션이다: {sorted(unknown)}")
