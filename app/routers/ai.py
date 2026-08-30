from fastapi import APIRouter, Depends, BackgroundTasks

from app.services.deck_generation import DeckGenerationService

from app.ai.deepseek import DeepSeekService
from app.ai.gemini import GeminiService
from app.schemas.ai import (
    DeckPlanRequest,
    DeckPlanResponse,
    GenerateDeckRequest,
    GeneratedDeckWithTimelineResponse,
    GeneratedDeckStatus,
    GeneratedChapterStatus,
)

from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.deck import Deck
from app.models.user import User


router = APIRouter(
    prefix="/ai",
    tags=["AI"],
)


@router.post(
    "/decks/plan",
    response_model=DeckPlanResponse,
)
async def generate_deck_plan(
    request: DeckPlanRequest,
):
    # service = GeminiService()
    service = DeepSeekService()

    return await service.generate_deck_plan(request)

from app.services.study_timeline import StudyTimelineService


@router.post(
    "/decks/generate",
    response_model=GeneratedDeckWithTimelineResponse,
)
async def generate_deck(
    request: GenerateDeckRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plan = request.plan

    # Create parent deck
    parent_deck = Deck(
        user_id=current_user.id,
        title=plan.title,
        subject=plan.subject,
        education_level=plan.education_level,
        generation_status="generating",
    )

    db.add(parent_deck)
    db.flush()

    # Create chapter decks
    chapter_decks = []

    for chapter in plan.chapters:
        chapter_deck = Deck(
            user_id=current_user.id,
            parent_deck_id=parent_deck.id,
            title=chapter.title,
            subject=plan.subject,
            education_level=plan.education_level,
            generation_status="pending",
        )

        db.add(chapter_deck)
        chapter_decks.append(chapter_deck)

    # Calculate total cards from the PLAN
    total_cards = sum(
        chapter.card_count
        for chapter in plan.chapters
    )

    # Generate timeline immediately
    timeline_service = StudyTimelineService()

    timeline = timeline_service.generate(
        total_cards=total_cards,
        target_date=request.target_date,
        study_purpose=request.study_purpose,
    )

    db.commit()

    db.refresh(parent_deck)

    for chapter_deck in chapter_decks:
        db.refresh(chapter_deck)

    generation_service = DeckGenerationService()

    background_tasks.add_task(
        generation_service.generate_deck,
        parent_deck.id,
        plan,
    )

    # Temporary response structure
    return GeneratedDeckWithTimelineResponse(
        deck=GeneratedDeckStatus(
            id=parent_deck.id,
            title=parent_deck.title,
            subject=parent_deck.subject,
            education_level=parent_deck.education_level,
            generation_status=parent_deck.generation_status,
            chapters=[
                GeneratedChapterStatus(
                    id=chapter_deck.id,
                    title=chapter_deck.title,
                    generation_status=chapter_deck.generation_status,
                )
                for chapter_deck in chapter_decks
            ],
        ),
        timeline=timeline,
    )