"""Real transaction/lock guarantees in disposable, randomly named PostgreSQL schemas."""
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db.database import settings
from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import Exam, ExamAttempt, ExamQuestion, UserExamProgression
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.models.user import User
from app.schemas.study_progress import ProgressResetRequest, StudyProgressSubmission
from app.schemas.exam import ExamSubmissionRequest
from app.services.exam import ExamService
from app.services.exam_submission import ExamSubmissionService
from app.services.study_progress import read_progress, reset_progress, submit_progress


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL for PostgreSQL progress checks")
class StudyProgressPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = "test_study_progress_" + uuid.uuid4().hex
        self.admin = create_engine(os.environ["TEST_DATABASE_URL"])
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        url = make_url(os.environ["TEST_DATABASE_URL"]).update_query_dict({"options": f"-csearch_path={self.schema}"})
        self.engine = create_engine(url)
        self.database_url = url.render_as_string(hide_password=False)
        self.addCleanup(self.cleanup_schema)
        self.migrate("upgrade", "head")
        with Session(self.engine) as db:
            user = User(name="Learner", email="test@example.com", password_hash="unused")
            db.add(user)
            db.flush()
            parent = Deck(user_id=user.id, title="Parent", subject="Science", education_level="School")
            db.add(parent)
            db.flush()
            deck = Deck(user_id=user.id, parent_deck_id=parent.id, title="Chapter", subject="Science", education_level="School")
            db.add(deck)
            db.flush()
            card = Card(deck_id=deck.id, front="Question", back="Answer")
            db.add(card)
            db.commit()
            self.user_id, self.parent_id, self.deck_id, self.card_id = user.id, parent.id, deck.id, card.id
            self.epoch = deck.progress_epoch
        self.submission = StudyProgressSubmission.model_validate(dict(
            session_id=str(uuid.uuid4()), completed_at=datetime.now(timezone.utc).isoformat(),
            decks=[dict(deck_id=str(self.deck_id), progress_epoch=str(self.epoch),
                        learned_cards=[dict(card_id=str(self.card_id), phase="review", answer="got_it")])],
        ))
        self.reset = ProgressResetRequest.model_validate(dict(
            reset_id=str(uuid.uuid4()), expected_decks=[dict(deck_id=str(self.deck_id), progress_epoch=str(self.epoch))],
        ))

    def migrate(self, direction, revision):
        with patch.object(settings, "database_url", self.database_url):
            getattr(command, direction)(Config("alembic.ini"), revision)

    def cleanup_schema(self):
        self.engine.dispose()
        with self.admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()

    def apply(self, db, operation):
        if operation == "reset":
            return reset_progress(db, self.user_id, self.parent_id, self.reset)
        return submit_progress(db, self.user_id, self.submission)

    def parallel(self, operations):
        barrier = threading.Barrier(len(operations))

        def attempt(operation):
            with Session(self.engine, autoflush=False) as db:
                barrier.wait(timeout=10)
                try:
                    response = self.apply(db, operation)
                    db.commit()
                    return 200, response.model_dump(mode="json")
                except HTTPException as error:
                    db.rollback()
                    return error.status_code, error.detail

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(attempt, operation) for operation in operations]
            return [future.result(timeout=15) for future in futures]

    def test_simultaneous_duplicate_sessions_apply_once(self):
        first, second = self.parallel(["submission", "submission"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], 200)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)
            progress = db.get(CardProgress, (self.user_id, self.card_id))
            self.assertIsNotNone(progress.learned_at.tzinfo)

    def test_simultaneous_duplicate_resets_rotate_once(self):
        first, second = self.parallel(["reset", "reset"])
        self.assertEqual(first, second)
        self.assertEqual(first[0], 200)
        with Session(self.engine) as db:
            self.assertEqual(str(db.get(Deck, self.deck_id).progress_epoch), first[1]["decks"][0]["progress_epoch"])
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)

    def race_with_first_transaction_held(self, first_operation):
        first_has_lock = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        second_operation = "submission" if first_operation == "reset" else "reset"

        def first():
            with Session(self.engine, autoflush=False) as db:
                result = self.apply(db, first_operation)
                first_has_lock.set()
                if not release_first.wait(timeout=10):
                    raise AssertionError("Timed out waiting to release first transaction")
                db.commit()
                return result

        def second():
            with Session(self.engine, autoflush=False) as db:
                second_started.set()
                try:
                    result = self.apply(db, second_operation)
                    db.commit()
                    return 200, result
                except HTTPException as error:
                    db.rollback()
                    return error.status_code, error.detail

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            try:
                self.assertTrue(first_has_lock.wait(timeout=10))
                second_future = pool.submit(second)
                self.assertTrue(second_started.wait(timeout=10))
            finally:
                release_first.set()
            first_future.result(timeout=15)
            status, detail = second_future.result(timeout=15)
        self.assertEqual(status, 409 if first_operation == "reset" else 200)
        if status == 409:
            self.assertEqual(detail["code"], "stale_progress_epoch")
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress).where(CardProgress.learned_at.is_not(None))), 0)
            self.assertNotEqual(db.get(Deck, self.deck_id).progress_epoch, self.epoch)

    def test_reset_wins_race_old_offline_result_is_rejected(self):
        self.race_with_first_transaction_held("reset")

    def test_submission_wins_race_reset_clears_it(self):
        self.race_with_first_transaction_held("submission")

    def test_transaction_rollback_preserves_all_or_nothing(self):
        with Session(self.engine, autoflush=False) as db:
            submit_progress(db, self.user_id, self.submission)
            db.rollback()
        with Session(self.engine, autoflush=False) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 0)
            submit_progress(db, self.user_id, self.submission)
            db.commit()
            reset_progress(db, self.user_id, self.parent_id, self.reset)
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(db.get(Deck, self.deck_id).progress_epoch, self.epoch)
            self.assertIsNotNone(db.get(CardProgress, (self.user_id, self.card_id)).learned_at)
            self.assertEqual(db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 1)

    def test_migration_reversible_and_existing_decks_get_distinct_epochs(self):
        self.migrate("downgrade", "b82e5c9d1a40")
        with self.engine.connect() as connection:
            self.assertNotIn("card_progress", inspect(connection).get_table_names())
            self.assertNotIn("progress_epoch", {col["name"] for col in inspect(connection).get_columns("decks")})
            self.assertEqual(connection.scalar(text("SELECT count(*) FROM cards")), 1)
        self.migrate("upgrade", "head")
        with Session(self.engine) as db:
            epochs = db.scalars(select(Deck.progress_epoch)).all()
            self.assertEqual(len(epochs), 2)
            self.assertEqual(len(set(epochs)), 2)
            self.assertNotIn(None, epochs)
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(User)), 1)

    def test_delete_cascades_current_fact_but_keeps_receipt(self):
        with Session(self.engine, autoflush=False) as db:
            acknowledgement = submit_progress(db, self.user_id, self.submission)
            db.commit()
            db.delete(db.get(Card, self.card_id))
            db.commit()
            self.assertEqual(db.scalar(select(func.count()).select_from(CardProgress)), 0)
            self.assertEqual(submit_progress(db, self.user_id, self.submission), acknowledgement)
            self.assertEqual(read_progress(db, self.user_id, self.deck_id).decks[0].cards, [])

    def prepare_exam(self):
        with Session(self.engine, autoflush=False) as db:
            submit_progress(db, self.user_id, self.submission)
            status = ExamService().get_status(self.parent_id, db, db.get(User, self.user_id))
            self.exam_id = status["exams"][0]["exam_id"]
            question = ExamQuestion(exam_id=self.exam_id, question_type="true_false", question="True?",
                                    options=["True", "False"], correct_answer="True", explanation="Yes", position=1)
            db.add(question)
            db.commit()
            self.answer = ExamSubmissionRequest.model_validate(dict(answers=[dict(question_id=question.id, answer="True")]))

    def test_simultaneous_exam_status_initializes_only_applicable_definitions_once(self):
        barrier = threading.Barrier(2)
        def read():
            with Session(self.engine, autoflush=False) as db:
                user = db.get(User, self.user_id)
                barrier.wait(timeout=10)
                result = ExamService().get_status(self.parent_id, db, user)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(read) for _ in range(2)]
            first, second = [future.result(timeout=15) for future in futures]
        self.assertEqual(first, second)
        self.assertIsNone(first["exams"][1]["exam_id"])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Exam)), 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(UserExamProgression)), 1)

    def test_concurrent_exam_submissions_preserve_distinct_attempt_numbers(self):
        self.prepare_exam()
        barrier = threading.Barrier(2)
        def submit():
            with Session(self.engine, autoflush=False) as db:
                user = db.get(User, self.user_id)
                barrier.wait(timeout=10)
                return ExamSubmissionService().submit(self.exam_id, self.answer, db, user)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(result.attempt_number for result in results), [1, 2])
        self.assertTrue(all(result.passed and result.next_exam_unlocked for result in results))
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ExamAttempt)), 2)
            self.assertEqual(db.scalar(select(UserExamProgression)).first_half_attempt_count, 2)

    def race_exam_and_reset(self, reset_first):
        self.prepare_exam()
        first_holds_lock = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def hold():
            first_holds_lock.set()
            if not release_first.wait(timeout=10):
                raise AssertionError("Timed out holding first transaction")

        def first():
            with Session(self.engine, autoflush=False) as db:
                if reset_first:
                    reset_progress(db, self.user_id, self.parent_id, self.reset)
                    hold()
                    db.commit()
                else:
                    service = ExamSubmissionService()
                    original = service._update_progression
                    def update(*args):
                        original(*args)
                        hold()
                    with patch.object(service, "_update_progression", side_effect=update):
                        service.submit(self.exam_id, self.answer, db, db.get(User, self.user_id))

        def second():
            with Session(self.engine, autoflush=False) as db:
                user = db.get(User, self.user_id)
                second_started.set()
                try:
                    if reset_first:
                        ExamSubmissionService().submit(self.exam_id, self.answer, db, user)
                    else:
                        reset_progress(db, self.user_id, self.parent_id, self.reset)
                        db.commit()
                    return 200
                except HTTPException as error:
                    db.rollback()
                    return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first)
            try:
                self.assertTrue(first_holds_lock.wait(timeout=10))
                second_future = pool.submit(second)
                self.assertTrue(second_started.wait(timeout=10))
                # A separate PostgreSQL session must wait for the user lock.
                with self.assertRaises(FutureTimeout):
                    second_future.result(timeout=0.15)
            finally:
                release_first.set()
            first_future.result(timeout=15)
            self.assertEqual(second_future.result(timeout=15), 403 if reset_first else 200)
        with Session(self.engine) as db:
            self.assertFalse(read_progress(db, self.user_id, self.parent_id).decks[0].completed)
            statuses = ExamService().get_status(self.parent_id, db, db.get(User, self.user_id))["exams"]
            self.assertEqual(statuses[0]["passed"], not reset_first)
            self.assertEqual(statuses[0]["available"], not reset_first)
            self.assertEqual(db.scalar(select(func.count()).select_from(ExamAttempt)), 0 if reset_first else 1)

    def test_reset_before_exam_submission_locks_unpassed_exam(self):
        self.race_exam_and_reset(reset_first=True)

    def test_exam_submission_before_reset_preserves_passed_attempt(self):
        self.race_exam_and_reset(reset_first=False)


if __name__ == "__main__":
    unittest.main()
