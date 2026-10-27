"""Offline tests for private 8aPDF Google Drive disaster-recovery backup."""
import json
import io
from contextlib import redirect_stdout
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
                      "GDRIVE_BACKUP_OAUTH_JSON": "",
                      "GDRIVE_BACKUP_CLIENT_ID": "",
                      "GDRIVE_BACKUP_CLIENT_SECRET": "",
                      "GDRIVE_BACKUP_REFRESH_TOKEN": "",
                  })):
                self.assertEqual(backup.backup(), 1)
                report = json.loads((Path(d) / "status.json").read_text())
                self.assertEqual(report["status"], "not_configured")


    def test_single_json_secret_with_refresh_token(self):
        config = {"client_id": "abc.apps.googleusercontent.com",
                  "client_secret": "not-public",
                  "refresh_token": "refresh"}
        with patch.dict(os.environ, {"GDRIVE_BACKUP_OAUTH_JSON": json.dumps(config)}):
            self.assertEqual(backup.configuration(), [])
            self.assertEqual(backup.oauth_credentials(), config)

    def test_google_cloud_downloaded_json_without_refresh_token_rejected(self):
        cloud_client_file = {
            "web": {"client_id": "abc.apps.googleusercontent.com",
                    "client_secret": "client-secret",
                    "redirect_uris": ["https://developers.google.com/oauthplayground"]},
        }
        with patch.dict(os.environ, {"GDRIVE_BACKUP_OAUTH_JSON": json.dumps(cloud_client_file)}):
            self.assertTrue(backup.configuration())
            with self.assertRaisesRegex(ValueError, "refresh_token"):
                backup.oauth_credentials()

    def test_invalid_json_does_not_expose_secret_in_error(self):
        private = "super-secret"
        with patch.dict(os.environ, {"GDRIVE_BACKUP_OAUTH_JSON": private}):
            self.assertTrue(backup.configuration())
            with self.assertRaises(ValueError) as raised:
                backup.oauth_credentials()
            self.assertNotIn(private, str(raised.exception))

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

    def test_existing_drive_pdf_is_skipped_without_download_or_duplicate(self):
        url = "https://edusecure.org/already-present.pdf"
        filename = backup.hashlib.sha256(url.encode("utf-8")).hexdigest() + ".pdf"

        class FakeDrive:
            def __init__(self):
                self.uploads = []
            def folder(self, parent, name):
                return "folder_" + name
            def list(self, parent, name=None):
                if parent == "folder_PDFs":
                    return [{"name": filename, "id": "existing-pdf", "mimeType": "application/pdf"}]
                return []
            def put(self, parent, name, data, mime_type, index):
                self.uploads.append((parent, name))
                return "json-" + name, True

        fake = FakeDrive()
        with tempfile.TemporaryDirectory() as d:
            with (patch.object(backup, "STATUS", Path(d) / "status.json"),
                  patch.dict(os.environ, {
                      "GDRIVE_BACKUP_OAUTH_JSON": json.dumps({
                          "client_id": "fake", "client_secret": "fake",
                          "refresh_token": "fake",
                      }),
                      "GDRIVE_BACKUP_FOLDER_ID": "",
                  }),
                  patch.object(backup, "Drive", return_value=fake),
                  patch.object(backup, "read_inventory", side_effect=[
                      [{"id": "existing", "pdf_url": url}],
                      [{"id": "announcement", "title": "Already in Supabase"}],
                  ]),
                  patch.object(backup, "fetch_pdf") as fetch):
                self.assertEqual(backup.backup(), 0)
                report = json.loads((Path(d) / "status.json").read_text())
            fetch.assert_not_called()
            self.assertEqual(report["pdfs_already_present"], 1)
            self.assertEqual(report["pdfs_copied"], 0)
            self.assertFalse(any(name == filename for _, name in fake.uploads))

    def test_oauth_refresh_failure_reports_safe_google_code_without_secret(self):
        secret = "PRIVATE_REFRESH_TOKEN_MUST_NOT_APPEAR"
        class FailedResponse:
            status_code = 400
            def __init__(self):
                self.closed = False
            def json(self):
                return {"error": "invalid_grant", "error_description": secret}
            def close(self):
                self.closed = True
        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.response = FailedResponse()
            def request(self, method, url, **kwargs):
                return self.response
        session = FakeSession()
        with patch.dict(os.environ, {
            "GDRIVE_BACKUP_OAUTH_JSON": json.dumps({
                "client_id": "test-client", "client_secret": "test-secret",
                "refresh_token": secret
            }),
        }):
            with self.assertRaises(backup.OAuthRefreshError) as error:
                backup.Drive(session=session)
        self.assertEqual(error.exception.oauth_error, "invalid_grant")
        self.assertEqual(error.exception.http_status, 400)
        self.assertTrue(session.response.closed)
        self.assertNotIn(secret, str(error.exception))
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (patch.object(backup, "STATUS", Path(directory) / "report.json"),
                  patch.object(backup, "BACKUP_STAGE", "oauth_refresh"),
                  patch.object(backup, "backup", side_effect=error.exception),
                  redirect_stdout(output)):
                self.assertEqual(backup.main(), 1)
            report = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(report["oauth_error"], "invalid_grant")
            self.assertEqual(report["stage"], "oauth_refresh")
            self.assertEqual(report["http_status"], 400)
            self.assertNotIn(secret, output.getvalue())

    def test_unknown_google_error_code_is_not_logged(self):
        error = backup.OAuthRefreshError(400, "MY_PRIVATE_UNRECOGNIZED_TOKEN")
        self.assertEqual(error.oauth_error, "unknown")
        self.assertNotIn("PRIVATE", str(error))

    def test_supabase_error_does_not_crash_primary_pipeline(self):
        with tempfile.TemporaryDirectory() as d:
            with (patch.object(backup, "STATUS", Path(d) / "status.json"),
                  patch.object(backup, "backup", side_effect=RuntimeError("network"))):
                self.assertEqual(backup.main(), 1)
                self.assertEqual(json.loads((Path(d) / "status.json").read_text())["status"],
                                 "failed")


if __name__ == "__main__":
    unittest.main()
