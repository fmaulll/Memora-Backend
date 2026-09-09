from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.models.deck import Deck
from app.models.exam import ExamAttempt, UserExamProgression
from app.models.study_material import StudyMaterial
from app.models.subscription import Subscription, PurchaseTokenAlias, AIGenerationRequest
from app.schemas.auth import UserResponse
from app.services.apple import fail
from app.services.subscriptions import entitlement, lock_user


def user_response(db, user):
    return UserResponse(
        id=user.id, name=user.name, email=user.email, created_at=user.created_at,
        is_anonymous=user.is_anonymous, app_account_token=user.app_account_token,
        free_ai_deck_available=not user.free_ai_deck_used, entitlement=entitlement(db, user.id),
    )


def upgrade_account(db, user, request):
    user = lock_user(db, user.id)
    if not user.is_anonymous:
        fail(409, "not_anonymous", "This account is already permanent.")
    if db.scalar(select(User.id).where(User.email == request.email, User.id != user.id)):
        fail(409, "email_already_registered", "Use explicit account merge to join an existing account.")
    user.email = request.email
    user.name = request.name
    user.password_hash = hash_password(request.password)
    user.is_anonymous = False
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "email_already_registered", "This email is already registered.")
    return user


def merge_account(db, source, request):
    target = db.scalar(select(User).where(User.email == request.email))
    if target is None or target.is_anonymous or not verify_password(request.password, target.password_hash):
        fail(401, "invalid_credentials", "Invalid destination account credentials.")
    target_id, source_id = target.id, source.id
    # All subscription/admission writes lock their owner; lock both in UUID order.
    users = {user_id: lock_user(db, user_id) for user_id in sorted({source_id, target_id})}
    source, target = users[source_id], users[target_id]
    if not source.is_anonymous or source.id == target.id:
        fail(409, "not_anonymous", "Only an anonymous account can be merged.")
    source_keys = select(AIGenerationRequest.idempotency_key).where(AIGenerationRequest.user_id == source.id)
    collision = db.scalar(select(AIGenerationRequest.id).where(
        AIGenerationRequest.user_id == target.id, AIGenerationRequest.idempotency_key.in_(source_keys),
    ))
    if collision:
        fail(409, "merge_idempotency_conflict", "Generation request keys conflict; contact support before merging.")
    target.free_ai_deck_used = target.free_ai_deck_used or source.free_ai_deck_used
    db.add(PurchaseTokenAlias(token=source.app_account_token, user_id=target.id))
    for model in (Deck, StudyMaterial, ExamAttempt, UserExamProgression, Subscription, PurchaseTokenAlias, AIGenerationRequest):
        db.execute(update(model).where(model.user_id == source.id).values(user_id=target.id))
    db.flush()
    db.delete(source)  # Old access and refresh tokens now resolve to no user.
    db.commit()
    return target
