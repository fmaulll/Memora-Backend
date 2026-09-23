import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.card import Card
from app.models.deck import Deck
from app.models.user import User
from app.services.study_plan import ensure_structure_editable
from app.schemas.deck import (
    ChapterReorderRequest,
    ChapterGenerationStatus,
    DeckCreate,
    DeckGenerationStatusResponse,
    DeckResponse,
    DeckUpdate,
)


router = APIRouter(
    prefix="/decks",
    tags=["Decks"],
)

def validate_parent_deck(
    parent_deck_id: uuid.UUID | None,
    current_deck_id: uuid.UUID | None,
    db: Session,
    current_user: User,
):
    if parent_deck_id is None:
        return

    # Prevent deck from becoming its own parent
    if current_deck_id is not None and parent_deck_id == current_deck_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A deck cannot be its own parent.",
        )

    # Parent must belong to current user
    parent = db.scalar(
        select(Deck).where(
            Deck.id == parent_deck_id,
            Deck.user_id == current_user.id,
        )
    )

    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parent deck not found.",
        )

    # Parent must be a root deck
    if parent.parent_deck_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A child deck cannot have another child deck.",
        )

    ensure_structure_editable(db, parent.id)


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
    validate_parent_deck(
        parent_deck_id=data.parent_deck_id,
        current_deck_id=None,
        db=db,
        current_user=current_user,
    )

    deck = Deck(
        **data.model_dump(),
        user_id=current_user.id,
    )

    db.add(deck)
    db.commit()
    db.refresh(deck)

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
    "/{deck_id}/generation-status",
    response_model=DeckGenerationStatusResponse,
)
def get_generation_status(
    deck_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Get parent deck
    parent_deck = db.scalar(
        select(Deck).where(
            Deck.id == deck_id,
            Deck.user_id == current_user.id,
        )
    )

    if parent_deck is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deck not found",
        )

    # Get child chapter decks
    chapters = db.scalars(
        select(Deck)
        .where(
            Deck.parent_deck_id == parent_deck.id,
            Deck.user_id == current_user.id,
        )
        .order_by(Deck.position.asc())
    ).all()

    chapter_statuses = []

    for chapter in chapters:
        card_count = db.scalar(
            select(func.count())
            .select_from(Card)
            .where(Card.deck_id == chapter.id)
        )

        chapter_statuses.append(
            ChapterGenerationStatus(
                id=chapter.id,
                title=chapter.title,
                generation_status=chapter.generation_status,
                card_count=card_count or 0,
            )
        )

    return DeckGenerationStatusResponse(
        deck_id=parent_deck.id,
        generation_status=parent_deck.generation_status,
        chapters=chapter_statuses,
    )

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

    if "parent_deck_id" in updates and updates["parent_deck_id"] != deck.parent_deck_id:
        ensure_structure_editable(db, deck.parent_deck_id or deck.id)
    if "position" in updates and updates["position"] != deck.position and deck.parent_deck_id:
        ensure_structure_editable(db, deck.parent_deck_id)

    if "parent_deck_id" in updates and updates["parent_deck_id"] != deck.parent_deck_id:
        validate_parent_deck(
            parent_deck_id=updates["parent_deck_id"],
            current_deck_id=deck.id,
            db=db,
            current_user=current_user,
        )

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

    if deck.parent_deck_id is not None:
        ensure_structure_editable(db, deck.parent_deck_id)
    db.delete(deck)
    db.commit()

@router.put(
    "/{parent_deck_id}/chapters/reorder",
    response_model=list[DeckResponse],
)
def reorder_chapters(
    parent_deck_id: uuid.UUID,
    data: ChapterReorderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    print("========== REORDER CHAPTERS ==========")
    print("PARENT:", parent_deck_id)
    print("CHAPTER IDS:", data.chapter_ids)

    # Make sure parent exists, belongs to user,
    # and is actually a root deck.
    parent = db.scalar(
        select(Deck).where(
            Deck.id == parent_deck_id,
            Deck.user_id == current_user.id,
            Deck.parent_deck_id.is_(None),
        )
    )

    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent deck not found.",
        )

    ensure_structure_editable(db, parent.id)

    # Load every chapter belonging to this parent.
    chapters = db.scalars(
        select(Deck).where(
            Deck.parent_deck_id == parent_deck_id,
            Deck.user_id == current_user.id,
        )
    ).all()

    chapters_by_id = {
        chapter.id: chapter
        for chapter in chapters
    }

    requested_ids = data.chapter_ids

    # Prevent duplicate chapter IDs.
    if len(requested_ids) != len(set(requested_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate chapter IDs.",
        )

    # The request must contain exactly the chapters
    # belonging to this parent.
    if set(requested_ids) != set(chapters_by_id.keys()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chapter list does not match parent deck.",
        )

    # Array order becomes the canonical position.
    for position, chapter_id in enumerate(requested_ids):
        chapter = chapters_by_id[chapter_id]
        chapter.position = position

        print(
            "CHAPTER POSITION:",
            chapter.title,
            "→",
            position,
        )

    db.commit()

    # Return chapters in their new order.
    updated_chapters = db.scalars(
        select(Deck)
        .where(
            Deck.parent_deck_id == parent_deck_id,
            Deck.user_id == current_user.id,
        )
        .order_by(
            Deck.position.asc(),
            Deck.created_at.asc(),
        )
    ).all()

    print(
        "✅ CHAPTER REORDER COMPLETE:",
        len(updated_chapters),
        "chapters",
    )

    return updated_chapters
