"""Regression tests for Supabase-only school announcement sync."""
import unittest
from datetime import date
from unittest.mock import patch

import supabase_announcements as a


class SupabaseAnnouncementTests(unittest.TestCase):
    def test_source_ids_loaded_without_firestore(self):
        docs=[
            {"id":"id-a","source_message_id":"id-a","published":True,"title":"Homework"},
            {"id":"id-b","source_message_id":"id-b","published":False,"title":"Processing Announcement"},
            {"id":"id-c","source_message_id":"id-c","published":True,"title":"Circular"},
        ]
        with patch.object(a.gateway,"call",return_value={"ok":True,"records":docs}) as call:
            self.assertEqual(a.list_complete_source_ids(),{"id-a","id-c"})
            call.assert_called_once_with("listAnnouncements")

    def test_existing_announcement_state(self):
        docs=[
            {"id":"a","source_message_id":"a","published":True,"message_date":"2026-10-14T00:00:00Z"},
            {"id":"b","source_message_id":"b","published":True,"message_date":"2026-10-16T00:00:00Z"},
            {"id":"c","published":False,"message_date":"2026-10-19T00:00:00Z"}
        ]
        with patch.object(a.gateway,"call",return_value={"ok":True,"records":docs}):
            ids,last=a.load_existing_state()
        self.assertEqual(ids,{"a","b"})
        self.assertEqual(last,date(2026,10,16))

    def test_announcement_publication_preserves_source_identity_and_fields(self):
        original={
            "sourceMessageId":"edusecure-abcdefgh",
            "title":"Bring English notebook",
            "subject":"English","category":"Homework",
            "description":"Bring notebook tomorrow",
            "originalMessage":"Please bring English notebook tomorrow",
            "published":True
        }
        with patch.object(a.gateway,"call",return_value={"ok":True,"inserted":True}) as call:
            self.assertTrue(a.upload_announcement(original,date(2026,10,16)))
            self.assertEqual(call.call_args.args[0],"publishAnnouncement")
            payload=call.call_args.kwargs["record"]
            self.assertEqual(payload["id"],"edusecure-abcdefgh")
            self.assertEqual(payload["title"],original["title"])
            self.assertEqual(payload["subject"],original["subject"])
            self.assertEqual(payload["raw"]["title"],original["title"])
            self.assertEqual(payload["message_date"],"2026-10-16T00:00:00Z")

    def test_duplicate_confirmation_is_success(self):
        item={"sourceMessageId":"edusecure-abc","title":"Reminder"}
        with patch.object(a.gateway,"call",return_value={"ok":True,"duplicate":True}):
            self.assertTrue(a.upload_announcement(item,date(2026,10,16)))

    def test_process_no_attachment_never_uses_firestore(self):
        item={"title":"Math exercise","description":"Math exercise","sourceMessageId":"", "category":"Homework","subject":"Math"}
        with (patch.object(a.legacy,"stable_message_id",return_value="edusecure-xyz"),
              patch.object(a.legacy,"build_announcement",return_value=item),
              patch.object(a,"upload_announcement",return_value=True) as publish):
            ids=set()
            status,value=a.process_no_attachment_message("Math exercise","Math exercise",date(2026,10,16),"",ids)
            self.assertEqual(status,"created")
            self.assertIn("edusecure-xyz",ids)
            self.assertEqual(value["sourceMessageId"],"edusecure-xyz")
            publish.assert_called_once()

    def test_reuses_existing_source_ids(self):
        with patch.object(a.legacy,"stable_message_id",return_value="edusecure-known"),patch.object(a,"upload_announcement") as publish:
            result=a.process_no_attachment_message("Known","Known",date(2026,10,16),"",{"edusecure-known"})
            self.assertEqual(result,("duplicate",None))
            publish.assert_not_called()

if __name__=="__main__":
    unittest.main()
