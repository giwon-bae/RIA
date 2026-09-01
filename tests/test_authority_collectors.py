"""B-2 authority-stats collector와 원자 적재 계약."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ria.collectors import (
    DataGoKrCollector,
    DataGoKrDatasetSpec,
    KosisCollector,
    KosisDatasetSpec,
    WorldBankCollector,
    persist_collect_result,
)
from ria.collectors.base import CollectorContractError
from ria.config import KST, Config
from ria.core.store import Store
from ria.http import HttpClient
from ria.policy.guard import PolicyBlocked

AS_OF = date(2026, 9, 1)
OBSERVED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=KST)
FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _config(tmp_path: Path, **credentials: str) -> Config:
    return Config(db_path=tmp_path / "authority.db", credentials=credentials)


def _row_count(store: Store, table: str) -> int:
    allowed = {"content_items", "source_observations", "metrics", "raw_snapshots"}
    assert table in allowed
    row = store.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def test_world_bank_maps_null_metric_and_persists_raw_snapshot(tmp_path: Path) -> None:
    payload = _fixture("world_bank_indicator.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path == "/v2/country/KOR/indicator/SP.POP.TOTL"
        assert request.url.params["format"] == "json"
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=payload, request=request)

    collector = WorldBankCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    )
    result = collector.collect(
        "SP.POP.TOTL",
        country="KOR",
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert len(calls) == 1
    assert len(result.contents) == 1
    assert len(result.observations) == 2
    assert len(result.metrics) == 1
    assert result.metrics[0].metric.value == 51751065
    assert result.metrics[0].metric.index_type == "absolute"

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert persisted.content_count == 1
        assert persisted.observation_count == 2
        assert persisted.metric_count == 1
        assert persisted.snapshot_count == 1
        assert _row_count(store, "raw_snapshots") == 1
        assert (
            store.connection.execute(
                "SELECT COUNT(*) AS n FROM source_observations WHERE snapshot_id IS NOT NULL"
            ).fetchone()["n"]
            == 2
        )


def test_world_bank_estimated_pagination_cannot_bypass_guard(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    collector = WorldBankCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    )
    result = collector.collect("SP.POP.TOTL", max_pages=51, requested_calls=1, as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "request_exceeds_rate_limit"
    assert calls == 0


def test_kosis_missing_credential_is_guarded_before_missing_dataset(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    result = KosisCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    ).collect("테스트 질의", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "missing_credential"
    assert calls == 0


def test_kosis_requires_caller_supplied_identifiers_before_http(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[], request=request)

    collector = KosisCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, RIA_KOSIS_API_KEY="fixture-secret"),
    )

    with pytest.raises(CollectorContractError, match="KosisDatasetSpec"):
        collector.collect("테스트 질의", as_of=AS_OF)

    assert calls == 0


def test_kosis_fixture_mapping_does_not_store_key(tmp_path: Path) -> None:
    payload = _fixture("kosis_rows.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["orgId"] == "fixture-org"
        assert request.url.params["tblId"] == "fixture-table"
        assert request.url.params["apiKey"] == "fixture-secret"
        return httpx.Response(200, json=payload, request=request)

    spec = KosisDatasetSpec(
        org_id="fixture-org",
        table_id="fixture-table",
        object_l1="fixture-region",
        item_id="fixture-item",
        period_type="Y",
    )
    result = KosisCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, RIA_KOSIS_API_KEY="fixture-secret"),
    ).collect("테스트 인구", dataset=spec, observed_at=OBSERVED_AT, as_of=AS_OF)

    assert result.allowed is True
    assert len(calls) == 1
    assert len(result.observations) == 2
    assert [item.metric.value for item in result.metrics] == [1234, "-"]
    assert "fixture-secret" not in repr(result)


def _data_go_spec(**overrides: Any) -> DataGoKrDatasetSpec:
    values: dict[str, Any] = {
        "dataset_id": "fixture-dataset",
        "endpoint": "https://apis.data.go.kr/fixture/service",
        "policy_url": "https://www.data.go.kr/data/fixture/openapi.do",
        "items_path": ("response", "body", "items"),
        "id_field": "row_id",
        "title_field": "name",
        "metric_field": "value",
        "metric_name": "fixture_count",
        "unit_field": "unit",
        "geography_field": "region",
        "period_field": "period",
        "approved": True,
        "storage_allowed": True,
    }
    values.update(overrides)
    return DataGoKrDatasetSpec(**values)


def test_data_go_missing_credential_is_guarded_before_missing_dataset(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    result = DataGoKrCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path),
    ).collect("테스트 질의", as_of=AS_OF)

    assert result.allowed is False
    assert isinstance(result.policy, PolicyBlocked)
    assert result.policy.reason == "missing_credential"
    assert calls == 0


@pytest.mark.parametrize(
    ("dataset", "message"),
    [
        (_data_go_spec(approved=False), "활용신청"),
        (_data_go_spec(endpoint="https://example.test/steal"), "endpoint host"),
    ],
)
def test_data_go_rejects_unverified_dataset_before_http(
    tmp_path: Path,
    dataset: DataGoKrDatasetSpec,
    message: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    collector = DataGoKrCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, RIA_DATA_GO_KR_KEY="fixture-secret"),
    )

    with pytest.raises(CollectorContractError, match=message):
        collector.collect("테스트 질의", dataset=dataset, as_of=AS_OF)

    assert calls == 0


def test_data_go_fixture_mapping_and_secret_safe_snapshot(tmp_path: Path) -> None:
    payload = _fixture("data_go_rows.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.params["serviceKey"] == "fixture-secret"
        assert request.url.params["pageNo"] == "1"
        return httpx.Response(200, json=payload, request=request)

    result = DataGoKrCollector(
        http=HttpClient(transport=httpx.MockTransport(handler)),
        config=_config(tmp_path, RIA_DATA_GO_KR_KEY="fixture-secret"),
    ).collect(
        "테스트 공공데이터",
        dataset=_data_go_spec(),
        observed_at=OBSERVED_AT,
        as_of=AS_OF,
    )

    assert result.allowed is True
    assert len(calls) == 1
    assert len(result.observations) == 1
    assert result.metrics[0].metric.value == 42.5
    assert "fixture-secret" not in repr(result)

    with Store(":memory:") as store:
        persisted = persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert (
            persisted.content_count,
            persisted.observation_count,
            persisted.metric_count,
            persisted.snapshot_count,
        ) == (1, 1, 1, 1)


def test_persistence_rolls_back_all_rows_on_invalid_snapshot_reference(
    tmp_path: Path,
) -> None:
    payload = _fixture("world_bank_indicator.json")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    result = WorldBankCollector(
        http=HttpClient(transport=transport), config=_config(tmp_path)
    ).collect("SP.POP.TOTL", country="KOR", observed_at=OBSERVED_AT, as_of=AS_OF)
    result.metadata["observation_snapshot_refs"][result.observations[0].ref] = "missing"

    with Store(":memory:") as store:
        with pytest.raises(CollectorContractError, match="없는 snapshot"):
            persist_collect_result(store, result, stored_at=OBSERVED_AT)
        assert all(
            _row_count(store, table) == 0
            for table in ("content_items", "source_observations", "metrics", "raw_snapshots")
        )
