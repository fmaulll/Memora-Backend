import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.user import User
from app.models.exam import Exam, ExamQuestion
from app.schemas.exam import (
    ExamProgressionResponse,
    ExamQuestionsResponse,
    ExamResponse,
    ExamSubmissionRequest,
    ExamSubmissionResponse,
    ExamType,
)
from app.services.exam import ExamService
from app.services.exam_generation import ExamGenerationService
from app.services.exam_submission import ExamSubmissionService


router = APIRouter(
    prefix="/decks",
    tags=["Exams"],
)

question_router = APIRouter(
    prefix="/exams",
    tags=["Exams"],
)


@question_router.post(
    "/{exam_id}/submit",
    response_model=ExamSubmissionResponse,
)
def submit_exam(
    exam_id: uuid.UUID,
    request: ExamSubmissionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ExamSubmissionService()

    return service.submit(
        exam_id=exam_id,
        request=request,
        db=db,
        current_user=current_user,
    )


def _public_questions(exam, questions):
    return {
        "exam_id": exam.id,
        "exam_type": exam.exam_type,
        "question_count": len(questions),
        "questions": questions,
    }


@router.post(
    "/{parent_deck_id}/exams/{exam_type}/generate",
    response_model=ExamQuestionsResponse,
)
async def generate_exam_questions(
    parent_deck_id: uuid.UUID,
    exam_type: ExamType,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ExamService()
    parent_deck = service.get_parent_deck(parent_deck_id, db, current_user)
    definitions = service.get_or_create_definitions(parent_deck, db)
    exam = definitions[exam_type]
    generation_service = ExamGenerationService()
    try:
        saved_exam, questions = await generation_service.generate(
            exam.id,
            db,
            current_user,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"Exam question generation failed: {error}",
        ) from error
    return _public_questions(saved_exam, questions)


@question_router.get(
    "/{exam_id}",
    response_model=ExamQuestionsResponse,
)
def get_exam_questions(
    exam_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    generation_service = ExamGenerationService()
    exam = db.scalar(
        select(Exam)
        .join(Exam.deck)
        .where(
            Exam.id == exam_id,
            Exam.deck.has(user_id=current_user.id),
        )
    )
    if exam is None:
        raise HTTPException(status_code=404, detail="Exam not found")
    generation_service._validate_unlocked(exam, db, current_user)
    questions = db.scalars(
        select(ExamQuestion)
        .where(ExamQuestion.exam_id == exam.id)
        .order_by(ExamQuestion.position.asc())
    ).all()
    return _public_questions(exam, questions)


@router.get(
    "/{parent_deck_id}/exams",
    response_model=ExamProgressionResponse,
)
def get_exam_progression(
    parent_deck_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ExamService()

    return service.get_status(
        parent_deck_id=parent_deck_id,
        db=db,
        current_user=current_user,
    )


@router.get(
    "/{parent_deck_id}/exams/{exam_type}",
    response_model=ExamResponse,
)
def get_exam(
    parent_deck_id: uuid.UUID,
    exam_type: ExamType,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = ExamService()

    return service.get_exam(
        parent_deck_id=parent_deck_id,
        exam_type=exam_type,
        db=db,
        current_user=current_user,
    )