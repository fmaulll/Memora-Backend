import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import settings
from app.models.subscription import Subscription, AppleNotification, PurchaseTokenAlias
from app.models.user import User
from app.schemas.subscription import Entitlement, SubscriptionResponse
from app.services.apple import aware, fail, get_apple_gateway, milliseconds, utcnow


def lock_user(db, user_id):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        fail(401, "account_unavailable", "Sign in again; this account is no longer available.")
    return user


def token_owner(db, token):
    try:
        token = uuid.UUID(str(token))
    except (ValueError, TypeError):
        return None
    owner = db.scalar(select(User.id).where(User.app_account_token == token))
    return owner or db.scalar(select(PurchaseTokenAlias.user_id).where(PurchaseTokenAlias.token == token))


def effective_status(subscription, now):
    if subscription.revoked_at is not None or subscription.status == "revoked":
        return "revoked", False
    if subscription.status == "grace_period":
        valid = bool(subscription.grace_period_expires_at and aware(subscription.grace_period_expires_at) > now)
        return ("grace_period", True) if valid else ("expired", False)
    if subscription.status == "active":
        valid = bool(subscription.expires_at and aware(subscription.expires_at) > now)
        return ("active", True) if valid else ("expired", False)
    return subscription.status, False


def entitlement(db, user_id):
    now = utcnow()
    subscriptions = db.scalars(select(Subscription).where(Subscription.user_id == user_id)).all()
    if not subscriptions:
        return Entitlement()
    def rank(sub):
        fresh = aware(sub.last_verified_at) >= now - timedelta(seconds=settings.subscription_max_staleness_seconds)
        return (effective_status(sub, now)[1] and fresh, effective_status(sub, now)[1],
                aware(sub.expires_at) or now - timedelta(days=36500), sub.original_transaction_id)
    sub = max(subscriptions, key=rank)
    status, access = effective_status(sub, now)
    fresh = aware(sub.last_verified_at) >= now - timedelta(seconds=settings.subscription_max_staleness_seconds)
    return Entitlement(
        is_subscribed=access and fresh, status=status if fresh else "verification_required",
        product_id=sub.product_id, expires_at=aware(sub.expires_at), auto_renew=sub.auto_renew,
        grace_period_expires_at=aware(sub.grace_period_expires_at), revoked_at=aware(sub.revoked_at),
        last_verified_at=aware(sub.last_verified_at),
    )


def subscription_response(db, user):
    return SubscriptionResponse(
        app_account_token=user.app_account_token, entitlement=entitlement(db, user.id),
        free_ai_deck_available=not user.free_ai_deck_used,
    )


def apply_snapshot(db, user, snapshot):
    tx, renewal = snapshot.transaction, snapshot.renewal
    sub = db.scalar(select(Subscription).where(Subscription.original_transaction_id == tx.originalTransactionId).with_for_update())
    if sub and sub.user_id != user.id:
        fail(409, "subscription_owned_by_another_account", "This subscription belongs to another Nudge account.")
    owner = token_owner(db, tx.appAccountToken)
    if (owner is not None and owner != user.id) or (sub is None and owner != user.id):
        fail(409, "app_account_token_mismatch", "Purchase with this account's appAccountToken or sign in to the owning account.")
    if tx.appAccountToken and owner is None:
        fail(409, "app_account_token_mismatch", "The purchase token does not belong to this account.")
    if sub and aware(sub.snapshot_started_at) > snapshot.started_at:
        return sub
    if sub is None:
        sub = Subscription(user_id=user.id, original_transaction_id=tx.originalTransactionId)
        db.add(sub)
    sub.product_id = tx.productId
    sub.environment = tx.environment.value.lower()
    sub.status = "revoked" if tx.revocationDate is not None else snapshot.status
    sub.expires_at = milliseconds(tx.expiresDate)
    sub.auto_renew = renewal.autoRenewStatus == 1
    sub.grace_period_expires_at = milliseconds(renewal.gracePeriodExpiresDate)
    sub.revoked_at = milliseconds(tx.revocationDate)
    sub.last_verified_at = utcnow()
    sub.snapshot_started_at = snapshot.started_at
    db.flush()
    return sub


def verify_purchase(db, user, signed, gateway=None):
    gateway = gateway or get_apple_gateway()
    tx = gateway.transaction(signed)
    user = lock_user(db, user.id)
    existing = db.scalar(select(Subscription).where(Subscription.original_transaction_id == tx.originalTransactionId))
    if existing and existing.user_id != user.id:
        fail(409, "subscription_owned_by_another_account", "This subscription belongs to another Nudge account.")
    if not existing and token_owner(db, tx.appAccountToken) != user.id:
        fail(409, "app_account_token_mismatch", "The purchase token does not belong to this account.")
    try:
        apply_snapshot(db, user, gateway.snapshot(tx.originalTransactionId))
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "subscription_owned_by_another_account", "This subscription is already associated with an account.")
    return subscription_response(db, user)


def reconcile_user(db, user, gateway=None, stale_only=False):
    user = lock_user(db, user.id)
    subs = db.scalars(select(Subscription).where(Subscription.user_id == user.id)).all()
    cutoff = utcnow() - timedelta(seconds=settings.subscription_max_staleness_seconds)
    for sub in subs:
        if stale_only and aware(sub.last_verified_at) > cutoff:
            continue
        gateway = gateway or get_apple_gateway()
        apply_snapshot(db, user, gateway.snapshot(sub.original_transaction_id))
    db.flush()
    return user


def require_paid(db, user):
    reconcile_user(db, user, stale_only=True)
    if not entitlement(db, user.id).is_subscribed:
        fail(402, "subscription_required", "An active Nudge subscription is required.")


def process_notification(db, signed, gateway=None):
    gateway = gateway or get_apple_gateway()
    notification = gateway.notification(signed)
    if db.get(AppleNotification, notification.notificationUUID):
        return {"status": "duplicate"}
    data = notification.data
    tx = None
    if data and data.signedTransactionInfo:
        tx = gateway.transaction(data.signedTransactionInfo)
        if data.signedRenewalInfo:
            gateway.renewal(data.signedRenewalInfo, tx.originalTransactionId)
        sub = db.scalar(select(Subscription).where(Subscription.original_transaction_id == tx.originalTransactionId))
        owner = sub.user_id if sub else token_owner(db, tx.appAccountToken)
        if owner:
            user = lock_user(db, owner)
            # Serialize with purchases and merges, then retrieve CURRENT Apple state.
            # Notification timestamps and arrival order never overwrite newer state.
            apply_snapshot(db, user, gateway.snapshot(tx.originalTransactionId))
    record = AppleNotification(
        notification_uuid=notification.notificationUUID,
        notification_type=str(getattr(notification.notificationType, "value", notification.notificationType)),
        original_transaction_id=tx.originalTransactionId if tx else None,
        signed_date=notification.signedDate, processed_at=utcnow(),
    )
    try:
        db.add(record)
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.get(AppleNotification, notification.notificationUUID):
            return {"status": "duplicate"}
        raise
    return {"status": "processed"}
