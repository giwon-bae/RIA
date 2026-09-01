"""World Bank Indicators API v2 collector (authority-stats, 지시서 B-2).

API key가 필요 없는 S2 수직 관통 소스다. ``query``는 공식 indicator code이며,
``country``·기간·pagination은 호출자가 명시한다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

WORLD_BANK_API = "https://api.worldbank.org/v2"
WORLD_BANK_DATA = "https://data.worldbank.org/indicator"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.;-]+$")


class WorldBankCollector(GuardedCollector):
    """공식 World Bank indicator 시계열을 정규화한다."""

    source_id = "world_bank"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        _validate_identifier("indicator", query)
        _validate_identifier("country", str(options.get("country", "all")))
        return _positive_int("max_pages", options.get("max_pages", 1))

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        allowed = {
            "country",
            "date",
            "mrv",
            "mrnev",
            "page",
            "per_page",
            "max_pages",
            "source",
            "observed_at",
            "unit",
        }
        _reject_unknown_options(options, allowed)

        country = str(options.get("country", "all"))
        _validate_identifier("country", country)
        max_pages = _positive_int("max_pages", options.get("max_pages", 1))
        first_page = _positive_int("page", options.get("page", 1))
        per_page = _positive_int("per_page", options.get("per_page", 100))
        observed_at = options.get("observed_at", now())
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")
        if options.get("date") is not None and (
            options.get("mrv") is not None or options.get("mrnev") is not None
        ):
            raise CollectorContractError("date와 mrv/mrnev는 함께 쓸 수 없다")

        endpoint = (
            f"{WORLD_BANK_API}/country/{quote(country, safe='.;')}/indicator/"
            f"{quote(query, safe='.;')}"
        )
        base_params: dict[str, Any] = {"format": "json", "per_page": per_page}
        for key in ("date", "mrv", "mrnev", "source"):
            if (value := options.get(key)) is not None:
                base_params[key] = value

        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}
        page_metadata: list[dict[str, Any]] = []

        for page_offset in range(max_pages):
            page = first_page + page_offset
            payload, response = self._http.get_json(endpoint, params={**base_params, "page": page})
            metadata, rows = _parse_response(payload)
            page_metadata.append(metadata)
            snapshot_ref = f"snapshot:world_bank:{page}"
            snapshots.append(
                CollectedSnapshot(
                    ref=snapshot_ref,
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=payload,
                        collected_at=observed_at,
                        url=response.url,
                        media_type="application/json",
                        query=query,
                        meta={"page": page, "indicator": query, "country": country},
                    ),
                )
            )

            for row_index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                indicator = _nested_value(row, "indicator", "id") or query
                indicator_name = _nested_value(row, "indicator", "value") or query
                country_id = str(row.get("countryiso3code") or "")
                if not country_id:
                    country_id = str(_nested_value(row, "country", "id") or country)
                country_name = str(_nested_value(row, "country", "value") or country_id)
                period = str(row.get("date") or "")
                content_ref = f"content:world_bank:{indicator}:{country_id}"
                data_url = (
                    f"{WORLD_BANK_DATA}/{quote(str(indicator), safe='.;')}"
                    f"?locations={quote(country_id, safe=';')}"
                )
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="document",
                            url=data_url,
                            title=f"{indicator_name} — {country_name}",
                            publisher="World Bank",
                            language="en",
                            metadata={
                                "indicator_id": indicator,
                                "country_id": country_id,
                            },
                        ),
                    ),
                )
                observation_ref = (
                    f"observation:world_bank:{indicator}:{country_id}:{period}:{page}:{row_index}"
                )
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="world_bank",
                        platform_item_id=f"{indicator}:{country_id}:{period}",
                        observed_at=observed_at,
                        url=data_url,
                        payload=dict(row),
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref

                if row.get("value") is not None:
                    metrics.append(
                        CollectedMetric(
                            content_ref=content_ref,
                            observation_ref=observation_ref,
                            metric=MetricInput(
                                metric_name=str(indicator),
                                value=row["value"],
                                index_type="absolute",
                                source_id=self.source_id,
                                observed_at=observed_at,
                                unit=_optional_text(options.get("unit") or row.get("unit")),
                                denominator=None,
                                geography=country_name,
                                period=period or None,
                                population=None,
                                method="World Bank Indicators API v2",
                                platform="world_bank",
                            ),
                        )
                    )

            total_pages = _positive_int("pages", metadata.get("pages", 1))
            if page >= total_pages:
                break

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                pages=page_metadata,
                indicator=query,
                country=country,
            ),
        )


def _parse_response(payload: Any) -> tuple[dict[str, Any], list[Any]]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise CollectorContractError("World Bank 응답은 [page metadata, rows] 형식이어야 한다")
    metadata, rows = payload
    if not isinstance(metadata, dict) or not isinstance(rows, list):
        raise CollectorContractError("World Bank page metadata 또는 rows 형식이 잘못됐다")
    return metadata, rows


def _nested_value(row: dict[str, Any], key: str, nested: str) -> Any:
    value = row.get(key)
    return value.get(nested) if isinstance(value, dict) else None


def _validate_identifier(name: str, value: str) -> None:
    if not value or _IDENTIFIER.fullmatch(value) is None:
        raise CollectorContractError(f"{name} 식별자 형식이 잘못됐다: {value!r}")


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CollectorContractError(f"{name}은 양의 정수여야 한다")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reject_unknown_options(options: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 World Bank 옵션이다: {sorted(unknown)}")
