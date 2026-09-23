"""Pure scheduler tests: no database, clock, or AI dependency."""
import unittest
from collections import Counter
from datetime import date, timedelta
from uuid import UUID

from app.services.study_timeline import ChapterInput, generate_schedule


class StudyTimelineTests(unittest.TestCase):
    start = date(2026, 9, 21)  # Monday

    def schedule(self, counts, target=None, weekdays=None, limit=20):
        chapters = [ChapterInput(UUID(int=index + 1), count) for index, count in enumerate(counts)]
        return generate_schedule(chapters, self.start, target, weekdays or list(range(7)), limit)

    def test_even_chapters_preserve_counts_and_exam_order(self):
        result = self.schedule([21] * 11 + [20])
        counts = Counter()
        for item in result.items:
            if item.item_type == "learn":
                counts[item.chapter_id] += item.target_card_count
        self.assertEqual(sum(counts.values()), 251)
        self.assertEqual(list(counts.values()), [21] * 11 + [20])
        self.assertEqual([i.item_type for i in result.items if i.item_type != "learn"],
                         ["first_half_exam", "second_half_exam", "final_exam"])
        first_exam = next(i for i in result.items if i.item_type == "first_half_exam")
        self.assertEqual({i.chapter_id for i in result.items if i.scheduled_date < first_exam.scheduled_date},
                         {UUID(int=n) for n in range(1, 7)})
        self.assertLess(result.items[-2].scheduled_date, result.items[-1].scheduled_date)

    def test_odd_split_gives_first_group_extra_chapter(self):
        result = self.schedule([3] * 5)
        first_exam = next(i for i in result.items if i.item_type == "first_half_exam")
        before = [i.chapter_id for i in result.items if i.scheduled_date < first_exam.scheduled_date]
        self.assertEqual(before, [UUID(int=1), UUID(int=2), UUID(int=3)])

    def test_one_chapter_has_no_second_half(self):
        result = self.schedule([8])
        self.assertEqual([i.item_type for i in result.items], ["learn", "first_half_exam", "final_exam"])
        self.assertEqual(result.estimated_finish_date, self.start + timedelta(days=2))

    def test_empty_chapter_does_not_get_learning_item(self):
        result = self.schedule([0, 8, 7])
        self.assertNotIn(UUID(int=1), [i.chapter_id for i in result.items])
        self.assertEqual(sum(i.target_card_count or 0 for i in result.items), 15)
        self.assertFalse(self.schedule([0, 0]).items)

    def test_no_target_uses_workload_limit(self):
        result = self.schedule([45], limit=20)
        self.assertEqual([i.target_card_count for i in result.items if i.item_type == "learn"], [20, 20, 5])
        self.assertIsNone(result.required_daily_card_count)

    def test_target_spreads_work_without_exceeding_limit(self):
        target = self.start + timedelta(days=9)
        result = self.schedule([40, 40], target)
        self.assertEqual(result.required_daily_card_count, 14)
        self.assertLessEqual(result.estimated_finish_date, target)

    def test_aggressive_target_exposes_required_workload(self):
        target = self.start + timedelta(days=4)
        result = self.schedule([60, 60], target)
        self.assertEqual(result.required_daily_card_count, 60)
        self.assertGreater(result.estimated_finish_date, target)
        self.assertTrue(all((i.target_card_count or 0) <= 20 for i in result.items))

    def test_not_enough_days_for_milestones(self):
        result = self.schedule([1, 1], self.start)
        self.assertIsNone(result.required_daily_card_count)
        self.assertGreater(result.estimated_finish_date, self.start)

    def test_every_item_including_exams_uses_allowed_weekday(self):
        result = self.schedule([21, 21], weekdays=[0, 2, 4])
        self.assertTrue(all(i.scheduled_date.weekday() in {0, 2, 4} for i in result.items))
        self.assertEqual(result.items[0].scheduled_date, self.start)

    def test_deterministic_and_does_not_mutate_inputs(self):
        chapters = [ChapterInput(UUID(int=2), 11), ChapterInput(UUID(int=1), 19)]
        result = generate_schedule(chapters, self.start, None, [0, 1, 2, 3, 4], 10)
        self.assertEqual(result, generate_schedule(chapters, self.start, None, [0, 1, 2, 3, 4], 10))
        self.assertEqual(chapters[0].id, UUID(int=2))
        self.assertEqual(result.items[0].chapter_id, UUID(int=2))

    def test_invalid_inputs(self):
        for weekdays, limit, target, count in [([], 20, None, 5), ([7], 20, None, 5),
                                                ([0], 0, None, 5), ([0], 20, None, -1),
                                                ([0], 20, self.start - timedelta(days=1), 5)]:
            with self.subTest(weekdays=weekdays, limit=limit, target=target, count=count):
                with self.assertRaises(ValueError):
                    generate_schedule([ChapterInput(UUID(int=1), count)], self.start, target, weekdays, limit)


if __name__ == "__main__":
    unittest.main()
