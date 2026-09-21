import unittest

import title_cleaner as intelligence
import openrouter_title as ai_title


class TitleSubjectIntelligenceTests(unittest.TestCase):
    def test_math_correction_title_and_subject(self):
        raw = (
            "Manav Mangal SMART SCHOOL-88 Circular Dear Students Good morning "
            "Kindly note the correction in Q no11 Exercise 7.2. Thanks"
        )
        self.assertEqual(
            intelligence.sanitize_title(raw, "Mathematics"),
            "Correction in Q No. 11 Exercise 7.2",
        )
        self.assertEqual(
            intelligence.detect_subject(raw, current_subject="Circular"),
            "Mathematics",
        )

    def test_circular_metadata_removed(self):
        raw = (
            "Manav Mangal SMART SCHOOL-88 Circular Dear Parent, "
            "PFA of Circular No. 065 - MMSS88 2026-27"
        )
        title = intelligence.sanitize_title(raw, "Circular")
        for banned in ("Manav Mangal", "Circular", "065", "MMSS88", "2026-27"):
            self.assertNotIn(banned.lower(), title.lower())

    def test_french(self):
        self.assertEqual(intelligence.detect_subject("French unseen passage"), "French")

    def test_ai_maps_to_computer(self):
        self.assertEqual(
            intelligence.detect_subject("Artificial Intelligence Chapter 2 Worksheet"),
            "Computer",
        )

    def test_holiday_notice_is_circular(self):
        self.assertEqual(
            intelligence.detect_subject("Holiday notice for students"),
            "Circular",
        )

    def test_subject_not_repeated_at_title_start(self):
        self.assertEqual(
            intelligence.sanitize_title(
                "Mathematics Correction in Q No. 11 Exercise 7.2",
                "Mathematics",
            ),
            "Correction in Q No. 11 Exercise 7.2",
        )

    def test_title_cleaner_is_idempotent(self):
        samples = [
            "Manav Mangal SMART SCHOOL-88 Circular Dear Students Good morning Kindly note the correction in Q no11 Exercise 7.2. Thanks",
            "Manav Mangal SMART SCHOOL-88 Circular Dear Parent, PFA of Circular No. 065 - MMSS88 2026-27",
            "Artificial Intelligence Chapter 2 Worksheet",
            "French unseen passage",
        ]
        for raw in samples:
            subject = intelligence.detect_subject(raw)
            once = intelligence.sanitize_title(raw, subject)
            twice = intelligence.sanitize_title(once, subject)
            self.assertEqual(once, twice)


    def test_ai_hard_filter_removes_dates_and_metadata(self):
        raw = (
            "Circular No. 065 - September 21, 2026 - "
            "Mathematics Exercise 7.2 Correction"
        )
        self.assertEqual(
            ai_title._final_title_cleanup(raw, "Mathematics"),
            "Exercise 7.2 Correction",
        )

    def test_ai_falls_back_cleanly_without_key(self):
        previous = ai_title.OPENROUTER_API_KEY
        try:
            ai_title.OPENROUTER_API_KEY = ""
            title = ai_title.generate_title(
                ["Dear Students September 21, 2026 Mathematics Exercise 7.2 Correction"],
                subject="Mathematics",
                fallback_title="Mathematics Exercise 7.2 Correction",
            )
            self.assertEqual(title, "Exercise 7.2 Correction")
        finally:
            ai_title.OPENROUTER_API_KEY = previous


if __name__ == "__main__":
    unittest.main()
