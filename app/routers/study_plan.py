import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.study_plan import StudyPlanCreate, StudyPlanResponse
from app.services.study_plan import create_plan, get_plan, plan_response

router = APIRouter(prefix="/decks", tags=["Study plans"])


@router.post("/{deck_id}/study-plan", response_model=StudyPlanResponse, status_code=201)
def create_study_plan(
    deck_id: uuid.UUID, request: StudyPlanCreate,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    try:
        plan = create_plan(db, deck_id, current_user.id, request)
        response = plan_response(db, plan)
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


@router.get("/{deck_id}/study-plan", response_model=StudyPlanResponse)
def get_study_plan(
    deck_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    return plan_response(db, get_plan(db, deck_id, current_user.id))
