"""A-8. Entity · Observation · Metric 분리 저장 (DESIGN §10.2 · §10.3)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from ria.config import KST
from ria.core.entities import (
    ContentItemInput,
    EntityInput,
    find_content_item_by_url,
    get_content_item,
    upsert_content_item,
    upsert_entity,
)
from ria.core.metrics import MetricInput, get_metric_history, latest_metric, record_metric
from ria.core.observations import (
    ObservationInput,
    count_observations,
    list_observations,
    platforms_for,
    record_observation,
)
from ria.core.store import Store

T0 = datetime(2026, 8, 27, 10, 0, tzinfo=KST)
ARTICLE = "https://example.com/ai-vision-inspection"


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as store:
        yield store


# --- ContentItem 과 Observation 의 분리 --------------------------------------
def test_same_url_on_two_platforms_gives_one_content_two_observations(store: Store) -> None:
    """지시서 A-8 필수 검증. 같은 외부 URL 이 HN 과 Reddit 에 올라온 경우."""
    hn_id = upsert_content_item(
        store,
        ContentItemInput(
            content_type="article", url=f"{ARTICLE}?utm_source=hn", title="AI 비전검사"
        ),
        now=T0,
    )
    reddit_id = upsert_content_item(
        store,
        ContentItemInput(content_type="article", url=f"{ARTICLE}/?fbclid=abc"),
        now=T0,
    )

    assert hn_id == reddit_id

    record_observation(
        store,
        ObservationInput(
            content_item_id=hn_id,
            source_id="hacker_news",
            platform="hacker_news",
            platform_item_id="41234567",
            observed_at=T0,
            payload={"score": 120, "comments": 34},
        ),
        now=T0,
    )
    record_observation(
        store,
        ObservationInput(
            content_item_id=reddit_id,
            source_id="reddit",
            platform="reddit",
            platform_item_id="t3_abcdef",
            observed_at=T0,
            payload={"subreddit": "MachineLearning", "score": 88, "comments": 12},
        ),
        now=T0,
    )

    content_count = store.connection.execute("SELECT COUNT(*) AS n FROM content_items").fetchone()[
        "n"
    ]

    assert content_count == 1
    assert count_observations(store, hn_id) == 2
    assert platforms_for(store, hn_id) == ["hacker_news", "reddit"]


def test_platform_observations_are_never_merged(store: Store) -> None:
    """플랫폼별 payload 가 서로를 덮어쓰지 않는다."""
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )
    for platform, payload in (
        ("hacker_news", {"score": 120}),
        ("reddit", {"score": 88}),
    ):
        record_observation(
            store,
            ObservationInput(
                content_item_id=content_id,
                source_id=platform,
                platform=platform,
                observed_at=T0,
                payload=payload,
            ),
            now=T0,
        )

    by_platform = {
        o.platform: o.payload["score"] for o in list_observations(store, content_item_id=content_id)
    }

    assert by_platform == {"hacker_news": 120, "reddit": 88}


def test_recollecting_the_same_observation_creates_a_new_row(store: Store) -> None:
    """같은 (source, source_id, observed_at) 재수집도 새 행이다."""
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )
    payload = ObservationInput(
        content_item_id=content_id,
        source_id="hacker_news",
        platform="hacker_news",
        platform_item_id="41234567",
        observed_at=T0,
        payload={"score": 120},
    )

    first = record_observation(store, payload, now=T0)
    second = record_observation(store, payload, now=T0 + timedelta(minutes=1))

    assert first != second
    assert count_observations(store, content_id) == 2


def test_observations_module_exposes_no_update_path(store: Store) -> None:
    """덮어쓰기 함수를 만들지 않는 것이 계약이다."""
    import ria.core.observations as module

    exported = {name for name in dir(module) if not name.startswith("_")}

    assert not {n for n in exported if "update" in n or "delete" in n}


def test_naive_observed_at_is_rejected(store: Store) -> None:
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        record_observation(
            store,
            ObservationInput(
                content_item_id=content_id,
                source_id="hacker_news",
                platform="hacker_news",
                observed_at=datetime(2026, 8, 27, 10, 0),
            ),
            now=T0,
        )


def test_observations_are_returned_in_time_order(store: Store) -> None:
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )
    for offset in (2, 0, 1):
        record_observation(
            store,
            ObservationInput(
                content_item_id=content_id,
                source_id="hacker_news",
                platform="hacker_news",
                observed_at=T0 + timedelta(hours=offset),
                payload={"score": offset},
            ),
            now=T0,
        )

    scores = [o.payload["score"] for o in list_observations(store, content_item_id=content_id)]

    assert scores == [0, 1, 2]


def test_list_observations_filters_by_platform(store: Store) -> None:
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )
    for platform in ("hacker_news", "reddit"):
        record_observation(
            store,
            ObservationInput(
                content_item_id=content_id,
                source_id=platform,
                platform=platform,
                observed_at=T0,
            ),
            now=T0,
        )

    assert len(list_observations(store, platform="reddit")) == 1


# --- ContentItem upsert 규칙 -------------------------------------------------
def test_existing_metadata_is_not_overwritten_by_a_poorer_observation(store: Store) -> None:
    content_id = upsert_content_item(
        store,
        ContentItemInput(
            content_type="article", url=ARTICLE, title="원제목", publisher="Example Times"
        ),
        now=T0,
    )
    upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE, title=None), now=T0
    )

    record = get_content_item(store, content_id)

    assert record is not None
    assert record.title == "원제목"
    assert record.publisher == "Example Times"


def test_missing_metadata_is_filled_in_later(store: Store) -> None:
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )
    upsert_content_item(
        store,
        ContentItemInput(content_type="article", url=ARTICLE, title="나중에 알아낸 제목"),
        now=T0,
    )

    record = get_content_item(store, content_id)

    assert record is not None
    assert record.title == "나중에 알아낸 제목"


def test_content_without_normalizable_url_is_not_merged(store: Store) -> None:
    """정규화 키가 없으면 억지로 묶지 않는다."""
    first = upsert_content_item(store, ContentItemInput(content_type="document"), now=T0)
    second = upsert_content_item(store, ContentItemInput(content_type="document"), now=T0)

    assert first != second


def test_find_content_item_by_url_normalizes(store: Store) -> None:
    content_id = upsert_content_item(
        store, ContentItemInput(content_type="article", url=ARTICLE), now=T0
    )

    found = find_content_item_by_url(store, f"{ARTICLE}/?utm_campaign=x")

    assert found is not None
    assert found.content_item_id == content_id


def test_entity_upsert_is_idempotent(store: Store) -> None:
    first = upsert_entity(store, EntityInput(entity_type="company", name="Example  Corp"), now=T0)
    second = upsert_entity(store, EntityInput(entity_type="company", name="example corp"), now=T0)

    assert first == second


def test_entities_of_different_types_are_distinct(store: Store) -> None:
    company = upsert_entity(store, EntityInput(entity_type="company", name="Vision"), now=T0)
    product = upsert_entity(store, EntityInput(entity_type="product", name="Vision"), now=T0)

    assert company != product


# --- Metric append-only ------------------------------------------------------
def test_three_observations_of_the_same_metric_create_three_rows(store: Store) -> None:
    """지시서 A-8 필수 검증. 같은 지표의 후속 관측은 INSERT 다."""
    for hours, value in ((0, 100), (1, 120), (2, 118)):
        record_metric(
            store,
            MetricInput(
                metric_name="hn_score",
                value=value,
                index_type="absolute",
                source_id="hacker_news",
                platform="hacker_news",
                observed_at=T0 + timedelta(hours=hours),
            ),
            now=T0,
        )

    history = get_metric_history(store, "hn_score")

    assert len(history) == 3
    assert [record.value for record in history] == [100.0, 120.0, 118.0]


def test_metric_history_keeps_earlier_values(store: Store) -> None:
    """변화 속도와 지속성을 분석하려면 이전 값이 남아 있어야 한다."""
    for hours, value in ((0, 100), (1, 90)):
        record_metric(
            store,
            MetricInput(
                metric_name="votes",
                value=value,
                index_type="absolute",
                source_id="hacker_news",
                observed_at=T0 + timedelta(hours=hours),
            ),
            now=T0,
        )

    assert latest_metric(store, "votes").value == 90.0
    assert len(get_metric_history(store, "votes")) == 2


def test_metrics_module_exposes_no_update_path(store: Store) -> None:
    import ria.core.metrics as module

    exported = {name for name in dir(module) if not name.startswith("_")}

    assert not {n for n in exported if "update" in n or "delete" in n}


def test_metric_keeps_scope_fields(store: Store) -> None:
    record_metric(
        store,
        MetricInput(
            metric_name="business_count",
            value=1234,
            index_type="absolute",
            source_id="kosis",
            observed_at=T0,
            unit="개",
            denominator=None,
            geography="KR",
            period="2024",
            population="제조업 중소기업",
            method="전수조사",
        ),
        now=T0,
    )

    record = get_metric_history(store, "business_count")[0]

    assert (record.unit, record.geography, record.period) == ("개", "KR", "2024")
    assert record.population == "제조업 중소기업"
    assert record.denominator is None


def test_relative_index_is_recorded_as_relative(store: Store) -> None:
    """상대 지수를 절대 검색량으로 표현하지 않는다 (DESIGN §6.3)."""
    record_metric(
        store,
        MetricInput(
            metric_name="search_interest",
            value=73.2,
            index_type="relative",
            source_id="naver_datalab",
            observed_at=T0,
            unit="index",
        ),
        now=T0,
    )

    assert get_metric_history(store, "search_interest")[0].index_type == "relative"


def test_string_metric_value_is_preserved(store: Store) -> None:
    record_metric(
        store,
        MetricInput(
            metric_name="disclosure_status",
            value="비공개",
            index_type="absolute",
            source_id="opendart",
            observed_at=T0,
        ),
        now=T0,
    )

    assert get_metric_history(store, "disclosure_status")[0].value == "비공개"


def test_bool_is_not_a_metric_value(store: Store) -> None:
    with pytest.raises(ValueError, match="bool"):
        record_metric(
            store,
            MetricInput(
                metric_name="flag",
                value=True,
                index_type="absolute",
                source_id="kosis",
                observed_at=T0,
            ),
            now=T0,
        )


def test_metric_history_filters_by_platform(store: Store) -> None:
    """플랫폼 간 수치를 합산하지 않으려면 플랫폼별로 따로 조회할 수 있어야 한다."""
    for platform, value in (("hacker_news", 120), ("reddit", 88)):
        record_metric(
            store,
            MetricInput(
                metric_name="score",
                value=value,
                index_type="absolute",
                source_id=platform,
                platform=platform,
                observed_at=T0,
            ),
            now=T0,
        )

    assert [r.value for r in get_metric_history(store, "score", platform="reddit")] == [88.0]


def test_metric_rejects_naive_observed_at(store: Store) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        record_metric(
            store,
            MetricInput(
                metric_name="score",
                value=1,
                index_type="absolute",
                source_id="hacker_news",
                observed_at=datetime(2026, 8, 27, 10, 0),
            ),
            now=T0,
        )
