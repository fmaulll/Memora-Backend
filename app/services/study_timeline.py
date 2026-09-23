"""Calendar projections only: no persistence, progress, or exam eligibility."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo

from app.services.exam_groups import split_chapters

if TYPE_CHECKING:
    from app.schemas.ai import StudyTimeline

ALGORITHM_VERSION = "chapter-plan-v1"
DEFAULT_DAILY_CARD_LIMIT = 20
MAX_SCHEDULE_DAYS = 3660
ItemType = Literal["learn", "first_half_exam", "second_half_exam", "final_exam"]


@dataclass(frozen=True)
class ChapterInput:
    id: UUID
    card_count: int


@dataclass(frozen=True)
class ScheduledItem:
    scheduled_date: date
    item_type: ItemType
    chapter_id: UUID | None = None
    target_card_count: int | None = None


@dataclass(frozen=True)
class ScheduleResult:
    items: tuple[ScheduledItem, ...]
    estimated_finish_date: date
    required_daily_card_count: int | None


def _eligible_dates(start: date, end: date, weekdays: frozenset[int]) -> int:
    days = (end - start).days + 1
    weeks, remainder = divmod(days, 7)
    return weeks * len(weekdays) + sum(
        (start.weekday() + offset) % 7 in weekdays for offset in range(remainder)
    )


def _days_needed(group_counts: list[int], daily_limit: int) -> int:
    # Each nonempty half has an exam day; the final has its own day.
    learning_days = sum((count + daily_limit - 1) // daily_limit for count in group_counts)
    return learning_days + len(group_counts) + 1


def _required_workload(group_counts: list[int], available_days: int) -> int | None:
    if available_days < 2 * len(group_counts) + 1:
        return None  # Even unlimited cards/day cannot fit the milestone days.
    low, high = 1, max(group_counts)
    while low < high:
        middle = (low + high) // 2
        if _days_needed(group_counts, middle) <= available_days:
            high = middle
        else:
            low = middle + 1
    return low


def generate_schedule(
    chapters: list[ChapterInput],
    start_date: date,
    target_date: date | None,
    study_weekdays: list[int],
    daily_card_limit: int = DEFAULT_DAILY_CARD_LIMIT,
) -> ScheduleResult:
    """Consume ordered chapters sequentially, with dedicated exam study days.

    A deadline can spread work more gently, but never raises the user's limit.
    An infeasible deadline is retained by the caller alongside our estimate.
    Dates are already local calendar dates; no UTC conversion belongs here.
    """
    weekdays = frozenset(study_weekdays)
    if not weekdays or not weekdays <= set(range(7)) or daily_card_limit < 1:
        raise ValueError("Provide study weekdays 0–6 and a positive daily card limit")
    if any(chapter.card_count < 0 for chapter in chapters):
        raise ValueError("Chapter card counts cannot be negative")
    if len({chapter.id for chapter in chapters}) != len(chapters):
        raise ValueError("Chapter IDs must be unique")
    if target_date is not None and target_date < start_date:
        raise ValueError("Target date cannot precede start date")

    first_half, second_half = split_chapters(chapters)
    groups: list[tuple[list[ChapterInput], ItemType]] = [
        (first_half, "first_half_exam"), (second_half, "second_half_exam"),
    ]
    group_counts = [sum(ch.card_count for ch in group) for group, _ in groups]
    nonempty_counts = [count for count in group_counts if count]
    if not nonempty_counts:
        return ScheduleResult((), start_date, 0 if target_date else None)

    required = None
    if target_date is not None:
        required = _required_workload(nonempty_counts, _eligible_dates(start_date, target_date, weekdays))
    limit = min(daily_card_limit, required) if required is not None else daily_card_limit
    items: list[ScheduledItem] = []
    offset = 0

    def next_study_day() -> date:
        nonlocal offset
        while offset < MAX_SCHEDULE_DAYS:
            day = start_date + timedelta(days=offset)
            offset += 1
            if day.weekday() in weekdays:
                return day
        raise ValueError("Schedule exceeds the ten-year planning horizon; increase the daily workload")

    for (group, exam_type), count in zip(groups, group_counts):
        if not count:
            continue  # A truly empty group cannot provide exam source cards.
        remaining_capacity = 0
        for chapter in group:
            remaining = chapter.card_count
            while remaining:
                if remaining_capacity == 0:
                    day = next_study_day()
                    remaining_capacity = limit
                assigned = min(remaining, remaining_capacity)
                items.append(ScheduledItem(day, "learn", chapter.id, assigned))
                remaining -= assigned
                remaining_capacity -= assigned
        items.append(ScheduledItem(next_study_day(), exam_type))
    items.append(ScheduledItem(next_study_day(), "final_exam"))
    return ScheduleResult(tuple(items), items[-1].scheduled_date, required)


def timeline_summary(result: ScheduleResult, start: date) -> "StudyTimeline":
    """Retain the existing AI response shape using the same scheduled items."""
    from app.schemas.ai import StudyDay, StudyTimeline

    counts: dict[date, int] = {}
    for item in result.items:
        if item.item_type == "learn":
            counts[item.scheduled_date] = counts.get(item.scheduled_date, 0) + (item.target_card_count or 0)
    total_days = (result.estimated_finish_date - start).days + 1
    return StudyTimeline(
        total_days=total_days, total_cards=sum(counts.values()),
        daily_plan=[StudyDay(
            day=index + 1, date=start + timedelta(days=index),
            new_cards=counts.get(start + timedelta(days=index), 0),
            focus="Learn chapter cards" if counts.get(start + timedelta(days=index)) else "No new cards planned",
        ) for index in range(total_days)],
    )


class StudyTimelineService:
    """Compatibility adapter for callers using the earlier service interface."""

    def generate(
        self, total_cards: int, target_date: date | None, study_purpose: str,
        *, chapters: list[ChapterInput] | None = None, timezone_name: str = "UTC",
    ) -> "StudyTimeline":
        start = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        result = generate_schedule(
            chapters if chapters is not None else [ChapterInput(UUID(int=0), total_cards)],
            start, target_date, list(range(7)),
        )
        return timeline_summary(result, start)
