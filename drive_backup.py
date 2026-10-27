"""Private Google Drive disaster-recovery backup for the 8aPDF EduSecure sync.

Reads BOTH complete catalogues using the existing GitHub-OIDC-verified Supabase
Edge Function. Copies JSON snapshots and bounded PDF originals into a private
Google Drive folder via OAuth. Does not write to Supabase or change website data.

Required GitHub Actions secret (recommended):
  GDRIVE_BACKUP_OAUTH_JSON: JSON containing client_id, client_secret,
  refresh_token. The standard Google Cloud OAuth client JSON alone does NOT
  include a refresh token, so it cannot authorize unattended backups.
  Legacy three-secret configuration is still supported:
  GDRIVE_BACKUP_CLIENT_ID, GDRIVE_BACKUP_CLIENT_SECRET,
  GDRIVE_BACKUP_REFRESH_TOKEN
Optional:
  GDRIVE_BACKUP_FOLDER_ID (use an existing, authorised folder instead of
  creating/finding "8aPDF_Backup" in OAuth user's My Drive)
  GDRIVE_BACKUP_MAX_PDFS_PER_RUN (default 10)
  GDRIVE_BACKUP_MAX_PDF_MIB (default 25)

Never share the refresh token or snapshots publicly. The Drive scope needs
permission to create/edit the files this backup writes (drive.file or drive).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import requests
import supabase_gateway

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
STATUS = Path("drive_backup_report.json")
FOLDER = "8aPDF_Backup"
BACKUP_STAGE = "configuration"
OAUTH_ERROR_CODES = frozenset({
    "invalid_grant", "invalid_client", "unauthorized_client",
    "invalid_request", "access_denied", "temporarily_unavailable",
    "unsupported_grant_type",
})


class OAuthRefreshError(RuntimeError):
    """Safe OAuth diagnostic: only allowlisted machine error codes, never response text."""

    def __init__(self, status, code):
        self.http_status = int(status)
        self.oauth_error = code if code in OAUTH_ERROR_CODES else "unknown"
        super().__init__("Google OAuth refresh failed")

# Fail closed for unexpected or internal URLs. Add verified public hosts explicitly
# via environment variables as new PDF providers are introduced.
DEFAULT_ALLOWED_HOSTS = (
    "edusecure.org,www.edusecure.org,www.image2url.com,"
    "xudhjwojgxblbsebkzso.supabase.co"
)


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def record_status(**kw):
    STATUS.write_text(json.dumps({"checked_at": stamp(), **kw}, indent=2), encoding="utf-8")


def oauth_credentials():
    """Read all credentials from one Actions secret, or legacy three secrets.

    Do not print or report credential contents, including invalid JSON.
    """
    packed = os.environ.get("GDRIVE_BACKUP_OAUTH_JSON", "").strip()
    if packed:
        try:
            value = json.loads(packed)
        except (ValueError, TypeError) as exc:
            raise ValueError("GDRIVE_BACKUP_OAUTH_JSON is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("GDRIVE_BACKUP_OAUTH_JSON must contain a JSON object")
        # The standard downloaded Google OAuth client file nests the client
        # details in a web or installed object, and has no refresh token.
        client = value.get("web") or value.get("installed") or value
        if not isinstance(client, dict):
            raise ValueError("GDRIVE_BACKUP_OAUTH_JSON client must be an object")
        result = {
            "client_id": client.get("client_id"),
            "client_secret": client.get("client_secret"),
            "refresh_token": value.get("refresh_token"),
        }
    else:
        result = {
            "client_id": os.getenv("GDRIVE_BACKUP_CLIENT_ID", ""),
            "client_secret": os.getenv("GDRIVE_BACKUP_CLIENT_SECRET", ""),
            "refresh_token": os.getenv("GDRIVE_BACKUP_REFRESH_TOKEN", ""),
        }
    if any(not isinstance(v, str) or not v.strip() for v in result.values()):
        raise ValueError("Google OAuth client_id, client_secret and refresh_token are required")
    return {k: v.strip() for k, v in result.items()}


def configuration():
    try:
        oauth_credentials()
        return []
    except ValueError:
        if os.getenv("GDRIVE_BACKUP_OAUTH_JSON", "").strip():
            return ["valid GDRIVE_BACKUP_OAUTH_JSON (including refresh_token)"]
        return [
            k for k in ("GDRIVE_BACKUP_CLIENT_ID", "GDRIVE_BACKUP_CLIENT_SECRET",
                         "GDRIVE_BACKUP_REFRESH_TOKEN")
            if not os.getenv(k, "").strip()
        ]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compact_json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str) + "\n").encode("utf-8")


def retry_call(session, method, url, **kwargs):
    """Retries transient Drive/Google server failures; keeps response bounded."""
    for attempt in range(4):
        res = session.request(method, url, timeout=(10, 65), **kwargs)
        if res.status_code not in (408, 429, 500, 502, 503, 504) and not (
            res.status_code == 403 and ("rateLimitExceeded" in res.text or
                                        "userRateLimitExceeded" in res.text)
        ):
            return res
        res.close()
        if attempt < 3:
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError("Google Drive transient error after retries")


def assert_ok(response):
    try:
        response.raise_for_status()
        return response
    except requests.HTTPError as exc:
        status = response.status_code
        response.close()
        # Do not echo Google OAuth response bodies; they can contain private data.
        raise RuntimeError("Google Drive request failed (HTTP " + str(status) + ")") from exc


class Drive:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        credentials = oauth_credentials()
        response = retry_call(
            self.session, "POST", "https://oauth2.googleapis.com/token",
            data={**credentials, "grant_type": "refresh_token"},
        )
        try:
            if response.status_code >= 400:
                # Google error_description and response body are deliberately
                # never logged; only fixed non-sensitive error codes.
                try:
                    payload = response.json()
                    raw_code = payload.get("error") if isinstance(payload, dict) else None
                except (ValueError, TypeError):
                    raw_code = None
                raise OAuthRefreshError(response.status_code, raw_code)
            response.raise_for_status()
            auth = response.json()
        finally:
            response.close()
        if not isinstance(auth, dict):
            raise RuntimeError("Google OAuth token response was not a JSON object")
        token = auth.get("access_token", "")
        if not token:
            raise RuntimeError("Google OAuth token response has no access_token")
        self.session.headers.update({"Authorization": "Bearer " + token})

    def list(self, parent, name=None):
        query = "'%s' in parents and trashed = false" % parent.replace("'", "\\'")
        if name:
            query += " and name = '%s'" % name.replace("\\", "\\\\").replace("'", "\\'")
        files, page = [], None
        while True:
            params = {"q": query, "pageSize": 1000,
                      "fields": "nextPageToken,files(id,name,mimeType,appProperties)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            response = assert_ok(retry_call(self.session, "GET", API, params=params))
            result = response.json()
            response.close()
            files.extend(result.get("files", []))
            page = result.get("nextPageToken")
            if not page:
                break
        return files

    def folder(self, parent, name):
        existing = [item for item in self.list(parent, name)
                    if item.get("mimeType") == "application/vnd.google-apps.folder"]
        if existing:
            return existing[0]["id"]
        response = assert_ok(retry_call(
            self.session, "POST", API,
            params={"fields": "id", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": "application/vnd.google-apps.folder",
                  "parents": [parent]},
        ))
        result = response.json()
        response.close()
        if not result.get("id"):
            raise RuntimeError("Google Drive folder creation returned no ID")
        return result["id"]

    def put(self, parent, name, data, mime_type, index):
        sha = digest(data)
        existing = index.get(name)
        if existing and (existing.get("appProperties") or {}).get("sha256") == sha:
            return existing["id"], False

        props = {"sha256": sha}
        if mime_type == "application/json" and name in ("materials.json", "announcements.json"):
            props["record_count"] = str(len(json.loads(data).get("records", [])))
        meta = {"name": name, "mimeType": mime_type, "appProperties": props}
        if not existing:
            meta["parents"] = [parent]
        url = UPLOAD_API + ("/" + existing["id"] if existing else "")
        method = "PATCH" if existing else "POST"

        # Resumable works for both JSON and PDFs; no 5 MB multipart boundary.
        initiate = assert_ok(retry_call(
            self.session, method, url,
            params={"uploadType": "resumable", "fields": "id,name,appProperties",
                    "supportsAllDrives": "true"},
            headers={"X-Upload-Content-Type": mime_type,
                     "X-Upload-Content-Length": str(len(data))},
            json=meta,
        ))
        upload_url = initiate.headers.get("Location")
        initiate.close()
        if not upload_url or not upload_url.startswith("https://"):
            raise RuntimeError("Google Drive resumable session URL missing")
        uploaded = assert_ok(self.session.put(
            upload_url, data=data,
            headers={"Content-Type": mime_type, "Content-Length": str(len(data))},
            timeout=(10, 120),
        ))
        result = uploaded.json()
        uploaded.close()
        file_id = result.get("id") or (existing and existing["id"])
        if not file_id:
            raise RuntimeError("Google Drive upload not confirmed")
        index[name] = {"id": file_id, "name": name, "appProperties": props}
        return file_id, True


def read_inventory(action):
    result = supabase_gateway.call(action)
    rows = result.get("records")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("Supabase returned incomplete " + action + " inventory")
    return rows


def validate_snapshot(rows, old_index, name):
    """Never replace an existing nonempty snapshot with a suspicious empty set."""
    old = old_index.get(name) or {}
    old_count = (old.get("appProperties") or {}).get("record_count")
    if old_count and int(old_count) > 0 and len(rows) == 0:
        raise RuntimeError("Refusing to overwrite previously populated " + name + " with zero records")


def pdf_host_allowed(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    allow = {h.strip().lower() for h in os.getenv(
        "GDRIVE_BACKUP_ALLOWED_PDF_HOSTS", DEFAULT_ALLOWED_HOSTS).split(",") if h.strip()}
    return parsed.hostname.lower() in allow


def fetch_pdf(url, max_bytes):
    """Stream with a hard cap and validate every redirect target against allowlist."""
    target = url
    for _ in range(6):
        if not pdf_host_allowed(target):
            raise ValueError("PDF host not allowlisted")
        response = requests.get(target, stream=True, allow_redirects=False,
                                timeout=(10, 35),
                                headers={"User-Agent": "8aPDF-Private-Backup/1.0"})
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                where = response.headers.get("Location", "")
                if not where:
                    raise RuntimeError("PDF redirect has no location")
                target = urljoin(target, where)
                continue
            response.raise_for_status()
            size = int(response.headers.get("Content-Length", "0") or "0")
            if size > max_bytes:
                raise ValueError("PDF exceeds size cap")
            out = io.BytesIO()
            for chunk in response.iter_content(chunk_size=131072):
                out.write(chunk)
                if out.tell() > max_bytes:
                    raise ValueError("PDF exceeds size cap")
            blob = out.getvalue()
            if not blob.lstrip().startswith(b"%PDF-"):
                raise ValueError("URL did not return a real PDF file")
            return blob
        finally:
            response.close()
    raise ValueError("PDF redirect chain too long")


def backup():
    global BACKUP_STAGE
    BACKUP_STAGE = "configuration"
    missing = configuration()
    if missing:
        record_status(status="not_configured", missing_secrets=missing)
        print("Google Drive backup NOT ACTIVE. Missing GitHub Actions secrets: " +
              ", ".join(missing), flush=True)
        return 1  # backup unhealthy; caller records failure without blocking sync

    BACKUP_STAGE = "oauth_refresh"
    drive = Drive()
    BACKUP_STAGE = "drive_folders"
    root = os.getenv("GDRIVE_BACKUP_FOLDER_ID", "").strip() or drive.folder("root", FOLDER)
    pdf_folder = drive.folder(root, "PDFs")
    daily_folder = drive.folder(root, "daily_snapshots")
    root_index = {f["name"]: f for f in drive.list(root) if f.get("name")}
    pdf_index = {f["name"]: f for f in drive.list(pdf_folder) if f.get("name")}
    daily_index = {f["name"]: f for f in drive.list(daily_folder) if f.get("name")}

    BACKUP_STAGE = "supabase_inventory"
    materials = read_inventory("listMaterials")
    announcements = read_inventory("listAnnouncements")
    validate_snapshot(materials, root_index, "materials.json")
    validate_snapshot(announcements, root_index, "announcements.json")

    # Deterministic content hash: no re-upload when nothing changed. Always
    # back up full record objects, including raw JSON and admin edits.
    material_bytes = compact_json({"schema": 1, "table": "study_materials", "records": materials})
    announce_bytes = compact_json({"schema": 1, "table": "announcements", "records": announcements})
    BACKUP_STAGE = "snapshot_upload"
    _, changed_mat = drive.put(root, "materials.json", material_bytes,
                               "application/json", root_index)
    _, changed_ann = drive.put(root, "announcements.json", announce_bytes,
                               "application/json", root_index)

    # Keep a dated historical version on a day when content changes (and on
    # first backup), without deleting older versions. If same-day records change,
    # Drive retains revision history for that daily file.
    today = datetime.now(timezone.utc).date().isoformat()
    if changed_mat:
        drive.put(daily_folder, today + "-materials.json", material_bytes,
                  "application/json", daily_index)
    if changed_ann:
        drive.put(daily_folder, today + "-announcements.json", announce_bytes,
                  "application/json", daily_index)

    max_pdfs = max(0, min(100, int(os.getenv("GDRIVE_BACKUP_MAX_PDFS_PER_RUN", "10"))))
    max_bytes = max(1, min(100, int(os.getenv("GDRIVE_BACKUP_MAX_PDF_MIB", "25")))) * 1024 * 1024
    copied, attempted, existing_count, pending, errors, pdf_map = 0, 0, 0, 0, [], {}
    # Rotate the starting point every hour. Otherwise ten permanently broken
    # URLs could exhaust the attempt budget forever and starve later PDFs.
    rotation = (int(time.time() // 3600) * max(1, max_pdfs)) % max(1, len(materials))
    BACKUP_STAGE = "pdf_backup"
    pdf_rows = materials[rotation:] + materials[:rotation]
    for row in pdf_rows:
        url = str(row.get("pdf_url") or "").strip()
        if not url:
            errors.append({"id": str(row.get("id") or ""), "reason": "no PDF URL"})
            continue
        name = hashlib.sha256(url.encode("utf-8")).hexdigest() + ".pdf"
        cached = pdf_index.get(name)
        if cached:
            existing_count += 1
            pdf_map[str(row.get("id") or url)] = {"drive_id": cached["id"], "name": name}
            continue
        if attempted >= max_pdfs:
            pending += 1
            continue
        attempted += 1
        try:
            blob = fetch_pdf(url, max_bytes)
            file_id, _ = drive.put(pdf_folder, name, blob, "application/pdf", pdf_index)
            pdf_map[str(row.get("id") or url)] = {"drive_id": file_id, "name": name,
                                                 "sha256": digest(blob), "bytes": len(blob)}
            copied += 1
        except (ValueError, requests.RequestException, RuntimeError) as exc:
            errors.append({"id": str(row.get("id") or ""),
                           "reason": str(exc)[:110]})

    report = {"status": "complete" if not errors and not pending else "partial",
              "materials": len(materials), "announcements": len(announcements),
              "snapshots_updated": {"materials": changed_mat, "announcements": changed_ann},
              "pdfs_copied": copied, "pdfs_already_present": existing_count,
              "pdfs_pending": pending, "pdf_errors": errors[:30],
              "drive_root_id": root}
    # Manifest intentionally never contains tokens or raw school data; use the
    # private snapshots for full records. Keep private folder permissions.
    BACKUP_STAGE = "manifest_upload"
    drive.put(root, "backup-manifest.json", compact_json({
        "created_at": stamp(), **report, "pdf_files_seen": len(pdf_map)}),
        "application/json", root_index)
    record_status(**report)
    print("Google Drive backup: " + json.dumps(
        {k: v for k, v in report.items() if k not in ("pdf_errors", "drive_root_id")}
    ), flush=True)
    return 0 if report["status"] == "complete" else 1


def main():
    try:
        return backup()
    except Exception as exc:
        # No secret values or message bodies in Action logs.
        kind = type(exc).__name__
        details = {"status": "failed", "error_type": kind, "stage": BACKUP_STAGE}
        if isinstance(exc, OAuthRefreshError):
            details["oauth_error"] = exc.oauth_error
            details["http_status"] = exc.http_status
        print("Google Drive backup FAILED: " + json.dumps(details, sort_keys=True) +
              "; primary EduSecure/Supabase sync is not blocked by backup failure.",
              flush=True)
        record_status(**details)
        return 1


if __name__ == "__main__":
    sys.exit(main())
