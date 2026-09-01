"""공공데이터포털 데이터셋별 OpenAPI collector (authority-stats, B-2).

포털 전체를 하나의 API로 가정하지 않는다. 승인된 dataset ID·endpoint·정책 URL·응답
field mapping을 호출자가 제공해야 하며, 누락 시 HTTP 전에 실패한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

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

_ALLOWED_ENDPOINT_HOSTS = frozenset({"apis.data.go.kr", "api.odcloud.kr"})


@dataclass(frozen=True)
class DataGoKrDatasetSpec:
    """활용신청이 끝난 데이터셋의 요청·정규화 계약."""

    dataset_id: str
    endpoint: str
    policy_url: str
    items_path: tuple[str, ...]
    request_params: Mapping[str, Any] = field(default_factory=dict)
    id_field: str | None = None
    title_field: str | None = None
    metric_field: str | None = None
    metric_name: str | None = None
    unit_field: str | None = None
    geography_field: str | None = None
    period_field: str | None = None
    approved: bool = False
    storage_allowed: bool = False
    page_parameter: str = "pageNo"
    page_size_parameter: str = "numOfRows"
    page_size: int = 100
    max_pages: int = 1
    key_parameter: str = "serviceKey"

    def validate(self) -> None:
        if not self.dataset_id.strip():
            raise CollectorContractError("data.go.kr dataset_id가 필요하다")
        endpoint = urlsplit(self.endpoint)
        if endpoint.scheme != "https" or not endpoint.hostname:
            raise CollectorContractError("data.go.kr endpoint는 확인된 HTTPS URL이어야 한다")
        if endpoint.hostname not in _ALLOWED_ENDPOINT_HOSTS:
            raise CollectorContractError(
                f"data.go.kr 승인 endpoint host가 아니다: {endpoint.hostname}"
            )
        policy = urlsplit(self.policy_url)
        if policy.scheme != "https" or not policy.hostname:
            raise CollectorContractError("data.go.kr dataset policy URL이 필요하다")
        if not self.items_path or not all(self.items_path):
            raise CollectorContractError("data.go.kr items_path가 필요하다")
        if not self.approved:
            raise CollectorContractError("data.go.kr 데이터셋 활용신청 승인이 확인되지 않았다")
        if not self.storage_allowed:
            raise CollectorContractError("data.go.kr 데이터셋 저장 허용이 확인되지 않았다")
        if self.page_size <= 0 or self.max_pages <= 0:
            raise CollectorContractError("data.go.kr page_size/max_pages는 양수여야 한다")
        if (self.metric_field is None) != (self.metric_name is None):
            raise CollectorContractError("metric_field와 metric_name은 함께 지정해야 한다")


class DataGoKrCollector(GuardedCollector):
    source_id = "data_go_kr"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _estimate_requested_calls(self, query: str, options: dict[str, Any]) -> int:
        # The Guard must get the first opportunity to report missing credentials.
        # A provided spec can still contribute its pagination cost without validating
        # identifiers; full dataset approval validation runs in ``_collect`` pre-HTTP.
        dataset = options.get("dataset")
        return dataset.max_pages if isinstance(dataset, DataGoKrDatasetSpec) else 1

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        if unknown := set(options) - {"dataset", "observed_at"}:
            raise CollectorContractError(f"지원하지 않는 data.go.kr 옵션이다: {sorted(unknown)}")
        dataset = _dataset(options)
        dataset.validate()
        observed_at = options.get("observed_at", now())
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise CollectorContractError("observed_at은 timezone-aware datetime이어야 한다")
        config = cast(Config, self._config or get_config())
        api_key = cast(str, config.credentials["RIA_DATA_GO_KR_KEY"])

        contents: dict[str, CollectedContent] = {}
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        snapshots: list[CollectedSnapshot] = []
        observation_snapshots: dict[str, str] = {}

        for page in range(1, dataset.max_pages + 1):
            params = dict(dataset.request_params)
            forbidden = {dataset.key_parameter, dataset.page_parameter, dataset.page_size_parameter}
            if forbidden & set(params):
                raise CollectorContractError(
                    "data.go.kr request_params에 key/page 제어 필드를 직접 넣을 수 없다"
                )
            params.update(
                {
                    dataset.key_parameter: api_key,
                    dataset.page_parameter: page,
                    dataset.page_size_parameter: dataset.page_size,
                }
            )
            payload, _response = self._http.get_json(dataset.endpoint, params=params)
            _validate_data_go_response(payload)
            rows = _at_path(payload, dataset.items_path)
            if isinstance(rows, dict) and "item" in rows:
                rows = rows["item"]
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list):
                raise CollectorContractError("data.go.kr items_path 결과는 row 배열이어야 한다")

            snapshot_ref = f"snapshot:data_go_kr:{dataset.dataset_id}:{page}"
            snapshots.append(
                CollectedSnapshot(
                    ref=snapshot_ref,
                    snapshot=SnapshotInput(
                        source_id=self.source_id,
                        body=payload,
                        collected_at=observed_at,
                        url=dataset.endpoint,
                        media_type="application/json",
                        query=query,
                        meta={
                            "dataset_id": dataset.dataset_id,
                            "policy_url": dataset.policy_url,
                            "page": page,
                        },
                    ),
                )
            )

            for row_index, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    continue
                row_id = _field_text(raw, dataset.id_field) or f"{page}:{row_index}"
                content_ref = f"content:data_go_kr:{dataset.dataset_id}:{row_id}"
                title = _field_text(raw, dataset.title_field) or query
                contents.setdefault(
                    content_ref,
                    CollectedContent(
                        ref=content_ref,
                        item=ContentItemInput(
                            content_type="document",
                            url=dataset.endpoint,
                            title=title,
                            publisher="공공데이터포털",
                            language="ko",
                            metadata={
                                "dataset_id": dataset.dataset_id,
                                "policy_url": dataset.policy_url,
                            },
                        ),
                    ),
                )
                observation_ref = f"observation:data_go_kr:{dataset.dataset_id}:{page}:{row_index}"
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="data_go_kr",
                        platform_item_id=row_id,
                        observed_at=observed_at,
                        url=dataset.endpoint,
                        payload=dict(raw),
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref

                if dataset.metric_field is not None and raw.get(dataset.metric_field) is not None:
                    metrics.append(
                        CollectedMetric(
                            content_ref=content_ref,
                            observation_ref=observation_ref,
                            metric=MetricInput(
                                metric_name=cast(str, dataset.metric_name),
                                value=_metric_value(raw[dataset.metric_field]),
                                index_type="absolute",
                                source_id=self.source_id,
                                observed_at=observed_at,
                                unit=_field_text(raw, dataset.unit_field),
                                denominator=None,
                                geography=_field_text(raw, dataset.geography_field),
                                period=_field_text(raw, dataset.period_field),
                                population=None,
                                method=f"data.go.kr dataset {dataset.dataset_id}",
                                platform="data_go_kr",
                            ),
                        )
                    )

            if len(rows) < dataset.page_size:
                break

        return CollectedBatch(
            contents=tuple(contents.values()),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                snapshots,
                observation_snapshots,
                dataset_id=dataset.dataset_id,
                policy_url=dataset.policy_url,
            ),
        )


def _dataset(options: dict[str, Any]) -> DataGoKrDatasetSpec:
    dataset = options.get("dataset")
    if not isinstance(dataset, DataGoKrDatasetSpec):
        raise CollectorContractError("data.go.kr는 승인된 DataGoKrDatasetSpec 없이 호출할 수 없다")
    return dataset


def _at_path(payload: Any, path: tuple[str, ...]) -> Any:
    value = payload
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise CollectorContractError(f"data.go.kr 응답에 items_path가 없다: {path}")
        value = value[key]
    return value


def _validate_data_go_response(payload: Any) -> None:
    if not isinstance(payload, Mapping):
        raise CollectorContractError("data.go.kr 응답은 JSON object여야 한다")
    response = payload.get("response")
    header = response.get("header") if isinstance(response, Mapping) else None
    if isinstance(header, Mapping):
        code = str(header.get("resultCode") or "")
        if code and code not in {"0", "00", "0000"}:
            raise CollectorContractError(
                f"data.go.kr 응답 오류 {code}: {header.get('resultMsg') or 'unknown'}"
            )


def _field_text(row: Mapping[str, Any], field_name: str | None) -> str | None:
    if field_name is None or row.get(field_name) is None:
        return None
    text = str(row[field_name]).strip()
    return text or None


def _metric_value(value: Any) -> int | float | str:
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
