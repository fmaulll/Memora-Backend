"""Persistence/API checks with an isolated database and actual foreign keys."""
import asyncio
import unittest
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.db.database import Base, get_db
from app.models.card import Card
from app.models.deck import Deck
from app.models.exam import Exam, UserExamProgression
from app.models.generation_job import GenerationJob
from app.models.study_plan import StudyPlan, StudyPlanItem
from app.models.user import User
from app.routers.decks import router as decks_router
from app.routers.study_plan import router
from app.routers.ai import router as ai_router
from app.schemas.ai import DeckPlanResponse, GeneratedCard, GeneratedChapter
from app.services.deck_generation import DeckGenerationService
from app.services.exam import ExamService
from app.services.study_plan import reconcile_generated_plan


class StudyPlanAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(self.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(name="Learner", email="learner@example.com", password_hash="unused")
        self.db.add(self.user)
        self.db.flush()
        self.parent = self.deck("Parent")
        self.chapters = [self.deck("Later", self.parent.id, 2), self.deck("Earlier", self.parent.id, 1)]
        for chapter in self.chapters:
            self.add_cards(chapter, 3)
        self.db.commit()
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.include_router(decks_router)
        self.app.include_router(ai_router)
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(self.app)
        self.url = f"/decks/{self.parent.id}/study-plan"
        self.request = dict(start_date="2026-09-21", timezone="Asia/Jakarta", study_weekdays=[0, 1, 2, 3, 4])

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()

    def deck(self, title, parent_id=None, position=0):
        deck = Deck(user_id=self.user.id, parent_deck_id=parent_id, position=position,
                    title=title, subject="Science", education_level="School", card_count=99)
        self.db.add(deck)
        self.db.flush()
        return deck

    def add_cards(self, chapter, count):
        self.db.add_all([Card(deck_id=chapter.id, front=f"Question {i}", back="Answer") for i in range(count)])
        self.db.flush()

    def create(self, **changes):
        return self.client.post(self.url, json=self.request | changes)

    def test_round_trip_actual_counts_and_position_order(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        fetched = self.client.get(self.url).json()
        self.assertEqual(body["items"], fetched["items"])
        self.assertEqual(body["revision"], 1)
        self.assertEqual(body["count_source"], "actual")
        self.assertEqual([ch["title"] for ch in body["chapters"]], ["Earlier", "Later"])
        self.assertEqual(body["items"][0]["chapter_id"], str(self.chapters[1].id))
        self.assertEqual(sum(i["target_card_count"] or 0 for i in body["items"]), 6)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Exam)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(UserExamProgression)), 0)
        for item in body["items"]:
            self.assertNotIn("status", item)
            self.assertNotIn("completed_card_count", item)
            self.assertEqual(len(item["scheduled_date"]), 10)

    def test_position_ties_use_id(self):
        for chapter in self.chapters:
            chapter.position = 0
        self.db.commit()
        body = self.create().json()
        self.assertEqual([ch["id"] for ch in body["chapters"]], sorted(str(ch.id) for ch in self.chapters))

    def test_completed_empty_and_pending_are_distinct(self):
        empty = self.deck("Empty", self.parent.id, 3)
        pending = self.deck("Pending", self.parent.id, 4)
        pending.generation_status = "pending"
        pending.card_count = 7
        self.db.commit()
        body = self.create().json()
        counts = {ch["id"]: ch["scheduled_card_count"] for ch in body["chapters"]}
        self.assertEqual(counts[str(empty.id)], 0)
        self.assertEqual(counts[str(pending.id)], 7)
        self.assertEqual(body["count_source"], "planned")

    def test_unfinished_unknown_count_is_explicit_conflict(self):
        self.chapters[0].generation_status = "pending"
        self.chapters[0].card_count = None
        self.db.commit()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "chapter_count_unknown")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 0)

    def test_no_target_and_aggressive_target(self):
        body = self.create(requested_target_date="2026-09-21").json()
        self.assertEqual(body["requested_target_date"], "2026-09-21")
        self.assertGreater(body["estimated_finish_date"], body["requested_target_date"])
        self.assertFalse(body["target_achievable"])
        self.assertIsNone(body["required_daily_card_count"])

    def test_ownership_on_create_and_get(self):
        self.assertEqual(self.create().status_code, 201)
        other = User(name="Other", email="other@example.com", password_hash="unused")
        self.db.add(other)
        self.db.commit()
        self.user = other
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.create().status_code, 404)

    def test_authentication_required(self):
        del self.app.dependency_overrides[get_current_user]
        self.assertIn(self.client.get(self.url).status_code, {401, 403})
        self.assertIn(self.create().status_code, {401, 403})

    def test_child_deck_rejected_and_missing_plan_is_not_created_by_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)
        url = f"/decks/{self.chapters[0].id}/study-plan"
        self.assertEqual(self.client.post(url, json=self.request).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_repeated_creation_conflicts_without_replacing(self):
        body = self.create().json()
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.client.get(self.url).json()["items"], body["items"])

    def test_invalid_preferences_and_completion_fields(self):
        for changes in ({"timezone": "wrong/timezone"}, {"study_weekdays": []},
                        {"study_weekdays": [1, 1]}, {"study_weekdays": [7]}, {"study_weekdays": [True]},
                        {"daily_card_limit": 0}, {"requested_target_date": "2026-09-20"},
                        {"completed_card_count": 5}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes).status_code, 422)

    def test_default_start_uses_users_local_date(self):
        with patch("app.services.study_plan.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 21, 18, tzinfo=timezone.utc)
            body = self.create(start_date=None).json()
        self.assertEqual(body["start_date"], "2026-09-22")

    def test_generation_reconciliation_is_once_and_get_is_read_only(self):
        chapter = self.chapters[0]
        chapter.generation_status = "generating"
        chapter.card_count = 12
        self.db.commit()
        body = self.create().json()
        self.assertEqual(sum(i["target_card_count"] or 0 for i in body["items"]), 15)
        chapter.generation_status = "completed"
        self.db.commit()
        self.assertEqual(self.client.get(self.url).json()["revision"], 1)
        reconcile_generated_plan(self.db, self.parent)
        self.db.commit()
        body = self.client.get(self.url).json()
        self.assertEqual(body["revision"], 2)
        self.assertEqual(body["count_source"], "actual")
        self.assertEqual(sum(i["target_card_count"] or 0 for i in body["items"]), 6)
        reconcile_generated_plan(self.db, self.parent)
        self.db.commit()
        self.assertEqual(self.client.get(self.url).json()["items"], body["items"])
        self.assertEqual(self.client.get(self.url).json()["revision"], 2)

    def test_deletion_and_structure_guards(self):
        self.assertEqual(self.create().status_code, 201)
        child_url = f"/decks/{self.chapters[0].id}"
        self.assertEqual(self.client.delete(child_url).status_code, 409)
        self.assertEqual(self.client.put(child_url, json={"parent_deck_id": None}).status_code, 409)
        self.assertEqual(self.client.put(child_url, json={"position": 20}).status_code, 409)
        self.assertEqual(self.client.put(child_url, json={"title": "Renamed"}).status_code, 200)
        self.assertEqual(self.client.put(child_url, json={"parent_deck_id": str(self.parent.id)}).status_code, 200)
        reorder_url = f"/decks/{self.parent.id}/chapters/reorder"
        self.assertEqual(self.client.put(reorder_url, json={"chapter_ids": [str(ch.id) for ch in self.chapters]}).status_code, 409)
        self.assertEqual(self.client.post("/decks", json=dict(
            title="New", subject="Science", education_level="School", parent_deck_id=str(self.parent.id),
        )).status_code, 409)
        self.assertEqual(self.client.delete(f"/decks/{self.parent.id}").status_code, 204)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlanItem)), 0)

    def test_projections_do_not_unlock_exams(self):
        before = ExamService().get_status(self.parent.id, self.db, self.user)
        self.create(start_date="2020-01-01", requested_target_date="2020-02-01")
        after = ExamService().get_status(self.parent.id, self.db, self.user)
        self.assertEqual(before, after)
        self.assertEqual([entry["status"] for entry in after["exams"]], ["unlocked", "locked", "locked"])

    def generation_request(self):
        return dict(
            plan=dict(title="Generated", subject="Science", education_level="School", learning_language="English",
                      chapters=[dict(title=f"Chapter {i}", description="Basics", key_concepts=["Concept"], card_count=3)
                                for i in range(2)]),
            study_purpose="Learn from Scratch",
            study_plan=self.request,
        )

    def test_ai_opt_in_creates_plan_and_worker_reconciles_once(self):
        request = self.generation_request()
        response = self.client.post("/ai/decks/generate", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["study_plan"]["count_source"], "planned")
        self.assertEqual(body["timeline"]["total_cards"], 6)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(GenerationJob)), 1)
        generated = GeneratedChapter(title="Ignored", cards=[GeneratedCard(front="Q", back="A") for _ in range(3)])
        with patch("app.services.deck_generation.SessionLocal", sessionmaker(bind=self.engine)), \
             patch("app.services.deck_generation.DeepSeekService") as ai:
            ai.return_value.generate_chapter = AsyncMock(return_value=generated)
            asyncio.run(DeckGenerationService().generate_deck(uuid.UUID(body["deck"]["id"]),
                                                             DeckPlanResponse.model_validate(request["plan"])))
        self.db.expire_all()
        saved = self.client.get(f"/decks/{body['deck']['id']}/study-plan").json()
        self.assertEqual(saved["count_source"], "actual")
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(saved["items"], body["study_plan"]["items"])

    def test_ai_without_opt_in_and_conflicting_dates(self):
        request = self.generation_request()
        del request["study_plan"]
        response = self.client.post("/ai/decks/generate", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["study_plan"])
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 0)
        request = self.generation_request()
        request["target_date"] = "2026-10-10"
        request["study_plan"]["requested_target_date"] = "2026-10-11"
        self.assertEqual(self.client.post("/ai/decks/generate", json=request).status_code, 422)

    def test_ai_job_failure_rolls_back_decks_and_plan(self):
        before = self.db.scalar(select(func.count()).select_from(Deck))
        with patch("app.routers.ai.GenerationJob", side_effect=RuntimeError("Simulated job creation failure")):
            with self.assertRaises(RuntimeError):
                self.client.post("/ai/decks/generate", json=self.generation_request())
        self.assertEqual(self.db.scalar(select(func.count()).select_from(Deck)), before)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(StudyPlan)), 0)

    def test_unfinished_generation_does_not_reconcile(self):
        self.chapters[0].generation_status = "failed"
        self.db.commit()
        original = self.create().json()
        reconcile_generated_plan(self.db, self.parent)
        self.db.commit()
        self.assertEqual(self.client.get(self.url).json()["items"], original["items"])
        self.assertEqual(self.client.get(self.url).json()["revision"], 1)

    def test_empty_parent_rejected_but_completed_empty_chapter_is_valid(self):
        empty_parent = self.deck("No chapters")
        self.db.commit()
        url = f"/decks/{empty_parent.id}/study-plan"
        self.assertEqual(self.client.post(url, json=self.request).status_code, 409)
        self.deck("Empty chapter", empty_parent.id)
        self.db.commit()
        response = self.client.post(url, json=self.request)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["items"], [])

    def test_single_chapter_final_projection_does_not_change_runtime_gate(self):
        self.db.delete(self.chapters[0])
        self.db.commit()
        body = self.create().json()
        self.assertEqual([i["item_type"] for i in body["items"]], ["learn", "first_half_exam", "final_exam"])
        statuses = ExamService().get_status(self.parent.id, self.db, self.user)
        self.assertEqual(statuses["exams"][-1]["status"], "locked")


if __name__ == "__main__":
    unittest.main()
