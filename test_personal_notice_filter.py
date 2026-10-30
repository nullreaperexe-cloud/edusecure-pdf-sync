"""Privacy regressions for EduSecure's public 8aPDF publishing paths."""
import os
import unittest
from datetime import date
from unittest.mock import patch

import personal_notice_filter as privacy
import supabase_announcements as announcements
import supabase_materials as materials


class PersonalNoticeFilterTests(unittest.TestCase):
    def test_private_attendance_notification_without_configured_name(self):
        text = "Attendance Oct 30, 2026 Your Child STUDENT EXAMPLE is absent on Date 30/10/2026"
        self.assertTrue(privacy.is_personal_notice(text))

    def test_private_student_name_in_detail_or_original(self):
        with patch.dict(os.environ, {"EDUSECURE_PRIVATE_STUDENT_NAMES": "Example Student"}):
            self.assertTrue(privacy.is_personal_notice("General notice", "For EXAMPLE STUDENT only"))
            self.assertTrue(privacy.is_personal_notice({"original_message": "Message to Example Student"}))
            self.assertTrue(privacy.is_personal_notice("Example's individual report card is ready"))

    def test_general_school_announcements_remain(self):
        examples = [
            "Oct 30, 2026 Home Work: Do the worksheet shared in PDF in your notebook. Class Work: FRENCH",
            "Dear Parent, the marks secured by students of Classes VI to VIII are now available in the Report Card tab. Please assess your child's progress.",
            "Dear Parents, pay October month fee of your child. Please ignore if already paid.",
            "School attendance is compulsory for all students tomorrow.",
            "Parents-teachers meeting on Saturday at 10 AM.",
            "Annual examination seating arrangements for Class VIII.",
        ]
        for msg in examples:
            with self.subTest(msg=msg):
                self.assertFalse(privacy.is_personal_notice(msg))

    def test_privacy_guard_prevents_announcement_publish_before_ai(self):
        with (patch.object(announcements.legacy, "build_announcement") as build,
              patch.object(announcements.gateway, "call") as gateway):
            status, item = announcements.process_no_attachment_message(
                "Attendance Your Child EXAMPLE is absent today",
                "Your Child EXAMPLE is absent on Date 30/10/2026",
                date(2026, 10, 30), "", set(),
            )
            self.assertEqual(status, "ignored")
            self.assertIsNone(item)
            build.assert_not_called()
            gateway.assert_not_called()

    def test_final_announcement_publish_guard_uses_original_message(self):
        with (patch.object(announcements.gateway, "call") as gateway,
              patch.dict(os.environ, {"EDUSECURE_PRIVATE_STUDENT_NAMES": "Example Student"})):
            self.assertFalse(announcements.upload_announcement({
                "sourceMessageId": "fake-id",
                "title": "General Reminder",
                "description": "Reminder", 
                "originalMessage": "Individual notification for Example Student",
            }, date(2026, 10, 30)))
            gateway.assert_not_called()

    def test_pdf_metadata_private_guard(self):
        with (patch.object(materials, "build_material") as builder,
              patch.object(materials.gateway, "call") as gateway,
              patch.dict(os.environ, {"EDUSECURE_PRIVATE_STUDENT_NAMES": "Example Student"})):
            self.assertFalse(materials.upload_material(
                {"title": "Example Student personal report", "description": "Report"},
                date(2026, 10, 30), 1
            ))
            builder.assert_not_called()
            gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
