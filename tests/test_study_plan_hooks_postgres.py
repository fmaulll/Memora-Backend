"""Production locking and revision guarantees for automatic mutation hooks."""
import asyncio
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

import test_study_plan_postgres as fixtures
from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import ExamAttempt, ExamQuestion
from app.models.study_plan import StudyPlan
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.models.user import User
from app.routers.cards import create_card
from app.schemas.ai import DeckPlanResponse, GeneratedChapter, GeneratedCard
from app.schemas.card import CardCreate
from app.schemas.exam import ExamSubmissionRequest
from app.schemas.study_progress import StudyProgressSubmission, ProgressResetRequest
from app.services.deck_generation import DeckGenerationService
from app.services.exam import ExamService
from app.services.exam_submission import ExamSubmissionService
from app.services.study_plan import create_plan, recalculate_plan, plan_response
from app.services.study_progress import submit_progress, reset_progress, lock_progress_user


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostgreSQL hook checks")
class StudyPlanHookPostgresTests(unittest.TestCase):
    cleanup_schema = fixtures.StudyPlanPostgresTests.cleanup_schema

    def setUp(self):
        fixtures.StudyPlanPostgresTests.setUp(self)
        self.clock = patch("app.services.study_plan.local_today", return_value=date(2026, 9, 22))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with Session(self.engine, autoflush=False) as db:
            plan = create_plan(db, self.parent_id, self.user_id, self.preferences)
            db.commit()
            self.plan_id = plan.id
            self.card_id = db.scalar(select(Card.id))
            self.epoch = db.get(Deck, self.chapter_id).progress_epoch
        self.submission = StudyProgressSubmission.model_validate(dict(
            session_id=str(uuid.uuid4()), completed_at=datetime(2026, 9, 21, 10, tzinfo=timezone.utc),
            decks=[dict(deck_id=self.chapter_id, progress_epoch=self.epoch,
                        learned_cards=[dict(card_id=self.card_id, phase="review", answer="got_it")])],
        ))
        self.reset_request = ProgressResetRequest.model_validate(dict(reset_id=str(uuid.uuid4()), expected_decks=[
            dict(deck_id=self.chapter_id, progress_epoch=self.epoch),
        ]))

    def test_duplicate_submissions_adapt_once_with_atomic_receipt_and_revision(self):
        barrier = threading.Barrier(2)
        def submit():
            with Session(self.engine, autoflush=False) as db:
                barrier.wait(timeout=10)
                response = submit_progress(db, self.user_id, self.submission)
                db.commit()
                return response.model_dump(mode="json")
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(submit) for _ in range(2)]
                first, second = [future.result(timeout=15) for future in futures]
        self.assertEqual(first, second)
        self.assertEqual(recalc.call_count, 1)
        with Session(self.engine) as db:
            self.assertEqual(db.get(StudyPlan, self.plan_id).revision, 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress)), 1)

    def prepare_exam(self):
        with Session(self.engine, autoflush=False) as db:
            submit_progress(db, self.user_id, self.submission)
            statuses = ExamService().get_status(self.parent_id, db, db.get(User, self.user_id))
            self.exam_id = statuses["exams"][0]["exam_id"]
            question = ExamQuestion(exam_id=self.exam_id, position=1, question_type="true_false", question="True?",
                                    options=["True", "False"], correct_answer="True", explanation="Yes")
            db.add(question)
            db.commit()
            self.answer = ExamSubmissionRequest.model_validate(dict(answers=[dict(question_id=question.id, answer="True")]))

    def operate(self, db, kind):
        if kind == "progress":
            return submit_progress(db, self.user_id, self.submission)
        if kind == "reset":
            return reset_progress(db, self.user_id, self.parent_id, self.reset_request)
        if kind == "recalculate":
            return recalculate_plan(db, self.parent_id, self.user_id)
        if kind == "card":
            return create_card(self.chapter_id, CardCreate(front="New", back="Answer"), db, db.get(User, self.user_id))
        if kind == "exam":
            return ExamSubmissionService().submit(self.exam_id, self.answer, db, db.get(User, self.user_id))
        raise AssertionError(kind)

    def race(self, first_kind, second_kind):
        held, release, started = threading.Event(), threading.Event(), threading.Event()
        def hold():
            held.set()
            if not release.wait(timeout=10):
                raise AssertionError("Timed out holding transaction")
        def first():
            with Session(self.engine, autoflush=False) as db:
                if first_kind == "exam":
                    service = ExamSubmissionService()
                    original = service._update_progression
                    def update(*args):
                        original(*args)
                        hold()
                    with patch.object(service, "_update_progression", side_effect=update):
                        service.submit(self.exam_id, self.answer, db, db.get(User, self.user_id))
                else:
                    self.operate(db, first_kind)
                    hold()
                    db.commit()
        def second():
            with Session(self.engine, autoflush=False) as db:
                started.set()
                try:
                    self.operate(db, second_kind)
                    db.commit()
                    return 200
                except HTTPException as error:
                    db.rollback()
                    return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            try:
                self.assertTrue(held.wait(timeout=10))
                second_future = pool.submit(second)
                self.assertTrue(started.wait(timeout=10))
                with self.assertRaises(FutureTimeout):
                    second_future.result(timeout=0.15)
            finally:
                release.set()
            first_future.result(timeout=15)
            return second_future.result(timeout=15)

    def test_progress_then_reset_preserves_history_and_automatically_reschedules(self):
        self.assertEqual(self.race("progress", "reset"), 200)
        with Session(self.engine) as db:
            response = plan_response(db, db.get(StudyPlan, self.plan_id))
            self.assertEqual(response.items[0].actual_learned_count, 1)
            self.assertEqual(response.remaining_card_count, 1)
            self.assertEqual(sum(item.target_card_count or 0 for item in response.items if item.period != "historical"), 1)
            self.assertIsNone(db.get(CardProgress, (self.user_id, self.card_id)).learned_at)

    def test_reset_then_stale_progress_rejects_without_extra_adaptation(self):
        self.assertEqual(self.race("reset", "progress"), 409)
        with Session(self.engine) as db:
            response = plan_response(db, db.get(StudyPlan, self.plan_id))
            self.assertEqual(response.items[0].actual_learned_count, 0)
            self.assertEqual(response.remaining_card_count, 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)

    def test_card_creation_waits_for_recalculation_then_updates_its_schedule(self):
        self.assertEqual(self.race("recalculate", "card"), 200)
        with Session(self.engine) as db:
            response = plan_response(db, db.get(StudyPlan, self.plan_id))
            self.assertEqual(response.remaining_card_count, 2)
            self.assertEqual(response.revision, 3)
            self.assertEqual(sum(item.target_card_count or 0 for item in response.items if item.period != "historical"), 2)

    def test_reset_before_exam_blocks_unpassed_attempt_and_keeps_schedule_current(self):
        self.prepare_exam()
        self.assertEqual(self.race("reset", "exam"), 403)
        with Session(self.engine) as db:
            response = plan_response(db, db.get(StudyPlan, self.plan_id))
            self.assertEqual(response.remaining_card_count, 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(ExamAttempt)), 0)

    def test_exam_before_reset_preserves_achievement_and_new_learning_work(self):
        self.prepare_exam()
        self.assertEqual(self.race("exam", "reset"), 200)
        with Session(self.engine) as db:
            response = plan_response(db, db.get(StudyPlan, self.plan_id))
            self.assertEqual(response.remaining_card_count, 1)
            achievements = [item for item in response.items if item.item_type == "first_half_exam" and item.achieved_at]
            self.assertEqual(len(achievements), 1)
            self.assertTrue(all(item.period == "historical" for item in response.items if item.item_type == "first_half_exam"))
            self.assertEqual(db.scalar(select(func.count()).select_from(ExamAttempt)), 1)

    def test_generation_releases_user_lock_during_external_work(self):
        with Session(self.engine) as db:
            db.delete(db.get(Card, self.card_id))
            db.get(Deck, self.parent_id).generation_status = "generating"
            chapter = db.get(Deck, self.chapter_id)
            chapter.generation_status = "pending"
            chapter.card_count = 1
            db.commit()
            title = chapter.title
        async def generated(*_):
            # This separate PostgreSQL session would time out if AI awaited under a user lock.
            with Session(self.engine) as db:
                db.execute(text("SET LOCAL lock_timeout = '300ms'"))
                lock_progress_user(db, self.user_id)
                db.commit()
            return GeneratedChapter(title=title, cards=[GeneratedCard(front="Q", back="A")])
        plan = DeckPlanResponse(title="Parent", subject="Science", education_level="School", learning_language="English",
                                chapters=[dict(title=title, description="Basics", key_concepts=["Concept"], card_count=1)])
        with patch("app.services.deck_generation.SessionLocal", sessionmaker(bind=self.engine, autoflush=False)), \
             patch("app.services.deck_generation.DeepSeekService") as ai, \
             patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            ai.return_value.generate_chapter = AsyncMock(side_effect=generated)
            asyncio.run(DeckGenerationService().generate_deck(self.parent_id, plan))
        self.assertEqual(recalc.call_count, 1)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Deck, self.parent_id).generation_status, "completed")
            self.assertEqual(db.scalar(select(func.count()).select_from(Card)), 1)
            self.assertGreater(db.get(StudyPlan, self.plan_id).revision, 1)


if __name__ == "__main__":
    unittest.main()
