"""Run hourly via deployment scheduler: python -m app.reconcile_subscriptions."""
import logging
from sqlalchemy import select
from app.db.database import SessionLocal
from app.models.subscription import Subscription
from app.models.user import User
from app.services.subscriptions import reconcile_user

logger = logging.getLogger(__name__)


def reconcile_all():
    failures = 0
    # Keyset pagination avoids loading every account and releases locks per account.
    last_id = None
    while True:
        with SessionLocal() as db:
            query = select(Subscription.user_id).distinct().order_by(Subscription.user_id).limit(100)
            if last_id is not None:
                query = query.where(Subscription.user_id > last_id)
            user_ids = db.scalars(query).all()
        if not user_ids:
            break
        for user_id in user_ids:
            with SessionLocal() as db:
                try:
                    user = db.get(User, user_id)
                    if user:
                        reconcile_user(db, user)
                        db.commit()
                except Exception:
                    db.rollback()
                    failures += 1
                    logger.error("Subscription reconciliation failed for user %s", user_id)
        last_id = user_ids[-1]
    return failures


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(1 if reconcile_all() else 0)
