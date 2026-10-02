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
    def test_ai_failure_still_publishes_safe_pending_announcement(self, mocked_ai):
        mocked_ai.return_value = None
        item = announcements.build_announcement(
            "Complete the worksheet as homework.",
            "Complete the worksheet as homework.",
            date(2026, 9, 21),
        )
        self.assertEqual(item["aiStatus"], "pending")
        self.assertEqual(item["category"], "General")
        self.assertTrue(item["title"])

    @patch("announcement_processor.ai_title.generate_announcement_metadata")
    def test_even_greeting_only_message_becomes_announcement(self, mocked_ai):
        mocked_ai.return_value = None
        item = announcements.build_announcement(
            "Dear Students Good Morning Thanks",
            "Dear Students Good Morning Thanks",
            date(2026, 9, 21),
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["aiStatus"], "pending")


    @patch("announcement_processor.ai_title.generate_announcement_metadata")
    def test_non_pdf_attachment_is_preserved(self, mocked_ai):
        mocked_ai.return_value = None
        item = announcements.build_announcement(
            "See the attached activity image.",
            "See the attached activity image.",
            date(2026, 9, 21),
            attachment_url="https://example.com/activity.jpg",
        )
        fields = announcements._announcement_fields(item, date(2026, 9, 21))
        self.assertTrue(fields["hasAttachment"]["booleanValue"])
        self.assertEqual(
            fields["attachmentUrl"]["stringValue"],
            "https://example.com/activity.jpg",
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


    def test_dash_separated_title_date_is_stripped(self):
        title = ai_title._final_title_cleanup(
            "Aug 27 - 2026 - School Excursion Educational Trip",
            "General",
        )
        self.assertEqual(title, "School Excursion Educational Trip")
        self.assertNotIn("2026", title)
        self.assertNotIn("Aug", title)

    def test_model_chatter_title_is_rejected(self):
        title = ai_title._final_title_cleanup(
            "The user wants a clean title for a Class 8 study-material library",
            "General",
        )
        self.assertEqual(title, "")

    def test_impossible_tests_category_is_rejected(self):
        parsed = {
            "title": "PTM Reminder",
            "category": "Tests",
            "subject": "General",
            "priority": "normal",
        }
        result = ai_title._validate_announcement_meta(
            parsed,
            "Kindly attend PTM tomorrow to discuss the academic report.",
            list(announcements.ALLOWED_CATEGORIES),
            "free-model",
        )
        self.assertIsNone(result)

    def test_real_test_category_can_pass_validation(self):
        parsed = {
            "title": "Computer Revision Test",
            "category": "Tests",
            "subject": "Computer",
            "priority": "normal",
        }
        result = ai_title._validate_announcement_meta(
            parsed,
            "Prepare for the Computer revision test. Syllabus Chapters 8 and 11.",
            list(announcements.ALLOWED_CATEGORIES),
            "free-model",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "Tests")



    @patch("announcement_processor.claim_announcement", return_value="existing")
    @patch("announcement_processor.firestore_request")
    @patch("announcement_processor.build_announcement")
    @patch("announcement_processor.upload_announcement", return_value=True)
    def test_unpublished_claim_is_recovered(
        self, mocked_upload, mocked_build, mocked_request, _claim,
    ):
        response = unittest.mock.Mock()
        response.ok = True
        response.json.return_value = {
            "fields": {
                "published": {"booleanValue": False},
                "title": {"stringValue": "Processing Announcement"},
            }
        }
        mocked_request.return_value = response
        mocked_build.return_value = {"title": "Exam Notice", "sourceMessageId": "ignored"}
        status, item = announcements.process_no_attachment_message(
            "Science exam tomorrow", "Science exam tomorrow",
            date(2026, 10, 2), "token", set(),
        )
        self.assertEqual(status, "created")
        self.assertEqual(item["title"], "Exam Notice")
        self.assertTrue(mocked_upload.called)

    @patch("announcement_processor.claim_announcement", return_value="existing")
    @patch("announcement_processor.firestore_request")
    @patch("announcement_processor.build_announcement")
    def test_published_complete_announcement_is_not_regenerated(
        self, mocked_build, mocked_request, _claim,
    ):
        response = unittest.mock.Mock()
        response.ok = True
        response.json.return_value = {
            "fields": {
                "published": {"booleanValue": True},
                "title": {"stringValue": "Correct Announcement"},
            }
        }
        mocked_request.return_value = response
        status, item = announcements.process_no_attachment_message(
            "School notice", "School notice",
            date(2026, 10, 2), "token", set(),
        )
        self.assertEqual(status, "duplicate")
        self.assertIsNone(item)
        mocked_build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
