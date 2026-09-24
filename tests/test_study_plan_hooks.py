"""Mutation integration uses the canonical adaptive service, once per affected plan."""
import asyncio
import unittest
import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

import test_study_plan_adaptation as fixtures
from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import ExamAttempt, ExamQuestion, UserExamProgression
from app.models.generation_job import GenerationJob
from app.models.study_plan import StudyPlan
from app.models.study_progress import CardProgress, StudyProgressReceipt
from app.models.user import User
from app.routers import cards, exams, study_progress
from app.schemas.ai import DeckPlanResponse, GeneratedCard, GeneratedChapter
from app.schemas.study_plan import StudyPlanCreate
from app.services.study_plan import create_plan, recalculate_plan
from app.services.study_plan_hooks import recalculate_affected_plans
from app.worker import GenerationWorker


class StudyPlanHookTests(unittest.TestCase):
    deck = fixtures.AdaptivePlanAPITests.deck
    add_cards = fixtures.AdaptivePlanAPITests.add_cards
    create = fixtures.AdaptivePlanAPITests.create
    tearDown = fixtures.AdaptivePlanAPITests.tearDown
    plan = fixtures.AdaptivePlanAPITests.plan
    history = fixtures.AdaptivePlanAPITests.history

    def setUp(self):
        fixtures.AdaptivePlanAPITests.setUp(self)
        # Alembic integration fixtures reconfigure logging with existing loggers
        # disabled; restore this logger only for these observability assertions.
        patch("app.services.study_plan_hooks.logger.disabled", False).start()
        self.app.include_router(cards.router)
        self.app.include_router(exams.router)
        self.app.include_router(exams.question_router)
        self.app.include_router(study_progress.router)
        self.original = self.plan()
        self.clock.return_value = date(2026, 9, 22)

    def payload(self, *chapters, count=4):
        return dict(session_id=str(uuid.uuid4()), completed_at="2026-09-21T18:00:00+07:00", decks=[dict(
            deck_id=str(chapter.id), progress_epoch=str(chapter.progress_epoch), learned_cards=[
                dict(card_id=str(card.id), phase="review", answer="got_it") for card in self.cards[chapter.id][:count]
            ],
        ) for chapter in (chapters or (self.chapters[0],))])

    def submit(self, payload=None):
        return self.client.post("/study/progress/submissions", json=payload or self.payload())

    def snapshot(self, parent=None):
        response = self.client.get(f"/decks/{(parent or self.parent).id}/study-plan")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def reset_payload(self, chapter=None):
        return dict(reset_id=str(uuid.uuid4()), expected_decks=[dict(deck_id=str(deck.id), progress_epoch=str(deck.progress_epoch))
                                                              for deck in ([chapter] if chapter else self.chapters)])

    def reset(self, chapter=None, payload=None):
        return self.client.post(f"/decks/{(chapter or self.parent).id}/study-progress/reset",
                                json=payload or self.reset_payload(chapter))

    def second_plan(self):
        root = self.deck("Another parent")
        chapter = self.deck("Another chapter", root.id)
        self.add_cards(chapter, 10)
        self.cards[chapter.id] = list(self.db.scalars(select(Card).where(Card.deck_id == chapter.id)).all())
        create_plan(self.db, root.id, self.user.id, StudyPlanCreate(**self.request))
        self.db.commit()
        return root, chapter

    def test_new_submission_automatically_recalculates_and_keeps_response_contract(self):
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()), {"session_id", "completed_at", "submitted_at", "decks"})
        self.assertEqual(recalc.call_count, 1)
        saved = self.snapshot()
        self.assertEqual(saved["items"][0]["actual_learned_count"], 4)
        self.assertEqual(saved["remaining_card_count"], 16)
        self.assertGreater(saved["revision"], self.original["revision"])

    def test_replay_never_recalculates_even_after_day_rollover(self):
        payload = self.payload()
        original = self.submit(payload)
        saved = self.snapshot()
        self.clock.return_value = date(2026, 9, 23)
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            replay = self.submit(payload)
        self.assertEqual(replay.json(), original.json())
        recalc.assert_not_called()
        self.assertEqual(self.snapshot()["revision"], saved["revision"])

    def test_new_receipt_with_only_already_learned_cards_does_not_recalculate(self):
        self.submit()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.submit()
        self.assertEqual(response.json()["decks"][0]["accepted_card_ids"], [])
        recalc.assert_not_called()

    def test_study_all_same_parent_recalculates_once(self):
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.submit(self.payload(*self.chapters))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(recalc.call_count, 1)
        self.assertEqual(self.snapshot()["remaining_card_count"], 12)

    def test_study_all_distinct_parents_each_recalculate_once_in_sorted_order(self):
        other, chapter = self.second_plan()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.submit(self.payload(*self.chapters, chapter))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([call.args[1] for call in recalc.call_args_list], sorted([self.parent.id, other.id]))

    def test_failure_on_second_plan_rolls_back_facts_receipt_and_first_plan(self):
        other, chapter = self.second_plan()
        before = [self.snapshot(), self.snapshot(other)]
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Injected adaptation failure")
            return recalculate_plan(*args)
        with patch("app.services.study_plan.recalculate_plan", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.submit(self.payload(self.chapters[0], chapter))
        self.assertEqual([self.snapshot(), self.snapshot(other)], before)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(CardProgress)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyProgressReceipt)), 0)

    def test_chapter_reset_recalculates_parent_once_and_preserves_history(self):
        self.submit(self.payload(*self.chapters, count=10))
        before = self.snapshot()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.reset(self.chapters[0])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(recalc.call_count, 1)
        after = self.snapshot()
        self.assertEqual(self.history(after), self.history(before))
        self.assertEqual(after["remaining_card_count"], 10)

    def test_parent_reset_once_and_replay_does_not_recalculate(self):
        self.submit(self.payload(*self.chapters))
        payload = self.reset_payload()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            original = self.reset(payload=payload)
            replay = self.reset(payload=payload)
        self.assertEqual(original.status_code, 200, original.text)
        self.assertEqual(original.json(), replay.json())
        self.assertEqual(recalc.call_count, 1)

    def test_stale_reset_leaves_timeline_unchanged(self):
        payload = self.reset_payload()
        self.reset(payload=payload)
        payload["reset_id"] = str(uuid.uuid4())
        before = self.snapshot()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.reset(payload=payload)
        self.assertEqual(response.status_code, 409)
        recalc.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_failed_reset_restores_epoch_learning_and_plan(self):
        self.submit()
        epoch = self.chapters[0].progress_epoch
        before = self.snapshot()
        with patch("app.services.study_plan.recalculate_plan", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.reset(self.chapters[0])
        self.assertEqual(self.chapters[0].progress_epoch, epoch)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(CardProgress).where(CardProgress.learned_at.is_not(None))), 4)

    def test_manual_card_add_increases_scheduled_work(self):
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.client.post(f"/decks/{self.chapters[0].id}/cards", json={"front": "New", "back": "Answer"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(recalc.call_count, 1)
        self.assertEqual(self.snapshot()["remaining_card_count"], 21)
        self.assertIsNone(self.db.get(CardProgress, (self.user.id, uuid.UUID(response.json()["id"]))))

    def test_card_delete_reduces_work_and_keeps_closed_history(self):
        self.submit()
        before = self.snapshot()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.client.delete(f"/cards/{self.cards[self.chapters[0].id][-1].id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(recalc.call_count, 1)
        after = self.snapshot()
        self.assertEqual(after["remaining_card_count"], 15)
        self.assertEqual(self.history(after), self.history(before))

    def test_bulk_replacement_deletes_many_and_recalculates_once(self):
        chapter = self.chapters[0]
        keep = self.cards[chapter.id][:2]
        payload = {"cards": [{"id": str(card.id), "front": card.front, "back": card.back} for card in keep] +
                            [{"id": str(uuid.uuid4()), "front": "New", "back": "New"} for _ in range(5)]}
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            response = self.client.post(f"/decks/{chapter.id}/cards/bulk", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(recalc.call_count, 1)
        self.assertEqual(self.snapshot()["remaining_card_count"], 17)

    def test_bulk_text_only_update_does_not_recalculate(self):
        chapter = self.chapters[0]
        payload = {"cards": [{"id": str(card.id), "front": "Reworded", "back": card.back} for card in self.cards[chapter.id]]}
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            self.assertEqual(self.client.post(f"/decks/{chapter.id}/cards/bulk", json=payload).status_code, 200)
        recalc.assert_not_called()

    def test_failed_card_add_and_delete_roll_back_both_mutations(self):
        before = self.snapshot()
        card_id = self.cards[self.chapters[0].id][-1].id
        with patch("app.services.study_plan.recalculate_plan", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/decks/{self.chapters[0].id}/cards", json={"front": "New", "back": "Answer"})
            with self.assertRaises(RuntimeError):
                self.client.delete(f"/cards/{card_id}")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Card)), 20)
        self.assertIsNotNone(self.db.get(Card, card_id))
        self.assertEqual(self.snapshot(), before)

    def test_pending_generation_defers_adaptation_but_keeps_valid_learning_and_card(self):
        self.chapters[1].generation_status = "pending"
        self.db.commit()
        with self.assertLogs("app.services.study_plan_hooks", level="INFO") as log:
            response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any("content_not_ready" in line for line in log.output))
        self.assertEqual(self.snapshot()["revision"], self.original["revision"])
        response = self.client.post(f"/decks/{self.chapters[0].id}/cards", json={"front": "New", "back": "Answer"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.snapshot()["revision"], self.original["revision"])

    def test_no_plan_is_created_and_direct_root_cards_do_not_affect_child_plan(self):
        standalone = self.deck("Standalone")
        self.db.commit()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            for deck in (standalone, self.parent):
                self.assertEqual(self.client.post(f"/decks/{deck.id}/cards", json={"front": "Q", "back": "A"}).status_code, 201)
        recalc.assert_not_called()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 1)

    def test_helper_deduplicates_filters_missing_roots_and_reports_revision_changes(self):
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            results = recalculate_affected_plans(self.db, self.user.id, [self.parent.id, self.parent.id, uuid.uuid4()], reason="test")
        self.db.commit()
        self.assertEqual(recalc.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].changed)
        self.assertFalse(recalculate_affected_plans(self.db, self.user.id, {self.parent.id}, reason="test")[0].changed)

    def test_helper_does_not_recalculate_another_users_existing_plan(self):
        other = User(name="Other", email="other@example.com", password_hash="unused")
        self.db.add(other)
        self.db.flush()
        root = Deck(user_id=other.id, title="Private", subject="Science", education_level="School")
        self.db.add(root)
        self.db.flush()
        chapter = Deck(user_id=other.id, parent_deck_id=root.id, title="Private chapter", subject="Science", education_level="School")
        self.db.add(chapter)
        self.db.flush()
        self.add_cards(chapter, 1)
        private_plan = create_plan(self.db, root.id, other.id, StudyPlanCreate(**self.request))
        self.db.commit()
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            results = recalculate_affected_plans(self.db, self.user.id, {root.id}, reason="test")
        self.assertEqual(results, [])
        recalc.assert_not_called()
        self.assertEqual(private_plan.revision, 1)

    def exam_question(self, kind):
        statuses = self.client.get(f"/decks/{self.parent.id}/exams").json()["exams"]
        entry = next(item for item in statuses if item["exam_type"] == kind)
        question = ExamQuestion(exam_id=uuid.UUID(entry["exam_id"]), position=1, question_type="true_false",
                                question="True?", options=["True", "False"], correct_answer="True", explanation="Yes")
        self.db.add(question)
        self.db.commit()
        return entry["exam_id"], question.id

    def answer(self, exam_id, question_id, correct=True):
        return self.client.post(f"/exams/{exam_id}/submit", json={"answers": [
            {"question_id": str(question_id), "answer": "True" if correct else "False"},
        ]})

    def test_first_exam_pass_adapts_failed_attempt_and_retakes_do_not(self):
        self.clock.return_value = date(2026, 9, 24)
        self.submit(self.payload(count=10))
        exam_id, question_id = self.exam_question("first_half")
        with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
            self.assertEqual(self.answer(exam_id, question_id, False).status_code, 200)
            recalc.assert_not_called()
            self.assertEqual(self.answer(exam_id, question_id).status_code, 200)
            self.assertEqual(recalc.call_count, 1)
            self.assertEqual(self.answer(exam_id, question_id).status_code, 200)
            self.assertEqual(recalc.call_count, 1)
        saved = self.snapshot()
        events = [item for item in saved["items"] if item["item_type"] == "first_half_exam" and item["achieved_at"]]
        self.assertEqual(len(events), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ExamAttempt)), 3)

    def test_second_and_final_first_pass_update_projections(self):
        self.clock.return_value = date(2026, 9, 24)
        self.submit(self.payload(*self.chapters, count=10))
        for kind in ("second_half", "final"):
            exam_id, question_id = self.exam_question(kind)
            with patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan) as recalc:
                response = self.answer(exam_id, question_id)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(recalc.call_count, 1)
            current = [item for item in self.snapshot()["items"] if item["item_type"] == kind + "_exam"]
            self.assertTrue(any(item["achieved_at"] for item in current))
            self.assertTrue(all(item["period"] == "historical" for item in current))

    def test_failed_exam_adaptation_rolls_back_achievement_and_attempt(self):
        self.submit(self.payload(count=10))
        exam_id, question_id = self.exam_question("first_half")
        before = self.snapshot()
        with patch("app.services.study_plan.recalculate_plan", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.answer(exam_id, question_id)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(ExamAttempt)), 0)
        self.assertFalse(self.db.scalar(select(UserExamProgression)).first_half_passed)
        self.assertEqual(self.snapshot(), before)

    def generation_job(self):
        self.parent.learning_language = "English"
        for chapter in self.chapters:
            chapter.learning_language = "English"
            chapter.generation_status = "pending"
            chapter.card_count = 4
            for card in self.cards[chapter.id]:
                self.db.delete(card)
        self.parent.generation_status = "generating"
        self.db.scalar(select(StudyPlan)).count_source = "planned"
        plan = DeckPlanResponse(title="Generated", subject="Science", education_level="School", learning_language="English",
                                chapters=[dict(title=chapter.title, description="Basics", key_concepts=["Concept"], card_count=4)
                                          for chapter in self.chapters])
        job = GenerationJob(parent_deck_id=self.parent.id, plan_json=plan.model_dump(mode="json"))
        self.db.add(job)
        self.db.commit()
        return job.id

    def run_generation(self, job_id, fail=False, partial=False):
        factory = sessionmaker(bind=self.engine, autoflush=False)
        generated = GeneratedChapter(title="Generated", cards=[GeneratedCard(front="Q", back="A") for _ in range(4)])
        with patch("app.worker.SessionLocal", factory), patch("app.services.deck_generation.SessionLocal", factory), \
             patch("app.services.deck_generation.DeepSeekService") as ai, \
             patch("app.services.study_plan.recalculate_plan", wraps=recalculate_plan,
                   side_effect=RuntimeError("injected") if fail else None) as recalc:
            ai.return_value.generate_chapter = AsyncMock(return_value=generated)
            if partial:
                ai.return_value.generate_chapter.side_effect = [generated] + [RuntimeError("AI unavailable") for _ in range(3)]
            asyncio.run(GenerationWorker().process_job(job_id))
        self.db.expire_all()
        return ai.return_value.generate_chapter.await_count, recalc.call_count

    def test_generation_adapts_once_not_per_card_and_keeps_history(self):
        job_id = self.generation_job()
        calls, recalcs = self.run_generation(job_id)
        self.assertEqual((calls, recalcs), (2, 1))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Card)), 8)
        self.assertEqual(self.db.get(GenerationJob, job_id).status, "completed")
        saved = self.snapshot()
        self.assertEqual(saved["items"][0]["id"], self.original["items"][0]["id"])
        self.assertEqual(saved["items"][0]["target_card_count"], 10)
        self.assertEqual(saved["remaining_card_count"], 8)
        self.assertEqual(saved["count_source"], "actual")

    def test_generation_finalization_failure_is_recoverable_without_regenerating(self):
        job_id = self.generation_job()
        self.assertEqual(self.run_generation(job_id, fail=True), (2, 1))
        job = self.db.get(GenerationJob, job_id)
        self.assertEqual(job.status, "failed")
        self.assertIn("completed chapters are retained", job.last_error)
        self.assertEqual(self.parent.generation_status, "failed")
        self.assertTrue(all(chapter.generation_status == "completed" for chapter in self.chapters))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Card)), 8)
        self.assertEqual(self.snapshot()["revision"], 1)
        response = self.client.post(f"/ai/decks/{self.parent.id}/retry")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.run_generation(job_id), (0, 1))
        self.assertEqual(self.db.get(GenerationJob, job_id).status, "completed")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Card)), 8)

    def test_repeated_completed_generation_does_not_adapt_or_generate(self):
        job_id = self.generation_job()
        self.run_generation(job_id)
        before = self.snapshot()
        self.assertEqual(self.run_generation(job_id), (0, 0))
        self.assertEqual(self.snapshot(), before)

    def test_partially_failed_generation_does_not_attempt_adaptation(self):
        job_id = self.generation_job()
        self.assertEqual(self.run_generation(job_id, partial=True), (4, 0))
        self.assertEqual(self.db.get(GenerationJob, job_id).status, "failed")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Card)), 4)
        self.assertEqual(self.snapshot()["revision"], 1)
        self.assertEqual([chapter.generation_status for chapter in self.chapters], ["completed", "failed"])

    def test_generation_without_plan_does_not_create_one(self):
        job_id = self.generation_job()
        self.db.delete(self.db.scalar(select(StudyPlan)))
        self.db.commit()
        self.assertEqual(self.run_generation(job_id), (2, 0))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 0)

    def test_logs_report_reason_and_revisions_without_card_contents(self):
        with self.assertLogs("app.services.study_plan_hooks", level="INFO") as log:
            self.submit()
        text = " ".join(log.output)
        for marker in ("reason=learning_progress", "old_revision=", "new_revision=", "plan_id="):
            self.assertIn(marker, text)
        self.assertNotIn(self.cards[self.chapters[0].id][0].front, text)

    def test_preference_endpoint_is_not_present_and_explicit_recalculation_remains(self):
        paths = self.app.openapi()["paths"]
        self.assertNotIn("/decks/{deck_id}/study-plan/preferences", paths)
        self.assertIn("post", paths["/decks/{deck_id}/study-plan/recalculate"])


if __name__ == "__main__":
    unittest.main()
