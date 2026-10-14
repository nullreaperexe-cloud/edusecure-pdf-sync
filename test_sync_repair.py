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
            self.assertEqual(repair.runner.main(), 0)


if __name__ == "__main__":
    unittest.main()
