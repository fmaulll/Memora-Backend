import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.deck import Deck
from app.models.user import User
from app.schemas.deck import DeckCreate, DeckResponse, DeckUpdate


router = APIRouter(
    prefix="/decks",
    tags=["Decks"],
)


@router.post(
    "",
    response_model=DeckResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_deck(
    data: DeckCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    print("REQUEST ID:", data.id)

    deck = Deck(
        id=data.id,
        user_id=current_user.id,
        title=data.title,
        subject=data.subject,
        education_level=data.education_level,
        is_favorite=data.is_favorite,
        parent_deck_id=data.parent_deck_id,
    )

    print("MODEL ID BEFORE DB:", deck.id)

    db.add(deck)
    db.commit()
    db.refresh(deck)

    print("MODEL ID AFTER DB:", deck.id)

    return deck


@router.get(
    "",
    response_model=list[DeckResponse],
)
def get_decks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statement = (
        select(Deck)
        .where(Deck.user_id == current_user.id)
        .order_by(Deck.created_at.desc())
    )

    return db.scalars(statement).all()


@router.get(
    "/{deck_id}",
    response_model=DeckResponse,
)
def get_deck(
    deck_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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


@router.put(
    "/{deck_id}",
    response_model=DeckResponse,
)
def update_deck(
    deck_id: uuid.UUID,
    data: DeckUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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

    updates = data.model_dump(exclude_unset=True)

    for field, value in updates.items():
        setattr(deck, field, value)

    db.commit()
    db.refresh(deck)

    return deck


@router.delete(
    "/{deck_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_deck(
    deck_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
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

    db.delete(deck)
    db.commit()