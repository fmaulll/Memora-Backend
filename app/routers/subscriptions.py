from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.database import get_db
from app.models.user import User
from app.schemas.subscription import AppleNotificationRequest, SubscriptionResponse, VerifyPurchaseRequest
from app.services.subscriptions import process_notification, reconcile_user, subscription_response, verify_purchase

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


@router.get("/me", response_model=SubscriptionResponse)
def get_subscription(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return subscription_response(db, user)


@router.post("/apple/verify", response_model=SubscriptionResponse)
def verify(request: VerifyPurchaseRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return verify_purchase(db, user, request.signed_transaction)


@router.post("/reconcile", response_model=SubscriptionResponse)
def reconcile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user = reconcile_user(db, user)
    db.commit()
    return subscription_response(db, user)


@router.post("/apple/notifications")
def apple_notifications(request: AppleNotificationRequest, db: Session = Depends(get_db)):
    return process_notification(db, request.signedPayload)
