"""지표 저장 — append-only (DESIGN §10.3).

같은 지표의 후속 관측은 UPDATE 가 아니라 INSERT 다. 그래야 절대값뿐 아니라 변화
속도와 지속성을 분석할 수 있다. 그래서 이 모듈에는 갱신 함수가 없다.

모든 지표는 `index_type` 을 명시해야 한다. 상대 지수(Naver DataLab · Google Trends)를
절대 검색량으로 표현하는 경로를 막기 위해서다 (DESIGN §6.3). 스키마에도 같은 제약이
CHECK 로 들어가 있다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ria.config import parse_iso8601, to_iso8601
from ria.core.entities import new_id
from ria.core.store import Store

IndexType = Literal["absolute", "relative"]


@dataclass(frozen=True)
class MetricInput:
    """지표 관측 1건.

    단위·분모·지역·기간·모집단·측정방법은 적용되지 않으면 None 으로 남기되
    필드 자체는 항상 존재한다 (DESIGN §13.2).
    """

    metric_name: str
    value: int | float | str
    index_type: IndexType
    source_id: str
    observed_at: datetime
    unit: str | None = None
    denominator: str | None = None
    geography: str | None = None
    period: str | None = None
    population: str | None = None
    method: str | None = None
    platform: str | None = None
    content_item_id: str | None = None
    entity_id: str | None = None
    observation_id: str | None = None
    research_id: str | None = None


@dataclass(frozen=True)
class MetricRecord:
    """저장된 지표 1행."""

    metric_row_id: str
    metric_name: str
    value: float | str
    index_type: str
    unit: str | None
    denominator: str | None
    geography: str | None
    period: str | None
    population: str | None
    method: str | None
    source_id: str
    platform: str | None
    content_item_id: str | None
    entity_id: str | None
    observation_id: str | None
    research_id: str | None
    observed_at: datetime
    created_at: str


def record_metric(store: Store, metric: MetricInput, *, now: datetime) -> str:
    """지표를 새 행으로 남긴다. 같은 지표의 이전 관측을 건드리지 않는다."""
    if metric.observed_at.tzinfo is None:
        raise ValueError("observed_at 은 timezone-aware 여야 한다")

    value_num: float | None = None
    value_text: str | None = None
    if isinstance(metric.value, bool):
        raise ValueError("bool 은 지표 값이 아니다")
    if isinstance(metric.value, int | float):
        value_num = float(metric.value)
    else:
        value_text = metric.value

    metric_row_id = new_id("met")
    store.connection.execute(
        "INSERT INTO metrics (metric_row_id, metric_name, value_num, value_text, unit,"
        " denominator, geography, period, population, method, index_type, source_id, platform,"
        " content_item_id, entity_id, observation_id, research_id, observed_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            metric_row_id,
            metric.metric_name,
            value_num,
            value_text,
            metric.unit,
            metric.denominator,
            metric.geography,
            metric.period,
            metric.population,
            metric.method,
            metric.index_type,
            metric.source_id,
            metric.platform,
            metric.content_item_id,
            metric.entity_id,
            metric.observation_id,
            metric.research_id,
            to_iso8601(metric.observed_at),
            to_iso8601(now),
        ),
    )
    return metric_row_id


def get_metric_history(
    store: Store,
    metric_name: str,
    *,
    source_id: str | None = None,
    platform: str | None = None,
    content_item_id: str | None = None,
    entity_id: str | None = None,
    research_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[MetricRecord]:
    """한 지표의 관측 이력을 시간순으로. 여러 행이 나오는 것이 정상이다."""
    clauses = ["metric_name = ?"]
    params: list[Any] = [metric_name]

    for column, value in (
        ("source_id", source_id),
        ("platform", platform),
        ("content_item_id", content_item_id),
        ("entity_id", entity_id),
        ("research_id", research_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)

    if since is not None:
        clauses.append("observed_at >= ?")
        params.append(to_iso8601(since))
    if until is not None:
        clauses.append("observed_at <= ?")
        params.append(to_iso8601(until))

    sql = (
        "SELECT * FROM metrics WHERE "
        + " AND ".join(clauses)
        + " ORDER BY observed_at ASC, created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = store.connection.execute(sql, params).fetchall()
    return [_to_record(row) for row in rows]


def latest_metric(store: Store, metric_name: str, **filters: Any) -> MetricRecord | None:
    """가장 최근 관측 1건. 이전 관측을 지우지 않고 그냥 마지막 것을 고른다."""
    history = get_metric_history(store, metric_name, **filters)
    return history[-1] if history else None


def _to_record(row: Any) -> MetricRecord:
    value = row["value_num"] if row["value_num"] is not None else row["value_text"]
    return MetricRecord(
        metric_row_id=row["metric_row_id"],
        metric_name=row["metric_name"],
        value=value,
        index_type=row["index_type"],
        unit=row["unit"],
        denominator=row["denominator"],
        geography=row["geography"],
        period=row["period"],
        population=row["population"],
        method=row["method"],
        source_id=row["source_id"],
        platform=row["platform"],
        content_item_id=row["content_item_id"],
        entity_id=row["entity_id"],
        observation_id=row["observation_id"],
        research_id=row["research_id"],
        observed_at=parse_iso8601(row["observed_at"]),
        created_at=row["created_at"],
    )
