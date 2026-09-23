"""Phase 2C production transaction/migration checks in disposable schemas."""
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

import test_study_plan_postgres as fixtures
from app.db.database import settings
from app.models.card import Card
from app.models.deck import Deck
from app.models.study_plan import StudyPlan, StudyPlanItem
from app.schemas.study_progress import StudyProgressSubmission, ProgressResetRequest
from app.services.study_plan import create_plan, plan_response, recalculate_plan
from app.services.study_progress import submit_progress, reset_progress


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostgreSQL adaptation checks")
class AdaptivePlanPostgresTests(unittest.TestCase):
    cleanup_schema = fixtures.StudyPlanPostgresTests.cleanup_schema

    def setUp(self):
        fixtures.StudyPlanPostgresTests.setUp(self)
        with Session(self.engine, autoflush=False) as db:
            plan = create_plan(db, self.parent_id, self.user_id, self.preferences)
            db.commit()
            self.plan_id = plan.id
            self.epoch = db.get(Deck, self.chapter_id).progress_epoch
            self.card_id = db.scalar(select(Card.id))
        self.clock_patch = patch("app.services.study_plan.local_today", return_value=date(2026, 9, 22))
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def learn(self):
        with Session(self.engine, autoflush=False) as db:
            submit_progress(db, self.user_id, StudyProgressSubmission.model_validate(dict(
                session_id=str(uuid.uuid4()), completed_at=datetime(2026, 9, 20, 17, 30, tzinfo=timezone.utc),
                decks=[dict(deck_id=self.chapter_id, progress_epoch=self.epoch,
                            learned_cards=[dict(card_id=self.card_id, phase="review", answer="got_it")])],
            )))
            db.commit()

    def reset(self, db):
        return reset_progress(db, self.user_id, self.parent_id, ProgressResetRequest.model_validate(dict(
            reset_id=str(uuid.uuid4()), expected_decks=[dict(deck_id=self.chapter_id, progress_epoch=self.epoch)],
        )))

    def test_concurrent_recalculation_increments_once_and_returns_same_ids(self):
        self.learn()
        barrier = threading.Barrier(2)
        def recalc():
            with Session(self.engine, autoflush=False) as db:
                barrier.wait(timeout=10)
                response = plan_response(db, recalculate_plan(db, self.parent_id, self.user_id))
                db.commit()
                return response.model_dump(mode="json")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(recalc) for _ in range(2)]
            first, second = [future.result(timeout=15) for future in futures]
        self.assertEqual(first, second)
        self.assertEqual(first["revision"], 2)
        self.assertEqual(first["items"][0]["actual_learned_count"], 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyPlanItem)), len(first["items"]))

    def test_rollback_after_replacing_items_restores_all_rows_and_revision(self):
        from app.services import study_plan
        original = study_plan._replace_schedule
        def fail(*args):
            original(*args)
            raise RuntimeError("failure after replacing rows")
        with Session(self.engine, autoflush=False) as db:
            before = [(item.id, item.position) for item in db.get(StudyPlan, self.plan_id).items]
            with patch.object(study_plan, "_replace_schedule", side_effect=fail):
                with self.assertRaises(RuntimeError):
                    recalculate_plan(db, self.parent_id, self.user_id)
            db.rollback()
        with Session(self.engine) as db:
            plan = db.get(StudyPlan, self.plan_id)
            self.assertEqual(plan.revision, 1)
            self.assertEqual([(item.id, item.position) for item in plan.items], before)
            self.assertTrue(all(item.closed_at is None for item in plan.items))

    def test_migration_round_trip_backfills_epoch_without_changing_targets(self):
        with Session(self.engine) as db:
            before = [(item.id, item.scheduled_date, item.target_card_count) for item in db.get(StudyPlan, self.plan_id).items]
        with patch.object(settings, "database_url", self.database_url):
            command.downgrade(Config("alembic.ini"), "c93f6d2e8b51")
            self.assertNotIn("closed_at", {column["name"] for column in inspect(self.engine).get_columns("study_plan_items")})
            command.upgrade(Config("alembic.ini"), "head")
        with Session(self.engine) as db:
            plan = db.get(StudyPlan, self.plan_id)
            self.assertEqual([(item.id, item.scheduled_date, item.target_card_count) for item in plan.items], before)
            self.assertEqual(plan.items[0].learning_epoch, self.epoch)
            self.assertTrue(all(item.closed_at is None for item in plan.items))
            self.assertEqual(db.scalar(select(func.count()).select_from(Card)), 1)

    def test_local_date_and_closed_history_survive_deletion_in_postgres(self):
        self.learn()  # 17:30 UTC on Sunday is 00:30 Monday in Jakarta.
        with Session(self.engine, autoflush=False) as db:
            plan = recalculate_plan(db, self.parent_id, self.user_id)
            db.commit()
            self.assertEqual(plan.items[0].actual_learned_count, 1)
            self.assertIsNotNone(plan.items[0].closed_at.tzinfo)
            db.delete(db.get(Card, self.card_id))
            db.commit()
            plan = recalculate_plan(db, self.parent_id, self.user_id)
            db.commit()
            self.assertEqual(plan.items[0].actual_learned_count, 1)

    def race_reset_and_recalculate(self, reset_first):
        self.learn()
        locked, release, started = threading.Event(), threading.Event(), threading.Event()
        def first():
            with Session(self.engine, autoflush=False) as db:
                if reset_first:
                    self.reset(db)
                else:
                    recalculate_plan(db, self.parent_id, self.user_id)
                locked.set()
                if not release.wait(timeout=10):
                    raise AssertionError("Timed out holding transaction")
                db.commit()
        def second():
            with Session(self.engine, autoflush=False) as db:
                started.set()
                if reset_first:
                    recalculate_plan(db, self.parent_id, self.user_id)
                else:
                    self.reset(db)
                db.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            try:
                self.assertTrue(locked.wait(timeout=10))
                second_future = pool.submit(second)
                self.assertTrue(started.wait(timeout=10))
                with self.assertRaises(FutureTimeout):
                    second_future.result(timeout=0.15)
            finally:
                release.set()
            first_future.result(timeout=15)
            second_future.result(timeout=15)
        with Session(self.engine, autoflush=False) as db:
            result = plan_response(db, recalculate_plan(db, self.parent_id, self.user_id))
            db.commit()
            self.assertEqual(result.items[0].actual_learned_count, 1)
            self.assertEqual(result.remaining_card_count, 1)
            self.assertEqual(sum(item.target_card_count or 0 for item in result.items if item.period != "historical"), 1)

    def test_reset_before_recalculate_preserves_old_cycle_and_schedules_new_cycle(self):
        self.race_reset_and_recalculate(reset_first=True)

    def test_recalculate_before_reset_preserves_snapshot(self):
        self.race_reset_and_recalculate(reset_first=False)


if __name__ == "__main__":
    unittest.main()
