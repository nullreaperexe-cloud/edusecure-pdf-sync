from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

import requests
from selenium.common.exceptions import WebDriverException

import sync as legacy

START_URL = legacy.START_URL
FIREBASE_PROJECT_ID = "academyvault-5d1eb"
FIREBASE_API_KEY = "AIzaSyATxKki6gkNWic_CnoGbZnOZjAUj1lbKGI"
FIREBASE_ADMIN_EMAIL = os.environ.get("FIREBASE_ADMIN_EMAIL") or "nullreaper.exe@gmail.com"
FIREBASE_ADMIN_PASSWORD = os.environ.get("FIREBASE_ADMIN_PASSWORD", "")
EDUSECURE_USERNAME = os.environ.get("EDUSECURE_USERNAME", "")
EDUSECURE_PASSWORD = os.environ.get("EDUSECURE_PASSWORD", "")
MAX_UPLOADS = int(os.environ.get("MAX_UPLOADS", "25"))
TODAY = date.today()
REPORT_FILE = Path("sync_report.json")

legacy.EDUSECURE_USERNAME = EDUSECURE_USERNAME
legacy.EDUSECURE_PASSWORD = EDUSECURE_PASSWORD


def clean(value: Any) -> str:
    return legacy.clean_text(str(value or ""))


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


def firebase_sign_in() -> str:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = {"email": FIREBASE_ADMIN_EMAIL, "password": FIREBASE_ADMIN_PASSWORD, "returnSecureToken": True}
    response = requests.post(url, json=payload, timeout=25)
    if response.ok:
        token = response.json().get("idToken", "")
        if token:
            print("Firebase admin authentication done ✅")
            return token
    print(f"Firebase Auth warning: HTTP {response.status_code}. Will try Firestore rules directly.")
    return ""


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


def list_firestore_materials(id_token: str) -> list[Dict[str, Any]]:
    base = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/study_materials"
    params: Dict[str, Any] = {"pageSize": 1000, "key": FIREBASE_API_KEY}
    docs: list[Dict[str, Any]] = []
    while True:
        response = requests.get(base, params=params, headers=firestore_headers(id_token), timeout=30)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        for raw in body.get("documents", []):
            fields = {k: decode_value(v) for k, v in (raw.get("fields") or {}).items()}
            fields["_name"] = raw.get("name", "")
            docs.append(fields)
        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return docs


def existing_state(materials: list[Dict[str, Any]]) -> tuple[Set[str], Optional[date], int]:
    urls: Set[str] = set()
    latest: Optional[date] = None
    orders: list[int] = []
    for item in materials:
        url = clean(item.get("pdf_url")).lower()
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
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source_ts = datetime(source_date.year, source_date.month, source_date.day, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "title": {"stringValue": clean(item.get("title"))},
        "subject": {"stringValue": clean(item.get("subject")) or "Circular"},
        "description": {"stringValue": clean(item.get("description")) or clean(item.get("title"))},
        "pdf_url": {"stringValue": clean(item.get("url"))},
        "source": {"stringValue": "EduSecure GitHub Sync"},
        "source_date": {"timestampValue": source_ts},
        "order": {"integerValue": str(order)},
        "created_at": {"timestampValue": now},
    }


def upload_firestore(item: Dict[str, str], source_date: date, order: int, id_token: str) -> bool:
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/study_materials"
    response = requests.post(
        url,
        params={"key": FIREBASE_API_KEY},
        headers=firestore_headers(id_token),
        json={"fields": fs_fields(item, source_date, order)},
        timeout=30,
    )
    if response.ok:
        print("✅ Firestore upload confirmed by server")
        return True
    print(f"❌ Firestore upload failed: HTTP {response.status_code}")
    print(response.text[:1200])
    return False


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
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def main() -> int:
    if not EDUSECURE_USERNAME or not EDUSECURE_PASSWORD:
        print("Missing EDUSECURE_USERNAME/EDUSECURE_PASSWORD GitHub secrets.")
        return 2

    print("=== STEP 1: READING EXISTING FIRESTORE DATA ===")
    id_token = firebase_sign_in()
    try:
        materials = list_firestore_materials(id_token)
    except Exception as exc:
        print(f"Could not read Firestore: {exc}")
        return 2

    existing_urls, latest_date, next_order = existing_state(materials)
    safe_cutoff = TODAY - timedelta(days=1)
    cutoff = latest_date or safe_cutoff
    if latest_date:
        print(f"Latest existing PDF source date: {latest_date.isoformat()}")
    else:
        print(f"No readable existing PDF date; safe cutoff retained: {safe_cutoff.isoformat()}")
    print(f"Existing PDF URLs loaded: {len(existing_urls)}")

    report: Dict[str, Any] = {
        "cutoff": cutoff.isoformat(),
        "existing_count": len(existing_urls),
        "messages_opened": 0,
        "newer_messages_seen": 0,
        "attachments_found": 0,
        "duplicates_skipped": 0,
        "uploaded": [],
        "failures": [],
    }

    driver = legacy.make_driver()
    processed: Set[str] = set()
    old_confirmations = 0
    bottom_confirmations = 0

    try:
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
                    break
                continue

            message = visible[0]
            message_text = clean(message.get("text"))
            fp = message.get("fp") or legacy.fingerprint(message_text)
            if fp:
                processed.add(fp)

            msg_date = legacy.extract_message_date(message_text)
            if msg_date is None:
                print("Undated message -> skip")
                continue

            # If Firestore already has PDFs for the cutoff date, still scan that
            # same date: a later school message may contain a new PDF. URL
            # deduplication below prevents re-uploading older same-day PDFs.
            is_older = msg_date < cutoff if latest_date else msg_date <= cutoff
            if is_older:
                old_confirmations += 1
                print(f"Reached older date {msg_date.isoformat()} (cutoff {cutoff.isoformat()}); skipping")
                if old_confirmations >= 4:
                    break
                continue

            old_confirmations = 0
            report["newer_messages_seen"] += 1
            saved_position = legacy.get_dashboard_scroll_position(driver)
            print(f"Opening new message dated {msg_date.isoformat()}: {message_text[:220]}")

            if not legacy.real_click_message_v29(driver, message):
                report["failures"].append(f"Could not open message: {message_text[:120]}")
                legacy.restore_dashboard_scroll_position(driver, saved_position)
                continue

            report["messages_opened"] += 1
            detail_text = legacy.app_current_text(driver)
            pdf_url = extract_attachment_url(driver, app_handle)

            driver.switch_to.window(app_handle)
            legacy.restore_app_after_pdf(driver, app_handle)

            if not pdf_url:
                print("No PDF attachment in this message")
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            report["attachments_found"] += 1
            normalized = clean(pdf_url).lower()
            if normalized in existing_urls:
                report["duplicates_skipped"] += 1
                print("Duplicate PDF URL -> skip")
                legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            extracted_title = legacy.make_title(detail_text or message_text, pdf_url, len(report["uploaded"]) + 1)
            item = {
                "title": extracted_title,
                "subject": legacy.detect_subject(detail_text or message_text),
                "description": extracted_title,
                "url": pdf_url,
            }

            print("Uploading exact fields to Firestore:")
            print(f"  Title: {item['title']}")
            print(f"  Subject: {item['subject']}")
            print(f"  Description: {item['description']}")
            print(f"  PDF Link: {item['url']}")

            if upload_firestore(item, msg_date, next_order, id_token):
                report["uploaded"].append({**item, "source_date": msg_date.isoformat()})
                existing_urls.add(normalized)
                next_order -= 1
            else:
                report["failures"].append(f"Firestore upload failed: {pdf_url}")

            legacy.return_dashboard_and_restore_v25(driver, app_handle, saved_position)

        save_report(report)
        print("\n=== SYNC COMPLETE ===")
        print(f"Newer messages seen: {report['newer_messages_seen']}")
        print(f"Messages opened: {report['messages_opened']}")
        print(f"Attachments found: {report['attachments_found']}")
        print(f"Duplicates skipped: {report['duplicates_skipped']}")
        print(f"New PDFs uploaded: {len(report['uploaded'])}")
        print(f"Failures: {len(report['failures'])}")
        return 1 if report["failures"] else 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())