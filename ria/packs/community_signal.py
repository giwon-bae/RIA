"""커뮤니티 문제·표현·여론 신호 Pack (DESIGN §5, B-10)."""

from ria.collectors.reddit import RedditCollector
from ria.collectors.threads import ThreadsCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="community-signal",
    purpose="승인된 공개 커뮤니티 범위에서 고객 문제와 표현의 초기 신호를 찾는다.",
    sources=(
        SourceStrategy(
            source_id="reddit",
            collector_type=RedditCollector,
            priority=10,
            strategy="Data Access 승인은 Policy Guard 상태로 확인하고 응답 헤더 한도를 따른다.",
        ),
        SourceStrategy(
            source_id="threads",
            collector_type=ThreadsCollector,
            priority=20,
            strategy="App Review 범위와 사용자별 쿼터를 확인해 키워드 검색을 수행한다.",
        ),
        SourceStrategy(
            source_id="x_twitter",
            collector_type=None,
            priority=30,
            strategy="예산·정책에 맞는 공식 읽기 권한이 생긴 뒤에만 후보로 검토한다.",
            unavailable_reason=(
                "not_attempted: X 읽기 비용과 승인 범위가 현재 정책에 맞지 않아 "
                "실행 경로를 열지 않는다."
            ),
        ),
    ),
)
