"""Offline regression tests: new 8aPDF project, OIDC and idempotent PDF writes."""
import os
import unittest
from datetime import date
from unittest.mock import patch

import supabase_gateway as gateway
import supabase_materials as store


class SupabaseMaterialsTests(unittest.TestCase):
    def test_backend_is_new_8apdf_project(self):
        with patch.dict(os.environ, {"SUPABASE_URL": "https://xudhjwojgxblbsebkzso.supabase.co"}):
            self.assertEqual(gateway.endpoint(),
                "https://xudhjwojgxblbsebkzso.supabase.co/functions/v1/edusecure-sync")
        with patch.dict(os.environ, {"SUPABASE_URL":"https://rzhtesfikvykdnmrqwjs.supabase.co"}):
            with self.assertRaisesRegex(RuntimeError, "new 8aPDF"):
                gateway.endpoint()

    def test_fails_closed_without_oidc_identity(self):
        with patch.dict(os.environ, {"ACTIONS_ID_TOKEN_REQUEST_URL":"", "ACTIONS_ID_TOKEN_REQUEST_TOKEN":""}):
            with patch.object(gateway,"_TOKEN",""):
                with self.assertRaisesRegex(RuntimeError, "id-token: write"):
                    gateway.token()

    def test_reads_catalogue_using_verified_gateway(self):
        docs=[{"id":"legacy-1","pdf_url":"https://example.org/existing.pdf"}]
        with patch.object(store.gateway, "call", return_value={"ok":True,"records":docs}) as api:
            self.assertEqual(store.list_materials(), docs)
            api.assert_called_once_with("listMaterials")

    def test_invalid_inventory_is_rejected(self):
        with patch.object(store.gateway, "call", return_value={"ok":True,"records":None}):
            with self.assertRaisesRegex(RuntimeError, "invalid inventory"):
                store.list_materials()

    def test_deterministic_url_based_document_id(self):
        item={"title":"Chapter 3 Notes","subject":"Science","description":"Chapter 3 Notes","url":"https://example.org/Caps.PDF"}
        a=store.build_material(item,date(2026,10,16),1)
        b=store.build_material(item,date(2026,10,16),2)
        self.assertEqual(a["id"],b["id"])
        self.assertEqual(a["pdf_url"],item["url"])
        self.assertEqual(a["source_date"],"2026-10-16T00:00:00Z")

    def test_confirmed_insert_and_duplicate_are_both_success(self):
        item={"title":"Chapter 3 Notes","subject":"Science","url":"https://example.org/A.pdf"}
        with patch.object(store.gateway, "call", return_value={"ok":True,"inserted":True,"record":{"pdf_url":item["url"]}}) as api:
            self.assertTrue(store.upload_material(item,date(2026,10,16),1))
            self.assertEqual(api.call_args.args[0],"insertMaterial")
        with patch.object(store.gateway, "call", return_value={"ok":True,"duplicate":True,"record":{"pdf_url":item["url"]}}):
            self.assertTrue(store.upload_material(item,date(2026,10,16),1))

    def test_wrong_returned_identity_fails(self):
        item={"title":"Notes","subject":"Science","url":"https://example.org/A.pdf"}
        with patch.object(store.gateway, "call", return_value={"ok":True,"duplicate":True,"record":{"pdf_url":"https://example.org/B.pdf"}}):
            self.assertFalse(store.upload_material(item,date(2026,10,16),1))

    def test_rejects_insecure_pdf_urls(self):
        with self.assertRaises(ValueError):
            store.build_material({"title":"Test","subject":"Science","url":"file:///secret.pdf"},date(2026,10,16),1)


if __name__=="__main__":
    unittest.main()
