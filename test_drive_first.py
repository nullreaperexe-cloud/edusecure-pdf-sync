"""Regression checks for EduSecure -> Supabase first, Drive backup second.

Drive failures must never prevent verified new PDF/announcement publication.
"""
import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import supabase_materials
import supabase_announcements


class SupabaseFirstTests(unittest.TestCase):
    def test_material_upload_happens_before_optional_drive_backup(self):
        events = []
        payload = {"id": "m1", "title": "Science",
                   "pdf_url": "https://edusecure.org/science.pdf"}
        with (patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": "1"}),
              patch.object(supabase_materials, "build_material", return_value=payload),
              patch.object(supabase_materials.gateway, "call",
                           side_effect=lambda *a, **kw: (
                               events.append("supabase") or {
                                   "inserted": True, "record": {"pdf_url": payload["pdf_url"]}
                               }))):
            self.assertTrue(supabase_materials.upload_material(
                {"url": payload["pdf_url"]}, date(2026, 10, 20), 1))
        self.assertEqual(events, ["supabase"])

    def test_pdf_publish_error_is_not_hidden_by_backup(self):
        payload = {"id": "m1", "title": "Science",
                   "pdf_url": "https://edusecure.org/science.pdf"}
        with (patch.object(supabase_materials, "build_material", return_value=payload),
              patch.object(supabase_materials.gateway, "call",
                           side_effect=RuntimeError("Supabase write unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "Supabase write unavailable"):
                supabase_materials.upload_material(
                    {"url": payload["pdf_url"]}, date(2026, 10, 20), 1)

    def test_announcement_publishes_without_drive_credentials(self):
        events = []
        with (patch.dict(os.environ, {
                  "DRIVE_FIRST_REQUIRED": "1",
                  "GDRIVE_BACKUP_OAUTH_JSON": "",
                  "GDRIVE_BACKUP_REFRESH_TOKEN": ""}),
              patch.object(supabase_announcements.gateway, "call",
                           side_effect=lambda *a, **kw: (
                               events.append("supabase") or {"inserted": True}))):
            self.assertTrue(supabase_announcements.upload_announcement(
                {"sourceMessageId": "a-123", "title": "Reminder"},
                date(2026, 10, 20)))
        self.assertEqual(events, ["supabase"])

    def test_workflow_has_no_drive_first_publication_gate(self):
        workflow = Path(".github/workflows/sync.yml").read_text(encoding="utf-8")
        self.assertNotIn("DRIVE_FIRST_REQUIRED:", workflow)
        self.assertNotIn("drive_bootstrap", workflow)
        self.assertNotIn("Bootstrap complete Google Drive backup BEFORE", workflow)
        self.assertIn("python -u worker_loop.py", workflow)
        self.assertIn("GDRIVE_BACKUP_OAUTH_JSON:", workflow)
        self.assertIn("steps.worker.outcome == 'success'", workflow)
        self.assertNotIn("import drive_first", Path("supabase_materials.py").read_text())
        self.assertNotIn("import drive_first", Path("supabase_announcements.py").read_text())


if __name__ == "__main__":
    unittest.main()
