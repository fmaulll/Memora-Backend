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
    status_code=status.HTTP_201_CREATED,
)
def create_cards_bulk(
    deck_id: uuid.UUID,
    data: BulkCardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_user_deck(deck_id, current_user, db)

    cards = [
        Card(
            id=card.id,
            deck_id=deck_id,
            front=card.front,
            back=card.back,
            front_image_url=card.front_image_url,
            back_image_url=card.back_image_url,
        )
        for card in data.cards
    ]

    db.add_all(cards)
    db.commit()

    for card in cards:
        db.refresh(card)

    return cards

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
    get_user_deck(deck_id, current_user, db)

    card = Card(
        id=data.id,
        deck_id=deck_id,
        front=data.front,
        back=data.back,
        front_image_url=data.front_image_url,
        back_image_url=data.back_image_url,
    )

    db.add(card)
    db.commit()
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

    db.delete(card)
    db.commit()