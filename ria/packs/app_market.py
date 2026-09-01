"""앱 경쟁 구도 신호 Pack 선언 (DESIGN §5, B-10)."""

from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="app-market",
    purpose="공식 접근 계약이 확인된 범위에서 앱 경쟁 구도와 리뷰 신호를 찾는다.",
    sources=(
        SourceStrategy(
            source_id="app_store",
            collector_type=None,
            priority=10,
            strategy="공식 문서의 내부 경쟁 조사 허용 범위를 재확인한 뒤에만 검토한다.",
            unavailable_reason=(
                "not_attempted: App Store Search 문서가 오래됐고 이용 조건 재검토가 "
                "필요해 실행 경로를 열지 않는다."
            ),
        ),
        SourceStrategy(
            source_id="google_play",
            collector_type=None,
            priority=20,
            strategy="운영 중인 자체 앱용 공식 API와 경쟁 앱 조사를 분리한다.",
            unavailable_reason=(
                "not_attempted: 경쟁 앱 검색용 공식 Google Play API가 없어 실행 경로를 열지 않는다."
            ),
        ),
    ),
)
