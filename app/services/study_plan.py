"""Bridge owned deck content to a persisted schedule; callers commit transactions."""
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.deck import Deck
from app.models.study_plan import StudyPlan, StudyPlanItem
from app.schemas.study_plan import StudyPlanCreate, StudyPlanChapterResponse, StudyPlanResponse
from app.services.chapters import ordered_chapters
from app.services.study_timeline import ALGORITHM_VERSION, ChapterInput, ScheduleResult, generate_schedule


def plan_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def local_today(timezone_name: str) -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()


def owned_parent(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False) -> Deck:
    query = select(Deck).where(Deck.id == deck_id, Deck.user_id == user_id)
    if lock:
        query = query.with_for_update()
    parent = db.scalar(query)
    if parent is None:
        raise plan_error(404, "deck_not_found", "Deck not found")
    if parent.parent_deck_id is not None:
        raise plan_error(400, "root_deck_required", "Study plans require a parent/root deck")
    return parent


def chapter_inputs(db: Session, chapters: list[Deck]) -> tuple[list[ChapterInput], str]:
    actual_counts = dict(db.execute(
        select(Card.deck_id, func.count(Card.id))
        .where(Card.deck_id.in_([chapter.id for chapter in chapters]))
        .group_by(Card.deck_id)
    ).all())
    inputs = []
    source = "actual"
    for chapter in chapters:
        if chapter.generation_status == "completed":
            count = actual_counts.get(chapter.id, 0)
        elif chapter.generation_status in {"pending", "generating", "failed"}:
            if chapter.card_count is None or chapter.card_count < 0:
                raise plan_error(409, "chapter_count_unknown", "An unfinished chapter has no valid planned card count")
            count = chapter.card_count
            source = "planned"
        else:
            raise plan_error(409, "chapter_status_unknown", "Chapter generation status is not recognized")
        inputs.append(ChapterInput(chapter.id, count))
    return inputs, source


def schedule_for(inputs: list[ChapterInput], start: date, settings: StudyPlanCreate) -> ScheduleResult:
    try:
        return generate_schedule(inputs, start, settings.requested_target_date,
                                 settings.study_weekdays, settings.daily_card_limit)
    except (ValueError, OverflowError) as error:
        raise plan_error(422, "invalid_study_schedule", str(error)) from error


def make_items(result: ScheduleResult) -> list[StudyPlanItem]:
    return [StudyPlanItem(
        scheduled_date=item.scheduled_date, item_type=item.item_type,
        chapter_id=item.chapter_id, target_card_count=item.target_card_count, position=position,
    ) for position, item in enumerate(result.items)]


def create_plan(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID, settings: StudyPlanCreate) -> StudyPlan:
    # Lock the parent to serialize concurrent creation; the unique constraint is the backstop.
    parent = owned_parent(db, deck_id, user_id, lock=True)
    if db.scalar(select(StudyPlan.id).where(StudyPlan.parent_deck_id == parent.id)) is not None:
        raise plan_error(409, "study_plan_exists", "This deck already has a study plan")
    chapters = ordered_chapters(db, parent)
    if not chapters:
        raise plan_error(409, "chapters_required", "The parent deck has no chapters to schedule")
    inputs, source = chapter_inputs(db, chapters)
    start = settings.start_date or local_today(settings.timezone)
    result = schedule_for(inputs, start, settings)
    plan = StudyPlan(
        user_id=user_id, parent_deck_id=parent.id, start_date=start,
        requested_target_date=settings.requested_target_date,
        estimated_finish_date=result.estimated_finish_date, timezone=settings.timezone,
        study_weekdays_mask=sum(1 << day for day in settings.study_weekdays),
        daily_card_limit=settings.daily_card_limit,
        required_daily_card_count=result.required_daily_card_count,
        revision=1, algorithm_version=ALGORITHM_VERSION, count_source=source,
        items=make_items(result),
    )
    db.add(plan)
    db.flush()
    return plan


def get_plan(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID) -> StudyPlan:
    owned_parent(db, deck_id, user_id)
    plan = db.scalar(select(StudyPlan).where(StudyPlan.parent_deck_id == deck_id, StudyPlan.user_id == user_id))
    if plan is None:
        raise plan_error(404, "study_plan_not_found", "This deck has no study plan")
    return plan


def plan_response(db: Session, plan: StudyPlan) -> StudyPlanResponse:
    counts: dict[uuid.UUID, int] = {}
    for item in plan.items:
        if item.chapter_id is not None:
            counts[item.chapter_id] = counts.get(item.chapter_id, 0) + (item.target_card_count or 0)
    return StudyPlanResponse(
        id=plan.id, parent_deck_id=plan.parent_deck_id, start_date=plan.start_date,
        requested_target_date=plan.requested_target_date, estimated_finish_date=plan.estimated_finish_date,
        timezone=plan.timezone, study_weekdays=plan.study_weekdays, daily_card_limit=plan.daily_card_limit,
        required_daily_card_count=plan.required_daily_card_count,
        target_achievable=(plan.estimated_finish_date <= plan.requested_target_date
                           if plan.requested_target_date is not None else None),
        revision=plan.revision, algorithm_version=plan.algorithm_version, count_source=plan.count_source,
        created_at=plan.created_at, updated_at=plan.updated_at,
        chapters=[StudyPlanChapterResponse(
            id=chapter.id, title=chapter.title, position=chapter.position,
            generation_status=chapter.generation_status, scheduled_card_count=counts.get(chapter.id, 0),
        ) for chapter in ordered_chapters(db, plan.parent_deck)],
        items=plan.items,
    )


def reconcile_generated_plan(db: Session, parent: Deck) -> None:
    """One planned→actual reconciliation after generation, never a progress update.

    Keep the original start and requested dates. This corrects the initial content
    forecast, including past projections, and does not infer any study activity.
    """
    db.scalar(select(Deck).where(Deck.id == parent.id).with_for_update())
    plan = db.scalar(select(StudyPlan).where(StudyPlan.parent_deck_id == parent.id))
    if plan is None or plan.count_source == "actual":
        return
    chapters = ordered_chapters(db, parent)
    if not chapters or any(ch.generation_status != "completed" for ch in chapters):
        return
    inputs, _ = chapter_inputs(db, chapters)
    settings = StudyPlanCreate(
        start_date=plan.start_date, requested_target_date=plan.requested_target_date,
        timezone=plan.timezone, study_weekdays=plan.study_weekdays, daily_card_limit=plan.daily_card_limit,
    )
    result = schedule_for(inputs, plan.start_date, settings)
    old_shape = [(item.scheduled_date, item.item_type, item.chapter_id, item.target_card_count) for item in plan.items]
    new_shape = [(item.scheduled_date, item.item_type, item.chapter_id, item.target_card_count) for item in result.items]
    if old_shape != new_shape:
        plan.items.clear()
        db.flush()  # Delete old positions before inserting their replacements.
        plan.items = make_items(result)
    plan.estimated_finish_date = result.estimated_finish_date
    plan.required_daily_card_count = result.required_daily_card_count
    plan.count_source = "actual"
    plan.revision += 1
    db.flush()


def ensure_structure_editable(db: Session, parent_id: uuid.UUID) -> None:
    """Phase 1 has no schedule editing: don't silently invalidate its chapter groups."""
    db.scalar(select(Deck.id).where(Deck.id == parent_id).with_for_update())
    if db.scalar(select(StudyPlan.id).where(StudyPlan.parent_deck_id == parent_id)) is not None:
        raise plan_error(409, "study_plan_structure_locked", "Chapter structure cannot change while a study plan exists")
