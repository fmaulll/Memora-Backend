"""Canonical owned chapter order for plans, learning progress, and exams."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.deck import Deck


def ordered_chapters(db: Session, parent: Deck, *, lock: bool = False) -> list[Deck]:
    query = select(Deck).where(
        Deck.parent_deck_id == parent.id, Deck.user_id == parent.user_id,
    ).order_by(Deck.position, Deck.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return list(db.scalars(query).all())
