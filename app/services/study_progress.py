"""Exact-card facts and derived reads. Callers commit facts and receipts together."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.deck import Deck
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.models.user import User
from app.schemas.study_progress import (
    CardLearningFact, DeckEpoch, DeckLearningFacts, DeckResetResult, DeckSubmissionResult,
    ProgressResetRequest, ProgressResetResponse, ProgressSummary, StudyProgressResponse,
    StudyProgressSubmission, StudyProgressSubmissionResponse,
)
from app.services.chapters import ordered_chapters
from app.services.chapter_progress import get_chapter_progress
from app.services.study_plan_hooks import plan_roots_for_decks, recalculate_affected_plans

MAX_CLOCK_SKEW = timedelta(minutes=5)


def progress_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def lock_progress_user(db: Session, user_id: uuid.UUID) -> None:
    # One lock shared by reset/submission/read prevents mixed epochs and facts.
    # It also serializes the first receipt insert without an application mutex.
    if db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise progress_error(404, "user_not_found", "User not found")


def _fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _replay(db: Session, user_id: uuid.UUID, operation_id: uuid.UUID, fingerprint: str) -> dict | None:
    receipt = db.get(StudyProgressReceipt, (user_id, operation_id))
    if receipt is None:
        return None
    if receipt.request_hash != fingerprint:
        raise progress_error(409, "idempotency_conflict", "This operation UUID was already used with different content")
    return receipt.response_json


def _remember(db: Session, user_id: uuid.UUID, operation_id: uuid.UUID,
              operation: str, fingerprint: str, response: BaseModel) -> None:
    db.add(StudyProgressReceipt(
        user_id=user_id, operation_id=operation_id, operation=operation,
        request_hash=fingerprint, response_json=response.model_dump(mode="json"),
    ))
    db.flush()


def _owned_decks(db: Session, user_id: uuid.UUID, deck_ids: list[uuid.UUID]) -> list[Deck]:
    decks = list(db.scalars(select(Deck).where(
        Deck.user_id == user_id, Deck.id.in_(deck_ids),
    ).order_by(Deck.id).with_for_update().execution_options(populate_existing=True)).all())
    if len(decks) != len(deck_ids):
        raise progress_error(404, "deck_not_found", "One or more decks were not found")
    return decks


def _scope(db: Session, user_id: uuid.UUID, deck_id: uuid.UUID) -> list[Deck]:
    deck = _owned_decks(db, user_id, [deck_id])[0]
    if deck.parent_deck_id is not None:
        return [deck]
    children = ordered_chapters(db, deck, lock=True)
    # A root with children scopes to those children; direct root cards are excluded.
    return children or [deck]


def _validate_epochs(decks: list[Deck], expected: list[DeckEpoch]) -> None:
    expected_by_id = {item.deck_id: item.progress_epoch for item in expected}
    if set(expected_by_id) != {deck.id for deck in decks}:
        raise progress_error(409, "progress_scope_changed", "Deck scope changed; fetch current progress before resetting")
    if any(expected_by_id[deck.id] != deck.progress_epoch for deck in decks):
        raise progress_error(409, "stale_progress_epoch", "Progress was reset; old offline learning must not be relabeled with a new epoch")


def _validate_cards(db: Session, request: StudyProgressSubmission) -> list[uuid.UUID]:
    requested = {item.card_id: group.deck_id for group in request.decks for item in group.learned_cards}
    cards = db.execute(select(Card.id, Card.deck_id).where(
        Card.id.in_(requested), Card.deck_id.in_([group.deck_id for group in request.decks]),
    ).order_by(Card.id).with_for_update()).all()
    actual = dict(cards)
    if actual != requested or any(
        actual.get(item.card_id) != group.deck_id for group in request.decks for item in group.learned_cards
    ):
        raise progress_error(422, "card_not_in_deck", "A card is missing, deleted, or does not belong to its submitted deck")
    return list(requested)


def _apply_learning(db: Session, user_id: uuid.UUID, request: StudyProgressSubmission,
                    card_ids: list[uuid.UUID]) -> list[DeckSubmissionResult]:
    existing = {row.card_id: row for row in db.scalars(select(CardProgress).where(
        CardProgress.user_id == user_id, CardProgress.card_id.in_(card_ids),
    )).all()}
    results = []
    for group in request.decks:
        accepted, already_learned = [], []
        for transition in group.learned_cards:
            progress = existing.get(transition.card_id)
            if progress is not None and progress.learned_at is not None:
                already_learned.append(transition.card_id)
                continue
            if progress is None:
                progress = CardProgress(user_id=user_id, card_id=transition.card_id)
                db.add(progress)
            progress.learned_at = request.completed_at
            accepted.append(transition.card_id)
        results.append(DeckSubmissionResult(
            deck_id=group.deck_id, progress_epoch=group.progress_epoch,
            accepted_card_ids=accepted, already_learned_card_ids=already_learned,
        ))
    return results


def submit_progress(db: Session, user_id: uuid.UUID,
                    request: StudyProgressSubmission) -> StudyProgressSubmissionResponse:
    lock_progress_user(db, user_id)
    fingerprint = _fingerprint("submission", request.model_dump(mode="json"))
    replay = _replay(db, user_id, request.session_id, fingerprint)
    if replay is not None:
        return StudyProgressSubmissionResponse.model_validate(replay)
    now = datetime.now(timezone.utc)
    if request.completed_at > now + MAX_CLOCK_SKEW:
        raise progress_error(422, "invalid_completion_time", "completed_at cannot be more than five minutes in the future")
    decks = _owned_decks(db, user_id, [group.deck_id for group in request.decks])
    _validate_epochs(decks, request.decks)
    card_ids = _validate_cards(db, request)
    response = StudyProgressSubmissionResponse(
        session_id=request.session_id, completed_at=request.completed_at, submitted_at=now,
        decks=_apply_learning(db, user_id, request, card_ids),
    )
    _remember(db, user_id, request.session_id, "submission", fingerprint, response)
    changed_ids = {result.deck_id for result in response.decks if result.accepted_card_ids}
    if changed_ids:
        recalculate_affected_plans(db, user_id, plan_roots_for_decks([deck for deck in decks if deck.id in changed_ids]),
                                   reason="learning_progress")
    return response


def _clear_learning(db: Session, user_id: uuid.UUID, decks: list[Deck]) -> list[DeckResetResult]:
    rows = db.execute(select(CardProgress, Card.deck_id).join(Card).where(
        CardProgress.user_id == user_id, Card.deck_id.in_([deck.id for deck in decks]),
        CardProgress.learned_at.is_not(None),
    )).all()
    cleared: dict[uuid.UUID, int] = {}
    for progress, deck_id in rows:
        progress.learned_at = None
        cleared[deck_id] = cleared.get(deck_id, 0) + 1
    results = []
    for deck in decks:
        previous_epoch = deck.progress_epoch
        deck.progress_epoch = uuid.uuid4()
        results.append(DeckResetResult(
            deck_id=deck.id, previous_epoch=previous_epoch, progress_epoch=deck.progress_epoch,
            cleared_card_count=cleared.get(deck.id, 0),
        ))
    return results


def reset_progress(db: Session, user_id: uuid.UUID, deck_id: uuid.UUID,
                   request: ProgressResetRequest) -> ProgressResetResponse:
    lock_progress_user(db, user_id)
    fingerprint = _fingerprint("reset", {"deck_id": str(deck_id), **request.model_dump(mode="json")})
    replay = _replay(db, user_id, request.reset_id, fingerprint)
    if replay is not None:
        return ProgressResetResponse.model_validate(replay)
    decks = _scope(db, user_id, deck_id)
    _validate_epochs(decks, request.expected_decks)
    response = ProgressResetResponse(
        reset_id=request.reset_id, deck_id=deck_id, reset_at=datetime.now(timezone.utc),
        decks=_clear_learning(db, user_id, decks),
    )
    _remember(db, user_id, request.reset_id, "reset", fingerprint, response)
    recalculate_affected_plans(db, user_id, plan_roots_for_decks(decks), reason="progress_reset")
    return response


def read_progress(db: Session, user_id: uuid.UUID, deck_id: uuid.UUID) -> StudyProgressResponse:
    lock_progress_user(db, user_id)
    decks = _scope(db, user_id, deck_id)
    rows = db.execute(select(Card, CardProgress.learned_at).outerjoin(
        CardProgress, (CardProgress.card_id == Card.id) & (CardProgress.user_id == user_id),
    ).where(Card.deck_id.in_([deck.id for deck in decks])).order_by(Card.id)).all()
    cards_by_deck: dict[uuid.UUID, list[CardLearningFact]] = {deck.id: [] for deck in decks}
    for card, learned_at in rows:
        if learned_at is not None and learned_at.tzinfo is None:
            learned_at = learned_at.replace(tzinfo=timezone.utc)  # SQLite test storage lacks timezone offsets.
        cards_by_deck[card.deck_id].append(CardLearningFact(card_id=card.id, learned_at=learned_at))
    progress = get_chapter_progress(db, user_id, decks)
    return StudyProgressResponse(deck_id=deck_id, decks=[DeckLearningFacts(
        deck_id=deck.id, progress_epoch=deck.progress_epoch, title=deck.title,
        parent_deck_id=deck.parent_deck_id, position=deck.position,
        generation_status=deck.generation_status, cards=cards_by_deck[deck.id],
        **progress[deck.id].model_dump(),
    ) for deck in decks], summary=ProgressSummary(
        total_deck_count=len(decks),
        completed_deck_count=sum(item.completed for item in progress.values()),
        learned_card_count=sum(item.learned_card_count for item in progress.values()),
        total_card_count=sum(item.total_card_count for item in progress.values()),
    ))
