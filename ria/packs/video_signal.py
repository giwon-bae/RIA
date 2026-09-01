"""영상 관심·사용 사례 신호 Pack (DESIGN §5, B-10)."""

from ria.collectors.youtube import YouTubeCollector
from ria.packs import PackDefinition, SourceStrategy

PACK = PackDefinition(
    pack_id="video-signal",
    purpose="YouTube 내부의 공개 영상 메타데이터와 원시 반응 지표를 관측한다.",
    sources=(
        SourceStrategy(
            source_id="youtube_data",
            collector_type=YouTubeCollector,
            priority=10,
            strategy="공식 API 원시 지표를 플랫폼별로 분리하고 30일 보관 정책을 지킨다.",
        ),
    ),
)
