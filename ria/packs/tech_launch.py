"""기술·제품 출시 초기 신호 Pack (DESIGN §5, B-10)."""

from ria.collectors.hacker_news import HackerNewsCollector, HNAlgoliaCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="tech-launch",
    purpose="기술·제품 출시 동향을 공식 Hacker News 원본 중심으로 관측한다.",
    sources=(
        SourceStrategy(
            source_id="hn_algolia",
            collector_type=HNAlgoliaCollector,
            priority=10,
            strategy=("후보 item ID만 반환하고 적재하지 않으며 다음 공식 Firebase 전략에 넘긴다."),
        ),
        SourceStrategy(
            source_id="hacker_news",
            collector_type=HackerNewsCollector,
            priority=20,
            strategy=(
                "Algolia가 허용돼 찾은 후보는 공식 Firebase item으로 재조회하고, "
                "후보 경로가 막히면 공식 feed/item 입력만 수집한다."
            ),
        ),
        SourceStrategy(
            source_id="product_hunt",
            collector_type=None,
            priority=30,
            strategy="상업 이용 서면 허용이 확보된 뒤 보조 출시 신호로 검토한다.",
            unavailable_reason=(
                "not_attempted: Product Hunt 상업 이용 허용이 확보되지 않아 "
                "실행 경로를 열지 않는다."
            ),
        ),
    ),
)
