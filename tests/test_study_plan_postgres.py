"""Opt-in PostgreSQL integration tests, confined to a fresh random schema.

Run with TEST_DATABASE_URL pointing to a disposable PostgreSQL database.
"""
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.database import settings
from app.models.card import Card
from app.models.deck import Deck
from app.models.study_plan import StudyPlan, StudyPlanItem
from app.models.user import User
from app.schemas.study_plan import StudyPlanCreate
from app.services.study_plan import create_plan, plan_response


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for isolated PostgreSQL checks")
class StudyPlanPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "test_study_plan_" + uuid.uuid4().hex
        self.admin = create_engine(os.environ["TEST_DATABASE_URL"])
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict({"options": f"-csearch_path={self.schema}"})
        self.engine = create_engine(url)
        self.database_url = url.render_as_string(hide_password=False)
        self.addCleanup(self.cleanup_schema)
        with patch.object(settings, "database_url", self.database_url):
            command.upgrade(Config("alembic.ini"), "head")
        with Session(self.engine) as db:
            user = User(name="Migration fixture", email="fixture@example.com", password_hash="unused")
            db.add(user)
            db.flush()
            parent = Deck(user_id=user.id, title="Parent", subject="Science", education_level="School")
            db.add(parent)
            db.flush()
            chapter = Deck(user_id=user.id, parent_deck_id=parent.id, title="Chapter", subject="Science", education_level="School")
            db.add(chapter)
            db.flush()
            db.add(Card(deck_id=chapter.id, front="Q", back="A"))
            db.commit()
            self.user_id, self.parent_id, self.chapter_id = user.id, parent.id, chapter.id
        self.preferences = StudyPlanCreate(start_date="2026-09-21", timezone="Asia/Jakarta")

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def test_upgrade_downgrade_preserves_existing_data(self):
        with Session(self.engine) as db:
            plan = create_plan(db, self.parent_id, self.user_id, self.preferences)
            response = plan_response(db, plan)
            self.assertIsNotNone(response.created_at.tzinfo)
            db.commit()
        with patch.object(settings, "database_url", self.database_url):
            command.downgrade(Config("alembic.ini"), "afc18ff984f6")
        with self.engine.connect() as connection:
            self.assertNotIn("study_plans", inspect(connection).get_table_names())
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM users")), 1)
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM decks")), 2)
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM cards")), 1)
        with patch.object(settings, "database_url", self.database_url):
            command.upgrade(Config("alembic.ini"), "head")
        with self.engine.connect() as connection:
            self.assertIn("study_plan_items", inspect(connection).get_table_names())

    def test_simultaneous_creates_have_one_winner(self):
        barrier = threading.Barrier(2)

        def attempt():
            with Session(self.engine) as db:
                barrier.wait(timeout=10)
                try:
                    create_plan(db, self.parent_id, self.user_id, self.preferences)
                    db.commit()
                    return 201
                except HTTPException as error:
                    db.rollback()
                    return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            self.assertEqual(sorted(future.result(timeout=15) for future in futures), [201, 409])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyPlan)), 1)

    def test_constraints_and_parent_cascade(self):
        with Session(self.engine) as db:
            plan = create_plan(db, self.parent_id, self.user_id, self.preferences)
            db.commit()
            for shape in (
                dict(item_type="learn", chapter_id=None, target_card_count=1),
                dict(item_type="learn", chapter_id=self.chapter_id, target_card_count=0),
                dict(item_type="learn", chapter_id=self.chapter_id, target_card_count=None),
                dict(item_type="final_exam", chapter_id=None, target_card_count=2),
                dict(item_type="review", chapter_id=None, target_card_count=None),
            ):
                with self.subTest(shape=shape):
                    with self.assertRaises(IntegrityError):
                        with db.begin_nested():
                            db.add(StudyPlanItem(study_plan_id=plan.id, scheduled_date=plan.start_date,
                                                 position=100, **shape))
                            db.flush()
            with self.assertRaises(IntegrityError):
                with db.begin_nested():
                    db.execute(text("DELETE FROM decks WHERE id = :id"), {"id": self.chapter_id})
            db.delete(db.get(Deck, self.parent_id))
            db.commit()
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyPlan)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyPlanItem)), 0)


if __name__ == "__main__":
    unittest.main()
