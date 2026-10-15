import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import reliable_http
import runner
import sync_repair
import pdf_integrity
from push_bridge import notify_published


def response(code, body=None):
    result = Mock(status_code=code, ok=200 <= code < 300, headers={})
    result.json.return_value = body or {}
    return result


class ReliabilityTests(unittest.TestCase):
    @patch('reliable_http.time.sleep')
    @patch('reliable_http.requests.request')
    def test_timeout_then_success(self, request, sleep):
        good = response(200)
        request.side_effect = [requests.Timeout(), good]
        self.assertIs(reliable_http.request('GET', 'https://example.test'), good)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    @patch('reliable_http.time.sleep')
    @patch('reliable_http.requests.request')
    def test_exhausted_network_budget_propagates(self, request, sleep):
        request.side_effect = requests.ConnectionError()
        with self.assertRaises(requests.ConnectionError):
            reliable_http.request('GET', 'https://example.test')
        self.assertEqual(request.call_count, 3)

    @patch('reliable_http.time.sleep')
    @patch('reliable_http.requests.request')
    def test_retry_closes_response_and_caps_retry_after(self, request, sleep):
        first = response(503)
        first.headers['Retry-After'] = '86400'
        request.side_effect = [first, response(200)]
        reliable_http.request('GET', 'https://example.test')
        first.close.assert_called_once()
        self.assertEqual(sleep.call_args.args, (30.0,))

    def test_unicode_dash_does_not_bypass_grammar_guard(self):
        self.assertEqual(sync_repair.clean_title_grammar("– Regarding Short Leaves From"), "Regarding Short Leaves")
        self.assertEqual(runner.intelligence.sanitize_title("— Regarding Short Leaves From", "Circular"), "Regarding Short Leaves")

    def test_url_identity_preserves_paths_and_signed_query_case(self):
        self.assertEqual(reliable_http.canonical_url('HTTPS://EXAMPLE.test/A.pdf?token=Ab#x'), 'https://example.test/A.pdf?token=Ab')
        urls, _, _ = runner.existing_state([{'pdf_url': 'https://example.test/A.pdf'}])
        self.assertNotIn('https://example.test/a.pdf', urls)

    @patch('runner.http_request')
    def test_idempotent_create_and_conflict_check(self, request):
        item = {'title': 'Chapter 3 Notes', 'subject': 'Science', 'url': 'https://example.test/A.pdf'}
        request.side_effect = [response(200), response(409), response(200, {'fields': {'pdf_url': {'stringValue': item['url']}}})]
        self.assertTrue(runner.upload_firestore(item, date(2026, 10, 15), 1, 'token'))
        self.assertTrue(runner.upload_firestore(item, date(2026, 10, 15), 1, 'token'))
        self.assertEqual(request.call_args_list[0].kwargs['params']['documentId'], request.call_args_list[1].kwargs['params']['documentId'])

    def test_identical_metadata_does_not_delete_distinct_pdfs(self):
        a = {'source':'EduSecure GitHub Sync','title':'Chapter Notes','description':'Chapter Notes','subject':'Science','source_date':'2026-10-15','pdf_url':'https://example.test/A.pdf'}
        b = {**a, 'pdf_url': 'https://example.test/B.pdf'}
        with patch.object(sync_repair, '_load_cached_materials', return_value=None), patch.object(sync_repair, '_material_cooldown_active', return_value=False), patch.object(sync_repair, 'ORIGINAL_LIST', return_value=[a,b]), patch.object(sync_repair, '_save_cached_materials'), patch.object(sync_repair, 'firestore_delete_document') as delete:
            self.assertEqual(len(sync_repair.list_and_cleanup_materials('token')), 2)
            delete.assert_not_called()

    @patch('pdf_integrity.requests.get')
    def test_oversize_pdf_closes_stream(self, get):
        stream = response(200)
        stream.headers = {'Content-Length': str(pdf_integrity.MAX_PDF_BYTES + 1)}
        get.return_value = stream
        self.assertEqual(pdf_integrity.fetch_pdf_text('https://example.test/a.pdf')[1], 'too-large')
        stream.close.assert_called_once()

    @patch('push_bridge.request')
    def test_push_bridge_uses_existing_auth_and_reports_failure(self, request):
        request.side_effect = requests.Timeout()
        result = notify_published('private-token')
        self.assertFalse(result['ok'])
        self.assertNotIn('private-token', str(result))
        self.assertEqual(request.call_args.kwargs['json'], {'action':'sync'})

    def test_incremental_cache_update_does_not_extend_refresh_ttl(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sync_repair,'RUNTIME_CACHE_DIR',Path(temp)), patch.object(sync_repair,'MATERIAL_CACHE_FILE',Path(temp)/'materials.json'):
            with patch('sync_repair.time.time',return_value=100):
                sync_repair._save_cached_materials([])
            with patch('sync_repair.time.time',return_value=200):
                sync_repair._save_cached_materials([{'title':'new'}],refreshed=False)
            self.assertEqual(sync_repair._read_runtime_json(sync_repair.MATERIAL_CACHE_FILE)['saved_at'],100)

    def test_auth_failure_writes_report_without_starting_browser(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(runner,'REPORT_FILE',Path(temp)/'report.json'), patch.object(runner,'EDUSECURE_USERNAME','user'), patch.object(runner,'EDUSECURE_PASSWORD','pass'), patch.object(runner,'firebase_sign_in',side_effect=RuntimeError()), patch.object(runner.legacy,'make_driver') as driver:
            self.assertEqual(runner.main(), 2)
            self.assertIn('failed', runner.REPORT_FILE.read_text())
            driver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
