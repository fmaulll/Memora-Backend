"""Pure calendar attribution and remaining-work math."""
import unittest
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.services.adaptive_study_timeline import generate_adaptive_schedule
from app.services.study_attribution import LearningEvent, LearningTarget, attribute_learning, receipt_events
from app.services.study_calendar import local_date
from app.services.study_timeline import ChapterInput


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.chapter, self.epoch = uuid4(), uuid4()
        self.day = date(2026, 9, 21)
        self.target = LearningTarget(uuid4(), self.chapter, self.epoch, self.day, 10, 0)

    def events(self, count, stamp="2026-09-21T18:00:00+07:00"):
        return [LearningEvent(uuid4(), self.chapter, self.epoch, datetime.fromisoformat(stamp)) for _ in range(count)]

    def test_full_partial_missed_and_overcompleted_targets(self):
        for count in (0, 4, 10, 15):
            with self.subTest(count=count):
                actual = attribute_learning([self.target], self.events(count), "Asia/Jakarta")
                self.assertEqual(actual[self.target.id], min(count, 10))

    def test_exact_fact_is_attributed_once_across_duplicate_events_and_targets(self):
        other = LearningTarget(uuid4(), self.chapter, self.epoch, self.day, 10, 1)
        events = self.events(14)
        actual = attribute_learning([other, self.target], events + events, "Asia/Jakarta")
        self.assertEqual((actual[self.target.id], actual[other.id]), (10, 4))

    def test_surplus_is_not_falsely_reported_as_tomorrows_learning(self):
        tomorrow = LearningTarget(uuid4(), self.chapter, self.epoch, self.day + timedelta(days=1), 10, 1)
        actual = attribute_learning([self.target, tomorrow], self.events(15), "Asia/Jakarta")
        self.assertEqual(actual, {self.target.id: 10, tomorrow.id: 0})

    def test_local_midnight_not_utc_date(self):
        events = self.events(1, "2026-09-21T00:30:00+07:00")
        self.assertEqual(events[0].learned_at.astimezone(timezone.utc).date(), date(2026, 9, 20))
        self.assertEqual(attribute_learning([self.target], events, "Asia/Jakarta")[self.target.id], 1)

    def test_dst_boundary_uses_iana_calendar(self):
        self.assertEqual(local_date(datetime.fromisoformat("2026-11-01T05:30:00+00:00"), "America/New_York"), date(2026, 11, 1))
        self.assertEqual(local_date(datetime.fromisoformat("2026-11-01T06:30:00+00:00"), "America/New_York"), date(2026, 11, 1))

    def test_late_receipt_uses_completion_time_and_ignores_already_learned_ids(self):
        receipt = dict(completed_at="2026-09-21T18:00:00+07:00", submitted_at="2026-09-23T19:00:00Z", decks=[dict(
            deck_id=str(self.chapter), progress_epoch=str(self.epoch), accepted_card_ids=[str(uuid4()) for _ in range(4)],
            already_learned_card_ids=[str(uuid4()) for _ in range(10)],
        )])
        events = receipt_events([receipt, receipt], {self.chapter})
        self.assertEqual(len(events), 4)
        self.assertEqual(attribute_learning([self.target], events, "Asia/Jakarta")[self.target.id], 4)

    def test_wrong_chapter_epoch_or_date_never_credits_target(self):
        correct = self.events(1)[0]
        events = [LearningEvent(uuid4(), uuid4(), self.epoch, correct.learned_at),
                  LearningEvent(uuid4(), self.chapter, uuid4(), correct.learned_at),
                  *self.events(1, "2026-09-22T09:00:00+07:00")]
        self.assertEqual(attribute_learning([self.target], events, "Asia/Jakarta")[self.target.id], 0)


class AdaptiveSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.ids = [uuid4() for _ in range(5)]
        self.day = date(2026, 9, 22)

    def schedule(self, counts, **kwargs):
        return generate_adaptive_schedule([ChapterInput(key, count) for key, count in zip(self.ids, counts)],
                                          self.day, kwargs.pop("target_date", None),
                                          kwargs.pop("study_weekdays", list(range(7))),
                                          kwargs.pop("daily_card_limit", 10), **kwargs)

    def test_partial_missed_and_multiple_missed_work_is_conserved(self):
        for remaining in (46, 50, 70):
            with self.subTest(remaining=remaining):
                result = self.schedule([remaining])
                self.assertEqual(sum(item.target_card_count or 0 for item in result.items), remaining)
                self.assertTrue(all(item.scheduled_date >= self.day for item in result.items))

    def test_overachievement_reduces_finish_and_does_not_create_negative_work(self):
        behind = self.schedule([50])
        ahead = self.schedule([35])
        self.assertLess(ahead.estimated_finish_date, behind.estimated_finish_date)
        self.assertEqual(sum(item.target_card_count or 0 for item in ahead.items), 35)

    def test_current_day_progress_consumes_capacity_without_double_counting(self):
        result = self.schedule([16], today_credits={self.ids[0]: 4})
        first = result.items[0]
        self.assertEqual((first.scheduled_date, first.target_card_count), (self.day, 10))
        self.assertEqual(sum(item.target_card_count or 0 for item in result.items) - 4, 16)

    def test_today_overachievement_retained_and_additional_work_moves_to_next_day(self):
        result = self.schedule([5], today_credits={self.ids[0]: 15})
        self.assertEqual(result.items[0].target_card_count, 15)
        self.assertGreater(result.items[1].scheduled_date, self.day)
        self.assertEqual(result.items[1].target_card_count, 5)

    def test_chapter_order_and_weekdays_preserved(self):
        result = self.schedule([13, 7, 5, 9, 10], study_weekdays=[0, 2, 4])
        order = [self.ids.index(item.chapter_id) for item in result.items if item.item_type == "learn"]
        self.assertEqual(order, sorted(order))
        self.assertTrue(all(item.scheduled_date.weekday() in [0, 2, 4] for item in result.items))

    def test_odd_halves_and_milestones_move_with_unfinished_work(self):
        before = self.schedule([1, 1, 1, 1, 1])
        after = self.schedule([10, 10, 10, 10, 10])
        for kind in ("first_half_exam", "second_half_exam", "final_exam"):
            self.assertGreater(next(i.scheduled_date for i in after.items if i.item_type == kind),
                               next(i.scheduled_date for i in before.items if i.item_type == kind))
        first_exam = next(i.scheduled_date for i in after.items if i.item_type == "first_half_exam")
        self.assertEqual({i.chapter_id for i in after.items if i.item_type == "learn" and i.scheduled_date < first_exam}, set(self.ids[:3]))

    def test_one_chapter_has_no_second_half_and_passed_milestones_are_omitted(self):
        result = self.schedule([12], passed_exams=frozenset({"first_half_exam"}))
        self.assertEqual([item.item_type for item in result.items if item.item_type != "learn"], ["final_exam"])
        self.assertEqual(self.schedule([0], passed_exams=frozenset({"first_half_exam", "final_exam"})).items, ())

    def test_all_learned_still_projects_unpassed_exams(self):
        result = self.schedule([0, 0])
        self.assertEqual([item.item_type for item in result.items], ["first_half_exam", "second_half_exam", "final_exam"])

    def test_required_workload_can_exceed_preferred_cap(self):
        deadline = self.day + timedelta(days=3)
        result = self.schedule([50], target_date=deadline)
        self.assertEqual(result.required_daily_card_count, 25)
        self.assertGreater(result.estimated_finish_date, deadline)
        self.assertLessEqual(max(i.target_card_count or 0 for i in result.items), 10)

    def test_impossible_or_expired_deadline_is_not_rewritten_or_reported_feasible(self):
        for deadline in (self.day, self.day - timedelta(days=2)):
            result = self.schedule([10], target_date=deadline)
            self.assertIsNone(result.required_daily_card_count)
            self.assertGreater(result.estimated_finish_date, deadline)

    def test_deterministic_and_does_not_mutate_input(self):
        credits = {self.ids[0]: 2}
        first = self.schedule([20, 3], today_credits=credits)
        self.assertEqual(first, self.schedule([20, 3], today_credits=credits))
        self.assertEqual(credits, {self.ids[0]: 2})

    def test_already_finished_has_zero_required_future_work_even_after_deadline(self):
        result = self.schedule([0], target_date=self.day - timedelta(days=2),
                               passed_exams=frozenset({"first_half_exam", "final_exam"}))
        self.assertEqual(result.items, ())
        self.assertEqual(result.required_daily_card_count, 0)

    def test_off_day_learning_reduces_work_without_inventing_off_day_targets(self):
        result = self.schedule([6], study_weekdays=[0, 2, 4], today_credits={self.ids[0]: 4})
        self.assertTrue(all(item.scheduled_date.weekday() in [0, 2, 4] for item in result.items))
        self.assertEqual(sum(item.target_card_count or 0 for item in result.items), 6)


if __name__ == "__main__":
    unittest.main()
