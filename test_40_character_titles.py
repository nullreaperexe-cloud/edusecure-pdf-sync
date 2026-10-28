"""Regression coverage for EduSecure 40-character titles and full-message AI input."""
import unittest
from datetime import date
from unittest.mock import patch

import title_quality as quality
import groq_title as groq
import announcement_processor as processor
import supabase_announcements
import supabase_materials


class FortyCharacterTitleTests(unittest.TestCase):
    def test_real_broken_screenshot_titles(self):
        cases = [
            ("complete the back exercises in your own. Class",
             "Complete the back exercises"),
            ("English- Bring literature notebook and BBC Compacta module 7",
             "Bring BBC Compacta module 7"),
            ("bring unchecked English notebooks tomorrow and come",
             "Bring unchecked English notebooks"),
            ("Computer - Write/print programs of chapter 6 in notebook", None),
            ("Fractions in Disguise Worksheet Answer Key",
             "Fractions in Disguise Answer Key"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = quality.fit_title(raw)
                self.assertLessEqual(len(result), 40)
                self.assertFalse(result.endswith((" and", " of", " to", " for")))
                self.assertNotIn("...", result)
                if expected:
                    self.assertEqual(result, expected)

    def test_complete_notice_recovers_topic_from_full_message(self):
        preview = "we are delighted to announce the results of"
        detail = (
            "Circular Oct 28, 2026 we are delighted to announce the results "
            "of Perfecting Warli Arts. The results are as follows: 1st position"
        )
        result = quality.fit_title(preview, source_text=detail)
        self.assertEqual(result, "Perfecting Warli Arts Results")

    def test_long_titles_never_split_a_word(self):
        text = "Bring your literature notebook and BBC Compacta module 7 on Saturday"
        value = quality.fit_title(text)
        self.assertLessEqual(len(value), 40)
        self.assertNotIn("\u2026", value)
        self.assertFalse(value.endswith((" and", " the", " of")))
        self.assertIn("BBC", value)

    def test_native_script_stays_native(self):
        value = quality.fit_title("ਪੰਜਾਬੀ ਪਾਠ ਪੜ੍ਹਨ ਲਈ ਕਿਰਪਾ ਕਰਕੇ ਪੂਰੀ ਤਿਆਰੀ ਨਾਲ ਸਕੂਲ ਆਉਣਾ")
        self.assertLessEqual(len(value), 40)
        self.assertTrue(any("\u0a00" <= c <= "\u0a7f" for c in value))

    def test_groq_validation_limits_long_output(self):
        from announcement_processor import ALLOWED_CATEGORIES
        meta = groq._validate_announcement_meta(
            {"title": "bring unchecked English notebooks tomorrow and come prepared",
             "category": "Activities", "subject": "English", "priority": "normal"},
            "Bring unchecked English notebooks tomorrow and come prepared.",
            list(ALLOWED_CATEGORIES), "test-groq",
        )
        self.assertIsNotNone(meta)
        self.assertLessEqual(len(meta["title"]), 40)
        self.assertIn("notebooks", meta["title"].lower())

    def test_prompt_examples_and_hard_rule(self):
        from announcement_processor import ALLOWED_CATEGORIES
        prompt = groq._announcement_system_prompt(list(ALLOWED_CATEGORIES))
        self.assertIn("40 Unicode characters", prompt)
        self.assertIn("FULL message", prompt)
        self.assertIn("Perfecting Warli Arts Results", prompt)

    def test_builder_passes_full_detail_even_when_preview_is_long(self):
        preview = "we are delighted to announce the results of"
        detail = preview + " Perfecting Warli Arts. First position: Student A"
        with patch.object(processor.ai_title, "generate_announcement_metadata", return_value={
            "title": "Perfecting Warli Arts Results",
            "category": "Results", "subject": "General",
            "priority": "normal", "aiModel": "mock",
        }) as call:
            result = processor.build_announcement(preview, detail, date(2026, 10, 28))
        self.assertEqual(result["title"], "Perfecting Warli Arts Results")
        evidence = call.call_args.args[0]
        self.assertEqual(len(evidence), 2)
        self.assertIn("Perfecting Warli Arts", evidence[1])

    def test_ai_outage_uses_meaningful_safe_fallback(self):
        preview = "we are delighted to announce the results of"
        detail = preview + " Perfecting Warli Arts. The results are as follows."
        with patch.object(processor.ai_title, "generate_announcement_metadata", return_value=None):
            item = processor.build_announcement(preview, detail, date(2026, 10, 28))
        self.assertEqual(item["aiStatus"], "pending")
        self.assertEqual(item["title"], "Perfecting Warli Arts Results")

    def test_supabase_announcement_final_gate(self):
        seen = {}
        def fake_call(action, **kwargs):
            seen.update(kwargs["record"])
            return {"inserted": True}
        item = {
            "sourceMessageId": "fixed-test-id",
            "title": "bring unchecked English notebooks tomorrow and come prepared",
            "description": "Bring unchecked English notebooks tomorrow and come prepared.",
            "originalMessage": "Bring unchecked English notebooks tomorrow and come prepared.",
            "category": "Activities", "subject": "English",
        }
        with patch.object(supabase_announcements.gateway, "call", side_effect=fake_call):
            self.assertTrue(supabase_announcements.upload_announcement(item, date(2026,10,28)))
        self.assertLessEqual(len(seen["title"]), 40)
        self.assertEqual(seen["raw"]["title"], seen["title"])
        self.assertIn("tomorrow", seen["description"])

    def test_pdf_pub_gate_keeps_url_and_identity(self):
        item = {
            "title": "Fractions in Disguise Worksheet Answer Key",
            "description": "Fractions in Disguise Worksheet Answer Key",
            "subject": "Mathematics",
            "url": "https://example.org/study.pdf",
        }
        result = supabase_materials.build_material(item, date(2026, 10, 28), 1)
        self.assertLessEqual(len(result["title"]), 40)
        self.assertEqual(result["pdf_url"], "https://example.org/study.pdf")


if __name__ == "__main__":
    unittest.main()
