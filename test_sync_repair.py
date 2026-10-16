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


    def test_live_sync_does_not_start_browser_if_supabase_read_fails(self):
        with (
            patch.object(repair.runner, "EDUSECURE_USERNAME", "user"),
            patch.object(repair.runner, "EDUSECURE_PASSWORD", "pass"),
            patch.object(repair.runner.supabase_gateway, "health"),
            patch.object(repair.runner, "list_firestore_materials", side_effect=RuntimeError("Supabase read blocked")),
            patch.object(repair.runner.legacy, "make_driver") as browser,
            patch.object(repair.runner, "save_report"),
        ):
            self.assertEqual(repair.runner.main(), 2)
            browser.assert_not_called()

    def test_supabase_inventory_never_uses_firestore_mutations(self):
        records=[{"id":"legacy-1","pdf_url":"https://example.org/one.pdf","title":"One"}]
        with (patch.object(repair, "ORIGINAL_LIST", return_value=records),
              patch.object(repair, "_save_cached_materials"),
              patch.object(repair, "firestore_delete_document") as delete,
              patch.object(repair, "firestore_update_subject") as update):
            self.assertEqual(repair.list_supabase_materials_safely(""),records)
            delete.assert_not_called()
            update.assert_not_called()


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



class AttachmentRecoveryTests(unittest.TestCase):
    def test_embedded_pdf_recovers_when_school_has_no_attachment_anchor(self):
        url = "https://edusecure.org/StudentInfo/Homework/example.pdf"
        with (
            patch.object(repair, "exact_attachment_anchors", return_value=[]),
            patch.object(repair.runner.legacy, "extract_pdf_from_current_page", return_value=url),
            patch.object(repair, "ORIGINAL_ATTACHMENT_EXTRACT") as fallback,
        ):
            self.assertEqual(repair.extract_attachment_url(Mock(), "main"), url)
        fallback.assert_not_called()

    def test_aspnet_attachment_button_fallback(self):
        url = "https://edusecure.org/StudentInfo/Homework/download.pdf"
        with (
            patch.object(repair, "exact_attachment_anchors", return_value=[]),
            patch.object(repair.runner.legacy, "extract_pdf_from_current_page", return_value=None),
            patch.object(repair, "ORIGINAL_ATTACHMENT_EXTRACT", return_value=url),
        ):
            self.assertEqual(repair.extract_attachment_url(Mock(), "main"), url)

    def test_same_message_can_contain_different_pdf_urls(self):
        first = "https://edusecure.org/StudentInfo/Homework/chapter1.pdf"
        second = "https://edusecure.org/StudentInfo/Homework/chapter2.pdf"
        with (
            patch.object(repair, "exact_attachment_anchors", return_value=[{"id": "attachment", "href": first}]),
            patch.object(repair, "right_click_open_link_in_new_tab", side_effect=[first, second]),
        ):
            repair.SEEN_MESSAGE_SIGNATURES.add("same description")
            self.assertEqual(repair.extract_attachment_url(Mock(), "main"), first)
            self.assertEqual(repair.extract_attachment_url(Mock(), "main"), second)
            repair.SEEN_MESSAGE_SIGNATURES.clear()

    def test_rejects_non_document_portal_fallback_url(self):
        with (
            patch.object(repair, "exact_attachment_anchors", return_value=[]),
            patch.object(repair.runner.legacy, "extract_pdf_from_current_page", return_value=None),
            patch.object(repair, "ORIGINAL_ATTACHMENT_EXTRACT",
                         return_value="https://edusecure.org/ManavMangal88/ParentApp/Dashboard.aspx"),
        ):
            self.assertIsNone(repair.extract_attachment_url(Mock(), "main"))


if __name__ == "__main__":
    unittest.main()
