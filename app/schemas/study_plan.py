import uuid
from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.study_timeline import DEFAULT_DAILY_CARD_LIMIT, ItemType


class StudyPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    requested_target_date: date | None = None
    timezone: str = Field(min_length=1, max_length=100)
    study_weekdays: list[Annotated[int, Field(strict=True, ge=0, le=6)]] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7,
        description="Allowed local weekdays: Monday=0, Sunday=6",
    )
    daily_card_limit: int = Field(default=DEFAULT_DAILY_CARD_LIMIT, ge=1, le=1000)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Use a valid IANA timezone") from error
        return value

    @field_validator("study_weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if not value or len(set(value)) != len(value) or not set(value) <= set(range(7)):
            raise ValueError("Study weekdays must be unique integers 0 (Monday) through 6 (Sunday)")
        return sorted(value)


class StudyPlanItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scheduled_date: date
    item_type: ItemType
    chapter_id: uuid.UUID | None
    target_card_count: int | None
    position: int
    actual_learned_count: int | None
    shortfall_count: int | None
    status: Literal["upcoming", "active", "completed", "missed", "partial"]
    period: Literal["historical", "current", "future"]
    closed_at: datetime | None
    achieved_at: datetime | None


class StudyPlanChapterResponse(BaseModel):
    id: uuid.UUID
    title: str
    position: int
    generation_status: str
    scheduled_card_count: int


class StudyPlanResponse(BaseModel):
    id: uuid.UUID
    parent_deck_id: uuid.UUID
    start_date: date
    requested_target_date: date | None
    estimated_finish_date: date
    timezone: str
    study_weekdays: list[int]
    daily_card_limit: int
    required_daily_card_count: int | None = Field(
        description="Minimum cards/study-day needed for the requested deadline; null without a deadline or when milestone days cannot fit",
    )
    target_achievable: bool | None
    remaining_card_count: int
    projection_blocked: bool
    revision: int
    algorithm_version: str
    count_source: Literal["planned", "actual"]
    created_at: datetime
    updated_at: datetime
    chapters: list[StudyPlanChapterResponse]
    items: list[StudyPlanItemResponse]
