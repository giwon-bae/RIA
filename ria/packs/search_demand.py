"""검색 관심과 수요 방향 Pack (DESIGN §5, B-10)."""

from ria.collectors.naver_datalab import NaverDataLabCollector
from ria.collectors.naver_search import NaverSearchCollector
from ria.collectors.naver_shopping_insight import NaverShoppingInsightCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="search-demand",
    purpose="검색 결과와 상대 관심 지표로 수요 방향을 관측한다.",
    sources=(
        SourceStrategy(
            source_id="naver_search",
            collector_type=NaverSearchCollector,
            priority=10,
            strategy="NAVER API Hub 검색 결과를 콘텐츠 관측으로 수집한다.",
        ),
        SourceStrategy(
            source_id="naver_datalab",
            collector_type=NaverDataLabCollector,
            priority=20,
            strategy="검색어 트렌드를 절대 검색량이 아닌 상대 지수로만 기록한다.",
        ),
        SourceStrategy(
            source_id="naver_shopping_insight",
            collector_type=NaverShoppingInsightCollector,
            priority=30,
            strategy="쇼핑 분야 클릭 트렌드를 상대 지수로만 기록한다.",
        ),
        SourceStrategy(
            source_id="google_trends",
            collector_type=None,
            priority=40,
            strategy="공식 Alpha 접근과 endpoint 계약이 확보된 뒤에만 후보로 검토한다.",
            unavailable_reason=(
                "not_attempted: 공식 Google Trends Alpha endpoint와 접근 권한이 없어 "
                "실행 경로를 열지 않는다."
            ),
        ),
    ),
)
