import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.card import Card
from app.models.deck import Deck
from app.models.user import User
from app.schemas.card import (
    BulkCardCreate,
    CardCreate,
    CardResponse,
    CardUpdate,
)
from app.services.study_progress import lock_progress_user
from app.services.study_plan_hooks import plan_roots_for_decks, recalculate_affected_plans


router = APIRouter(
    tags=["Cards"],
)


def get_user_deck(
    deck_id: uuid.UUID,
    current_user: User,
    db: Session,
):
    deck = db.scalar(
        select(Deck).where(
            Deck.id == deck_id,
            Deck.user_id == current_user.id,
        )
    )

    if deck is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deck not found",
        )

    return deck

@router.post(
    "/decks/{deck_id}/cards/bulk",
    response_model=list[CardResponse],
    status_code=status.HTTP_200_OK,
)
def create_or_update_cards_bulk(
    deck_id: uuid.UUID,
    data: BulkCardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lock_progress_user(db, current_user.id)
    # Make sure the deck belongs to the current user
    deck = (
        db.query(Deck)
        .filter(
            Deck.id == deck_id,
            Deck.user_id == current_user.id,
        )
        .first()
    )

    if not deck:
        raise HTTPException(
            status_code=404,
            detail="Deck not found",
        )

    # Get all existing cards
    existing_cards = (
        db.query(Card)
        .filter(Card.deck_id == deck_id)
        .all()
    )

    existing_by_id = {
        card.id: card
        for card in existing_cards
    }

    incoming_ids = set()
    added = False
    removed = False

    # Insert / update
    for card_data in data.cards:

        incoming_ids.add(card_data.id)

        existing = existing_by_id.get(card_data.id)

        if existing:
            # UPDATE
            existing.front = card_data.front
            existing.back = card_data.back
            existing.front_image_url = card_data.front_image_url
            existing.back_image_url = card_data.back_image_url

        else:
            added = True
            # INSERT
            new_card = Card(
                id=card_data.id,
                deck_id=deck_id,
                front=card_data.front,
                back=card_data.back,
                front_image_url=card_data.front_image_url,
                back_image_url=card_data.back_image_url,
            )

            db.add(new_card)

    # DELETE cards that no longer exist locally
    for existing in existing_cards:

        if existing.id not in incoming_ids:
            removed = True
            db.delete(existing)

    try:
        if added or removed:
            reason = "cards_replaced" if added and removed else "card_added" if added else "card_deleted"
            recalculate_affected_plans(db, current_user.id, plan_roots_for_decks([deck]), reason=reason)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Return the current server state
    return (
        db.query(Card)
        .filter(Card.deck_id == deck_id)
        .all()
    )

@router.post(
    "/decks/{deck_id}/cards",
    response_model=CardResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_card(
    deck_id: uuid.UUID,
    data: CardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lock_progress_user(db, current_user.id)
    deck = get_user_deck(deck_id, current_user, db)

    card = Card(
        id=data.id,
        deck_id=deck_id,
        front=data.front,
        back=data.back,
        front_image_url=data.front_image_url,
        back_image_url=data.back_image_url,
    )

    db.add(card)
    try:
        recalculate_affected_plans(db, current_user.id, plan_roots_for_decks([deck]), reason="card_added")
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(card)

    return card


@router.get(
    "/decks/{deck_id}/cards",
    response_model=list[CardResponse],
)
def get_cards(
    deck_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_user_deck(deck_id, current_user, db)

    statement = (
        select(Card)
        .where(Card.deck_id == deck_id)
        .order_by(Card.created_at.asc())
    )

    return db.scalars(statement).all()


@router.get(
    "/cards/{card_id}",
    response_model=CardResponse,
)
def get_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    card = db.scalar(
        select(Card)
        .join(Deck)
        .where(
            Card.id == card_id,
            Deck.user_id == current_user.id,
        )
    )

    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found",
        )

    return card


@router.put(
    "/cards/{card_id}",
    response_model=CardResponse,
)
def update_card(
    card_id: uuid.UUID,
    data: CardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    card = db.scalar(
        select(Card)
        .join(Deck)
        .where(
            Card.id == card_id,
            Deck.user_id == current_user.id,
        )
    )

    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found",
        )

    updates = data.model_dump(exclude_unset=True)

    for field, value in updates.items():
        setattr(card, field, value)

    db.commit()
    db.refresh(card)

    return card


@router.delete(
    "/cards/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_card(
    card_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lock_progress_user(db, current_user.id)
    card = db.scalar(
        select(Card)
        .join(Deck)
        .where(
            Card.id == card_id,
            Deck.user_id == current_user.id,
        )
    )

    if card is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Card not found",
        )

    deck = get_user_deck(card.deck_id, current_user, db)
    db.delete(card)
    try:
        recalculate_affected_plans(db, current_user.id, plan_roots_for_decks([deck]), reason="card_deleted")
        db.commit()
    except Exception:
        db.rollback()
        raise
