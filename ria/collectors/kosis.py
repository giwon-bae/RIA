"""KOSIS API Hub 이전과 무관한 공식 OpenAPI collector (authority-stats, B-2).

통계표 식별자를 추측하지 않는다. 호출자는 공식 KOSIS에서 확인한 ``org_id``·
``table_id``·``object_l1``·``item_id``·``period_type``을 모두 제공해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
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
from ria.config import Config, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

KOSIS_API = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
KOSIS_TABLE = "https://kosis.kr/statHtml/statHtml.do"


@dataclass(frozen=True)
class KosisDatasetSpec:
    """KOSIS 통계표의 공식 selector. 값은 호출자가 포털에서 확인해 전달한다."""

    org_id: str
    table_id: str
    object_l1: str
    item_id: str
    period_type: str
    start_period: str | None = None
    end_period: str | None = None
    latest_count: int | None = 1

    @property
    def dataset_id(self) -> str:
        return f"{self.org_id}/{self.table_id}"

    def validate(self) -> None:
        required = {
            "org_id": self.org_id,
            "table_id": self.table_id,
            "object_l1": self.object_l1,
            "item_id": self.item_id,
            "period_type": self.period_type,
        }
        if missing := [name for name, value in required.items() if not value.strip()]:
            raise CollectorContractError(
                f"KOSIS 통계표 식별자가 비었다: {', '.join(sorted(missing))}"
            )
        if (self.start_period is None) != (self.end_period is None):
            raise CollectorContractError("KOSIS start_period와 end_period는 함께 지정해야 한다")
        if self.start_period is not None and self.latest_count is not None:
            raise CollectorContractError("KOSIS 기간 범위와 latest_count는 함께 쓸 수 없다")
        if self.latest_count is not None and self.latest_count <= 0:
            raise CollectorContractError("KOSIS latest_count는 양수여야 한다")


class KosisCollector(GuardedCollector):
    source_id = "kosis"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        # Dataset selector validation belongs after the source Guard.  This preserves
        # ``missing_credential`` as the first decision when both the key and selector
        # are absent, while a keyed but incomplete request still fails before HTTP.
        return 1

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        if unknown := set(options) - {"dataset", "observed_at"}:
            raise CollectorContractError(f"지원하지 않는 KOSIS 옵션이다: {sorted(unknown)}")
        dataset = _dataset(options)
        dataset.validate()
        observed_at = options.get("observed_at", now())
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")

        config = cast(Config, self._config or get_config())
        api_key = cast(str, config.credentials["RIA_KOSIS_API_KEY"])
        params: dict[str, Any] = {
            "method": "getList",
            "format": "json",
            "jsonVD": "Y",
            "apiKey": api_key,
            "orgId": dataset.org_id,
            "tblId": dataset.table_id,
            "objL1": dataset.object_l1,
            "itmId": dataset.item_id,
            "prdSe": dataset.period_type,
        }
        if dataset.start_period is not None:
            params.update(startPrdDe=dataset.start_period, endPrdDe=dataset.end_period)
        elif dataset.latest_count is not None:
            params["newEstPrdCnt"] = dataset.latest_count

        payload, _response = self._http.get_json(KOSIS_API, params=params)
        if not isinstance(payload, list):
            raise CollectorContractError("KOSIS 응답은 row 배열이어야 한다")

        snapshot_ref = f"snapshot:kosis:{dataset.dataset_id}"
        snapshots = (
            CollectedSnapshot(
                ref=snapshot_ref,
                snapshot=SnapshotInput(
                    source_id=self.source_id,
                    body=payload,
                    collected_at=observed_at,
                    url=KOSIS_API,
                    media_type="application/json",
                    query=query,
                    meta={"dataset_id": dataset.dataset_id},
                ),
            ),
        )
        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        observation_snapshots: dict[str, str] = {}

        for index, raw in enumerate(payload):
            if not isinstance(raw, dict):
                continue
            table_id = str(raw.get("TBL_ID") or dataset.table_id)
            item_id = str(raw.get("ITM_ID") or dataset.item_id)
            class_id = str(raw.get("C1") or dataset.object_l1)
            period = str(raw.get("PRD_DE") or "")
            content_ref = f"content:kosis:{dataset.org_id}:{table_id}:{item_id}:{class_id}"
            table_url = f"{KOSIS_TABLE}?orgId={quote(dataset.org_id)}&tblId={quote(table_id)}"
            contents.setdefault(
                content_ref,
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        url=table_url,
                        title=str(raw.get("TBL_NM") or query),
                        publisher="KOSIS 국가통계포털",
                        language="ko",
                        metadata={"dataset_id": dataset.dataset_id, "item_id": item_id},
                    ),
                ),
            )
            observation_ref = f"observation:kosis:{dataset.dataset_id}:{index}:{period}"
            observations.append(
                CollectedObservation(
                    ref=observation_ref,
                    content_ref=content_ref,
                    source_id=self.source_id,
                    platform="kosis",
                    platform_item_id=f"{table_id}:{item_id}:{class_id}:{period}",
                    observed_at=observed_at,
                    url=table_url,
                    payload=dict(raw),
                )
            )
            observation_snapshots[observation_ref] = snapshot_ref
            value = _metric_value(raw.get("DT"))
            if value is not None:
                metrics.append(
                    CollectedMetric(
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        metric=MetricInput(
                            metric_name=str(raw.get("ITM_NM") or item_id),
                            value=value,
                            index_type="absolute",
                            source_id=self.source_id,
                            observed_at=observed_at,
                            unit=_text_or_none(raw.get("UNIT_NM")),
                            denominator=None,
                            geography=_text_or_none(raw.get("C1_NM")),
                            period=period or None,
                            population=None,
                            method="KOSIS OpenAPI",
                            platform="kosis",
                        ),
                    )
                )

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                dataset_id=dataset.dataset_id,
            ),
        )


def _dataset(options: dict[str, Any]) -> KosisDatasetSpec:
    dataset = options.get("dataset")
    if not isinstance(dataset, KosisDatasetSpec):
        raise CollectorContractError("KOSIS는 확인된 KosisDatasetSpec 식별자 없이 호출할 수 없다")
    return dataset


def _metric_value(value: Any) -> int | float | str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace(",", "")
    try:
        number = float(normalized)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
