import unittest
from unittest.mock import Mock, patch

import requests
import sync_repair as repair


class SyncRepairTitleGrammarTests(unittest.TestCase):
    def test_removes_observed_dangling_from(self):
        self.assertEqual(
            repair.clean_title_grammar("Regarding Short Leaves From"),
            "Regarding Short Leaves",
        )

    def test_preserves_valid_internal_from(self):
        self.assertEqual(
            repair.clean_title_grammar("Letter From Principal"),
            "Letter From Principal",
        )

    def test_shared_sanitizer_uses_runtime_grammar_guard(self):
        self.assertEqual(
            repair.runner.intelligence.sanitize_title(
                "Regarding Short Leaves From",
                "Circular",
            ),
            "Regarding Short Leaves",
        )


    def test_live_sync_defers_safely_on_firestore_429(self):
        response = Mock()
        response.status_code = 429
        error = requests.HTTPError("quota", response=response)
        with (
            patch.object(repair.runner, "EDUSECURE_USERNAME", "user"),
            patch.object(repair.runner, "EDUSECURE_PASSWORD", "pass"),
            patch.object(repair.runner, "firebase_sign_in", return_value="token"),
            patch.object(repair.runner, "list_firestore_materials", side_effect=error),
        ):
            self.assertEqual(repair.runner.main(), 75)


    def test_existing_dangling_from_record_is_patched_narrowly(self):
        material = {
            "_name": "projects/test/databases/(default)/documents/study_materials/1",
            "source": "EduSecure GitHub Sync",
            "title": "Regarding Short Leaves From",
            "description": "Regarding Short Leaves From",
            "subject": "Circular",
            "pdf_url": "https://example.com/a.pdf",
            "source_date": "2026-10-14T00:00:00Z",
            "order": 1,
        }
        with (
            patch.object(repair, "_load_cached_materials", side_effect=[None, None]),
            patch.object(repair, "_material_cooldown_active", return_value=False),
            patch.object(repair, "ORIGINAL_LIST", return_value=[material]),
            patch.object(repair, "_save_cached_materials"),
            patch.object(
                repair.runner.intelligence,
                "patch_material_fields",
                return_value=True,
            ) as mocked_patch,
        ):
            result = repair.list_and_cleanup_materials("token")

        self.assertEqual(result[0]["title"], "Regarding Short Leaves")
        mocked_patch.assert_called_once()
        self.assertEqual(mocked_patch.call_args.kwargs["title"], "Regarding Short Leaves")
        self.assertEqual(
            mocked_patch.call_args.kwargs["description"],
            "Regarding Short Leaves",
        )


if __name__ == "__main__":
    unittest.main()
