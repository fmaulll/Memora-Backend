import uuid

from fastapi import (
    APIRouter,
    Depends,
    BackgroundTasks,
    UploadFile,
    File,
    HTTPException,
    Header,
)

from app.models.subscription import AIGenerationRequest
from app.services.ai_access import admit_deck, require_plan_access
from app.services.apple import utcnow
from app.services.subscriptions import lock_user, require_paid

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
from app.models.generation_job import GenerationJob

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
    require_plan_access(db, current_user)
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
def generate_deck(
    request: GenerateDeckRequest,
    idempotency_key: uuid.UUID = Header(alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    prior, used_free, request_hash = admit_deck(db, current_user, idempotency_key, request)
    if prior:
        return GeneratedDeckWithTimelineResponse.model_validate(prior.response_json)
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
            key_concepts=chapter.key_concepts,
            card_count=chapter.card_count,
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

    db.flush()
    db.add(GenerationJob(
        parent_deck_id=parent_deck.id,
        plan_json=plan.model_dump(mode="json"),
        status="pending",
    ))

    response = GeneratedDeckWithTimelineResponse(
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

    db.add(AIGenerationRequest(
        user_id=current_user.id, idempotency_key=idempotency_key,
        request_hash=request_hash, used_free_allowance=used_free,
        parent_deck_id=parent_deck.id, response_json=response.model_dump(mode="json"),
        created_at=utcnow(),
    ))
    db.commit()
    return response


@router.post(
    "/decks/{deck_id}/retry",
    response_model=GeneratedDeckWithTimelineResponse,
)
def retry_deck_generation(
    deck_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lock_user(db, current_user.id)
    parent_deck = db.scalar(
        select(Deck).where(
            Deck.id == deck_id,
            Deck.user_id == current_user.id,
            Deck.parent_deck_id.is_(None),
        )
    )

    if parent_deck is None:
        raise HTTPException(
            status_code=404,
            detail="Parent deck not found",
        )

    job = db.scalar(
        select(GenerationJob).where(
            GenerationJob.parent_deck_id == parent_deck.id,
        ).with_for_update()
    )

    if job is None:
        raise HTTPException(
            status_code=400,
            detail="No saved generation plan exists for this deck.",
        )

    receipt = db.scalar(select(AIGenerationRequest).where(
        AIGenerationRequest.parent_deck_id == parent_deck.id,
        AIGenerationRequest.user_id == current_user.id,
    ))
    if receipt is None:
        require_paid(db, current_user)

    chapter_decks = db.scalars(
        select(Deck)
        .where(
            Deck.parent_deck_id == parent_deck.id,
            Deck.user_id == current_user.id,
        )
        .order_by(Deck.position.asc())
    ).all()

    # Pending/running/completed jobs are idempotent no-ops. Only failed work resumes.
    if job.status == "failed":
        for chapter_deck in chapter_decks:
            if chapter_deck.generation_status != "completed":
                chapter_deck.generation_status = "pending"
        parent_deck.generation_status = "generating"
        job.status = "pending"
        job.last_error = None
        job.locked_at = None
        job.completed_at = None
    db.commit()

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
        timeline=None,
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