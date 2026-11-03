"""No-broken-title and exact EduSecure description regression tests."""
import unittest
from datetime import date

import school_text
import title_quality
import supabase_materials


class NonBrokenEduSecureTests(unittest.TestCase):
    def test_original_description_from_full_opened_school_message(self):
        preview = "For extra practice find the NCERT textbook Ch"
        detail = (
            "Home\nBack\n" + preview +
            "apter 6. PDF (Tales by Dots & Lines). Solve the extra questions.\n"
            "Download PDF"
        )
        # School detail has source instruction and must not be turned into
        # a 72-character AI title or contain UI controls.
        actual = school_text.source_pdf_description(
            preview, detail.replace("Home\nBack\n", "Home\n")
        )
        self.assertIn("Chapter 6", actual.replace("Ch" + "apter", "Chapter"))
        self.assertIn("Solve the extra questions", actual)
        self.assertNotIn("Download PDF", actual)

    def test_never_use_stale_edu_detail(self):
        preview = "Practice Exercise 6.2 questions in the notebook."
        stale = "Someone else's mathematics instructions. Open attachment."
        self.assertEqual(school_text.source_pdf_description(preview, stale), preview)

    def test_remove_date_but_keep_question_numbers_and_teacher_text(self):
        school = (
            "Exercise 6.1 Q10 - Exercise 6.2:Complete Q1 - 2 - 3 - "
            "4(a - b - c) - 5(a - b) Test of Chapter 5 on day 2(5-11-26)"
        )
        cleaned = school_text.clean_school_description(school)
        self.assertIn("Exercise 6.1 Q10", cleaned)
        self.assertIn("4(a - b - c)", cleaned)
        self.assertIn("day 2", cleaned)
        self.assertNotIn("5-11-26", cleaned)
        self.assertNotIn("…", cleaned)

    def test_no_date_formats_left_in_school_description(self):
        school = "Nov 3, 2026: Finish on 05/11/26; revision Nov 7. Exercise 6.2."
        cleaned = school_text.clean_school_description(school)
        self.assertNotIn("2026", cleaned)
        self.assertNotIn("05/11/26", cleaned)
        self.assertNotIn("Nov 7", cleaned)
        self.assertIn("Exercise 6.2", cleaned)

    def test_ncert_preview_cannot_end_with_broken_chapter(self):
        raw = "For extra practice find the NCERT textbook Ch"
        title = title_quality.contextual_pdf_title(
            raw, [raw, "For extra practice find the NCERT textbook Ch. PDF(Tales by Dots &"],
            "Mathematics",
        )
        self.assertEqual(title, "NCERT Textbook – Extra Practice")
        self.assertLessEqual(len(title), 72)

    def test_qualified_ncert_chapter_uses_real_number(self):
        raw = "For extra practice find the NCERT textbook Ch"
        title = title_quality.contextual_pdf_title(
            raw, [raw, "For extra practice find the NCERT textbook Chapter 6"],
            "Mathematics",
        )
        self.assertIn("Chapter 6", title)

    def test_math_homework_and_test_both_survive_short_title(self):
        raw = (
            "Exercise 6.1 Q10 - Exercise 6.2:Complete Q1 - 2 - 3 - "
            "4(a - b - c) - 5(a"
        )
        full = raw + " - b) Test of Chapter 5 on day 2(5-11-26) Find the"
        title = title_quality.contextual_pdf_title(raw, [raw, full], "Mathematics")
        self.assertIn("Exercise 6.1 Q10", title)
        self.assertIn("Exercise 6.2 Q1–5", title)
        self.assertIn("Chapter 5 Test", title)
        self.assertLessEqual(len(title), title_quality.MAX_TITLE_LENGTH)
        self.assertEqual(title.count("("), title.count(")"))

    def test_publisher_never_overwrites_school_description_with_title(self):
        item = {
            "title": "Exercise 6.1 Q10; Exercise 6.2 Q1–5",
            "description": (
                "Exercise 6.1 Q10 - Exercise 6.2:Complete Q1 - 2 - 3 - "
                "4(a - b - c) - 5(a - b). Test of Chapter 5 on 05/11/26."
            ),
            "subject": "Mathematics",
            "_description_from_school": True,
            "url": "https://example.org/secure-example.pdf",
        }
        result = supabase_materials.build_material(item, date(2026, 11, 3), 1)
        self.assertEqual(
            result["description"],
            "Exercise 6.1 Q10 - Exercise 6.2:Complete Q1 - 2 - 3 - "
            "4(a - b - c) - 5(a - b). Test of Chapter 5 on.",
        )
        self.assertNotEqual(result["title"], result["description"])
        self.assertEqual(result["pdf_url"], item["url"])
        self.assertTrue(result["id"].startswith("edusecure-"))


if __name__ == "__main__":
    unittest.main()
