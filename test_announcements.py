import unittest
from datetime import date
from unittest.mock import patch

import announcement_processor as announcements


class AnnouncementProcessorTests(unittest.TestCase):
    def test_stable_message_id_is_repeatable(self):
        day = date(2026, 9, 21)
        first = announcements.stable_message_id("Science test tomorrow", day)
        second = announcements.stable_message_id("Science   test tomorrow", day)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("edusecure-"))

    @patch("announcement_processor.ai_title.generate_announcement_metadata")
    def test_ai_controls_title_category_subject_priority(self, mocked_ai):
        mocked_ai.return_value = {
            "title": "Chapter 5 Science Test",
            "category": "Tests",
            "subject": "Science",
            "priority": "important",
            "aiModel": "free-model",
        }
        item = announcements.build_announcement(
            "Dear Students Science test tomorrow from Chapter 5.",
            "Science test tomorrow from Chapter 5.",
            date(2026, 9, 21),
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Chapter 5 Science Test")
        self.assertEqual(item["category"], "Tests")
        self.assertEqual(item["subject"], "Science")
        self.assertEqual(item["priority"], "important")
        mocked_ai.assert_called_once()

    @patch("announcement_processor.ai_title.generate_announcement_metadata")
    def test_ai_failure_postpones_instead_of_guessing_category(self, mocked_ai):
        mocked_ai.return_value = None
        item = announcements.build_announcement(
            "Complete the worksheet as homework.",
            "Complete the worksheet as homework.",
            date(2026, 9, 21),
        )
        self.assertEqual(item, {"_retry": True})

    def test_greeting_only_is_ignored_before_ai_call(self):
        self.assertFalse(
            announcements.useful_announcement("Dear Students Good Morning Thanks")
        )


if __name__ == "__main__":
    unittest.main()
