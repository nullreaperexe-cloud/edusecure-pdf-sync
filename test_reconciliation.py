"""Regression tests: historic missed records must be eligible without duplication."""
import unittest
from datetime import date

import runner


class ReconciliationEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.pdf_cutoff = date(2026, 10, 6)
        self.announcement_cutoff = date(2026, 10, 20)

    def eligible(self, d, full=False, history=True):
        return runner.eligible_for_recovery(
            d,
            pdf_cutoff=self.pdf_cutoff,
            announcement_cutoff=self.announcement_cutoff,
            known_pdf_history=history,
            full_history=full,
        )

    def test_normal_mode_rejects_old_records(self):
        self.assertEqual(self.eligible(date(2026, 9, 1)), (False, False))

    def test_full_history_mode_recovers_old_pdfs_and_announcements(self):
        self.assertEqual(self.eligible(date(2026, 9, 1), full=True), (True, True))

    def test_undated_cannot_be_published_as_pdf(self):
        self.assertEqual(self.eligible(None, full=True), (False, True))
        self.assertEqual(self.eligible(None), (False, True))

    def test_normal_recent_record_remains_eligible(self):
        self.assertEqual(self.eligible(date(2026, 10, 25)), (True, True))

    def test_first_time_older_than_safe_cutoff_not_eligible(self):
        self.assertEqual(self.eligible(date(2026, 10, 6), history=False),
                         (False, False))

if __name__ == "__main__":
    unittest.main()
