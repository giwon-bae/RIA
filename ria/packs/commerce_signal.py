"""상품·가격·제휴 행동 신호 Pack 선언 (DESIGN §5, B-10)."""

from ria.collectors.naver_shopping_insight import NaverShoppingInsightCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="commerce-signal",
    purpose="승인된 커머스 API 범위의 쇼핑 관심과 운영 신호를 관측한다.",
    sources=(
        SourceStrategy(
            source_id="naver_shopping_insight",
            collector_type=NaverShoppingInsightCollector,
            priority=10,
            strategy="쇼핑 카테고리의 상대 클릭 지수만 수요 방향 신호로 기록한다.",
        ),
        SourceStrategy(
            source_id="coupang_seller",
            collector_type=None,
            priority=20,
            strategy="본인 판매자 계정 운영 목적과 시장조사를 분리한다.",
            unavailable_reason=(
                "not_attempted: Coupang Seller API는 본인 계정 운영용이며 "
                "시장조사 실행 경로로 열지 않는다."
            ),
        ),
        SourceStrategy(
            source_id="coupang_partners",
            collector_type=None,
            priority=30,
            strategy="파트너 승인·노출·링크 정책이 확인된 수익 활동에만 사용한다.",
            unavailable_reason=(
                "not_attempted: 승인된 Partners 사용 범위가 없어 실행 경로를 열지 않는다."
            ),
        ),
        SourceStrategy(
            source_id="naver_shopping_search",
            collector_type=None,
            priority=40,
            strategy="종료된 API는 대체 호출 없이 영구 차단 상태로 남긴다.",
            unavailable_reason=(
                "not_attempted: NAVER 쇼핑 검색 API는 2026-07-31 종료돼 "
                "collector와 실행 경로를 만들지 않는다."
            ),
        ),
    ),
)
