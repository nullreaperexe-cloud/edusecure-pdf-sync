import unittest
from unittest.mock import patch, Mock

import pdf_integrity as integrity


class PdfIntegrityTests(unittest.TestCase):
    CHESS_PDF_TEXT = (
        "MMSS88 / 2026-27 / 069 / October 01, 2026\n"
        "manav mangal SMART SCHOOL - 88\n"
        "Shining Manavite\nMst. K. S. Thanuram\nVI B\n"
        "secured Classical Chess FIDE rating of 1465\n"
        "won SECOND PRIZE in Interschool Chess Tournament."
    )
    CHESS_MESSAGE = (
        "Circular Oct 01, 2026 Dear Parent PFA - Circular No. 069 "
        "- Shining Manavite - Mst. K. S. Thanuram"
    )

    def test_false_social_science_title_is_blocked(self):
        title, reason = integrity.select_verified_title(
            self.CHESS_MESSAGE,
            "Class 8 Social Science Project Guidelines",
            "Shining Manavite",
            self.CHESS_PDF_TEXT,
        )
        self.assertEqual(title, "Interschool Chess Tournament Achievement")
        self.assertEqual(reason, "document-topic-override")

    def test_correct_chess_title_is_preserved(self):
        title, reason = integrity.select_verified_title(
            self.CHESS_MESSAGE,
            "Chess Tournament Prize", "Shining Manavite",
            self.CHESS_PDF_TEXT,
        )
        self.assertEqual(title, "Chess Tournament Prize")
        self.assertEqual(reason, "pdf-and-ai-agree")

    def test_unreadable_pdf_does_not_authorize_unrelated_topic(self):
        title, reason = integrity.select_verified_title(
            "Science worksheet for Chapter 4",
            "Class 8 Social Science Project Guidelines",
            "Science Worksheet Chapter 4",
            "",
        )
        self.assertEqual(title, "Science Worksheet Chapter 4")
        self.assertEqual(reason, "unverified-ai-title")

    def test_matching_topic_passes_evidence_gate(self):
        title, reason = integrity.select_verified_title(
            "Science project on renewable energy",
            "Renewable Energy Science Project", "Science Project",
            "Renewable energy: solar and wind science project guidelines.",
        )
        self.assertEqual(title, "Renewable Energy Science Project")
        self.assertEqual(reason, "verified-evidence-overlap")

    @patch("pdf_integrity.requests.get")
    def test_broken_pdf_link_is_not_trusted(self, get):
        resp = Mock()
        resp.headers = {}
        resp.iter_content.return_value = [b"<html>Not a PDF</html>"]
        get.return_value = resp
        self.assertEqual(
            integrity.fetch_pdf_text("https://example.org/bad.pdf"),
            ("", "not-a-pdf"),
        )


if __name__ == "__main__":
    unittest.main()
