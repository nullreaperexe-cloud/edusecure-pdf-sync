"""Read and publish EduSecure PDF metadata to the 8aPDF Supabase table.

The browser uses a publishable key for SELECT; this GitHub Actions worker must
use a server-side secret key. Never commit or print a Supabase secret key.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone
from typing import Any, Dict
from urllib.parse import urlsplit

import requests

from reliable_http import canonical_url, request as http_request

DEFAULT_SUPABASE_URL = "https://rzhtesfikvykdnmrqwjs.supabase.co"
TABLE = "study_materials"
PAGE_SIZE = 500
MAX_MATERIALS = 100_000


def endpoint() -> str:
    root = (os.environ.get("SUPABASE_URL") or DEFAULT_SUPABASE_URL).strip().rstrip("/")
    parsed = urlsplit(root)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
        raise RuntimeError("SUPABASE_URL must be an HTTPS project origin.")
    return root + "/rest/v1/" + TABLE


def secret_key() -> str:
    key = (os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not key:
        raise RuntimeError("Missing SUPABASE_SECRET_KEY GitHub Actions secret (private server-side key required).")
    if key.startswith(("sb_publishable_", "anon_")):
        raise RuntimeError("A Supabase publishable/anon key is not safe for automation writes; set SUPABASE_SECRET_KEY.")
    return key


def headers(*, write: bool = False) -> Dict[str, str]:
    key = secret_key()
    result = {"apikey": key, "Accept": "application/json"}
    if write:
        result.update({"Content-Type": "application/json", "Prefer": "return=representation"})
    # JWT-style legacy service_role keys also require Authorization for PostgREST.
    if key.count(".") == 2:
        result["Authorization"] = "Bearer " + key
    return result


def _safe_supabase_error(response, operation: str) -> requests.HTTPError:
    """Show the actual HTTP status and PostgREST error without logging secrets."""
    code = ""
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            code = str(body.get("code") or "").strip()[:45]
            message = str(body.get("message") or body.get("error_description") or body.get("error") or "").strip()
    except (ValueError, TypeError):
        pass
    # A gateway error may quote the submitted key. Never print it, even if
    # GitHub's masking fails because of different escaping or quoting.
    message = re.sub(r"(?i)sb_(?:secret|publishable)_[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = re.sub(r"(?i)(?:eyJ[A-Za-z0-9_-]+\\.){2}[A-Za-z0-9_-]+", "[REDACTED]", message)
    message = message.replace("\\n", " ").replace("\\r", " ")[:210]
    status = int(response.status_code)
    guidance = {
        400: "Check required table columns/order (pdf_url, title, subject, description, source_date, created_at).",
        401: "The GitHub SUPABASE_SECRET_KEY may be incorrect, inactive, or from a different Supabase project.",
        403: "Database table/schema permissions deny the configured key; check Supabase grants and RLS.",
        404: "Supabase REST table study_materials was not found; confirm schema and project URL.",
        429: "Supabase is rate limiting reads; try the next scheduled sync.",
    }.get(status, "Check Supabase REST API and project logs.")
    summary = f"Supabase {operation} failed: HTTP {status}"
    if code:
        summary += f", code {code}"
    if message:
        summary += f": {message}"
    summary += f" — {guidance}"
    return requests.HTTPError(summary, response=response)


def _require_success(response, operation: str) -> None:
    if not response.ok:
        raise _safe_supabase_error(response, operation)


def list_materials(_unused_token: str = "") -> list[Dict[str, Any]]:
    """Fully load the target table; never scan against incomplete duplicate state."""
    result: list[Dict[str, Any]] = []
    for offset in range(0, MAX_MATERIALS, PAGE_SIZE):
        response = http_request(
            "GET",
            endpoint(),
            headers=headers(),
            # PDF URL is the document identity. Do not require an 'id' column:
            # some existing Supabase study_materials tables only have pdf_url.
            params={"select": "*", "order": "pdf_url.asc", "offset": offset, "limit": PAGE_SIZE},
            timeout=(7, 30),
        )
        try:
            _require_success(response, "study_materials read")
            rows = response.json()
            if not isinstance(rows, list):
                raise RuntimeError("Unexpected Supabase study_materials response (expected JSON array).")
            result.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < PAGE_SIZE:
                print(f"Supabase study_materials loaded: {len(result)} records.")
                return result
        finally:
            response.close()
    # Exact-boundary read verifies that a table of MAX_MATERIALS is not truncated.
    response = http_request(
        "GET", endpoint(), headers=headers(),
        params={"select": "pdf_url", "order": "pdf_url.asc", "offset": MAX_MATERIALS, "limit": 1},
        timeout=(7, 30),
    )
    try:
        _require_success(response, "study_materials pagination")
        if response.json():
            raise RuntimeError("Supabase materials exceeded safe pagination bound; refusing partial duplicate state.")
    finally:
        response.close()
    return result


def is_already_published(url: str) -> bool:
    """Check exact canonical URL after an uncertain POST result or timeout."""
    identity = canonical_url(url)
    if not identity:
        raise ValueError("Cannot verify an empty PDF URL")
    # Full scan remains correct even when PostgREST filter escaping, duplicate
    # URLs or case-sensitive PDF paths would make a targeted eq filter unsafe.
    return any(canonical_url(str(row.get("pdf_url") or "")) == identity for row in list_materials())


def build_material(item: Dict[str, Any], source_date: date, order: int) -> Dict[str, Any]:
    # Same final sanitization gate that protected the previous Firestore writer.
    import title_cleaner as intelligence

    safe = intelligence.finalize_material_fields(item)
    pdf_url = str(safe.get("url") or item.get("url") or item.get("pdf_url") or "").strip()
    parsed = urlsplit(pdf_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Refusing to publish a non-HTTP document URL")
    title = str(safe.get("title") or "").strip()
    if not title:
        raise ValueError("Refusing to publish a PDF with an empty title")
    utc_midnight = datetime(source_date.year, source_date.month, source_date.day, tzinfo=timezone.utc)
    return {
        "title": title,
        "subject": intelligence.normalize_subject(safe.get("subject")) or "Circular",
        "description": str(safe.get("description") or title).strip(),
        "pdf_url": pdf_url,
        "source": "EduSecure GitHub Sync",
        "source_date": utc_midnight.isoformat().replace("+00:00", "Z"),
        "order": int(order),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def upload_material(item: Dict[str, Any], source_date: date, order: int, _unused_token: str = "") -> bool:
    """Publish a PDF only if its exact URL is absent; verify ambiguous writes.

    Concurrency is also serialized in the GitHub Actions workflow. For
    cross-system race protection, a UNIQUE index on pdf_url is recommended.
    """
    payload = build_material(item, source_date, order)
    url = payload["pdf_url"]
    # Always use the live remote table before POST, not a possibly stale cache.
    if is_already_published(url):
        print("PDF already exists in Supabase; no duplicate inserted.")
        return True

    try:
        # No blind automatic retry of POST: the first request may have committed.
        response = requests.post(
            endpoint(),
            headers=headers(write=True),
            params={"select": "*"},
            json=payload,
            timeout=(7, 30),
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        print(f"Supabase write response uncertain ({type(exc).__name__}); checking committed PDF URL.")
        try:
            return is_already_published(url)
        except Exception as verify_error:
            print(f"Supabase verification unavailable: {type(verify_error).__name__}")
            return False

    try:
        if response.ok:
            rows = response.json()
            if isinstance(rows, list) and rows and canonical_url(str(rows[0].get("pdf_url") or "")) == canonical_url(url):
                print("Supabase PDF publication confirmed by REST response.")
                return True
            print("Supabase POST returned success without matching PDF; verifying live record.")
        else:
            print(str(_safe_supabase_error(response, "study_materials insert")))
            if response.status_code not in (409, 408, 429, 500, 502, 503, 504):
                return False
    finally:
        response.close()

    try:
        return is_already_published(url)
    except Exception as exc:
        print(f"Supabase follow-up verification failed: {type(exc).__name__}")
        return False
