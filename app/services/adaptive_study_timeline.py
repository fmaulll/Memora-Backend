"""Pure remaining-work projections. No persistence, eligibility decisions, or AI."""
from datetime import date, timedelta

from app.services.exam_groups import split_chapters
from app.services.study_timeline import ChapterInput, MAX_SCHEDULE_DAYS, ScheduledItem, ScheduleResult

ALGORITHM_VERSION = "chapter-plan-v2"


def generate_adaptive_schedule(
    chapters: list[ChapterInput], start_date: date, target_date: date | None,
    study_weekdays: list[int], daily_card_limit: int,
    *, passed_exams: frozenset[str] = frozenset(), today_credits: dict | None = None,
) -> ScheduleResult:
    """Keep ordered remaining work, dedicate milestone days, and respect the preferred cap.

    Credits are actual learning already done on start_date, never future workload.
    They consume today's capacity; overachievement is retained even above the cap.
    A passed milestone is omitted independently of newly outstanding learning.
    """
    weekdays = frozenset(study_weekdays)
    if not weekdays or not weekdays <= set(range(7)) or daily_card_limit < 1:
        raise ValueError("Provide study weekdays 0–6 and a positive daily card limit")
    if any(chapter.card_count < 0 for chapter in chapters) or len({ch.id for ch in chapters}) != len(chapters):
        raise ValueError("Chapter IDs must be unique and remaining counts nonnegative")
    credits = dict(today_credits or {})
    if any(value < 0 for value in credits.values()) or not set(credits) <= {ch.id for ch in chapters}:
        raise ValueError("Today credits must be nonnegative and belong to the supplied chapters")
    if start_date.weekday() not in weekdays:
        # Off-day learning already reduces remaining counts; do not invent a
        # scheduled target on a day excluded by the user's preferences.
        credits = {}
    groups = list(zip(split_chapters(chapters), ("first_half_exam", "second_half_exam")))

    def build(limit: int) -> tuple[ScheduledItem, ...]:
        items = [ScheduledItem(start_date, "learn", ch.id, credits[ch.id]) for ch in chapters if credits.get(ch.id)]
        learning_indices = {(item.scheduled_date, item.chapter_id): i for i, item in enumerate(items)}
        day = start_date
        capacity = max(0, limit - sum(credits.values())) if day.weekday() in weekdays else 0
        learning_today = bool(items)

        def next_day():
            nonlocal day, capacity, learning_today
            while True:
                day += timedelta(days=1)
                if (day - start_date).days >= MAX_SCHEDULE_DAYS:
                    raise ValueError("Schedule exceeds the ten-year planning horizon; increase the daily workload")
                if day.weekday() in weekdays:
                    capacity, learning_today = limit, False
                    return

        def milestone(kind):
            nonlocal capacity
            if learning_today or capacity == 0 or day.weekday() not in weekdays:
                next_day()
            items.append(ScheduledItem(day, kind))
            capacity = 0  # A milestone occupies its own study day.

        for group, exam_type in groups:
            for chapter in group:
                remaining = chapter.card_count
                while remaining:
                    if capacity == 0:
                        next_day()
                    assigned = min(capacity, remaining)
                    # Merge today's prior learning with today's newly assigned work.
                    existing = learning_indices.get((day, chapter.id))
                    if existing is None:
                        learning_indices[(day, chapter.id)] = len(items)
                        items.append(ScheduledItem(day, "learn", chapter.id, assigned))
                    else:
                        old = items[existing]
                        items[existing] = ScheduledItem(day, "learn", chapter.id, old.target_card_count + assigned)
                    capacity -= assigned
                    remaining -= assigned
                    learning_today = True
            if group and exam_type not in passed_exams:
                milestone(exam_type)
        if chapters and "final_exam" not in passed_exams:
            milestone("final_exam")
        chapter_order = {ch.id: i for i, ch in enumerate(chapters)}
        return tuple(sorted(items, key=lambda item: (item.scheduled_date, chapter_order.get(item.chapter_id, len(chapters)))))

    if not any(ch.card_count for ch in chapters) and not any(credits.values()) and not build(daily_card_limit):
        return ScheduleResult((), start_date, 0 if target_date else None)
    required = None
    if target_date is not None:
        upper = max(1, sum(ch.card_count for ch in chapters) + sum(credits.values()))
        fastest = build(upper)
        if (fastest[-1].scheduled_date if fastest else start_date) <= target_date:
            if not any(ch.card_count for ch in chapters):
                required = 0
            else:
                low, high = 1, upper
                while low < high:
                    middle = (low + high) // 2
                    try:
                        candidate = build(middle)
                        fits = candidate[-1].scheduled_date <= target_date
                    except ValueError:
                        fits = False
                    if fits:
                        high = middle
                    else:
                        low = middle + 1
                required = low
    limit = min(daily_card_limit, required) if required else daily_card_limit
    items = build(limit)
    return ScheduleResult(items, items[-1].scheduled_date if items else start_date, required)
