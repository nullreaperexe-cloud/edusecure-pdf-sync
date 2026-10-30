from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Set

import requests
from selenium.common.exceptions import WebDriverException

import sync as legacy
import title_cleaner as intelligence
import groq_title as ai_title
import supabase_announcements as announcements
import supabase_materials as supabase_store
import supabase_gateway
import pdf_integrity as pdfguard
import personal_notice_filter as privacy
from reliable_http import request as http_request, canonical_url
from push_bridge import notify_published

START_URL = legacy.START_URL
FIREBASE_PROJECT_ID = "academyvault-5d1eb"
FIREBASE_API_KEY = "AIzaSyATxKki6gkNWic_CnoGbZnOZjAUj1lbKGI"
FIREBASE_ADMIN_EMAIL = os.environ.get("FIREBASE_ADMIN_EMAIL") or "nullreaper.exe@gmail.com"
FIREBASE_ADMIN_PASSWORD = os.environ.get("FIREBASE_ADMIN_PASSWORD", "")
EDUSECURE_USERNAME = os.environ.get("EDUSECURE_USERNAME", "")
EDUSECURE_PASSWORD = os.environ.get("EDUSECURE_PASSWORD", "")
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "25"))
TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date()
REPORT_FILE = Path("sync_report.json")

legacy.EDUSECURE_USERNAME = EDUSECURE_USERNAME
legacy.EDUSECURE_PASSWORD = EDUSECURE_PASSWORD


def clean(value: Any) -> str:
    return legacy.clean_text(str(value or ""))


def normalize_subject_name(value: Any) -> str:
    """Apply the website's canonical subject policy."""
    return intelligence.normalize_subject(value)


def eligible_for_recovery(
    msg_date: Optional[date], *,
    pdf_cutoff: date,
    announcement_cutoff: date,
    known_pdf_history: bool,
    full_history: bool = False,
) -> tuple[bool, bool]:
    """Reconcile source messages against Supabase, never by date alone.

    Ordinary checks use bounded recovery windows for speed; periodic complete
    audits must revisit ALL dated PDFs and all non-PDF messages. Exact PDF URL
    and deterministic announcement ID checks still decide whether to insert.
    """
    if full_history:
        return msg_date is not None, True
    if msg_date is None:
        return False, True
    pdf_ok = not (msg_date < pdf_cutoff if known_pdf_history else msg_date <= pdf_cutoff)
    return pdf_ok, msg_date >= announcement_cutoff


def parse_date_text(text: str) -> Optional[date]:
    text = clean(text)
    patterns = [
        (r"\b([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\b", "%b %d %Y"),
        (r"\b([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\b", "%B %d %Y"),
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", "%Y %m %d"),
    ]
    for pattern, fmt in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            return datetime.strptime(" ".join(m.groups()), fmt).date()
        except ValueError:
            pass
    return None


class FirebaseAuthenticationError(RuntimeError):
    """Safe, actionable Firebase Auth error. Never includes passwords/tokens."""


_FIREBASE_LOGIN_HELP = {
    "INVALID_LOGIN_CREDENTIALS": "Email/password rejected. Update the GitHub Actions FIREBASE_ADMIN_EMAIL and FIREBASE_ADMIN_PASSWORD secrets using an existing Firebase Authentication user.",
    "INVALID_PASSWORD": "Incorrect password. Reset/update FIREBASE_ADMIN_PASSWORD in GitHub Actions using the same Firebase Authentication user.",
    "EMAIL_NOT_FOUND": "Account does not exist in this Firebase project. Check FIREBASE_ADMIN_EMAIL and the users in Firebase Console > Authentication > Users.",
    "INVALID_EMAIL": "Invalid Firebase admin email. Correct the FIREBASE_ADMIN_EMAIL GitHub Actions secret.",
    "USER_DISABLED": "Firebase administrator account is disabled. Enable it in Firebase Authentication > Users.",
    "OPERATION_NOT_ALLOWED": "Email/password sign-in is disabled. Enable it in Firebase Console > Authentication > Sign-in method.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Firebase temporarily blocked repeated failed sign-ins. Check credentials and retry later.",
    "API_KEY_INVALID": "Firebase API key is invalid. Verify the academyvault-5d1eb project settings.",
    "PROJECT_NOT_FOUND": "The configured Firebase project does not exist or is inaccessible.",
    "CONFIGURATION_NOT_FOUND": "Firebase Authentication is not configured for the selected project.",
    "MISSING_PASSWORD": "FIREBASE_ADMIN_PASSWORD secret is missing or blank in GitHub Actions.",
}


def firebase_sign_in() -> str:
    """Sign in for Firestore; report Firebase's *code*, never credentials.

    HTTP 400 usually means a rejected sign-in, not a Firestore data problem.
    A bad password cannot be repaired in code: the matching Firebase Auth
    account and GitHub secret must be updated.
    """
    if not FIREBASE_ADMIN_EMAIL or "@" not in FIREBASE_ADMIN_EMAIL:
        raise FirebaseAuthenticationError(
            "FIREBASE_ADMIN_EMAIL is missing or invalid. Set it in GitHub Actions secrets."
        )
    if not FIREBASE_ADMIN_PASSWORD or not FIREBASE_ADMIN_PASSWORD.strip():
        raise FirebaseAuthenticationError(
            "FIREBASE_ADMIN_PASSWORD is missing/empty. Add the Firebase Authentication user's password as a GitHub Actions secret."
        )

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = {"email": FIREBASE_ADMIN_EMAIL, "password": FIREBASE_ADMIN_PASSWORD, "returnSecureToken": True}
    response = http_request("POST", url, json=payload, timeout=25)
    try:
        if response.ok:
            data = response.json()
            token = data.get("idToken", "") if isinstance(data, dict) else ""
            if token:
                print("Firebase administrator authentication succeeded.")
                return token
            raise FirebaseAuthenticationError(
                "Firebase sign-in returned HTTP 200 but no ID token. Check Firebase Authentication configuration."
            )

        # Only allow known uppercase Firebase error codes from the response.
        # Never echo raw response text, API keys, email addresses or passwords.
        try:
            error_data = response.json()
            error = error_data.get("error") or {} if isinstance(error_data, dict) else {}
            raw_code = str(error.get("message", "") if isinstance(error, dict) else "").split(" : ", 1)[0].strip()
        except (ValueError, TypeError, AttributeError):
            raw_code = ""
        code = raw_code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", raw_code) else "UNKNOWN_FIREBASE_ERROR"
        guidance = _FIREBASE_LOGIN_HELP.get(
            code, "Check GitHub FIREBASE_ADMIN_EMAIL/FIREBASE_ADMIN_PASSWORD and Firebase Console Authentication settings."
        )
        raise FirebaseAuthenticationError(
            f"Firebase administrator authentication failed (HTTP {response.status_code}; code {code}). {guidance}"
        )
    finally:
        response.close()


def firestore_headers(id_token: str = "") -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    return headers


def decode_value(value: Dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "timestampValue", "integerValue", "doubleValue", "booleanValue"):
        if key in value:
            return value[key]
    return None


def patch_subject(document_name: str, subject: str, id_token: str) -> bool:
    """Patch only the subject field of one existing Firestore material."""
    if not document_name or not subject:
        return False
    response = requests.patch(
        f"https://firestore.googleapis.com/v1/{document_name}",
        params={
            "key": FIREBASE_API_KEY,
            "updateMask.fieldPaths": "subject",
        },
        headers=firestore_headers(id_token),
        json={"fields": {"subject": {"stringValue": subject}}},
        timeout=25,
    )
    return response.ok


def list_firestore_materials(id_token: str = "") -> list[Dict[str, Any]]:
    """Compatibility name: the active source is new 8aPDF Supabase."""
    return supabase_store.list_materials()



def existing_state(materials: list[Dict[str, Any]]) -> tuple[Set[str], Optional[date], int]:
    urls: Set[str] = set()
    latest: Optional[date] = None
    orders: list[int] = []
    for item in materials:
        url = canonical_url(clean(item.get("pdf_url")))
        if url:
            urls.add(url)
        try:
            if item.get("order") is not None:
                orders.append(int(item["order"]))
        except Exception:
            pass
        for raw in (item.get("source_date"), item.get("title"), item.get("description")):
            if not raw:
                continue
            s = str(raw)
            d: Optional[date]
            try:
                if re.match(r"^\d{4}-\d{2}-\d{2}T", s):
                    d = datetime.fromisoformat(s.replace("Z", "+00:00")).date()
                else:
                    d = parse_date_text(s)
            except Exception:
                d = parse_date_text(s)
            if d and (latest is None or d > latest):
                latest = d
    next_order = (min(orders) - 1) if orders else 1
    return urls, latest, next_order


def fs_fields(item: Dict[str, str], source_date: date, order: int) -> Dict[str, Any]:
    """Final Firestore safety gate: never upload raw EduSecure UI text."""
    safe = intelligence.finalize_material_fields(item)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_ts = datetime(source_date.year, source_date.month, source_date.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    subject = normalize_subject_name(safe.get("subject")) or "Circular"
    return {
        "title": {"stringValue": clean(safe.get("title"))},
        "subject": {"stringValue": subject},
        "description": {"stringValue": clean(safe.get("description")) or clean(safe.get("title"))},
        "pdf_url": {"stringValue": clean(safe.get("url"))},
        "source": {"stringValue": "EduSecure GitHub Sync"},
        "source_date": {"timestampValue": source_ts},
        "order": {"integerValue": str(order)},
        "created_at": {"timestampValue": now},
    }


def upload_firestore(item: Dict[str, str], source_date: date, order: int, id_token: str = "") -> bool:
    """Compatibility name: uploads directly to 8aPDF Supabase (never Firestore)."""
    return supabase_store.upload_material(item, source_date, order)



def resolve_target_url(driver, target: Dict[str, str]) -> Optional[str]:
    path = target.get("path", "")
    js = r"""
    const path = arguments[0];
    function getEl(p) {
      if (!p) return null;
      if (p.startsWith('id:')) return document.getElementById(p.slice(3));
      if (p.startsWith('css:')) return document.querySelector(p.slice(4));
      return null;
    }
    function abs(u) { try { return new URL(u, location.href).href; } catch(e) { return ''; } }
    let el = getEl(path);
    if (!el) return '';
    el = el.closest('a,button,[onclick],form') || el;
    const nodes = [el, ...Array.from(el.querySelectorAll ? el.querySelectorAll('a[href],[data-url],[data-href],[data-src],[src],form[action]') : [])];
    for (const node of nodes) {
      for (const attr of ['href','data-url','data-href','data-src','src','action']) {
        const raw = node.getAttribute && node.getAttribute(attr);
        if (raw && raw !== '#' && !/^javascript:/i.test(raw)) {
          const u = abs(raw);
          if (/^https?:/i.test(u)) return u;
        }
      }
    }
    const code = (el.getAttribute && el.getAttribute('onclick')) || '';
    const quoted = code.match(/[\"'](https?:\/\/[^\"']+|\/[^\"']+)[\"']/i);
    if (quoted) {
      const u = abs(quoted[1]);
      if (/^https?:/i.test(u)) return u;
    }
    return '';
    """
    try:
        value = clean(driver.execute_script(js, path))
        return value or None
    except Exception:
        return None


def valid_attachment_url(url: Optional[str], before_url: str = "") -> bool:
    if not url:
        return False
    u = clean(url)
    if not re.match(r"^https?://", u, re.I):
        return False
    if before_url and u.rstrip("/") == before_url.rstrip("/"):
        return False
    low = u.lower()
    if "dashboard.aspx" in low or "login" in low:
        return False
    return True



def is_pdf_attachment_url(url: Optional[str]) -> bool:
    """Only real PDF URLs belong in the PDF library."""
    if not url:
        return False
    value = clean(url)
    return bool(re.search(r"\.pdf(?:$|[?#])", value, flags=re.I))



def extract_attachment_url(driver, app_handle: str) -> Optional[str]:
    direct = legacy.extract_pdf_from_current_page(driver)
    if valid_attachment_url(direct):
        return clean(direct)

    target = legacy.visible_attachment_target(driver, set())
    if not target:
        return None

    before_url = driver.current_url
    href = resolve_target_url(driver, target)
    if valid_attachment_url(href, before_url):
        print(f"Attachment URL resolved directly: {href}")
        return href

    before_tabs = set(driver.window_handles)
    if not legacy.click_path(driver, target.get("path", "")):
        return None
    time.sleep(0.8)

    after_tabs = set(driver.window_handles)
    new_tabs = list(after_tabs - before_tabs)
    if new_tabs:
        try:
            driver.switch_to.window(new_tabs[-1])
            time.sleep(0.8)
            current = driver.current_url
            nested = legacy.extract_pdf_from_current_page(driver)
            candidate = nested if valid_attachment_url(nested) else current
            result = clean(candidate) if valid_attachment_url(candidate, before_url) else None
            driver.close()
            driver.switch_to.window(app_handle)
            return result
        except WebDriverException:
            try:
                driver.switch_to.window(app_handle)
            except Exception:
                pass
            return None

    current = driver.current_url
    nested = legacy.extract_pdf_from_current_page(driver)
    candidate = nested if valid_attachment_url(nested) else current
    if valid_attachment_url(candidate, before_url):
        result = clean(candidate)
        try:
            driver.back()
            legacy.wait_ready(driver)
            time.sleep(0.6)
        except Exception:
            pass
        return result
    return None


def save_report(report: Dict[str, Any]) -> None:
    # Atomic replacement preserves the previous valid report if the process dies.
    temporary = REPORT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(REPORT_FILE)


def main() -> int:
    global TODAY
    TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    save_report({"status": "starting", "started_at": datetime.now(timezone.utc).isoformat()})
    if not EDUSECURE_USERNAME or not EDUSECURE_PASSWORD:
        print("Missing EDUSECURE_USERNAME/EDUSECURE_PASSWORD GitHub secrets.")
        save_report({"status": "failed", "failures": ["EduSecure credentials are missing"]})
        return 2

    print("=== STEP 1: READING EXISTING 8aPDF SUPABASE DATA ===")
    id_token = ""  # Firebase administrator login is no longer used.
    try:
        supabase_gateway.health()
        materials = list_firestore_materials(id_token)
        # Verify BOTH migrated tables before launching Selenium.  This is a
        # fresh published-ID inventory, never a historical Firestore cache.
        existing_announcement_ids: Set[str] = announcements.list_complete_source_ids(id_token)
    except Exception as exc:
        # Fail closed instead of writing PDFs without duplicate state.
        detail = str(exc)
        print("Supabase inventory read failed: " + detail[:300])
        save_report({"status": "failed", "failures": [detail[:300]]})
        return 2

    existing_urls, latest_date, next_order = existing_state(materials)
    full_history = os.environ.get("EDUSECURE_FULL_RECONCILIATION", "0") == "1"
    safe_cutoff = TODAY - timedelta(days=1)
    recovery_floor = TODAY - timedelta(days=int(os.environ.get("PDF_RECOVERY_DAYS", "21")))
    cutoff = min(latest_date or safe_cutoff, recovery_floor)
    announcement_cutoff = TODAY - timedelta(days=int(os.environ.get("ANNOUNCEMENT_RECOVERY_DAYS", "7")))
    if latest_date:
        print(f"Latest existing PDF source date: {latest_date.isoformat()}")
    else:
        print(f"No readable existing PDF date; safe cutoff retained: {safe_cutoff.isoformat()}")
    print(f"Existing PDF URLs loaded from Supabase: {len(existing_urls)}")
    if full_history:
        print("FULL HISTORY RECONCILIATION: scan all available EduSecure messages, "
              "regardless of age; publish ONLY missing Supabase PDF URLs / announcement IDs.")
    else:
        print(f"Normal PDF lookback starts {cutoff.isoformat()}; "
              f"announcement lookback starts {announcement_cutoff.isoformat()}.")
    print("After Supabase publication, worker verifies Drive snapshots and missing PDFs.")

    report: Dict[str, Any] = {
        "cutoff": cutoff.isoformat(),
        "reconciliation_mode": "full_history" if full_history else "recent",
        "source_messages_examined": 0,
        "dashboard_bottom_confirmed": False,
        "existing_count": len(existing_urls),
        "messages_opened": 0,
        "newer_messages_seen": 0,
        "attachments_found": 0,
        "duplicates_skipped": 0,
        "announcements_created": [],
        "announcement_duplicates_skipped": 0,
        "announcement_messages_ignored": 0,
        "personal_messages_skipped": 0,
        "announcement_ai_retries": 0,
        "uploaded": [],
        "failures": [],
    }

    driver = None
    processed: Set[str] = set()
    old_confirmations = 0
    bottom_confirmations = 0

    try:
        driver = legacy.make_driver()
        print("Opening EduSecure Dashboard...")
        driver.get(START_URL)
        if not legacy.auto_login_edusecure(driver):
            print("❌ EduSecure auto-login failed in headless mode.")
            legacy.save_debug_screenshot(driver, "debug_edusecure_login.png")
            report["failures"].append("EduSecure login failed")
            save_report(report)
            return 2

        driver.get(START_URL)
        legacy.wait_ready(driver)
        time.sleep(0.9)
        app_handle = driver.current_window_handle
        legacy.restore_dashboard_scroll_position(driver, 0)

        while len(report["uploaded"]) < MAX_UPLOADS:
            driver.switch_to.window(app_handle)
            legacy.restore_app_after_pdf(driver, app_handle)
            if "dashboard.aspx" not in driver.current_url.lower():
                driver.get(START_URL)
                legacy.wait_ready(driver)
                time.sleep(0.65)

            visible = legacy.find_visible_dashboard_messages_v29(driver, processed)
            if not visible:
                scroll_result = legacy.dashboard_scroll_v24(driver)
                print(f"Dashboard scroll: {scroll_result}")
                if scroll_result.get("atBottom"):
                    bottom_confirmations += 1
                else:
                    bottom_confirmations = 0
                if bottom_confirmations >= 5:
                    report["dashboard_bottom_confirmed"] = True
                    break
                continue

            message = visible[0]
            report["source_messages_examined"] += 1
            message_text = clean(message.get("text"))
            fp = message.get("fp") or legacy.fingerprint(message_text)
            if fp:
                processed.add(fp)
            if privacy.is_personal_notice(message_text):
                report["personal_messages_skipped"] += 1
                print("Private student message skipped at EduSecure dashboard.")
                continue

            msg_date = legacy.extract_message_date(message_text)

            # Normal cycles use date windows; full audits bypass those windows
            # but still check Supabase identities and never duplicate records.
            pdf_date_eligible, announcement_date_eligible = eligible_for_recovery(
                msg_date, pdf_cutoff=cutoff,
                announcement_cutoff=announcement_cutoff,
                known_pdf_history=bool(latest_date),
                full_history=full_history,
            )
            if msg_date is None:
                print("Undated EduSecure message -> announcement-eligible only")

            if msg_date is not None and not pdf_date_eligible and not announcement_date_eligible:
                old_confirmations += 1
                print(
                    f"Reached message older than both cutoffs: {msg_date.isoformat()} "
                    f"(PDF cutoff {cutoff.isoformat()}, announcement cutoff {announcement_cutoff.isoformat()})"
                )
                if old_confirmations >= 4:
                    break
                continue

            old_confirmations = 0
            report["newer_messages_seen"] += 1
            saved_position = legacy.get_dashboard_scroll_position(driver)
            print(
                f"Opening EduSecure message dated "
                f"{msg_date.isoformat() if msg_date else 'UNKNOWN'}: {message_text[:220]}"
            )

            if not legacy.real_click_message_v29(driver, message):
                report["failures"].append(f"Could not open message: {message_text[:120]}")
                legacy.restore_dashboard_scroll_position(driver, saved_position)
                continue

            report["messages_opened"] += 1
            detail_text = legacy.app_current_text(driver)
            if privacy.is_personal_notice(message_text, detail_text):
                report["personal_messages_skipped"] += 1
                print("Private student message skipped after reading detail.")
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue
            attachment_url = extract_attachment_url(driver, app_handle)
            pdf_url = attachment_url if is_pdf_attachment_url(attachment_url) else None

            driver.switch_to.window(app_handle)
            legacy.restore_app_after_pdf(driver, app_handle)

            if not pdf_url:
                # An explicitly promised PDF with a broken link is a failed PDF,
                # never an invented text-only announcement.
                if not attachment_url and re.search(r'(?:PDF\s+attached|attached\s+PDF|attached\s+PDF\s+file)', message_text, re.I):
                    report['failures'].append('Explicit PDF attachment could not be extracted: ' + message_text[:130])
                    legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                    continue
                if not announcement_date_eligible:
                    print("Non-PDF message is outside live recovery window -> historical backfill owns it")
                    legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                    continue

                if attachment_url:
                    print("Non-PDF attachment found -> routing message to Announcements with attachment")
                else:
                    print("No PDF attachment -> routing message to Announcements")

                status, announcement_item = announcements.process_no_attachment_message(
                    message_text=message_text,
                    detail_text=detail_text,
                    message_date=msg_date,
                    id_token=id_token,
                    existing_source_ids=existing_announcement_ids,
                    attachment_url=clean(attachment_url),
                )
                if status == "created" and announcement_item:
                    report["announcements_created"].append({
                        "title": announcement_item.get("title", ""),
                        "category": announcement_item.get("category", ""),
                        "source_date": msg_date.isoformat() if msg_date else "",
                    })
                elif status == "duplicate":
                    report["announcement_duplicates_skipped"] += 1
                elif status == "ignored":
                    report["announcement_messages_ignored"] += 1
                elif status == "retry":
                    report["announcement_ai_retries"] += 1
                    print("Announcement processing deferred; the next sync cycle will retry.")
                else:
                    report["failures"].append(
                        f"Announcement upload failed for message dated {msg_date.isoformat() if msg_date else 'UNKNOWN'}"
                    )
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            report["attachments_found"] += 1

            if msg_date is None:
                report["failures"].append("Undated PDF message cannot be safely uploaded")
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            if not pdf_date_eligible:
                print(
                    "Attachment found, but it is same-date/older than the strict PDF cutoff -> "
                    "leave it in existing PDF history and do not create an Announcement."
                )
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            normalized = canonical_url(clean(pdf_url))
            if normalized in existing_urls:
                report["duplicates_skipped"] += 1
                print("Duplicate PDF URL -> skip")
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            # Use the selected dashboard message, not potentially stale full-page
            # detail DOM text from another EduSecure card.
            original_evidence = [message_text]
            extracted_title = legacy.make_title(
                message_text,
                pdf_url,
                len(report["uploaded"]) + 1,
            )
            pdf_text, pdf_state = pdfguard.fetch_pdf_text(pdf_url)
            if pdf_state == "not-a-pdf":
                report["failures"].append("Attachment URL did not return a PDF: " + pdf_url)
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue
            if pdf_text:
                original_evidence.append(pdf_text[:2500])
            initially_detected = normalize_subject_name(
                legacy.detect_subject(message_text)
            )
            base_item = intelligence.finalize_material_fields(
                {
                    "title": extracted_title,
                    "subject": initially_detected,
                    "description": extracted_title,
                    "url": pdf_url,
                    "_evidence": original_evidence,
                }
            )

            # Generate the NEW material title with Groq's free router only.
            # If Groq is unavailable/rate-limited, ai_title.generate_title()
            # returns the existing deterministic clean title so syncing continues.
            generated_title = ai_title.generate_title(
                original_evidence,
                subject=base_item["subject"],
                fallback_title=base_item["title"],
            )
            final_title, verification = pdfguard.select_verified_title(
                message_text, generated_title, base_item["title"], pdf_text,
            )
            print(f"PDF title integrity: {verification}; extraction={pdf_state}; title={final_title}")
            item = {
                **base_item,
                "title": final_title,
                "description": final_title,
            }

            # Different URLs may be separate parts of the same lesson with the
            # same title/date. Only an exact URL identity can suppress an upload.

            print("Uploading exact fields to Supabase after intelligence safety gate:")
            print(f"  Title: {item['title']}")
            print(f"  Subject: {item['subject']}")
            print(f"  Description: {item['description']}")
            print(f"  PDF Link: {item['url']}")

            if upload_firestore(item, msg_date, next_order, id_token):
                report["uploaded"].append({**item, "source_date": msg_date.isoformat()})
                existing_urls.add(normalized)
                next_order -= 1
            else:
                report["failures"].append(f"Supabase upload failed: {pdf_url}")

            legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)

        if report["uploaded"] or report["announcements_created"]:
            # The former push wake-up needed a Firebase Auth ID token. Supabase
            # publication itself must never fail due to a retired Firebase hook.
            report["push"] = {"ok": False, "skipped": True, "reason": "Firebase push wake-up disabled after Supabase migration"}
        report["status"] = "failed" if report["failures"] else "deferred" if report["announcement_ai_retries"] else "completed"
        save_report(report)
        print("\n=== SYNC COMPLETE ===")
        print(f"Reconciliation mode: {report['reconciliation_mode']}")
        print(f"Messages examined: {report['source_messages_examined']}")
        print(f"Dashboard history bottom reached: {report['dashboard_bottom_confirmed']}")
        print(f"Newer messages seen: {report['newer_messages_seen']}")
        print(f"Messages opened: {report['messages_opened']}")
        print(f"Attachments found: {report['attachments_found']}")
        print(f"PDF duplicates skipped: {report['duplicates_skipped']}")
        print(f"New PDFs uploaded: {len(report['uploaded'])}")
        print(f"New announcements uploaded: {len(report['announcements_created'])}")
        print(f"Announcement duplicates skipped: {report['announcement_duplicates_skipped']}")
        print(f"Announcement messages ignored: {report['announcement_messages_ignored']}")
        print(f"Announcements deferred for retry: {report['announcement_ai_retries']}")
        print(f"Private student messages blocked: {report['personal_messages_skipped']}")
        print(f"Failures: {len(report['failures'])}")
        return 1 if report["failures"] else 75 if report["announcement_ai_retries"] else 0
    except Exception as exc:
        report["status"] = "failed"
        report["failures"].append("Sync cycle interrupted: " + type(exc).__name__)
        save_report(report)
        print("Sync cycle interrupted; diagnostics were saved: " + type(exc).__name__)
        return 1
    finally:
        try:
            if driver is not None:
                driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
