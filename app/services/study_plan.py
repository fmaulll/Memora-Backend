"""Bridge owned deck content to a persisted schedule; callers commit transactions."""
import uuid
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import UserExamProgression
from app.models.study_plan import StudyPlan, StudyPlanItem
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.schemas.study_plan import StudyPlanCreate, StudyPlanChapterResponse, StudyPlanResponse, StudyPlanItemResponse
from app.services.chapters import ordered_chapters
from app.services.exam_groups import split_chapters
from app.services.chapter_progress import get_chapter_progress
from app.services.study_progress import lock_progress_user
from app.services.study_calendar import as_utc, local_date, local_today
from app.services.study_attribution import LearningEvent, LearningTarget, attribute_learning, receipt_events
from app.services.adaptive_study_timeline import generate_adaptive_schedule, ALGORITHM_VERSION as ADAPTIVE_VERSION
from app.services.study_timeline import ALGORITHM_VERSION, ChapterInput, ScheduleResult, generate_schedule


def plan_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


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


def make_items(result: ScheduleResult, chapters: list[Deck]) -> list[StudyPlanItem]:
    epochs = {chapter.id: chapter.progress_epoch for chapter in chapters}
    return [StudyPlanItem(
        scheduled_date=item.scheduled_date, item_type=item.item_type,
        chapter_id=item.chapter_id, target_card_count=item.target_card_count, position=position,
        learning_epoch=epochs.get(item.chapter_id),
    ) for position, item in enumerate(result.items)]


def create_plan(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID, settings: StudyPlanCreate) -> StudyPlan:
    # Keep the same user -> parent lock order as recalculation and progress/reset.
    lock_progress_user(db, user_id)
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
        items=make_items(result, chapters),
    )
    db.add(plan)
    db.flush()
    return plan


def get_plan(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID) -> StudyPlan:
    lock_progress_user(db, user_id)
    owned_parent(db, deck_id, user_id)
    plan = db.scalar(select(StudyPlan).where(StudyPlan.parent_deck_id == deck_id, StudyPlan.user_id == user_id)
                     .with_for_update().execution_options(populate_existing=True))
    if plan is None:
        raise plan_error(404, "study_plan_not_found", "This deck has no study plan")
    return plan


def plan_response(db: Session, plan: StudyPlan) -> StudyPlanResponse:
    chapters = ordered_chapters(db, plan.parent_deck)
    progress = get_chapter_progress(db, plan.user_id, chapters)
    today = local_today(plan.timezone)
    actual = _attributed_counts(db, plan)
    current_targets = [LearningTarget(item.id, item.chapter_id, item.learning_epoch, item.scheduled_date,
                                      item.target_card_count, item.position)
                       for item in plan.items if item.item_type == "learn" and item.scheduled_date >= today and not item.closed_at]
    actual.update(attribute_learning(current_targets, _current_events(db, plan.user_id, chapters), plan.timezone))
    achievements = _exam_achievements(db, plan)
    groups = zip(split_chapters(chapters), ("first_half_exam", "second_half_exam"))
    projection_blocked = any(ch.generation_status != "completed" for ch in chapters) or any(
        progress[ch.id].total_card_count == 0 for group, kind in groups if kind not in achievements for ch in group
    )
    counts: dict[uuid.UUID, int] = {}
    for item in plan.items:
        if item.chapter_id is not None:
            counts[item.chapter_id] = counts.get(item.chapter_id, 0) + (item.target_card_count or 0)
    return StudyPlanResponse(
        id=plan.id, parent_deck_id=plan.parent_deck_id, start_date=plan.start_date,
        requested_target_date=plan.requested_target_date, estimated_finish_date=plan.estimated_finish_date,
        timezone=plan.timezone, study_weekdays=plan.study_weekdays, daily_card_limit=plan.daily_card_limit,
        required_daily_card_count=plan.required_daily_card_count,
        target_achievable=(not projection_blocked and plan.estimated_finish_date <= plan.requested_target_date
                           if plan.requested_target_date is not None else None),
        remaining_card_count=sum(item.total_card_count - item.learned_card_count for item in progress.values()),
        projection_blocked=projection_blocked,
        revision=plan.revision, algorithm_version=plan.algorithm_version, count_source=plan.count_source,
        created_at=as_utc(plan.created_at), updated_at=as_utc(plan.updated_at),
        chapters=[StudyPlanChapterResponse(
            id=chapter.id, title=chapter.title, position=chapter.position,
            generation_status=chapter.generation_status, scheduled_card_count=counts.get(chapter.id, 0),
        ) for chapter in chapters],
        items=[_item_response(item, today, actual.get(item.id, 0)) for item in plan.items],
    )


def _attributed_counts(db: Session, plan: StudyPlan) -> dict[uuid.UUID, int]:
    targets = [LearningTarget(item.id, item.chapter_id, item.learning_epoch, item.scheduled_date,
                              item.target_card_count, item.position)
               for item in plan.items if item.item_type == "learn"]
    receipts = list(db.scalars(select(StudyProgressReceipt.response_json).where(
        StudyProgressReceipt.user_id == plan.user_id, StudyProgressReceipt.operation == "submission",
    )).all())
    return attribute_learning(targets, receipt_events(receipts, {item.chapter_id for item in targets}), plan.timezone)


def _current_events(db: Session, user_id: uuid.UUID, chapters: list[Deck]) -> list[LearningEvent]:
    epochs = {chapter.id: chapter.progress_epoch for chapter in chapters}
    rows = db.execute(select(Card.id, Card.deck_id, CardProgress.learned_at).join(
        CardProgress, (CardProgress.card_id == Card.id) & (CardProgress.user_id == user_id),
    ).where(Card.deck_id.in_(epochs), CardProgress.learned_at.is_not(None))).all()
    return [LearningEvent(card_id, chapter_id, epochs[chapter_id], as_utc(learned_at))
            for card_id, chapter_id, learned_at in rows]


def _item_response(item: StudyPlanItem, today: date, attributed: int) -> StudyPlanItemResponse:
    period = "historical" if item.closed_at or item.scheduled_date < today else "current" if item.scheduled_date == today else "future"
    actual = None
    shortfall = None
    if item.item_type == "learn":
        actual = item.actual_learned_count if item.closed_at else attributed
        shortfall = max(0, item.target_card_count - actual)
        if shortfall == 0:
            state = "completed"
        elif actual:
            state = "partial"
        else:
            state = {"historical": "missed", "current": "active", "future": "upcoming"}[period]
    else:
        state = "completed" if item.achieved_at else {"historical": "missed", "current": "active", "future": "upcoming"}[period]
    return StudyPlanItemResponse(
        id=item.id, scheduled_date=item.scheduled_date, item_type=item.item_type,
        chapter_id=item.chapter_id, target_card_count=item.target_card_count, position=item.position,
        actual_learned_count=actual, shortfall_count=shortfall, status=state, period=period,
        closed_at=as_utc(item.closed_at) if item.closed_at else None,
        achieved_at=as_utc(item.achieved_at) if item.achieved_at else None,
    )


def _exam_achievements(db: Session, plan: StudyPlan) -> dict[str, datetime | None]:
    # Achievement facts only. Runtime eligibility remains exclusively in ExamService.
    row = db.scalar(select(UserExamProgression).where(
        UserExamProgression.user_id == plan.user_id, UserExamProgression.deck_id == plan.parent_deck_id,
    ).execution_options(populate_existing=True))
    if row is None:
        return {}
    return {kind + "_exam": getattr(row, kind + "_completed_at") for kind in ("first_half", "second_half", "final")
            if getattr(row, kind + "_passed")}


def _close_history(plan: StudyPlan, today: date, now: datetime, actual: dict, achievements: dict) -> list[StudyPlanItem]:
    history = []
    for item in plan.items:
        if item.scheduled_date < today or item.closed_at is not None:
            item.closed_at = item.closed_at or now
            if item.item_type == "learn":
                # Late accepted uploads can add credit; deletion/reset cannot remove it.
                item.actual_learned_count = max(item.actual_learned_count or 0, actual.get(item.id, 0))
            history.append(item)
    for kind, achieved_at in achievements.items():
        if achieved_at is None or any(item.item_type == kind and item.achieved_at for item in history):
            continue  # Legacy passes without timestamps are not assigned invented dates.
        day = local_date(achieved_at, plan.timezone)
        item = next((item for item in plan.items if item.item_type == kind and item.scheduled_date == day), None)
        if item is None:
            # An early pass fulfilled a now-past projection. Preserve its target
            # date and expose the earlier actual timestamp instead of calling it missed.
            item = next((item for item in history if item.item_type == kind and item.scheduled_date >= day), None)
        if item is None:
            item = StudyPlanItem(item_type=kind, scheduled_date=day, position=0)
        item.closed_at = item.closed_at or now
        item.achieved_at = achieved_at
        if item not in history:
            history.append(item)
    return history


def _schedule_signature(items: list[StudyPlanItem]) -> list[tuple]:
    return [(item.scheduled_date, item.item_type, item.chapter_id, item.target_card_count,
             item.learning_epoch, item.actual_learned_count, bool(item.closed_at),
             as_utc(item.achieved_at) if item.achieved_at else None) for item in items]


def _replace_schedule(db: Session, plan: StudyPlan, items: list[StudyPlanItem]) -> None:
    # Keep surviving row IDs, including unchanged future targets. Vacate the unique
    # position range first so reordered rows/inserts never collide on PostgreSQL.
    reusable = {signature: item for signature, item in zip(_schedule_signature(plan.items), plan.items)}
    items = [reusable.get(signature, item) for signature, item in zip(_schedule_signature(items), items)]
    offset = max((item.position for item in plan.items), default=0) + len(items) + 1
    for item in plan.items:
        item.position += offset
    db.flush()
    temporary_start = max((item.position for item in plan.items), default=0) + 1
    for position, item in enumerate(items):
        item.position = temporary_start + position
    plan.items[:] = items
    db.flush()
    for position, item in enumerate(items):
        item.position = position
    db.flush()


def recalculate_plan(db: Session, deck_id: uuid.UUID, user_id: uuid.UUID) -> StudyPlan:
    """Explicit, atomic adaptation. Caller commits; no progress/content mutation hooks."""
    lock_progress_user(db, user_id)
    parent = owned_parent(db, deck_id, user_id, lock=True)
    plan = get_plan(db, deck_id, user_id)
    db.expire(plan, ["items"])
    chapters = ordered_chapters(db, parent, lock=True)
    if any(chapter.generation_status != "completed" for chapter in chapters):
        raise plan_error(409, "study_plan_content_not_ready", "Finish chapter generation before recalculating from actual cards")
    today = local_today(plan.timezone)
    start = max(today, plan.start_date)
    now = datetime.now(timezone.utc)
    before = _schedule_signature(plan.items)
    progress = get_chapter_progress(db, user_id, chapters)
    achievements = _exam_achievements(db, plan)
    history = _close_history(plan, today, now, _attributed_counts(db, plan), achievements)
    credits: dict[uuid.UUID, int] = {}
    if start == today:
        for event in _current_events(db, user_id, chapters):
            if local_date(event.learned_at, plan.timezone) == today:
                credits[event.chapter_id] = credits.get(event.chapter_id, 0) + 1
    remaining = [ChapterInput(ch.id, progress[ch.id].total_card_count - progress[ch.id].learned_card_count) for ch in chapters]
    try:
        result = generate_adaptive_schedule(remaining, start, plan.requested_target_date, plan.study_weekdays,
                                            plan.daily_card_limit, passed_exams=frozenset(achievements), today_credits=credits)
    except (ValueError, OverflowError) as error:
        raise plan_error(422, "invalid_study_schedule", str(error)) from error
    items = history + make_items(result, chapters)
    chapter_order = {ch.id: i for i, ch in enumerate(chapters)}
    items.sort(key=lambda item: (item.scheduled_date, 0 if item in history else 1,
                                 chapter_order.get(item.chapter_id, len(chapters)), item.item_type))
    finish = max([plan.start_date] + [item.scheduled_date for item in items])
    metrics = (finish, result.required_daily_card_count, "actual")
    previous_metrics = (plan.estimated_finish_date, plan.required_daily_card_count, plan.count_source)
    if before != _schedule_signature(items) or previous_metrics != metrics:
        _replace_schedule(db, plan, items)
        plan.estimated_finish_date, plan.required_daily_card_count, plan.count_source = metrics
        plan.algorithm_version = ADAPTIVE_VERSION
        plan.revision += 1
        db.flush()
    return plan


def reconcile_generated_plan(db: Session, parent: Deck) -> None:
    """One planned→actual reconciliation after generation, never a progress update.

    Correct the initial content forecast only before any targets become historical.
    Older plans must use explicit adaptation to preserve their past projections.
    """
    db.scalar(select(Deck).where(Deck.id == parent.id).with_for_update())
    plan = db.scalar(select(StudyPlan).where(StudyPlan.parent_deck_id == parent.id))
    if plan is None or plan.count_source == "actual":
        return
    if any(item.closed_at or item.scheduled_date < local_today(plan.timezone) for item in plan.items):
        # A generation callback cannot replace historical targets. The explicit
        # adaptive operation will reconcile actual content and close those days.
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
        plan.items = make_items(result, chapters)
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
