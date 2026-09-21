import unittest
from datetime import date
from unittest.mock import patch

import announcement_processor as announcements
import openrouter_title as ai_title


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


    def test_title_date_is_stripped(self):
        title = ai_title._final_title_cleanup(
            "Science Project Submission - 21 September 2026",
            "Science",
        )
        self.assertEqual(title, "Project Submission")
        self.assertNotIn("2026", title)
        self.assertNotIn("September", title)

    def test_created_at_uses_edusecure_message_date(self):
        fields = announcements._announcement_fields(
            {
                "title": "Project Submission",
                "description": "Submit the project.",
                "category": "Projects",
                "subject": "Science",
                "priority": "normal",
                "sourceMessageId": "edusecure-test",
            },
            date(2026, 9, 21),
        )
        self.assertEqual(
            fields["createdAt"]["timestampValue"],
            "2026-09-21T00:00:00Z",
        )
        self.assertEqual(
            fields["messageDate"]["timestampValue"],
            "2026-09-21T00:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
