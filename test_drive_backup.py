"""Offline tests for private 8aPDF Google Drive disaster-recovery backup."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import drive_backup as backup


class BackupTests(unittest.TestCase):
    def test_no_credentials_skips_without_blocking_edusecure(self):
        with tempfile.TemporaryDirectory() as d:
            with (patch.object(backup, "STATUS", Path(d) / "status.json"),
                  patch.dict(os.environ, {
                      "GDRIVE_BACKUP_CLIENT_ID": "",
                      "GDRIVE_BACKUP_CLIENT_SECRET": "",
                      "GDRIVE_BACKUP_REFRESH_TOKEN": "",
                  })):
                self.assertEqual(backup.backup(), 0)
                report = json.loads((Path(d) / "status.json").read_text())
                self.assertEqual(report["status"], "not_configured")

    def test_only_explicit_https_pdf_providers_are_fetched(self):
        with patch.dict(os.environ, {"GDRIVE_BACKUP_ALLOWED_PDF_HOSTS":
                                     "edusecure.org,files.example.org"}):
            self.assertTrue(backup.pdf_host_allowed("https://edusecure.org/doc.pdf"))
            self.assertTrue(backup.pdf_host_allowed("https://files.example.org/f.pdf"))
            self.assertFalse(backup.pdf_host_allowed("http://edusecure.org/f.pdf"))
            self.assertFalse(backup.pdf_host_allowed("https://edusecure.org.evil.test/f.pdf"))
            self.assertFalse(backup.pdf_host_allowed("https://127.0.0.1/f.pdf"))
            self.assertFalse(backup.pdf_host_allowed("https://user:pass@edusecure.org/f.pdf"))

    def test_json_deterministic(self):
        a = backup.compact_json({"records": [{"a": 2, "b": 3}]})
        b = backup.compact_json({"records": [{"b": 3, "a": 2}]})
        self.assertEqual(a, b)
        self.assertTrue(a.endswith(b"\n"))

    def test_full_snapshots_and_pdf_files_saved(self):
        class FakeDrive:
            def __init__(self):
                self.files = []
            def folder(self, parent, name):
                return "folder_" + name
            def list(self, parent, name=None):
                return []
            def put(self, parent, name, data, mime_type, index):
                self.files.append((parent, name, data, mime_type))
                index[name] = {"id": "new_" + name, "name": name,
                               "appProperties": {"sha256": backup.digest(data)}}
                return "new_" + name, True
        drive = FakeDrive()
        materials = [{"id": "m1", "title": "Test",
                      "pdf_url": "https://edusecure.org/test.pdf"}]
        announcements = [{"id": "a1", "title": "Notice", "raw": {"msg": "Hello"}}]
        with tempfile.TemporaryDirectory() as d:
            env = {
                "GDRIVE_BACKUP_CLIENT_ID": "fake",
                "GDRIVE_BACKUP_CLIENT_SECRET": "fake",
                "GDRIVE_BACKUP_REFRESH_TOKEN": "fake",
                "GDRIVE_BACKUP_MAX_PDFS_PER_RUN": "10",
                "GDRIVE_BACKUP_FOLDER_ID": "",
            }
            with (patch.object(backup, "STATUS", Path(d) / "status.json"),
                  patch.dict(os.environ, env),
                  patch.object(backup, "Drive", return_value=drive),
                  patch.object(backup, "read_inventory",
                               side_effect=[materials, announcements]),
                  patch.object(backup, "fetch_pdf", return_value=b"%PDF-1.7\nexample") as fetch):
                self.assertEqual(backup.backup(), 0)
                report = json.loads((Path(d) / "status.json").read_text())
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["materials"], 1)
            self.assertEqual(report["announcements"], 1)
            self.assertEqual(report["pdfs_copied"], 1)
            self.assertEqual(fetch.call_count, 1)
            names = [x[1] for x in drive.files]
            self.assertIn("materials.json", names)
            self.assertIn("announcements.json", names)
            self.assertIn("backup-manifest.json", names)
            self.assertIn("folder_PDFs", [x[0] for x in drive.files if x[1].endswith(".pdf")])
            row = next(data for _, name, data, _ in drive.files if name == "announcements.json")
            self.assertEqual(json.loads(row)["records"], announcements)

    def test_supabase_error_does_not_crash_primary_pipeline(self):
        with tempfile.TemporaryDirectory() as d:
            with (patch.object(backup, "STATUS", Path(d) / "status.json"),
                  patch.object(backup, "backup", side_effect=RuntimeError("network"))):
                self.assertEqual(backup.main(), 1)
                self.assertEqual(json.loads((Path(d) / "status.json").read_text())["status"],
                                 "failed")


if __name__ == "__main__":
    unittest.main()
