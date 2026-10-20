"""Drive-first durable staging for new EduSecure PDFs/announcements.

Only publishes to Supabase AFTER a private Google Drive write has succeeded.
The files in pending_materials/pending_announcements are durable retry evidence,
not public hosting links. The original source URL remains the site's PDF URL.
"""
from __future__ import annotations

import hashlib
import json
import os

from drive_backup import Drive, compact_json, digest, fetch_pdf

_DRIVE = None
_ROOT = None
_FOLDER_CACHE = {}


def drive_first_required():
    return os.environ.get("DRIVE_FIRST_REQUIRED", "").strip() == "1"


def require_drive():
    """Fail closed: no Supabase publication without write-capable Drive auth."""
    global _DRIVE, _ROOT
    if _DRIVE is None:
        _DRIVE = Drive()  # Throws if OAuth invalid/unconfigured.
    if _ROOT is None:
        configured = os.environ.get("GDRIVE_BACKUP_FOLDER_ID", "").strip()
        _ROOT = configured or _DRIVE.folder("root", "8aPDF_Backup")
    return _DRIVE, _ROOT


def folder(name):
    drive, root = require_drive()
    if name not in _FOLDER_CACHE:
        _FOLDER_CACHE[name] = drive.folder(root, name)
    return _FOLDER_CACHE[name]


def put_json(parent, filename, payload):
    drive, _ = require_drive()
    entries = {item["name"]: item for item in drive.list(parent, filename)}
    blob = compact_json(payload)
    file_id, _ = drive.put(parent, filename, blob, "application/json", entries)
    if not file_id:
        raise RuntimeError("Google Drive did not confirm JSON staging")
    return file_id


def prepare_material(record):
    """Make byte-level PDF backup and staging record BEFORE Supabase insert."""
    if not drive_first_required():
        return None
    document_url = str(record.get("pdf_url") or "").strip()
    if not document_url or not record.get("id"):
        raise ValueError("Cannot stage a PDF without an id and source URL")

    drive, _ = require_drive()
    pdf_parent = folder("PDFs")
    filename = hashlib.sha256(document_url.encode("utf-8")).hexdigest() + ".pdf"
    matches = {item["name"]: item for item in drive.list(pdf_parent, filename)}
    original = matches.get(filename)
    if not original:
        max_mib = max(1, min(100, int(os.environ.get("GDRIVE_BACKUP_MAX_PDF_MIB", "25"))))
        data = fetch_pdf(document_url, max_mib * 1024 * 1024)
        original_id, _ = drive.put(pdf_parent, filename, data, "application/pdf", matches)
        size, sha256 = len(data), digest(data)
    else:
        original_id = original["id"]
        sha256 = (original.get("appProperties") or {}).get("sha256")
        size = None

    if not original_id or not sha256:
        raise RuntimeError("PDF original is not verified in Google Drive")
    # Staging record carries the complete final Supabase fields plus verified
    # Drive reference. A failed Supabase write can be safely retried by scraper.
    meta = {"table": "study_materials", "record": record,
            "original": {"drive_id": original_id, "name": filename,
                         "sha256": sha256, "size_bytes": size}}
    staged_id = put_json(folder("pending_materials"), str(record["id"]) + ".json", meta)
    print("Drive-first: original PDF and pending material record confirmed.", flush=True)
    return {"original_drive_id": original_id, "stage_drive_id": staged_id}


def prepare_announcement(record):
    """Stage announcement metadata before Supabase publication."""
    if not drive_first_required():
        return None
    if not record.get("id"):
        raise ValueError("Cannot stage announcement without an ID")
    meta = {"table": "announcements", "record": record}
    staged_id = put_json(folder("pending_announcements"), str(record["id"]) + ".json", meta)
    print("Drive-first: pending announcement confirmed.", flush=True)
    return staged_id
