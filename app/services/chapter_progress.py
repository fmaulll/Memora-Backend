"""Derived progress over current cards; no persisted completion flag."""
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.card import Card
from app.models.deck import Deck
from app.models.study_progress import CardProgress
from app.schemas.study_progress import ChapterProgress


def get_chapter_progress(
    db: Session, user_id: uuid.UUID, chapters: list[Deck],
) -> dict[uuid.UUID, ChapterProgress]:
    """One aggregate query, independent of chapter count. Callers own the transaction."""
    if any(chapter.user_id != user_id for chapter in chapters):
        raise HTTPException(status_code=404, detail="Deck not found")
    if not chapters:
        return {}
    rows = db.execute(
        select(Card.deck_id, func.count(Card.id), func.count(CardProgress.learned_at))
        .join(Deck, Deck.id == Card.deck_id)
        .outerjoin(CardProgress, (CardProgress.card_id == Card.id) & (CardProgress.user_id == user_id))
        .where(Deck.user_id == user_id, Card.deck_id.in_([chapter.id for chapter in chapters]))
        .group_by(Card.deck_id)
    ).all()
    counts = {deck_id: (total, learned) for deck_id, total, learned in rows}
    result = {}
    for chapter in chapters:
        total, learned = counts.get(chapter.id, (0, 0))
        result[chapter.id] = ChapterProgress(
            total_card_count=total,
            learned_card_count=learned,
            completion_percentage=round(100 * learned / total, 2) if total else 0,
            completed=chapter.generation_status == "completed" and total > 0 and learned == total,
        )
    return result
