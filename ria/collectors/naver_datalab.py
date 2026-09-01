"""NAVER API Hub 검색어 트렌드 collector (search-demand, B-4).

응답의 ratio는 요청 범위 최대값을 100으로 둔 상대 지수다. 절대 검색량으로 해석할
수 있는 이름이나 단위를 만들지 않고, Observation과 Metric 양쪽에 상대 지수임을 남긴다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, cast

from ria.collectors.base import (
    CollectedBatch,
    CollectedContent,
    CollectedMetric,
    CollectedObservation,
    CollectorContractError,
    GuardedCollector,
)
from ria.collectors.naver_search import (
    NAVER_API_HUB_BASE,
    _api_headers,
    _observed_at,
)
from ria.collectors.persistence import CollectedSnapshot, snapshot_metadata
from ria.config import Config, get_config, now
from ria.core.entities import ContentItemInput
from ria.core.metrics import MetricInput
from ria.core.snapshots import SnapshotInput
from ria.http import HttpClient
from ria.policy.guard import PolicyAllowed

NAVER_DATALAB_ENDPOINT = f"{NAVER_API_HUB_BASE}/search-trend/v1/search"
RELATIVE_INDEX_TYPE = "relative"
RELATIVE_INDEX_UNIT = "relative_index_0_100"
RELATIVE_INDEX_DENOMINATOR = "요청 범위 최대값=100"

_TIME_UNITS = frozenset({"date", "week", "month"})
_DEVICES = frozenset({"pc", "mo"})
_GENDERS = frozenset({"m", "f"})


class NaverDataLabCollector(GuardedCollector):
    """검색어 그룹별 기간 ratio를 상대지수 Metric으로 정규화한다."""

    source_id = "naver_datalab"

    def __init__(self, *, http: HttpClient | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._http = http or HttpClient()

    def _collect(
        self,
        query: str,
        *,
        policy: PolicyAllowed,
        **options: Any,
    ) -> CollectedBatch:
        del policy
        _reject_unknown_options(options)
        observed_at = _observed_at(options.get("observed_at", now()))
        body = _request_body(query, options)
        config = cast(Config, self._config or get_config())
        payload, response = self._http.post_json(
            NAVER_DATALAB_ENDPOINT,
            headers=_api_headers(config, self.source_id),
            json_body=body,
        )
        results = _results(payload)
        snapshot_ref = "snapshot:naver_datalab:search"
        snapshot = CollectedSnapshot(
            ref=snapshot_ref,
            snapshot=SnapshotInput(
                source_id=self.source_id,
                body=payload,
                collected_at=observed_at,
                url=response.url,
                media_type="application/json",
                query=query,
                meta={
                    "start_date": body["startDate"],
                    "end_date": body["endDate"],
                    "time_unit": body["timeUnit"],
                    "index_type": RELATIVE_INDEX_TYPE,
                },
            ),
        )

        contents: list[CollectedContent] = []
        observations: list[CollectedObservation] = []
        metrics: list[CollectedMetric] = []
        observation_snapshots: dict[str, str] = {}
        for result_index, result in enumerate(results):
            if not isinstance(result, Mapping):
                raise CollectorContractError("NAVER DataLab results 항목이 객체가 아니다")
            title = _required_text("results.title", result.get("title"))
            keywords = _text_sequence("results.keywords", result.get("keywords", ()))
            points = result.get("data")
            if not isinstance(points, list):
                raise CollectorContractError("NAVER DataLab result.data가 배열이 아니다")
            digest = hashlib.sha256(
                f"{result_index}:{title}:{'|'.join(keywords)}".encode()
            ).hexdigest()[:20]
            content_ref = f"content:naver_datalab:{digest}"
            contents.append(
                CollectedContent(
                    ref=content_ref,
                    item=ContentItemInput(
                        content_type="document",
                        title=f"NAVER DataLab 검색 트렌드 — {title}",
                        publisher="NAVER DataLab",
                        language="ko",
                        metadata={
                            "keyword_group": title,
                            "keywords": keywords,
                            "index_type": RELATIVE_INDEX_TYPE,
                            "unit": RELATIVE_INDEX_UNIT,
                        },
                    ),
                )
            )
            for point_index, point in enumerate(points):
                if not isinstance(point, Mapping):
                    raise CollectorContractError("NAVER DataLab data 항목이 객체가 아니다")
                period = _required_text("data.period", point.get("period"))
                ratio = _ratio(point.get("ratio"))
                observation_ref = f"observation:naver_datalab:{digest}:{period}:{point_index}"
                observations.append(
                    CollectedObservation(
                        ref=observation_ref,
                        content_ref=content_ref,
                        source_id=self.source_id,
                        platform="naver_datalab",
                        platform_item_id=f"{title}:{period}",
                        observed_at=observed_at,
                        url=NAVER_DATALAB_ENDPOINT,
                        payload={
                            "title": title,
                            "keywords": keywords,
                            "period": period,
                            "ratio": ratio,
                            "index_type": RELATIVE_INDEX_TYPE,
                            "unit": RELATIVE_INDEX_UNIT,
                            "denominator": RELATIVE_INDEX_DENOMINATOR,
                        },
                    )
                )
                observation_snapshots[observation_ref] = snapshot_ref
                metrics.append(
                    CollectedMetric(
                        content_ref=content_ref,
                        observation_ref=observation_ref,
                        metric=MetricInput(
                            metric_name="search_interest_index",
                            value=ratio,
                            index_type="relative",
                            source_id=self.source_id,
                            observed_at=observed_at,
                            unit=RELATIVE_INDEX_UNIT,
                            denominator=RELATIVE_INDEX_DENOMINATOR,
                            geography="KR",
                            period=period,
                            population=None,
                            method="NAVER API Hub Search Trend",
                            platform="naver_datalab",
                        ),
                    )
                )

        return CollectedBatch(
            contents=tuple(contents),
            observations=tuple(observations),
            metrics=tuple(metrics),
            metadata=snapshot_metadata(
                (snapshot,),
                observation_snapshots,
                start_date=payload.get("startDate", body["startDate"]),
                end_date=payload.get("endDate", body["endDate"]),
                time_unit=payload.get("timeUnit", body["timeUnit"]),
                index_type=RELATIVE_INDEX_TYPE,
            ),
        )


def _request_body(query: str, options: dict[str, Any]) -> dict[str, Any]:
    start_date = _iso_date("start_date", options.get("start_date"))
    end_date = _iso_date("end_date", options.get("end_date"))
    if start_date > end_date:
        raise CollectorContractError("start_date는 end_date보다 늦을 수 없다")
    time_unit = str(options.get("time_unit", "date"))
    if time_unit not in _TIME_UNITS:
        raise CollectorContractError(f"time_unit이 잘못됐다: {time_unit!r}")
    body: dict[str, Any] = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "timeUnit": time_unit,
        "keywordGroups": _keyword_groups(query, options.get("keyword_groups")),
    }
    _add_filters(body, options)
    return body


def _keyword_groups(query: str, raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return [{"groupName": query, "keywords": [query]}]
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise CollectorContractError("keyword_groups는 객체 배열이어야 한다")
    if not 1 <= len(raw) <= 5:
        raise CollectorContractError("keyword_groups는 1..5개여야 한다")
    groups: list[dict[str, Any]] = []
    for group in raw:
        if not isinstance(group, Mapping):
            raise CollectorContractError("keyword_groups 항목은 객체여야 한다")
        name = _required_text("groupName", group.get("groupName", group.get("group_name")))
        keywords = _text_sequence("keywords", group.get("keywords"))
        if not 1 <= len(keywords) <= 20:
            raise CollectorContractError("keywords는 그룹당 1..20개여야 한다")
        groups.append({"groupName": name, "keywords": list(keywords)})
    return groups


def _add_filters(body: dict[str, Any], options: dict[str, Any]) -> None:
    if (device := options.get("device")) is not None:
        device = str(device)
        if device not in _DEVICES:
            raise CollectorContractError(f"device가 잘못됐다: {device!r}")
        body["device"] = device
    if (gender := options.get("gender")) is not None:
        gender = str(gender)
        if gender not in _GENDERS:
            raise CollectorContractError(f"gender가 잘못됐다: {gender!r}")
        body["gender"] = gender
    if (ages := options.get("ages")) is not None:
        body["ages"] = list(_text_sequence("ages", ages))


def _results(payload: Any) -> list[Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise CollectorContractError("NAVER DataLab 응답 results가 배열이 아니다")
    return payload["results"]


def _iso_date(name: str, value: Any) -> date:
    if isinstance(value, datetime):
        raise CollectorContractError(f"{name}은 YYYY-MM-DD 날짜여야 한다")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise CollectorContractError(f"{name}은 YYYY-MM-DD 날짜여야 한다")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CollectorContractError(f"{name}은 YYYY-MM-DD 날짜여야 한다") from error


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise CollectorContractError(f"{name}은 비어 있지 않은 문자열이어야 한다")
    return text


def _text_sequence(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise CollectorContractError(f"{name}은 문자열 배열이어야 한다")
    result = tuple(_required_text(name, item) for item in value)
    if not result:
        raise CollectorContractError(f"{name}은 하나 이상이어야 한다")
    return result


def _ratio(value: Any) -> int | float:
    if isinstance(value, bool):
        raise CollectorContractError("ratio는 숫자여야 한다")
    if isinstance(value, int | float):
        number: int | float = value
        if not 0 <= number <= 100:
            raise CollectorContractError("ratio는 0..100 상대지수여야 한다")
        return number
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError as error:
            raise CollectorContractError("ratio는 숫자여야 한다") from error
        if not 0 <= number <= 100:
            raise CollectorContractError("ratio는 0..100 상대지수여야 한다")
        return int(number) if number.is_integer() else number
    raise CollectorContractError("ratio는 숫자여야 한다")


def _reject_unknown_options(options: dict[str, Any]) -> None:
    allowed = {
        "ages",
        "device",
        "end_date",
        "gender",
        "keyword_groups",
        "observed_at",
        "start_date",
        "time_unit",
    }
    if unknown := set(options) - allowed:
        raise CollectorContractError(f"지원하지 않는 NAVER DataLab 옵션이다: {sorted(unknown)}")
