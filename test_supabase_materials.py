"""Offline regression tests for Supabase PDF migration and no-duplicate writes."""
import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

import requests

import supabase_materials as store


def response(status=200, data=None):
    obj = Mock()
    obj.ok = status < 400
    obj.status_code = status
    obj.raise_for_status.side_effect = requests.HTTPError(response=obj) if status >= 400 else None
    obj.json.return_value = [] if data is None else data
    return obj


class SupabaseMaterialsTests(unittest.TestCase):
    def setUp(self):
        self.key = patch.dict(os.environ, {
            "SUPABASE_URL": "https://rzhtesfikvykdnmrqwjs.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_test_only",
        })
        self.key.start()
        self.addCleanup(self.key.stop)

    def test_validates_server_side_key(self):
        self.assertEqual(store.headers()["apikey"], "sb_secret_test_only")
        with patch.dict(os.environ, {"SUPABASE_SECRET_KEY": "sb_publishable_public"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "publishable"):
                store.secret_key()
        with patch.dict(os.environ, {"SUPABASE_SECRET_KEY": "", "SUPABASE_SERVICE_ROLE_KEY": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Missing SUPABASE_SECRET_KEY"):
                store.secret_key()

    def test_reads_real_json_page(self):
        rows = [{"id": "1", "pdf_url": "https://example.org/1.pdf"}]
        with patch.object(store, "http_request", return_value=response(data=rows)) as get:
            self.assertEqual(store.list_materials(), rows)
        self.assertEqual(get.call_args.kwargs["params"]["select"], "*")
        self.assertEqual(get.call_args.kwargs["params"]["order"], "pdf_url.asc")
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 0)
        self.assertEqual(get.call_args.kwargs["headers"]["apikey"], "sb_secret_test_only")

    def test_reads_more_than_one_page(self):
        rows = [{"id": str(i), "pdf_url": f"https://example.org/{i}.pdf"} for i in range(501)]
        with patch.object(store, "http_request", side_effect=[
            response(data=rows[:500]), response(data=rows[500:])
        ]) as get:
            self.assertEqual(len(store.list_materials()), 501)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 500)

    def test_http_error_does_not_return_partial_duplicate_state(self):
        with patch.object(store, "http_request", return_value=response(403)):
            with self.assertRaises(requests.HTTPError):
                store.list_materials()

    def test_http_error_reports_actionable_status_without_leaking_keys(self):
        failure = response(401, {
            "code": "PGRST301",
            "message": "Invalid API key sb_secret_test_only",
        })
        with patch.object(store, "http_request", return_value=failure):
            with self.assertRaises(requests.HTTPError) as caught:
                store.list_materials()
        description = str(caught.exception)
        self.assertIn("HTTP 401", description)
        self.assertIn("PGRST301", description)
        self.assertIn("SUPABASE_SECRET_KEY", description)
        self.assertNotIn("sb_secret_test_only", description)

    def test_missing_id_column_does_not_break_sort(self):
        with patch.object(store, "http_request", return_value=response(data=[])) as request:
            self.assertEqual(store.list_materials(), [])
        self.assertEqual(request.call_args.kwargs["params"]["order"], "pdf_url.asc")
        self.assertNotIn("id.asc", str(request.call_args.kwargs["params"]))

    def test_duplicate_url_is_not_posted(self):
        payload = {"pdf_url": "https://example.org/Caps.PDF", "title": "Chapter"}
        with (patch.object(store, "build_material", return_value=payload),
              patch.object(store, "is_already_published", return_value=True),
              patch.object(store.requests, "post") as post):
            self.assertTrue(store.upload_material({}, date(2026, 10, 16), 1))
            post.assert_not_called()

    def test_post_success_requires_server_confirmation(self):
        payload = {"pdf_url": "https://example.org/Caps.PDF", "title": "Chapter"}
        with (patch.object(store, "build_material", return_value=payload),
              patch.object(store, "is_already_published", return_value=False),
              patch.object(store.requests, "post", return_value=response(201, [payload])) as post):
            self.assertTrue(store.upload_material({}, date(2026, 10, 16), 1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["headers"]["apikey"], "sb_secret_test_only")

    def test_uncertain_network_write_checks_before_retrying(self):
        payload = {"pdf_url": "https://example.org/a.pdf", "title": "Chapter"}
        with (patch.object(store, "build_material", return_value=payload),
              patch.object(store, "is_already_published", side_effect=[False, True]) as lookup,
              patch.object(store.requests, "post", side_effect=requests.Timeout)):
            self.assertTrue(store.upload_material({}, date(2026, 10, 16), 1))
        self.assertEqual(lookup.call_count, 2)

    def test_no_supabase_secret_fails_fast_before_school_login(self):
        import runner
        with (patch.object(runner, "MATERIAL_BACKEND", "supabase"),
              patch.object(runner, "EDUSECURE_USERNAME", "test-user"),
              patch.object(runner, "EDUSECURE_PASSWORD", "test-pass"),
              patch.object(runner.supabase_store, "secret_key", side_effect=RuntimeError("missing secret")),
              patch.object(runner, "firebase_sign_in") as sign_in,
              patch.object(runner.legacy, "make_driver") as browser,
              patch.object(runner, "save_report")):
            self.assertEqual(runner.main(), 2)
            sign_in.assert_not_called()
            browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
