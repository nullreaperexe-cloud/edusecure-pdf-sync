"""EduSecure PDF metadata synchronized to the new 8aPDF Supabase project.

All reads and writes are authenticated by short-lived GitHub OIDC tokens via
supabase_gateway. No public publishable key, Firebase login or stored service
role credential is used for GitHub Actions publication.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any, Dict
from urllib.parse import urlsplit

import supabase_gateway as gateway
from reliable_http import canonical_url


def list_materials(_unused_token: str = "") -> list[Dict[str, Any]]:
    response = gateway.call("listMaterials")
    rows = response.get("records")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RuntimeError("Supabase study_materials returned invalid inventory")
    print(f"Verified Supabase study_materials inventory: {len(rows)} records")
    return rows


def build_material(item: Dict[str, Any], source_date: date, order: int) -> Dict[str, Any]:
    import title_cleaner as intelligence

    safe = intelligence.finalize_material_fields(item)
    pdf_url = str(safe.get("url") or item.get("url") or item.get("pdf_url") or "").strip()
    parsed = urlsplit(pdf_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Refusing to publish a non-HTTP document URL")
    title = str(safe.get("title") or "").strip()
    if not title:
        raise ValueError("Refusing to publish a PDF with an empty title")
    identity = canonical_url(pdf_url)
    if not identity:
        raise ValueError("Empty PDF identity")
    document_id = "edusecure-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    source_timestamp = datetime(
        source_date.year, source_date.month, source_date.day, tzinfo=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    return {
        "id": document_id,
        "title": title,
        "subject": intelligence.normalize_subject(safe.get("subject")) or "Circular",
        "description": str(safe.get("description") or title).strip(),
        "pdf_url": pdf_url,
        "source_date": source_timestamp,
        "order": int(order),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def upload_material(
    item: Dict[str, Any], source_date: date, order: int, _unused_token: str = ""
) -> bool:
    payload = build_material(item, source_date, order)
    response = gateway.call("insertMaterial", record=payload)
    if response.get("inserted"):
        print("✅ New PDF publication confirmed by 8aPDF Supabase")
    elif response.get("duplicate"):
        print("PDF URL already exists in Supabase; no duplicate created")
    else:
        raise RuntimeError("Supabase did not confirm PDF insert or duplicate")
    existing = response.get("record") or {}
    return canonical_url(str(existing.get("pdf_url") or "")) == canonical_url(payload["pdf_url"])
