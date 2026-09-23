import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.study_progress import (
    ProgressResetRequest, ProgressResetResponse, StudyProgressResponse,
    StudyProgressSubmission, StudyProgressSubmissionResponse,
)
from app.services.study_progress import read_progress, reset_progress, submit_progress

router = APIRouter(tags=["Study progress"])


@router.get("/decks/{deck_id}/study-progress", response_model=StudyProgressResponse)
def get_study_progress(
    deck_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    return read_progress(db, current_user.id, deck_id)


@router.post("/study/progress/submissions", response_model=StudyProgressSubmissionResponse)
def submit_study_progress(
    request: StudyProgressSubmission, db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    try:
        response = submit_progress(db, current_user.id, request)
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


@router.post("/decks/{deck_id}/study-progress/reset", response_model=ProgressResetResponse)
def reset_study_progress(
    deck_id: uuid.UUID, request: ProgressResetRequest,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    try:
        response = reset_progress(db, current_user.id, deck_id, request)
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise
