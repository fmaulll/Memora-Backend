from fastapi import (
    APIRouter,
    Depends,
    BackgroundTasks,
    UploadFile,
    File,
    HTTPException,
)

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
from app.schemas.study_material import (
    StudyMaterialResponse,
    StudyMaterialUploadResponse,
)

from app.services.study_material import (
    StudyMaterialService,
)

from app.models.study_material import StudyMaterial

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.deck import Deck
from app.models.user import User

from pathlib import Path

from app.services.study_timeline import StudyTimelineService

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    materials = []

    if request.study_material_ids:

        materials = db.scalars(
            select(StudyMaterial).where(
                StudyMaterial.id.in_(
                    request.study_material_ids
                ),
                StudyMaterial.user_id
                == current_user.id,
            )
        ).all()

    if len(materials) != len(
        request.study_material_ids
    ):
        raise HTTPException(
            status_code=404,
            detail=(
                "One or more study materials were not found."
            ),
        )

    service = DeepSeekService()

    return await service.generate_deck_plan(
        request,
        materials=materials,
    )


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
        learning_language=plan.learning_language,
        generation_status="generating",
    )

    db.add(parent_deck)
    db.flush()

    # Create chapter decks
    chapter_decks = []

    for index, chapter in enumerate(plan.chapters):
        chapter_deck = Deck(
            user_id=current_user.id,
            parent_deck_id=parent_deck.id,
            title=chapter.title,
            subject=plan.subject,
            education_level=plan.education_level,
            learning_language=plan.learning_language,
            position=index,
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
            learning_language=parent_deck.learning_language,
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

@router.post(
    "/study-materials",
    response_model=StudyMaterialUploadResponse,
)
async def upload_study_materials(
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    allowed_content_types = {
        "application/pdf",
        "text/plain",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    study_material_service = StudyMaterialService()

    materials = []

    for file in files:

        filename = file.filename or ""

        extension = Path(filename).suffix.lower()

        allowed_extensions = {
            ".pdf",
            ".txt",
            ".docx",
            ".pptx",
        }

        if extension not in allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {filename}",
            )

        extracted_text = (
            await study_material_service.extract_text(
                file
            )
        )

        study_material = StudyMaterial(
            user_id=current_user.id,
            filename=file.filename or "Untitled",
            content_type=file.content_type,
            extracted_text=extracted_text,
        )

        db.add(study_material)

        materials.append(study_material)

    db.commit()

    for material in materials:
        db.refresh(material)

    return StudyMaterialUploadResponse(
        materials=[
            StudyMaterialResponse(
                id=material.id,
                filename=material.filename,
            )
            for material in materials
        ]
    )