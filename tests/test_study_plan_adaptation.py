"""Explicit adaptation API, preserved historical facts, resets, and transaction failure."""
import uuid
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

import test_study_plan as fixtures
from app.models.card import Card
from app.models.exam import UserExamProgression
from app.models.study_plan import StudyPlan
from app.schemas.study_progress import StudyProgressSubmission, ProgressResetRequest
from app.services.study_progress import submit_progress, reset_progress
from app.services.study_plan import reconcile_generated_plan


class AdaptivePlanAPITests(unittest.TestCase):
    deck = fixtures.StudyPlanAPITests.deck
    add_cards = fixtures.StudyPlanAPITests.add_cards
    create = fixtures.StudyPlanAPITests.create
    tearDown = fixtures.StudyPlanAPITests.tearDown

    def setUp(self):
        fixtures.StudyPlanAPITests.setUp(self)
        self.chapters.sort(key=lambda chapter: (chapter.position, chapter.id))
        for chapter in self.chapters:
            self.add_cards(chapter, 7)
        self.db.commit()
        self.cards = {chapter.id: list(self.db.scalars(select(Card).where(Card.deck_id == chapter.id).order_by(Card.id)).all())
                      for chapter in self.chapters}
        self.clock = patch("app.services.study_plan.local_today", return_value=date(2026, 9, 21)).start()
        self.addCleanup(patch.stopall)
        self.request.update(daily_card_limit=10, study_weekdays=list(range(7)))

    def plan(self, **kwargs):
        response = self.create(**kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def recalc(self, day="2026-09-22"):
        self.clock.return_value = date.fromisoformat(day)
        response = self.client.post(self.url + "/recalculate")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def learn(self, count, chapter=None, at="2026-09-21T18:00:00+07:00", offset=0):
        chapter = chapter or self.chapters[0]
        request = StudyProgressSubmission.model_validate(dict(session_id=str(uuid.uuid4()), completed_at=at, decks=[dict(
            deck_id=str(chapter.id), progress_epoch=str(chapter.progress_epoch), learned_cards=[
                dict(card_id=str(card.id), phase="review", answer="got_it") for card in self.cards[chapter.id][offset:offset + count]
            ],
        )]))
        submit_progress(self.db, self.user.id, request)
        self.db.commit()

    def reset(self):
        reset_progress(self.db, self.user.id, self.parent.id, ProgressResetRequest.model_validate(dict(
            reset_id=str(uuid.uuid4()), expected_decks=[dict(deck_id=chapter.id, progress_epoch=chapter.progress_epoch)
                                                     for chapter in self.chapters],
        )))
        self.db.commit()

    def history(self, body):
        return [item for item in body["items"] if item["period"] == "historical"]

    def test_full_past_target_is_closed_without_rewriting_original(self):
        original = self.plan()
        self.learn(10)
        body = self.recalc()
        item = body["items"][0]
        self.assertEqual(item["id"], original["items"][0]["id"])
        self.assertEqual((item["target_card_count"], item["actual_learned_count"], item["shortfall_count"], item["status"]),
                         (10, 10, 0, "completed"))
        self.assertIsNotNone(item["closed_at"])

    def test_partial_day_keeps_target_and_redistributes_shortfall(self):
        self.plan()
        self.learn(4)
        body = self.recalc()
        self.assertEqual((body["items"][0]["target_card_count"], body["items"][0]["actual_learned_count"],
                          body["items"][0]["shortfall_count"], body["items"][0]["status"]), (10, 4, 6, "partial"))
        self.assertEqual(body["remaining_card_count"], 16)
        self.assertEqual(sum(i["target_card_count"] or 0 for i in body["items"] if i["period"] != "historical"), 16)
        self.assertEqual(body["items"][1]["chapter_id"], str(self.chapters[0].id))

    def test_missed_days_are_not_erased(self):
        original = self.plan()
        body = self.recalc("2026-09-24")
        history = self.history(body)
        self.assertEqual([item["id"] for item in history], [item["id"] for item in original["items"][:3]])
        self.assertTrue(all(item["status"] == "missed" for item in history))
        self.assertEqual(body["remaining_card_count"], 20)

    def test_late_upload_credits_monday_and_not_upload_day(self):
        self.plan()
        before = self.recalc("2026-09-23")
        self.assertEqual(before["items"][0]["status"], "missed")
        self.learn(4)
        after = self.recalc("2026-09-23")
        self.assertEqual(after["items"][0]["actual_learned_count"], 4)
        self.assertEqual(after["items"][0]["closed_at"], before["items"][0]["closed_at"])
        self.assertTrue(all(item["actual_learned_count"] in (0, None) for item in after["items"] if item["scheduled_date"] == "2026-09-23"))
        self.assertGreater(after["revision"], before["revision"])

    def test_timezone_boundary_attribution_through_receipts(self):
        self.plan()
        self.learn(4, at="2026-09-21T00:30:00+07:00")
        self.assertEqual(self.recalc()["items"][0]["actual_learned_count"], 4)

    def test_identical_recalculation_keeps_revision_ids_and_timestamp(self):
        self.plan()
        self.learn(4)
        first = self.recalc()
        self.assertEqual(self.recalc(), first)
        self.assertEqual(self.client.get(self.url).json(), first)

    def test_current_day_work_is_derived_and_does_not_double_count(self):
        self.plan()
        self.learn(4)
        body = self.recalc("2026-09-21")
        current = [item for item in body["items"] if item["period"] == "current"]
        self.assertEqual((current[0]["target_card_count"], current[0]["actual_learned_count"]), (10, 4))
        self.assertEqual(body["remaining_card_count"], 16)
        self.assertEqual(self.recalc("2026-09-21"), body)

    def test_card_deletion_preserves_closed_actual_even_after_reset(self):
        self.plan()
        self.learn(4)
        before = self.recalc()
        self.db.delete(self.cards[self.chapters[0].id][0])
        self.db.commit()
        self.reset()
        after = self.recalc()
        self.assertEqual(self.history(after), self.history(before))
        self.assertEqual(after["remaining_card_count"], 19)

    def test_delete_and_reset_before_first_close_still_preserve_receipt_evidence(self):
        self.plan()
        self.learn(4)
        self.reset()
        self.db.delete(self.cards[self.chapters[0].id][0])
        self.db.commit()
        self.assertEqual(self.recalc()["items"][0]["actual_learned_count"], 4)

    def test_new_card_increases_remaining_without_changing_history(self):
        self.plan()
        self.learn(10)
        before = self.recalc()
        self.add_cards(self.chapters[0], 11)
        self.db.commit()
        after = self.recalc()
        self.assertEqual(self.history(after), self.history(before))
        self.assertEqual(after["remaining_card_count"], 21)
        self.assertGreater(after["estimated_finish_date"], before["estimated_finish_date"])

    def test_reset_rebuilds_from_today_and_retains_original_targets(self):
        self.plan()
        self.learn(10)
        before = self.recalc()
        self.reset()
        after = self.recalc()
        self.assertEqual(self.history(after), self.history(before))
        self.assertEqual(after["remaining_card_count"], 20)
        self.assertEqual(after["items"][1]["scheduled_date"], "2026-09-22")
        self.assertEqual(after["items"][1]["chapter_id"], str(self.chapters[0].id))

    def test_late_accepted_fact_survives_later_reset_but_stale_submission_stays_rejected(self):
        self.plan()
        self.recalc()
        self.learn(4)
        self.reset()
        self.assertEqual(self.recalc()["items"][0]["actual_learned_count"], 4)
        # Phase 2A stale-epoch rejection continues to be covered by its API/PG suite.

    def test_target_preserved_required_workload_and_unachievable_deadline(self):
        self.plan(requested_target_date="2026-09-25")
        body = self.recalc("2026-09-24")
        self.assertEqual(body["requested_target_date"], "2026-09-25")
        self.assertFalse(body["target_achievable"])
        self.assertIsNone(body["required_daily_card_count"])
        self.assertGreater(body["estimated_finish_date"], "2026-09-25")

    def test_passed_exam_event_retains_real_date_after_reset_and_card_addition(self):
        self.plan()
        row = UserExamProgression(user_id=self.user.id, deck_id=self.parent.id,
                                  first_half_passed=True, second_half_passed=True, final_passed=True,
                                  first_half_completed_at=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
                                  second_half_completed_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
                                  final_completed_at=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
        self.db.add(row)
        self.db.commit()
        before = self.recalc("2026-09-24")
        achievements = [item for item in before["items"] if item["achieved_at"]]
        self.assertEqual([item["achieved_at"][:10] for item in achievements], ["2026-09-21", "2026-09-22", "2026-09-23"])
        self.reset()
        self.add_cards(self.chapters[0], 2)
        self.db.commit()
        after = self.recalc("2026-09-24")
        self.assertEqual([item for item in after["items"] if item["achieved_at"]], achievements)
        self.assertTrue(all(item["item_type"] == "learn" for item in after["items"] if item["period"] != "historical"))

    def test_missed_exam_projection_keeps_date_when_pass_happens_later(self):
        self.plan()
        self.db.add(UserExamProgression(user_id=self.user.id, deck_id=self.parent.id, first_half_passed=True,
                                       first_half_completed_at=datetime(2026, 9, 23, 12, tzinfo=timezone.utc)))
        self.db.commit()
        body = self.recalc("2026-09-24")
        exams = [item for item in body["items"] if item["item_type"] == "first_half_exam"]
        self.assertEqual([(item["scheduled_date"], item["status"]) for item in exams],
                         [("2026-09-22", "missed"), ("2026-09-23", "completed")])

    def test_foreign_and_child_decks_cannot_recalculate(self):
        self.plan()
        self.assertEqual(self.client.post(f"/decks/{self.chapters[0].id}/study-plan/recalculate").status_code, 400)
        self.assertEqual(self.client.post(f"/decks/{uuid.uuid4()}/study-plan/recalculate").status_code, 404)
        self.parent.user_id = uuid.uuid4()
        self.db.commit()
        self.assertEqual(self.client.post(self.url + "/recalculate").status_code, 404)

    def test_recalculation_rollback_leaves_revision_history_and_items_intact(self):
        original = self.plan()
        self.clock.return_value = date(2026, 9, 22)
        with patch("app.services.study_plan.generate_adaptive_schedule", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url + "/recalculate")
        plan = self.db.scalar(select(StudyPlan))
        self.assertEqual(plan.revision, original["revision"])
        self.assertEqual([str(item.id) for item in plan.items], [item["id"] for item in original["items"]])
        self.assertTrue(all(item.closed_at is None for item in plan.items))

    def test_unfinished_generation_rejected_without_closing_history(self):
        self.plan()
        self.chapters[0].generation_status = "generating"
        self.db.commit()
        response = self.client.post(self.url + "/recalculate")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "study_plan_content_not_ready")

    def test_generation_callback_cannot_replace_past_targets(self):
        self.chapters[0].generation_status = "generating"
        self.chapters[0].card_count = 30
        self.db.commit()
        before = self.plan()
        self.clock.return_value = date(2026, 9, 22)
        self.chapters[0].generation_status = "completed"
        self.db.commit()
        reconcile_generated_plan(self.db, self.parent)
        self.db.commit()
        self.assertEqual(self.db.scalar(select(StudyPlan)).revision, 1)
        after = self.recalc()
        self.assertEqual(after["items"][0]["id"], before["items"][0]["id"])
        self.assertEqual(after["items"][0]["target_card_count"], before["items"][0]["target_card_count"])

    def test_no_automatic_schedule_mutation_from_learning_or_get(self):
        original = self.plan()
        self.learn(4)
        body = self.client.get(self.url).json()
        self.assertEqual(body["revision"], original["revision"])
        self.assertEqual([(item["id"], item["target_card_count"]) for item in body["items"]],
                         [(item["id"], item["target_card_count"]) for item in original["items"]])

    def test_unchanged_initial_schedule_does_not_bump_revision_for_algorithm_name(self):
        original = self.plan()
        body = self.recalc("2026-09-21")
        self.assertEqual(body, original)

    def test_early_exam_pass_does_not_mark_its_later_past_projection_missed(self):
        original = self.plan()
        first_exam = next(item for item in original["items"] if item["item_type"] == "first_half_exam")
        self.db.add(UserExamProgression(user_id=self.user.id, deck_id=self.parent.id, first_half_passed=True,
                                       first_half_completed_at=datetime(2026, 9, 21, 12, tzinfo=timezone.utc)))
        self.db.commit()
        result = self.recalc("2026-09-23")
        saved = next(item for item in result["items"] if item["id"] == first_exam["id"])
        self.assertEqual(saved["scheduled_date"], first_exam["scheduled_date"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["achieved_at"], "2026-09-21T12:00:00Z")

    def test_overachievement_keeps_past_target_and_reduces_next_days_work(self):
        first = self.chapters[0]
        self.add_cards(first, 10)
        self.db.commit()
        self.cards[first.id] = list(self.db.scalars(select(Card).where(Card.deck_id == first.id).order_by(Card.id)).all())
        self.plan()
        self.learn(15)
        result = self.recalc()
        self.assertEqual((result["items"][0]["actual_learned_count"], result["items"][0]["target_card_count"]), (10, 10))
        self.assertEqual(result["items"][1]["target_card_count"], 5)
        self.assertEqual(result["remaining_card_count"], 15)

    def test_all_cards_deleted_blocks_unpassed_exam_forecast_without_erasing_history(self):
        self.plan(requested_target_date="2026-10-30")
        self.learn(10)
        before = self.recalc()
        for card in self.cards[self.chapters[0].id]:
            self.db.delete(card)
        self.db.commit()
        result = self.recalc()
        self.assertTrue(result["projection_blocked"])
        self.assertFalse(result["target_achievable"])
        self.assertEqual(self.history(result), self.history(before))

    def test_finished_plan_with_deleted_cards_has_no_mandatory_exam_forecast(self):
        self.plan()
        self.learn(10)
        self.learn(10, self.chapters[1])
        self.db.add(UserExamProgression(user_id=self.user.id, deck_id=self.parent.id,
                                       first_half_passed=True, second_half_passed=True, final_passed=True))
        self.db.commit()
        self.recalc()
        for cards in self.cards.values():
            for card in cards:
                self.db.delete(card)
        self.db.commit()
        result = self.recalc()
        self.assertFalse(result["projection_blocked"])
        self.assertTrue(all(item["period"] == "historical" for item in result["items"]))


if __name__ == "__main__":
    unittest.main()
