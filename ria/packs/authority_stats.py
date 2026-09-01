"""객관 통계 근거 Pack (DESIGN §5, B-10)."""

from ria.collectors.data_go_kr import DataGoKrCollector
from ria.collectors.kosis import KosisCollector
from ria.collectors.world_bank import WorldBankCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="authority-stats",
    purpose="시장 규모·사업체 수·인구·산업 통계를 공식 자료로 확인한다.",
    sources=(
        SourceStrategy(
            source_id="world_bank",
            collector_type=WorldBankCollector,
            priority=10,
            strategy="무자격증명 공식 지표로 먼저 수집·정규화·저장 경로를 관통한다.",
        ),
        SourceStrategy(
            source_id="kosis",
            collector_type=KosisCollector,
            priority=20,
            strategy="호출자가 확정한 통계표 식별자와 기간만 사용한다.",
        ),
        SourceStrategy(
            source_id="data_go_kr",
            collector_type=DataGoKrCollector,
            priority=30,
            strategy="활용 승인·보관 조건이 확인된 데이터셋 명세만 사용한다.",
        ),
    ),
)
