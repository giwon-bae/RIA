"""법령·규제·지원정책 Pack의 보류 선언 (DESIGN §5, B-7/B-10)."""

from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="regulation-policy",
    purpose="시행일과 공식 원문이 확인된 법령·규제·지원정책 근거를 다룬다.",
    sources=(
        SourceStrategy(
            source_id="data_go_kr",
            collector_type=None,
            priority=10,
            strategy="국가법령정보 등 등록 소스 판정이 끝난 뒤 규제 데이터셋을 선택한다.",
            unavailable_reason=(
                "not_attempted: B-7은 등록 소스 부족으로 보류됐으며, authority-stats용 "
                "data.go.kr collector를 규제 Pack 실행 경로로 재사용하지 않는다."
            ),
        ),
    ),
)
