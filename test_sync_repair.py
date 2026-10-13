import unittest

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


if __name__ == "__main__":
    unittest.main()
