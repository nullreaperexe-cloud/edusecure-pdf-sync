"""Supabase-powered EduSecure school announcements.

Reuses the original message cleaning, category detection, AI title validation,
and deterministic source-message IDs from announcement_processor, but NEVER
calls its Firebase transport. GitHub's OIDC-verified Edge Function securely
reads and inserts into public.announcements in the new 8aPDF project.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

import announcement_processor as legacy
from announcement_processor import *  # reuse text sanitization and categorization
import supabase_gateway as gateway


def _timestamp(d: Optional[date]) -> str:
    moment = datetime(d.year,d.month,d.day,tzinfo=timezone.utc) if isinstance(d,date) else datetime.now(timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _original(item: Dict[str,Any], message_date: Optional[date]) -> Dict[str,Any]:
    """Maintain pre-migration announcement field names and date semantics."""
    value = dict(item)
    ts = _timestamp(message_date)
    value["published"] = True
    value["createdAt"] = ts
    value["messageDate"] = ts if message_date else None
    return value


def announcement_document_name(source_id: str) -> str:
    return str(source_id or "")


def list_complete_source_ids(_id_token: str = "") -> Set[str]:
    rows = gateway.call("listAnnouncements").get("records", [])
    if not isinstance(rows,list):
        raise RuntimeError("Supabase announcement inventory was not an array")
    ids=set()
    for row in rows:
        if not isinstance(row,dict) or row.get("published") is not True:
            continue
        source=str(row.get("source_message_id") or row.get("id") or "").strip()
        if source and str(row.get("title") or "").strip() not in ("", "Processing Announcement"):
            ids.add(source)
    print(f"Verified Supabase announcement IDs: {len(ids)}")
    return ids


def load_existing_state(id_token: str = "") -> Tuple[Set[str], Optional[date]]:
    rows=gateway.call("listAnnouncements").get("records", [])
    ids=set()
    latest=None
    for row in rows:
        if row.get("published") is not True:
            continue
        source=str(row.get("source_message_id") or row.get("id") or "")
        if source:
            ids.add(source)
        raw=str(row.get("message_date") or "")
        if raw:
            try:
                parsed=datetime.fromisoformat(raw.replace("Z","+00:00")).date()
                if latest is None or parsed>latest:
                    latest=parsed
            except (TypeError,ValueError):
                pass
    return ids,latest


def upload_announcement(
    item: Dict[str,Any],
    message_date: Optional[date],
    id_token: str = "",
    existing_document_name: str = "",
    preserve_created_at: bool = False,
) -> bool:
    source_id=str(item.get("sourceMessageId") or "").strip()
    if not source_id:
        return False
    raw=_original(item,message_date)
    payload={
        "id":source_id,
        "title":str(item.get("title") or "").strip(),
        "description":str(item.get("description") or ""),
        "subject":str(item.get("subject") or "General"),
        "category":str(item.get("category") or "General"),
        "created_at":_timestamp(message_date),
        "message_date":_timestamp(message_date) if message_date else None,
        "original_message":str(item.get("originalMessage") or ""),
        "attachment_url":str(item.get("attachmentUrl") or ""),
        "has_attachment":bool(item.get("hasAttachment")),
        "priority":str(item.get("priority") or "normal"),
        "ai_model":str(item.get("aiModel") or ""),
        "ai_status":str(item.get("aiStatus") or "complete"),
        "raw":raw,
    }
    result=gateway.call("publishAnnouncement",record=payload)
    if result.get("inserted") or result.get("duplicate"):
        print("✅ Supabase school announcement confirmed: "+payload["title"][:100])
        return True
    return False


def process_no_attachment_message(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
    id_token: str,
    existing_source_ids: Set[str],
    attachment_url: str = "",
) -> Tuple[str, Optional[Dict[str,Any]]]:
    source_id=legacy.stable_message_id(message_text,message_date)
    if source_id in existing_source_ids:
        return "duplicate",None
    # The Edge Function's deterministic id and published check prevents
    # duplicate inserts even if another job races or the last POST timed out.
    item=legacy.build_announcement(message_text,detail_text,message_date,attachment_url=attachment_url)
    item["sourceMessageId"]=source_id
    if upload_announcement(item,message_date,id_token):
        existing_source_ids.add(source_id)
        return "created",item
    return "failed",item


def firestore_is_throttled() -> bool:
    return False


def backfill_completed(_id_token: str) -> bool:
    return False


def mark_backfill_completed(_id_token: str, _scanned: int, _repaired: int) -> bool:
    return True


def cleanup_legacy_announcement_documents(*_args: Any,**_kwargs: Any) -> dict[str,int]:
    # Never run old Firestore cleanup/deletion against migrated Supabase data.
    return {"deleted":0,"rewritten":0,"skipped":0}
