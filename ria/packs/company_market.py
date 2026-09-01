"""기업·시장 공식 공시 Pack (DESIGN §5, B-10)."""

from ria.collectors.opendart import OpenDartCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="company-market",
    purpose="기업 공시와 원시 재무 계정을 객관 근거로 수집한다.",
    sources=(
        SourceStrategy(
            source_id="opendart",
            collector_type=OpenDartCollector,
            priority=10,
            strategy="공식 공시검색과 단일회사 전체 재무제표 응답을 구분해 수집한다.",
        ),
    ),
)
