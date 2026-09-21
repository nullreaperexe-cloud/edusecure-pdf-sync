import unittest
from datetime import date

import announcement_processor as announcements
import openrouter_title as ai_title


class AnnouncementProcessorTests(unittest.TestCase):
    def setUp(self):
        self.old_key = ai_title.OPENROUTER_API_KEY
        ai_title.OPENROUTER_API_KEY = ""

    def tearDown(self):
        ai_title.OPENROUTER_API_KEY = self.old_key

    def test_stable_message_id_is_repeatable(self):
        day = date(2026, 9, 21)
        first = announcements.stable_message_id("Science test tomorrow", day)
        second = announcements.stable_message_id("Science   test tomorrow", day)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("edusecure-"))

    def test_test_message_category_and_subject(self):
        item = announcements.build_announcement(
            "Dear Students Science test tomorrow from Chapter 5.",
            "Science test tomorrow from Chapter 5.",
            date(2026, 9, 21),
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["category"], "Tests")
        self.assertEqual(item["subject"], "Science")
        self.assertNotIn("Dear Students", item["title"])

    def test_homework_category(self):
        self.assertEqual(
            announcements.classify_category("Complete the worksheet as homework."),
            "Homework",
        )

    def test_holiday_category(self):
        self.assertEqual(
            announcements.classify_category("School closed tomorrow due to holiday."),
            "Holidays",
        )

    def test_greeting_only_is_ignored(self):
        self.assertFalse(announcements.useful_announcement("Dear Students Good Morning Thanks"))


if __name__ == "__main__":
    unittest.main()
