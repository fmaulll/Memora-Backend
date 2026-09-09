import hashlib
import json

from sqlalchemy import select, update

from app.models.subscription import AIGenerationRequest
from app.models.user import User
from app.services.apple import fail
from app.services.subscriptions import entitlement, lock_user, reconcile_user


def admit_deck(db, user, key, request):
    """Caller commits receipt + allowance + deck + job in ONE transaction."""
    user = lock_user(db, user.id)
    request_hash = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = db.scalar(select(AIGenerationRequest).where(
        AIGenerationRequest.user_id == user.id, AIGenerationRequest.idempotency_key == key,
    ))
    if prior:
        if prior.request_hash != request_hash:
            fail(409, "idempotency_conflict", "Use the same request body when retrying an idempotency key.")
        if prior.parent_deck_id is None:
            fail(410, "generation_deleted", "The deck for this request was deleted; the allowance remains used.")
        return prior, False, request_hash
    reconcile_user(db, user, stale_only=True)
    if entitlement(db, user.id).is_subscribed:
        return None, False, request_hash
    # Conditional UPDATE remains atomic even on databases without FOR UPDATE.
    result = db.execute(update(User).where(
        User.id == user.id, User.free_ai_deck_used.is_(False),
    ).values(free_ai_deck_used=True))
    if result.rowcount != 1:
        fail(402, "free_deck_already_used", "Your free AI deck has been used. Subscribe to create another.")
    return None, True, request_hash


def require_plan_access(db, user):
    user = reconcile_user(db, user, stale_only=True)
    if user.free_ai_deck_used and not entitlement(db, user.id).is_subscribed:
        fail(402, "subscription_required", "Subscribe to plan another AI deck.")
    db.commit()
