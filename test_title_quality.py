import json
import unittest
from unittest.mock import Mock, patch

import groq_title as title_ai
import title_quality as quality
import supabase_title_cleanup as historical


class TitleQualityTests(unittest.TestCase):
    def test_strip_attached_pdf_boilerplate(self):
        self.assertEqual(
            quality.polish_title("find attached the PDF of chapter 12", "English"),
            "Chapter 12",
        )

    def test_chapter_notes_grammar(self):
        self.assertEqual(quality.polish_title("notes of chapter 6", "Science"), "Chapter 6 Notes")

    def test_preserves_non_english_script(self):
        self.assertEqual(
            quality.polish_title("ਪਾਠ ਮਾਂ ਦਾ ਸਾਰ", "Punjabi"),
            "ਪਾਠ ਮਾਂ ਦਾ ਸਾਰ",
        )

    def test_rejects_invented_lesson_or_chapter(self):
        self.assertFalse(quality.supported_by_evidence(
            "Chapter 16 Geometry Revision",
            "Chapter 6 Science Notes",
        ))
        self.assertFalse(quality.supported_by_evidence(
            "Interschool Chess Championship",
            "English Grammar Worksheet",
        ))

    def test_live_teacher_message_templates(self):
        cases = [
            ("find the attached Answer Key of Worksheet on Fractions in Disguise. Reminder:Test of Chapter 1",
             "Fractions in Disguise Worksheet Answer Key"),
            ("Question answers of chapter 12 were done (PDF attached)",
             "Chapter 12 Question Answers"),
            ("practice the worksheet of Mathematics Chapter Fraction in Disguise in a separate notebook. Maths",
             "Fraction in Disguise Worksheet"),
            ("bring English literature book and notebook tomorrow. Also - find attached the PDF",
             "English literature book and notebook"),
        ]
        for before, after in cases:
            with self.subTest(before=before):
                self.assertEqual(quality.polish_title(before), after)
                self.assertTrue(quality.supported_by_evidence(after, before))

    def test_never_translate_hindi_or_punjabi_title_into_english(self):
        self.assertFalse(quality.supported_by_evidence(
            "Class Revision: Lesson Tomorrow",
            'प्रिय छात्रों कक्षा पुनरावृत्ति "मत बांधो" पाठ का कल',
        ))
        self.assertFalse(quality.supported_by_evidence(
            "Lesson Summary and Character Study",
            "ਪਾਠ ਮਾਂ ਦਾ ਸਾਰ ਰਾਵਤ ਦਾ ਪਾਤਰ ਚਿਤਰਨ",
        ))

    def test_grounded_title_is_allowed(self):
        self.assertTrue(quality.supported_by_evidence(
            "Fractions in Disguise Worksheet",
            "practice the worksheet of Mathematics Chapter Fraction in Disguise",
        ))

    def test_groq_auto_json_generates_short_grounded_title(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {
            "model": "llama-3.3-70b-versatile",
            "choices": [{"message": {"content": '{"title":"Chapter 12 Question Answers"}'}}],
        }
        with (patch.object(title_ai, "GROQ_API_KEY", "test-placeholder"), patch.object(title_ai, "_MODEL_CACHE", ("llama-3.3-70b-versatile",))):
            with patch.object(title_ai.requests, "post", return_value=response) as send:
                got = title_ai.generate_title(
                    "Question answers of chapter 12 were done (PDF attached)",
                    subject="English",
                    fallback_title="Question answers of chapter 12 were done (PDF attached)",
                )
        self.assertEqual(got, "Chapter 12 Question Answers")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs["json"]["model"], "llama-3.3-70b-versatile")
        self.assertEqual(send.call_args.kwargs["json"]["response_format"], {"type": "json_object"})

    def test_invalid_unrelated_groq_title_uses_local_fallback(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"choices": [{"message": {"content": '{"title":"Chess Championship"}'}}]}
        with (patch.object(title_ai, "GROQ_API_KEY", "test-placeholder"), patch.object(title_ai, "_MODEL_CACHE", ("llama-3.3-70b-versatile",))):
            with patch.object(title_ai.requests, "post", return_value=response):
                got = title_ai.generate_title(
                    "notes of chapter 6",
                    subject="Science",
                    fallback_title="notes of chapter 6",
                )
        self.assertEqual(got, "Chapter 6 Notes")

    def test_429_quota_exceeded_does_not_retry(self):
        response = Mock(status_code=429, ok=False)
        with (patch.object(title_ai, "GROQ_API_KEY", "test-placeholder"), patch.object(title_ai, "_MODEL_CACHE", ("llama-3.3-70b-versatile",))):
            with patch.object(title_ai.requests, "post", return_value=response) as send:
                got = title_ai.generate_title(
                    "notes of chapter 6", "Science", "notes of chapter 6"
                )
        self.assertEqual(got, "Chapter 6 Notes")
        self.assertEqual(send.call_count, 1)

    def test_groq_auto_models_discovers_text_models_only(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"data": [
            {"id": "whisper-large-v3"},
            {"id": "llama-3.1-8b-instant"},
            {"id": "openai/gpt-oss-20b"},
        ]}
        with (patch.object(title_ai, "GROQ_API_KEY", "test-key"),
              patch.object(title_ai, "_MODEL_CACHE", None),
              patch.object(title_ai.requests, "get", return_value=response) as get):
            models = title_ai._auto_models()
        self.assertEqual(models[0], "openai/gpt-oss-20b")
        self.assertNotIn("whisper-large-v3", models)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[0], title_ai.GROQ_MODELS_URL)
        response.close.assert_called_once()

    def test_groq_request_never_uses_literal_auto_or_legacy_provider(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {
            "choices": [{"message": {"content": '{"title":"Chapter 12 Question Answers"}'}}]}
        with (patch.object(title_ai, "GROQ_API_KEY", "test-key"),
              patch.object(title_ai, "_MODEL_CACHE", ("llama-3.3-70b-versatile",)),
              patch.object(title_ai.requests, "post", return_value=response) as send):
            title_ai.generate_title("Chapter 12 Question Answers", "English", "Chapter 12 Question Answers")
        self.assertEqual(send.call_args.args[0], title_ai.GROQ_CHAT_URL)
        self.assertNotEqual(send.call_args.kwargs["json"]["model"], "auto")
        self.assertEqual(send.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_groq_announcement_classification_keeps_grammar_rules(self):
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"model": "llama-3.3-70b-versatile",
            "choices": [{"message": {"content":
            '{"title":"Computer Revision Test","category":"Tests","subject":"Computer","priority":"normal"}'
            }}]}
        with (patch.object(title_ai, "GROQ_API_KEY", "fake-key"),
              patch.object(title_ai, "_MODEL_CACHE", ("llama-3.3-70b-versatile",)),
              patch.object(title_ai.requests, "post", return_value=response) as send):
            from announcement_processor import ALLOWED_CATEGORIES
            result = title_ai.generate_announcement_metadata("Prepare for the Computer revision test", ALLOWED_CATEGORIES)
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "Tests")
        self.assertEqual(result["aiModel"], "llama-3.3-70b-versatile")
        self.assertEqual(send.call_args.args[0], title_ai.GROQ_CHAT_URL)

    def test_existing_title_cleanup_is_pure_without_ai(self):
        item = {
            "title": "notes of chapter 6",
            "subject": "Science",
            "description": "notes of chapter 6",
        }
        got, used_ai = historical.choose_title(item, allow_ai=False)
        self.assertEqual(got, "Chapter 6 Notes")
        self.assertFalse(used_ai)


if __name__ == "__main__":
    unittest.main()
