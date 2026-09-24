"""Explicit mutation integration. No commits, scheduling math, or plan creation."""
import logging
import uuid
from dataclasses import dataclass
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.deck import Deck
from app.models.study_plan import StudyPlan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanAdaptation:
    plan_id: uuid.UUID
    parent_deck_id: uuid.UUID
    old_revision: int
    new_revision: int
    deferred: bool = False

    @property
    def changed(self) -> bool:
        return self.old_revision != self.new_revision


def plan_roots_for_decks(decks: list[Deck]) -> set[uuid.UUID]:
    # Plans currently schedule child chapters only. Direct root/standalone cards
    # are excluded by the existing progress and plan contracts.
    return {deck.parent_deck_id for deck in decks if deck.parent_deck_id is not None}


def recalculate_affected_plans(
    db: Session, user_id: uuid.UUID, root_ids: Iterable[uuid.UUID], *, reason: str,
) -> list[PlanAdaptation]:
    # Local imports keep the progress -> hooks -> plan dependency explicit without
    # introducing a module-import cycle. Scheduling remains solely in Phase 2C.
    from app.services.study_progress import lock_progress_user
    from app.services.study_plan import recalculate_plan

    root_ids = set(root_ids)
    if not root_ids:
        return []
    lock_progress_user(db, user_id)
    db.flush()  # Canonical reads must see the caller's uncommitted mutation.
    plans = list(db.scalars(select(StudyPlan).join(Deck, Deck.id == StudyPlan.parent_deck_id).where(
        StudyPlan.user_id == user_id, Deck.user_id == user_id,
        StudyPlan.parent_deck_id.in_(root_ids),
    ).order_by(StudyPlan.parent_deck_id).execution_options(populate_existing=True)).all())
    results = []
    for plan in plans:
        old_revision = plan.revision
        try:
            updated = recalculate_plan(db, plan.parent_deck_id, user_id)
        except HTTPException as error:
            if error.status_code != 409 or not isinstance(error.detail, dict) or error.detail.get("code") != "study_plan_content_not_ready":
                raise
            # Phase 2C checks readiness before changing any historical/schedule rows.
            logger.info("study_plan_adaptation_deferred plan_id=%s reason=%s revision=%s content_not_ready",
                        plan.id, reason, old_revision)
            results.append(PlanAdaptation(plan.id, plan.parent_deck_id, old_revision, old_revision, deferred=True))
            continue
        results.append(PlanAdaptation(plan.id, plan.parent_deck_id, old_revision, updated.revision))
        # This is an in-transaction result; the caller still owns commit/rollback.
        logger.info("study_plan_adapted_pending_commit plan_id=%s reason=%s old_revision=%s new_revision=%s",
                    plan.id, reason, old_revision, updated.revision)
    return results
