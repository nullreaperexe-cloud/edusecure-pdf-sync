"""Regression tests for mandatory Google Drive-first publication.

These use fake Drive/inventory interactions; no real user tokens are exposed.
"""
import os
import unittest
from datetime import date
from unittest.mock import patch

import drive_first
import supabase_materials
import supabase_announcements


class DriveFirstTests(unittest.TestCase):
    def tearDown(self):
        drive_first._DRIVE = None
        drive_first._ROOT = None
        drive_first._FOLDER_CACHE.clear()

    def test_requires_enabled_flag(self):
        with patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": ""}):
            self.assertIsNone(drive_first.prepare_material({"id": "m1", "pdf_url": "https://edusecure.org/x.pdf"}))

    def test_google_drive_folder_created_without_preconfigured_id(self):
        class FakeDrive:
            def __init__(self):
                self.calls = []
            def folder(self, parent, name):
                self.calls.append((parent, name))
                return "new-folder-" + name
        d = FakeDrive()
        with (patch.object(drive_first, "Drive", return_value=d),
              patch.dict(os.environ, {"GDRIVE_BACKUP_FOLDER_ID": ""})):
            _, root = drive_first.require_drive()
        self.assertEqual(root, "new-folder-8aPDF_Backup")
        self.assertEqual(d.calls, [("root", "8aPDF_Backup")])

    def test_successful_pdf_original_and_record_precede_supabase(self):
        events = []
        class FakeDrive:
            def folder(self, parent, name):
                return "id-" + name
            def list(self, parent, name=None):
                return []
            def put(self, parent, name, data, mime_type, index):
                events.append("drive-" + mime_type)
                return "drive-file-123", True
        item = {"title": "Science", "subject": "Science",
                "url": "https://edusecure.org/new.pdf"}
        with (patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": "1",
                                      "GDRIVE_BACKUP_FOLDER_ID": ""}),
              patch.object(drive_first, "Drive", return_value=FakeDrive()),
              patch.object(drive_first, "fetch_pdf", return_value=b"%PDF-1.7\nbody"),
              patch.object(supabase_materials.gateway, "call",
                           side_effect=lambda action, **kw: (
                               events.append("supabase") or
                               {"ok": True, "inserted": True,
                                "record": {"pdf_url": item["url"]}}
                           ))):
            self.assertTrue(supabase_materials.upload_material(item, date(2026, 10, 20), -5))
        self.assertEqual(events, ["drive-application/pdf", "drive-application/json", "supabase"])

    def test_failed_pdf_download_never_calls_supabase(self):
        item = {"title": "Science", "subject": "Science",
                "url": "https://edusecure.org/protected.pdf"}
        class FakeDrive:
            def folder(self, parent, name):
                return "id-" + name
            def list(self, parent, name=None):
                return []
        with (patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": "1",
                                      "GDRIVE_BACKUP_FOLDER_ID": ""}),
              patch.object(drive_first, "Drive", return_value=FakeDrive()),
              patch.object(drive_first, "fetch_pdf", side_effect=ValueError("Protected PDF")),
              patch.object(supabase_materials.gateway, "call") as api):
            with self.assertRaisesRegex(ValueError, "Protected PDF"):
                supabase_materials.upload_material(item, date(2026, 10, 20), 1)
            api.assert_not_called()

    def test_unconfigured_drive_never_calls_supabase(self):
        item = {"title": "Science", "subject": "Science",
                "url": "https://edusecure.org/new.pdf"}
        with (patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": "1",
                                      "GDRIVE_BACKUP_OAUTH_JSON": "",
                                      "GDRIVE_BACKUP_CLIENT_ID": "",
                                      "GDRIVE_BACKUP_CLIENT_SECRET": "",
                                      "GDRIVE_BACKUP_REFRESH_TOKEN": ""}),
              patch.object(supabase_materials.gateway, "call") as api):
            with self.assertRaises(ValueError):
                supabase_materials.upload_material(item, date(2026, 10, 20), 1)
            api.assert_not_called()

    def test_announcement_staged_before_publication(self):
        events = []
        class FakeDrive:
            def folder(self, parent, name):
                return "id-" + name
            def list(self, parent, name=None):
                return []
            def put(self, parent, name, data, mime_type, index):
                events.append("drive")
                return "id", True
        with (patch.dict(os.environ, {"DRIVE_FIRST_REQUIRED": "1",
                                      "GDRIVE_BACKUP_FOLDER_ID": ""}),
              patch.object(drive_first, "Drive", return_value=FakeDrive()),
              patch.object(supabase_announcements.gateway, "call",
                           side_effect=lambda *a, **kw: events.append("supabase") or
                           {"ok": True, "inserted": True})):
            self.assertTrue(supabase_announcements.upload_announcement(
                {"sourceMessageId": "a-123", "title": "Reminder"}, date(2026, 10, 20)))
        self.assertEqual(events, ["drive", "supabase"])


if __name__ == "__main__":
    unittest.main()
