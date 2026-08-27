"""ResearchBrief — NIA → RIA 입력 계약 (DESIGN §4.1).

NIA 가 자연어 요청을 이 구조로 만들어 RIA subagent 를 호출한다.
RIA Core 는 이 계약을 검증하고 저장할 뿐 해석하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Research Lane (DESIGN §4.1 · §5.2 — 8종) -------------------------------
ResearchLane = Literal[
    "market_size",
    "demand",
    "customer_pain",
    "competitors",
    "technology",
    "regulation",
    "economics",
    "distribution",
]

RESEARCH_LANES: tuple[ResearchLane, ...] = (
    "market_size",
    "demand",
    "customer_pain",
    "competitors",
    "technology",
    "regulation",
    "economics",
    "distribution",
)

Freshness = Literal["realtime", "7d", "30d", "1y", "historical"]
CodexUsage = Literal["normal", "high"]

T = TypeVar("T")

# DESIGN 은 `exclude` 만 명시한다. §15 의 "조사 목적상 필수일 때만 제한적으로 보존"
# 경로를 위해 `minimal` 을 함께 두되 기본값은 `exclude` 다.
PersonalDataPolicy = Literal["exclude", "minimal"]


class TimeRange(BaseModel):
    """조사 대상 기간. 양끝 모두 열려 있을 수 있다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # `from` 은 파이썬 예약어라 alias 로 받는다. DESIGN §4.1 의 YAML 키는 `from` 이다.
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError(f"time_range.from({self.from_date}) 이 to({self.to_date}) 보다 늦다")
        return self


class Budget(BaseModel):
    """수집 budget. 구독 사용량이 소진돼도 API 과금 모드로 전환하지 않는다 (DESIGN §3.4)."""

    model_config = ConfigDict(extra="forbid")

    max_minutes: int = Field(default=30, gt=0)
    max_source_calls: int | None = Field(default=None, gt=0)
    # 기본값 False. 유료 소스는 명시적으로 켜야 한다.
    allow_paid_sources: bool = False
    max_codex_usage: CodexUsage = "normal"


class Constraints(BaseModel):
    """조사 제약.

    `commercial_context` 기본값은 True 다. 신규 비즈니스 발굴을 위한 내부 조사도
    플랫폼 약관상 상업 이용으로 해석될 수 있으므로, 비상업 전용 API 를 자동 허용하지
    않는다 (DESIGN §4.1).
    """

    model_config = ConfigDict(extra="forbid")

    commercial_context: bool = True
    personal_data: PersonalDataPolicy = "exclude"
    blocked_sources: list[str] = Field(default_factory=list)

    @field_validator("blocked_sources")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return _dedupe_preserving_order(value)


class ResearchBrief(BaseModel):
    """조사 요청 1건."""

    model_config = ConfigDict(extra="forbid")

    research_id: str = Field(min_length=1)
    decision_question: str = Field(min_length=1)
    business_domain: str = Field(min_length=1)
    target_customer: str | None = None
    geography: list[str] = Field(default_factory=list)
    time_range: TimeRange = Field(default_factory=TimeRange)
    freshness: Freshness = "30d"
    research_lanes: list[ResearchLane] = Field(min_length=1)
    budget: Budget = Field(default_factory=Budget)
    constraints: Constraints = Field(default_factory=Constraints)

    @field_validator("research_lanes")
    @classmethod
    def _dedupe_lanes(cls, value: list[ResearchLane]) -> list[ResearchLane]:
        return _dedupe_preserving_order(value)

    @field_validator("geography")
    @classmethod
    def _dedupe_geography(cls, value: list[str]) -> list[str]:
        return _dedupe_preserving_order(value)

    def is_source_blocked(self, source_id: str) -> bool:
        """brief 가 명시적으로 막은 소스인가. Policy Guard 의 차단과는 별개다."""
        return source_id in self.constraints.blocked_sources


def _dedupe_preserving_order(values: list[T]) -> list[T]:
    """중복만 제거하고 순서는 유지한다. 조사 우선순위가 순서에 담기기 때문이다."""
    seen: set[T] = set()
    result: list[T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
