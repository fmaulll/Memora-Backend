"""Phase 2B canonical aggregates, exam gates, and additive API contracts."""
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.db.database import Base, get_db
from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import Exam, ExamAttempt, ExamQuestion, UserExamProgression
from app.models.study_progress import CardProgress
from app.models.user import User
from app.routers import exams, study_progress
from app.schemas.exam import ExamType
from app.services import exam, study_plan, study_timeline
from app.services.chapter_progress import get_chapter_progress
from app.services.chapters import ordered_chapters
from app.services.exam_groups import split_chapters


class ChapterProgressTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.user = User(name="Learner", email="learner@example.com", password_hash="unused")
        self.other = User(name="Other", email="other@example.com", password_hash="unused")
        self.db.add_all([self.user, self.other])
        self.db.flush()
        self.parent = self.make_deck("Parent")
        self.chapters = [self.make_deck(f"Chapter {i}", self.parent.id, i) for i in range(2)]
        self.cards = {chapter.id: self.make_cards(chapter, 3) for chapter in self.chapters}
        self.db.commit()
        app = FastAPI()
        app.include_router(study_progress.router)
        app.include_router(exams.router)
        app.include_router(exams.question_router)
        # Reuse the fixture session but emulate request rollback on exceptions.
        def get_test_db():
            try:
                yield self.db
            except Exception:
                self.db.rollback()
                raise
        app.dependency_overrides[get_db] = get_test_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def make_deck(self, title, parent_id=None, position=0, **values):
        deck = Deck(user_id=values.pop("user_id", self.user.id), title=title, parent_deck_id=parent_id,
                    position=position, subject="Science", education_level="School", **values)
        self.db.add(deck)
        self.db.flush()
        return deck

    def make_cards(self, chapter, count):
        cards = [Card(deck_id=chapter.id, front=f"Question {i}", back="Answer") for i in range(count)]
        self.db.add_all(cards)
        self.db.flush()
        return cards

    def learn(self, *chapters, count=None):
        payload = dict(session_id=str(uuid.uuid4()), completed_at=datetime.now(timezone.utc).isoformat(), decks=[
            dict(deck_id=str(chapter.id), progress_epoch=str(chapter.progress_epoch), learned_cards=[
                dict(card_id=str(card.id), phase="review", answer="got_it")
                for card in self.cards[chapter.id][:count]
            ]) for chapter in chapters
        ])
        response = self.client.post("/study/progress/submissions", json=payload)
        self.assertEqual(response.status_code, 200, response.text)

    def progress(self, deck=None):
        response = self.client.get(f"/decks/{(deck or self.parent).id}/study-progress")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def statuses(self, parent=None):
        response = self.client.get(f"/decks/{(parent or self.parent).id}/exams")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["exam_type"]: item for item in response.json()["exams"]}

    def reset(self, deck=None):
        deck = deck or self.parent
        snapshot = self.progress(deck)
        response = self.client.post(f"/decks/{deck.id}/study-progress/reset", json=dict(
            reset_id=str(uuid.uuid4()), expected_decks=[dict(deck_id=item["deck_id"], progress_epoch=item["progress_epoch"])
                                                     for item in snapshot["decks"]],
        ))
        self.assertEqual(response.status_code, 200, response.text)

    def seed_pass(self, exam_type, parent=None):
        parent = parent or self.parent
        self.statuses(parent)
        row = self.db.scalar(select(UserExamProgression).where(UserExamProgression.deck_id == parent.id))
        setattr(row, exam_type + "_passed", True)
        self.db.commit()

    def question(self, exam_id):
        question = ExamQuestion(exam_id=uuid.UUID(exam_id), position=1, question_type="true_false",
                                question="True?", options=["True", "False"], correct_answer="True", explanation="Yes")
        self.db.add(question)
        self.db.commit()
        return question

    def answer(self, exam_id, question, answer=" True "):
        return self.client.post(f"/exams/{exam_id}/submit", json={"answers": [
            {"question_id": str(question.id), "answer": answer},
        ]})

    def single_chapter(self):
        parent = self.make_deck("Single parent")
        chapter = self.make_deck("Only chapter", parent.id)
        self.cards[chapter.id] = self.make_cards(chapter, 1)
        self.db.commit()
        return parent, chapter

    def test_zero_learned(self):
        item = self.progress()["decks"][0]
        self.assertEqual((item["learned_card_count"], item["total_card_count"], item["completion_percentage"], item["completed"]),
                         (0, 3, 0, False))
        self.assertEqual(len(item["cards"]), 3)
        self.assertEqual(item["progress_epoch"], str(self.chapters[0].progress_epoch))

    def test_partial_learned(self):
        self.learn(self.chapters[0], count=1)
        item = self.progress()["decks"][0]
        self.assertEqual((item["learned_card_count"], item["total_card_count"], item["completion_percentage"], item["completed"]),
                         (1, 3, 33.33, False))

    def test_fully_learned(self):
        self.learn(self.chapters[0])
        item = self.progress()["decks"][0]
        self.assertEqual((item["learned_card_count"], item["total_card_count"], item["completion_percentage"], item["completed"]),
                         (3, 3, 100, True))

    def test_empty_chapter_is_not_complete(self):
        for card in self.cards[self.chapters[0].id]:
            self.db.delete(card)
        self.db.commit()
        item = self.progress()["decks"][0]
        self.assertEqual(item["total_card_count"], 0)
        self.assertEqual(item["completion_percentage"], 0)
        self.assertFalse(item["completed"])
        self.assertFalse(self.statuses()["first_half"]["available"])

    def test_unfinished_generation_is_not_complete_even_if_all_current_cards_learned(self):
        self.learn(self.chapters[0])
        for state in ["pending", "generating", "failed", "unknown"]:
            with self.subTest(state=state):
                self.chapters[0].generation_status = state
                self.db.commit()
                item = self.progress()["decks"][0]
                self.assertEqual(item["completion_percentage"], 100)
                self.assertFalse(item["completed"])
                self.assertFalse(self.statuses()["first_half"]["available"])
        self.chapters[0].generation_status = "completed"
        self.db.commit()
        self.assertTrue(self.progress()["decks"][0]["completed"])

    def test_added_card_relocks_unpassed_exam(self):
        self.learn(self.chapters[0])
        self.assertTrue(self.statuses()["first_half"]["available"])
        self.make_cards(self.chapters[0], 1)
        self.db.commit()
        item = self.progress()["decks"][0]
        self.assertEqual((item["learned_card_count"], item["total_card_count"], item["completed"]), (3, 4, False))
        self.assertFalse(self.statuses()["first_half"]["available"])

    def test_deleted_unlearned_card_updates_denominator(self):
        self.learn(self.chapters[0], count=2)
        self.db.delete(self.cards[self.chapters[0].id][2])
        self.db.commit()
        item = self.progress()["decks"][0]
        self.assertEqual((item["learned_card_count"], item["total_card_count"], item["completed"]), (2, 2, True))
        self.assertTrue(self.statuses()["first_half"]["available"])

    def test_reset_clears_derived_completion_and_relocks_unpassed_exam(self):
        self.learn(*self.chapters)
        self.reset(self.chapters[0])
        self.assertEqual([item["completed"] for item in self.progress()["decks"]], [False, True])
        statuses = self.statuses()
        self.assertFalse(statuses["first_half"]["available"])
        self.assertTrue(statuses["second_half"]["available"])

    def test_study_all_exact_cards_aggregate_across_chapters(self):
        self.learn(*self.chapters)
        self.assertEqual(self.progress()["summary"], dict(total_deck_count=2, completed_deck_count=2,
                                                         learned_card_count=6, total_card_count=6))
        self.assertTrue(all(item["completed"] for item in self.progress()["decks"]))

    def test_parent_summary_excludes_direct_parent_cards_and_standalone_includes_own(self):
        self.make_cards(self.parent, 4)
        standalone = self.make_deck("Standalone")
        self.make_cards(standalone, 2)
        self.db.commit()
        self.assertEqual(self.progress()["summary"]["total_card_count"], 6)
        self.assertEqual(self.progress(standalone)["summary"], dict(total_deck_count=1, completed_deck_count=0,
                                                                 learned_card_count=0, total_card_count=2))

    def test_even_and_odd_split(self):
        for count, first_size in [(12, 6), (5, 3), (1, 1), (0, 0)]:
            with self.subTest(count=count):
                source = list(range(count))
                first, second = split_chapters(source)
                self.assertEqual(first, source[:first_size])
                self.assertEqual(second, source[first_size:])

    def test_shared_order_with_id_fallback_and_odd_groups(self):
        for number in [9, 3, 7]:
            chapter = self.make_deck(f"Title {10-number}", self.parent.id, 0,
                                     id=uuid.UUID(f"00000000-0000-0000-a000-{number:012x}"))
            self.cards[chapter.id] = self.make_cards(chapter, 1)
        for chapter in self.chapters:
            chapter.position = 0
        self.db.commit()
        ordered = ordered_chapters(self.db, self.parent)
        expected = sorted(chapter.id for chapter in ordered)
        self.assertEqual([chapter.id for chapter in ordered], expected)
        self.assertIs(study_plan.ordered_chapters, ordered_chapters)
        self.assertIs(exam.ordered_chapters, ordered_chapters)
        self.assertIs(study_timeline.split_chapters, split_chapters)
        self.assertIs(exam.split_chapters, split_chapters)
        self.assertEqual([item["deck_id"] for item in self.progress()["decks"]], list(map(str, expected)))
        statuses = self.statuses()
        self.assertEqual(statuses["first_half"]["chapter_ids"], list(map(str, expected[:3])))
        self.assertEqual(statuses["second_half"]["chapter_ids"], list(map(str, expected[3:])))
        self.learn(*ordered)
        response = self.client.get(f"/decks/{self.parent.id}/exams/first_half")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sub_deck_ids"], statuses["first_half"]["chapter_ids"])

    def test_first_half_requires_all_chapters_in_group(self):
        extra = self.make_deck("Third", self.parent.id, 2)
        self.cards[extra.id] = self.make_cards(extra, 1)
        self.db.commit()
        self.learn(self.chapters[0])
        self.assertFalse(self.statuses()["first_half"]["available"])
        self.learn(self.chapters[1])
        self.assertTrue(self.statuses()["first_half"]["available"])

    def test_second_half_unlocks_independently_of_first(self):
        before = self.statuses()
        self.assertEqual([item["status"] for item in before.values()], ["locked"] * 3)
        self.learn(self.chapters[1])
        after = self.statuses()
        self.assertFalse(after["first_half"]["available"])
        self.assertFalse(after["first_half"]["passed"])
        self.assertTrue(after["second_half"]["available"])
        self.assertFalse(after["final"]["available"])

    def test_first_pass_does_not_replace_second_half_learning_or_unlock_final(self):
        self.seed_pass("first_half")
        statuses = self.statuses()
        self.assertFalse(statuses["second_half"]["available"])
        self.assertFalse(statuses["final"]["available"])

    def test_final_requires_second_pass_not_first_pass_or_chapter_counts(self):
        self.learn(*self.chapters)
        self.assertFalse(self.statuses()["final"]["available"])
        self.seed_pass("second_half")
        statuses = self.statuses()
        self.assertFalse(statuses["first_half"]["passed"])
        self.assertTrue(statuses["final"]["available"])

    def test_single_chapter_skips_second_definition_and_final_waits_for_first_pass(self):
        parent, chapter = self.single_chapter()
        self.learn(chapter)
        statuses = self.statuses(parent)
        second = statuses["second_half"]
        self.assertEqual((second["applicable"], second["available"], second["completed"], second["exam_id"], second["status"]),
                         (False, False, False, None, "not_applicable"))
        self.assertFalse(statuses["final"]["available"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Exam).where(Exam.deck_id == parent.id)), 2)
        question = self.question(statuses["first_half"]["exam_id"])
        response = self.answer(statuses["first_half"]["exam_id"], question)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["next_exam_type"], "final")
        self.assertTrue(response.json()["next_exam_unlocked"])
        self.assertTrue(self.statuses(parent)["final"]["available"])

    def test_legacy_nonapplicable_second_half_is_retained_but_not_available(self):
        parent, _ = self.single_chapter()
        legacy = Exam(deck_id=parent.id, exam_type="second_half", passing_score=70)
        self.db.add(legacy)
        self.db.commit()
        self.seed_pass("second_half", parent)
        second = self.statuses(parent)["second_half"]
        self.assertEqual(second["exam_id"], str(legacy.id))
        self.assertEqual(second["status"], "completed")
        self.assertTrue(second["passed"])
        self.assertFalse(second["applicable"])
        self.assertFalse(second["available"])
        self.assertFalse(self.statuses(parent)["final"]["available"])
        response = self.client.get(f"/exams/{legacy.id}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["code"], "exam_not_applicable")

    def test_no_chapters_does_not_create_empty_definitions(self):
        parent = self.make_deck("Empty root")
        self.db.commit()
        self.assertTrue(all(item["status"] == "not_applicable" for item in self.statuses(parent).values()))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Exam).where(Exam.deck_id == parent.id)), 0)

    def test_passed_exam_survives_added_content_and_allows_retake(self):
        self.learn(self.chapters[0])
        exam_id = self.statuses()["first_half"]["exam_id"]
        question = self.question(exam_id)
        first_attempt = self.answer(exam_id, question)
        self.assertEqual(first_attempt.status_code, 200, first_attempt.text)
        self.assertEqual(first_attempt.json()["score"], 100)
        self.assertFalse(first_attempt.json()["next_exam_unlocked"])
        before = self.statuses()["first_half"]
        self.make_cards(self.chapters[0], 1)
        self.db.commit()
        self.assertFalse(self.progress()["decks"][0]["completed"])
        self.assertEqual(self.statuses()["first_half"], before)
        retake = self.answer(exam_id, question, "False")
        self.assertEqual(retake.status_code, 200, retake.text)
        self.assertFalse(retake.json()["passed"])
        self.assertEqual(retake.json()["attempt_number"], 2)
        after = self.statuses()["first_half"]
        self.assertTrue(after["passed"] and after["completed"] and after["available"])
        self.assertEqual(after["best_score"], 100)
        self.assertEqual(after["completed_at"], before["completed_at"])

    def test_reset_preserves_passed_history_and_final_gate(self):
        self.learn(*self.chapters)
        second_id = self.statuses()["second_half"]["exam_id"]
        question = self.question(second_id)
        response = self.answer(second_id, question)
        self.assertEqual(response.status_code, 200, response.text)
        before = self.statuses()["second_half"]
        self.reset()
        after = self.statuses()
        self.assertEqual(after["second_half"], before)
        self.assertEqual(after["first_half"]["status"], "locked")
        self.assertTrue(after["final"]["available"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ExamAttempt)), 1)

    def test_failed_attempt_does_not_complete_exam_or_unlock_final(self):
        self.learn(self.chapters[1])
        exam_id = self.statuses()["second_half"]["exam_id"]
        response = self.answer(exam_id, self.question(exam_id), "False")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["score"], 0)
        self.assertFalse(response.json()["next_exam_unlocked"])
        self.assertFalse(self.statuses()["second_half"]["completed"])
        self.assertEqual(self.statuses()["second_half"]["attempt_count"], 1)

    def test_final_submission_completed_retains_existing_meaning(self):
        self.seed_pass("second_half")
        exam_id = self.statuses()["final"]["exam_id"]
        response = self.answer(exam_id, self.question(exam_id))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["completed"])
        self.assertIsNone(response.json()["next_exam_type"])
        self.assertFalse(response.json()["next_exam_unlocked"])
        self.reset()
        self.assertTrue(self.statuses()["final"]["available"])

    def test_locked_exam_gates_all_routes_before_ai_or_attempt(self):
        exam_id = self.statuses()["first_half"]["exam_id"]
        question = self.question(exam_id)
        with patch("app.routers.exams.ExamGenerationService") as ai:
            response = self.client.post(f"/decks/{self.parent.id}/exams/first_half/generate")
            self.assertEqual(response.status_code, 403)
            ai.assert_not_called()
        responses = [self.client.get(f"/decks/{self.parent.id}/exams/first_half"),
                     self.client.get(f"/exams/{exam_id}"), self.answer(exam_id, question)]
        for response in responses:
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["detail"]["code"], "exam_locked")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ExamAttempt)), 0)

    def test_unlocked_question_read_and_generation_reuse_existing_questions(self):
        self.learn(self.chapters[0])
        exam_id = self.statuses()["first_half"]["exam_id"]
        self.question(exam_id)
        responses = [self.client.get(f"/exams/{exam_id}"),
                     self.client.post(f"/decks/{self.parent.id}/exams/first_half/generate")]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["question_count"], 1)
            self.assertNotIn("correct_answer", response.json()["questions"][0])

    def test_reset_during_ai_generation_is_rechecked_before_persisting(self):
        self.learn(self.chapters[0])
        exam_id = self.statuses()["first_half"]["exam_id"]
        async def generated(*_):
            self.reset()
            return SimpleNamespace(questions=[SimpleNamespace(
                question_type="true_false", question="True?", options=["True", "False"], correct_answer="True",
                explanation="Yes", source_card_id=None,
            )])
        with patch("app.ai.deepseek.DeepSeekService.generate_exam_questions", new=AsyncMock(side_effect=generated)):
            response = self.client.post(f"/decks/{self.parent.id}/exams/first_half/generate")
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ExamQuestion).where(ExamQuestion.exam_id == uuid.UUID(exam_id))), 0)

    def test_foreign_deck_and_exam_are_not_exposed(self):
        foreign = self.make_deck("Foreign", user_id=self.other.id)
        foreign_exam = Exam(deck_id=foreign.id, exam_type="first_half")
        self.db.add(foreign_exam)
        self.db.commit()
        for path in [f"/decks/{foreign.id}/study-progress", f"/decks/{foreign.id}/exams",
                     f"/decks/{foreign.id}/exams/first_half", f"/exams/{foreign_exam.id}"]:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_foreign_learned_facts_and_passes_cannot_influence_eligibility(self):
        self.db.add_all([CardProgress(user_id=self.other.id, card_id=card.id, learned_at=datetime.now(timezone.utc))
                         for card in self.cards[self.chapters[0].id]])
        self.db.add(UserExamProgression(user_id=self.other.id, deck_id=self.parent.id,
                                       first_half_passed=True, second_half_passed=True))
        self.db.commit()
        self.assertEqual(self.progress()["decks"][0]["learned_card_count"], 0)
        self.assertTrue(all(not item["available"] for item in self.statuses().values()))

    def test_aggregate_query_count_does_not_grow_with_chapters(self):
        for i in range(30):
            self.make_deck(f"Empty {i}", self.parent.id, i + 2)
        self.db.commit()
        chapters = ordered_chapters(self.db, self.parent)
        user_id = self.user.id
        queries = []
        def record(*args):
            queries.append(args[2])
        event.listen(self.engine, "before_cursor_execute", record)
        try:
            progress = get_chapter_progress(self.db, user_id, chapters)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        self.assertEqual(len(progress), 32)
        self.assertEqual(len(queries), 1)


if __name__ == "__main__":
    unittest.main()
