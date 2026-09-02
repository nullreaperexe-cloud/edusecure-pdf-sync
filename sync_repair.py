"""Single consolidated repair runner for the EduSecure -> 8aPDF automation.

This file contains the active fixes that were previously spread across
runner_v2.py, runner_v4.py, runner_v5.py and sync_repair.py.

Base engines remain in runner.py and sync.py. The GitHub workflow should run
this file directly.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

import runner


WEBSITE_URL = "https://eightapdf-study-library.nullreaper-exe.chatgpt.site/?i=1"
DOWNLOAD_DIR = "/tmp/edusecure-downloads"
BAD_URL_RE = re.compile(
    r"(?:/ParentApp/morelinks\.aspx(?:\?|$)|/images/loader\.gif(?:\?|$)|dashboard\.aspx(?:\?|$)|/login(?:\?|/|$))",
    re.I,
)

ORIGINAL_LIST = runner.list_firestore_materials
ORIGINAL_EXISTING_STATE = runner.existing_state
WEBSITE_LATEST_DATE: Optional[date] = None


def clean(value: Any) -> str:
    return runner.clean(value)


# ---------------------------------------------------------------------------
# Title + subject intelligence repair
# ---------------------------------------------------------------------------

def make_message_title(text: str, pdf_url: str, number: int = 0) -> str:
    """Use the actual EduSecure message/homework text as the uploaded title."""
    raw = clean(text)

    hw = re.search(r"Home\s*Work\s*:\s*(.*?)(?=\s*Class\s*Work\s*:|$)", raw, flags=re.I)
    cw = re.search(r"Class\s*Work\s*:\s*(.*)$", raw, flags=re.I)

    candidate = ""
    if hw:
        candidate = clean(hw.group(1))
        if candidate.lower() in {"-", "--", "nil", "none"}:
            candidate = ""
    if not candidate and cw:
        candidate = clean(cw.group(1))
    if not candidate:
        candidate = raw

    candidate = re.sub(r"^(School\s*Diary|Circular|Message)\s+", "", candidate, flags=re.I)
    candidate = re.sub(r"\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b", "", candidate)
    candidate = re.sub(
        r"\bAttachment\b|\bAttach\b|\bDownload\b|\bOpen\b|\bClick\s+Here\b",
        "",
        candidate,
        flags=re.I,
    )
    candidate = re.sub(r"\s+", " ", candidate).strip(" -:|,")

    if not candidate or candidate.lower() in {"attachment", "pdf", "document"}:
        candidate = runner.legacy.title_from_pdf_url(pdf_url)

    if len(candidate) > 125:
        candidate = candidate[:125].rsplit(" ", 1)[0]

    return candidate or f"PDF {number}".strip()


SUBJECT_ALIASES = {
    "math": "Mathematics",
    "maths": "Mathematics",
    "mathematics": "Mathematics",
    "science": "Science",
    "physics": "Science",
    "chemistry": "Science",
    "biology": "Science",
    "english": "English",
    "hindi": "Hindi",
    "punjabi": "Punjabi",
    "french": "French",
    "sanskrit": "Sanskrit",
    "computer": "Computer",
    "computer science": "Computer",
    "ict": "Computer",
    "artificial intelligence": "Artificial Intelligence",
    "ai": "Artificial Intelligence",
    "iot": "IoT",
    "internet of things": "IoT",
    "social science": "Social Science",
    "social studies": "Social Science",
    "sst": "Social Science",
    "history": "Social Science",
    "geography": "Social Science",
    "civics": "Social Science",
    "political science": "Social Science",
    "general knowledge": "GK",
    "gk": "GK",
    "evs": "EVS",
    "environmental studies": "EVS",
    "moral science": "Moral Science",
    "value education": "Moral Science",
    "physical education": "Physical Education",
    "pe": "Physical Education",
    "art": "Art",
    "music": "Music",
}

EXPLICIT_SUBJECT_RE = re.compile(
    r"\b(Artificial\s+Intelligence|Internet\s+of\s+Things|Computer\s+Science|"
    r"Political\s+Science|Social\s+Science|Social\s+Studies|Environmental\s+Studies|"
    r"General\s+Knowledge|Moral\s+Science|Value\s+Education|Physical\s+Education|"
    r"Mathematics|Maths|Math|Science|Physics|Chemistry|Biology|English|Hindi|Punjabi|"
    r"French|Sanskrit|Computer|ICT|AI|IoT|SST|History|Geography|Civics|GK|EVS|Art|Music|PE)\b",
    re.I,
)


def _canonical_subject(value: str) -> Optional[str]:
    key = clean(value).lower()
    return SUBJECT_ALIASES.get(key)


def detect_subject_smart(text: str, default: str = "General") -> str:
    """Identify the academic subject; message type is never treated as a subject.

    Priority:
    1. Explicit subject names/labels from EduSecure.
    2. Native-script signals (Gurmukhi, Sanskrit markers, Devanagari).
    3. Strong academic vocabulary scoring.
    4. Circular for real notices/circulars, otherwise General.
    """
    raw = clean(text)
    if not raw:
        return default

    # First inspect explicit subject labels such as "Subject: Hindi" or
    # "Home Work : Mathematics : ...". This is the highest-confidence signal.
    labeled_patterns = (
        r"(?:Subject|Sub)\s*[:\-]\s*([^:|\n]{2,40})",
        r"Home\s*Work\s*:\s*([^:|\n]{2,40})\s*:",
        r"Class\s*Work\s*:\s*([^:|\n]{2,40})\s*:",
    )
    for pattern in labeled_patterns:
        match = re.search(pattern, raw, flags=re.I)
        if not match:
            continue
        named = EXPLICIT_SUBJECT_RE.search(match.group(1))
        if named:
            canonical = _canonical_subject(named.group(1))
            if canonical:
                return canonical

    # Any explicit subject word anywhere is strong evidence. Longer/specialized
    # names are intentionally checked by the regex before short aliases.
    named = EXPLICIT_SUBJECT_RE.search(raw)
    if named:
        canonical = _canonical_subject(named.group(1))
        if canonical:
            return canonical

    # Native scripts: Gurmukhi is a strong Punjabi signal.
    if re.search(r"[\u0A00-\u0A7F]", raw):
        return "Punjabi"

    # Detect Sanskrit before generic Devanagari/Hindi. Keep this conservative so
    # normal Hindi grammar words do not get mislabeled as Sanskrit.
    if re.search(
        r"(?:संस्कृत(?:म्)?|श्लोक(?:ः|म्)?|सुभाषित|धातुरूप|शब्दरूप|संस्कृत\s*व्याकरण)",
        raw,
        flags=re.I,
    ):
        return "Sanskrit"

    # Devanagari content without a Sanskrit-specific signal is Hindi for this
    # school workflow. Example: "संबंधित पीडीएफ प्राप्त करें।" -> Hindi.
    if re.search(r"[\u0900-\u097F]", raw):
        return "Hindi"

    lower = raw.lower()
    scores: Dict[str, int] = {}

    def add(subject: str, points: int) -> None:
        scores[subject] = scores.get(subject, 0) + points

    keyword_rules = {
        "Mathematics": (
            r"\balgebra\b|\bgeometry\b|\barithmetic\b|\bfraction(?:s)?\b|\bdecimal(?:s)?\b|"
            r"\binteger(?:s)?\b|\brational\s+number(?:s)?\b|\blinear\s+equation(?:s)?\b|"
            r"\bmensuration\b|\bpercentage(?:s)?\b|\bratio\b|\bproportion\b|\bexponent(?:s)?\b",
            5,
        ),
        "Science": (
            r"\bforce\b|\bfriction\b|\bcell(?:s)?\b|\bmicroorganism(?:s)?\b|\bcombustion\b|"
            r"\bphotosynthesis\b|\becosystem\b|\bmetals?\b|\bnon[- ]?metals?\b|\breproduction\b|"
            r"\bchemical\s+reaction(?:s)?\b|\belectric(?:ity|\s+current)\b|\blight\b|\bsound\b",
            4,
        ),
        "Social Science": (
            r"\bconstitution\b|\bparliament\b|\bjudiciary\b|\bdemocracy\b|\bcolonial(?:ism)?\b|"
            r"\bresources?\b|\bagriculture\b|\bindustries\b|\bcivil\s+rights\b|\bmap\s+work\b",
            4,
        ),
        "Computer": (
            r"\bcomputer\b|\bcoding\b|\bprogramming\b|\bpython\b|\bhtml\b|\bcss\b|\bjavascript\b|"
            r"\bspreadsheet\b|\bms\s+excel\b|\bpowerpoint\b|\bdatabase\b|\bcybersecurity\b|\bnetworking\b",
            5,
        ),
        "Artificial Intelligence": (
            r"\bartificial\s+intelligence\b|\bmachine\s+learning\b|\bneural\s+network(?:s)?\b|"
            r"\bchatbot(?:s)?\b|\bcomputer\s+vision\b|\bnatural\s+language\s+processing\b",
            7,
        ),
        "IoT": (
            r"\binternet\s+of\s+things\b|\biot\b|\bsmart\s+device(?:s)?\b|\bsensor(?:s)?\b|"
            r"\bconnected\s+device(?:s)?\b",
            7,
        ),
        "French": (
            r"\bfrench\b|\bfrançais(?:e)?\b|\bcompréhension\b|\bconjugaison\b|\bgrammaire\b|"
            r"\bvocabulaire\b|\bbonjour\b|\bunseen\s+passage\s+in\s+french\b",
            7,
        ),
        "English": (
            r"\benglish\s+grammar\b|\benglish\s+literature\b|\breading\s+comprehension\b|"
            r"\bcreative\s+writing\b|\bnotice\s+writing\b|\bletter\s+writing\b|\bpoem\b|\bprose\b",
            5,
        ),
        "GK": (r"\bgeneral\s+knowledge\b|\bcurrent\s+affairs\b", 6),
        "EVS": (r"\benvironmental\s+studies\b|\benvironmental\s+science\b|\bevs\b", 6),
    }

    for subject, (pattern, points) in keyword_rules.items():
        hits = len(re.findall(pattern, lower, flags=re.I))
        if hits:
            add(subject, min(points * hits, points + 6))

    if scores:
        ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
        best_subject, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0
        if best_score >= 4 and best_score >= second_score + 1:
            return best_subject

    # School Diary is a MESSAGE TYPE, never an academic subject.
    if re.search(r"\b(?:Circular|Announcement|Notice)\b", raw, flags=re.I):
        return "Circular"
    return default


# ---------------------------------------------------------------------------
# Browser/network repair
# ---------------------------------------------------------------------------

def valid_real_attachment_url(url: Optional[str]) -> bool:
    """Accept any real HTTP(S) attachment URL, regardless of file extension."""
    value = clean(url)
    if not re.match(r"^https?://", value, re.I):
        return False
    if BAD_URL_RE.search(value):
        return False
    if value.lower().startswith(("javascript:", "data:", "blob:")):
        return False
    return True


def looks_like_attachment_url(url: Optional[str]) -> bool:
    """Network-log helper used only to rank likely attachment requests."""
    if not valid_real_attachment_url(url):
        return False

    low = clean(url).lower()
    parsed = urlparse(low)
    path_query = f"{parsed.path}?{parsed.query}"

    if "/studentinfo/homework/" in parsed.path.lower():
        return True

    strong_markers = (
        "downloadfile",
        "download-file",
        "download_file",
        "downloadattachment",
        "download",
        "attachment",
        "getfile",
        "get-file",
        "filedownload",
        "filehandler",
        "documenthandler",
        "viewdocument",
        "viewfile",
    )
    if any(marker in path_query for marker in strong_markers):
        return True

    return bool(re.search(r"\.(?:pdf|jpe?g|png|webp|docx?|xlsx?|pptx?|txt|zip)(?:\?|$)", low, re.I))


def make_driver_with_network_logs() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": DOWNLOAD_DIR,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        },
    )
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})

    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    driver = webdriver.Chrome(options=options)

    for command in ("Network.enable", "Page.enable"):
        try:
            driver.execute_cdp_cmd(command, {})
        except Exception:
            pass

    try:
        driver.execute_cdp_cmd(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": DOWNLOAD_DIR, "eventsEnabled": True},
        )
    except Exception:
        try:
            driver.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": DOWNLOAD_DIR},
            )
        except Exception:
            pass

    return driver


def drain_performance_log(driver) -> None:
    try:
        driver.get_log("performance")
    except Exception:
        pass


def _header_value(headers: object, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def attachment_url_from_performance(driver) -> Optional[str]:
    """Return the strongest real attachment URL observed in Chrome logs."""
    best: List[tuple[int, str, str]] = []
    try:
        entries = driver.get_log("performance")
    except Exception:
        return None

    for entry in entries:
        try:
            outer = json.loads(entry.get("message", "{}"))
            message = outer.get("message", {})
            method = message.get("method", "")
            params = message.get("params", {}) or {}
        except Exception:
            continue

        if method in ("Page.downloadWillBegin", "Browser.downloadWillBegin"):
            url = clean(params.get("url"))
            filename = clean(params.get("suggestedFilename"))
            if valid_real_attachment_url(url):
                score = 160 if looks_like_attachment_url(url) else 80
                if filename:
                    score += 30
                best.append((score, url, f"download event filename={filename!r}"))
            continue

        if method == "Network.responseReceived":
            response = params.get("response", {}) or {}
            url = clean(response.get("url"))
            if not valid_real_attachment_url(url):
                continue

            mime = clean(response.get("mimeType")).lower()
            headers = response.get("headers", {}) or {}
            content_type = _header_value(headers, "content-type").lower()
            disposition = _header_value(headers, "content-disposition").lower()

            score = 0
            if looks_like_attachment_url(url):
                score += 120
            if "attachment" in disposition or "filename=" in disposition:
                score += 100
            if "application/pdf" in mime or "application/pdf" in content_type:
                score += 95
            if score:
                best.append((score, url, f"response mime={mime!r} disposition={disposition[:120]!r}"))
            continue

        if method == "Network.requestWillBeSent":
            request = params.get("request", {}) or {}
            url = clean(request.get("url"))
            if valid_real_attachment_url(url) and looks_like_attachment_url(url):
                best.append((90, url, "request URL"))

    if not best:
        return None

    best.sort(key=lambda item: item[0], reverse=True)
    score, url, reason = best[0]
    print(f"Captured attachment URL from browser network ({reason}, score={score}): {url}")
    return url


# ---------------------------------------------------------------------------
# Firestore cleanup + previous wrong-subject repair
# ---------------------------------------------------------------------------

def firestore_delete_document(document_name: str, id_token: str) -> bool:
    if not document_name:
        return False
    response = requests.delete(
        f"https://firestore.googleapis.com/v1/{document_name}",
        params={"key": runner.FIREBASE_API_KEY},
        headers=runner.firestore_headers(id_token),
        timeout=25,
    )
    return response.ok or response.status_code == 404


def firestore_update_subject(document_name: str, subject: str, id_token: str) -> bool:
    """Patch only the subject field of an existing Firestore material."""
    if not document_name or not subject:
        return False
    response = requests.patch(
        f"https://firestore.googleapis.com/v1/{document_name}",
        params={
            "key": runner.FIREBASE_API_KEY,
            "updateMask.fieldPaths": "subject",
        },
        headers=runner.firestore_headers(id_token),
        json={"fields": {"subject": {"stringValue": subject}}},
        timeout=25,
    )
    return response.ok


def list_and_cleanup_materials(id_token: str) -> List[Dict[str, Any]]:
    """Clean known-bad URLs and repair old 'School Diary' subject mistakes."""
    materials = ORIGINAL_LIST(id_token)
    kept: List[Dict[str, Any]] = []
    removed = 0
    subjects_fixed = 0

    for item in materials:
        url = clean(item.get("pdf_url"))
        source = clean(item.get("source")).lower()
        bad_automation_record = (
            "edusecure" in source and bool(url) and bool(BAD_URL_RE.search(url))
        )

        if bad_automation_record:
            if firestore_delete_document(clean(item.get("_name")), id_token):
                removed += 1
                print(f"Removed invalid previous EduSecure automation URL: {url}")
                continue

        old_subject = clean(item.get("subject"))
        if "edusecure" in source and old_subject.lower() == "school diary":
            evidence = " ".join(
                part for part in (
                    clean(item.get("title")),
                    clean(item.get("description")),
                ) if part
            )
            corrected = detect_subject_smart(evidence)
            if corrected not in {"General", "Circular", "School Diary"}:
                if firestore_update_subject(clean(item.get("_name")), corrected, id_token):
                    item["subject"] = corrected
                    subjects_fixed += 1
                    print(f"Corrected existing subject: School Diary -> {corrected} | {evidence[:100]}")

        kept.append(item)

    if removed:
        print(f"Invalid previous EduSecure automation records cleaned: {removed}")
    if subjects_fixed:
        print(f"Existing wrong School Diary subjects corrected: {subjects_fixed}")
    return kept


# ---------------------------------------------------------------------------
# Exact Attachment-link repair
# ---------------------------------------------------------------------------

def exact_attachment_anchors(driver) -> List[Dict[str, str]]:
    """Return only the real visible <a> link for the message Attachment button."""
    js = r"""
    function visible(el) {
      if (!el) return false;
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden' &&
             r.width > 4 && r.height > 4 && r.bottom > 0 && r.top < innerHeight;
    }
    function text(el) {
      return (el.innerText || el.textContent || el.getAttribute('aria-label') ||
              el.getAttribute('title') || '').replace(/\s+/g,' ').trim();
    }
    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return ''; }
    }

    const anchors = Array.from(document.querySelectorAll('a[href]')).filter(visible);
    const out = [];
    const seen = new Set();

    for (const a of anchors) {
      const t = text(a);
      const id = a.id || '';
      const cls = String(a.className || '');
      const href = abs(a.getAttribute('href') || '');
      const exactText = /^(attachment|attachments)\s*[:：]?$/i.test(t);
      const exactEduSecureId = /(?:^|_)HyperLink1$/i.test(id);

      if (!exactText && !exactEduSecureId) continue;
      if (!/^https?:/i.test(href)) continue;
      if (/morelinks\.aspx|loader\.gif|dashboard\.aspx|\/login/i.test(href)) continue;
      if (seen.has(href)) continue;
      seen.add(href);

      const r = a.getBoundingClientRect();
      let score = 0;
      if (exactText) score += 1000;
      if (exactEduSecureId) score += 900;
      if (/HyperLink1/i.test(id)) score += 300;
      if (r.width <= 220 && r.height <= 80) score += 80;

      out.push({
        id,
        text: t,
        href,
        className: cls,
        score: String(score),
        top: String(Math.round(r.top)),
        left: String(Math.round(r.left)),
        width: String(Math.round(r.width)),
        height: String(Math.round(r.height))
      });
    }

    return out.sort((a,b) => Number(b.score)-Number(a.score) || Number(a.top)-Number(b.top));
    """
    try:
        rows = driver.execute_script(js) or []
    except Exception as exc:
        print(f"Exact Attachment anchor scan failed: {exc}")
        return []

    if rows:
        print(f"Exact Attachment <a> links found: {len(rows)}")
        for row in rows[:4]:
            print(
                "  exact anchor -> "
                f"id={row.get('id')} text={row.get('text')!r} "
                f"rect=({row.get('left')},{row.get('top')},"
                f"{row.get('width')}x{row.get('height')})"
            )
    return rows


def get_anchor_element(driver, anchor: Dict[str, str]):
    anchor_id = clean(anchor.get("id"))
    href = clean(anchor.get("href"))

    if anchor_id:
        try:
            element = driver.execute_script(
                "return document.getElementById(arguments[0]);", anchor_id
            )
            if element is not None:
                return element
        except Exception:
            pass

    if href:
        try:
            element = driver.execute_script(
                """
                const wanted = arguments[0];
                return Array.from(document.querySelectorAll('a[href]')).find(a => {
                  try { return new URL(a.href, location.href).href === wanted; }
                  catch(e) { return false; }
                }) || null;
                """,
                href,
            )
            if element is not None:
                return element
        except Exception:
            pass
    return None


def final_attachment_from_dom(driver) -> Optional[str]:
    """Find a real file/document URL on the opened page, regardless of extension."""
    js = r"""
    function abs(u){try{return new URL(u,location.href).href}catch(e){return ''}}
    const bad = /morelinks\.aspx|loader\.gif|dashboard\.aspx|\/login/i;
    for(const el of document.querySelectorAll('a[href],iframe[src],frame[src],embed[src],object[data],[data-url],[data-href],[data-src]')){
      for(const attr of ['href','src','data','data-url','data-href','data-src']){
        const raw=el.getAttribute&&el.getAttribute(attr); if(!raw) continue;
        const u=abs(raw);
        if(/^https?:/i.test(u) && !bad.test(u)) return u;
      }
    }
    return '';
    """
    try:
        value = clean(driver.execute_script(js))
        return value if valid_real_attachment_url(value) else None
    except Exception:
        return None


def close_extra_tabs(driver, app_handle: str) -> None:
    try:
        for handle in list(driver.window_handles):
            if handle == app_handle:
                continue
            try:
                driver.switch_to.window(handle)
                driver.close()
            except Exception:
                pass
        driver.switch_to.window(app_handle)
    except Exception:
        pass


def right_click_open_link_in_new_tab(
    driver, app_handle: str, anchor: Dict[str, str]
) -> Optional[str]:
    """Right-click exact Attachment anchor, open it in a new tab and copy final URL."""
    element = get_anchor_element(driver, anchor)
    if element is None:
        print("Exact Attachment anchor element disappeared before right-click")
        return None

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center',inline:'center'});", element
        )
        time.sleep(0.15)
    except Exception:
        pass

    print(
        "Right-clicking exact Attachment link -> "
        f"id={anchor.get('id')} text={anchor.get('text')!r}"
    )

    try:
        ActionChains(driver).move_to_element(element).context_click(element).perform()
        time.sleep(0.25)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
    except Exception as exc:
        print(f"Native context click fallback: {str(exc)[:120]}")
        try:
            driver.execute_script(
                """
                arguments[0].dispatchEvent(new MouseEvent('contextmenu', {
                  bubbles:true,cancelable:true,view:window,button:2,buttons:2
                }));
                """,
                element,
            )
        except Exception:
            return None

    try:
        href = clean(element.get_attribute("href"))
    except Exception:
        href = clean(anchor.get("href"))

    if not valid_real_attachment_url(href):
        print(f"Rejected Attachment href before opening new tab: {href}")
        return None

    print("Open link in new tab: exact Attachment href")

    try:
        driver.switch_to.new_window("tab")
        attachment_tab = driver.current_window_handle
        drain_performance_log(driver)
        driver.get(href)
    except Exception as exc:
        print(f"Could not open Attachment href in new tab: {str(exc)[:150]}")
        close_extra_tabs(driver, app_handle)
        return None

    deadline = time.time() + 12.0
    observed: Set[str] = set()
    first_real_seen_at: Optional[float] = None
    last_real_url: Optional[str] = None

    while time.time() < deadline:
        try:
            driver.switch_to.window(attachment_tab)
            current = clean(driver.current_url)
            if current and current not in observed:
                observed.add(current)
                print(f"New-tab URL observed: {current}")

            if valid_real_attachment_url(current):
                last_real_url = current
                if first_real_seen_at is None:
                    first_real_seen_at = time.time()
                if time.time() - first_real_seen_at >= 0.8:
                    print(f"FINAL attachment URL copied from new tab: {last_real_url}")
                    close_extra_tabs(driver, app_handle)
                    return last_real_url

            dom_url = final_attachment_from_dom(driver)
            if dom_url and dom_url != current:
                last_real_url = dom_url
                if first_real_seen_at is None:
                    first_real_seen_at = time.time()
        except WebDriverException:
            pass

        try:
            captured = attachment_url_from_performance(driver)
        except Exception:
            captured = None
        if valid_real_attachment_url(captured):
            last_real_url = clean(captured)
            if first_real_seen_at is None:
                first_real_seen_at = time.time()

        time.sleep(0.2)

    if last_real_url and valid_real_attachment_url(last_real_url):
        print(f"FINAL attachment URL copied after wait: {last_real_url}")
        close_extra_tabs(driver, app_handle)
        return last_real_url

    if valid_real_attachment_url(href):
        print(f"Using exact opened Attachment link: {href}")
        close_extra_tabs(driver, app_handle)
        return href

    print("Attachment opened, but no valid real URL was captured; skipping.")
    for value in sorted(observed):
        print(f"  observed intermediate: {value}")
    close_extra_tabs(driver, app_handle)
    return None


def extract_attachment_url(driver, app_handle: str) -> Optional[str]:
    """Only exact Attachment anchors are eligible; file extension is irrelevant."""
    anchors = exact_attachment_anchors(driver)
    if not anchors:
        print("No exact Attachment <a> link found in this message")
        return None

    for index, anchor in enumerate(anchors, start=1):
        print(f"Trying exact Attachment anchor {index}/{len(anchors)}")
        result = right_click_open_link_in_new_tab(driver, app_handle, anchor)
        if result and valid_real_attachment_url(result):
            print(f"Verified uploadable attachment link: {result}")
            return result

    print("Exact Attachment link(s) found, but none produced a usable URL")
    return None


# ---------------------------------------------------------------------------
# Website-first strict date boundary repair
# ---------------------------------------------------------------------------

class _WebsiteBoundaryDate(date):
    """Internally latest+1 while logs display the actual website latest date."""

    def __new__(cls, internal_date: date, displayed_date: date):
        obj = date.__new__(cls, internal_date.year, internal_date.month, internal_date.day)
        obj._displayed_date = displayed_date
        return obj

    def isoformat(self) -> str:
        return self._displayed_date.isoformat()


def parse_website_added_dates(text: str) -> List[date]:
    raw = clean(text)
    found: List[date] = []
    pattern = re.compile(
        r"\bADDED\s+([A-Z][A-Za-z]{2,8})\s+(\d{1,2}),\s+(\d{4})\b",
        re.I,
    )

    for month, day, year in pattern.findall(raw):
        parsed: Optional[date] = None
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                parsed = datetime.strptime(f"{month} {day} {year}", fmt).date()
                break
            except ValueError:
                pass
        if parsed:
            found.append(parsed)
    return found


def read_latest_date_from_website() -> Optional[date]:
    """First action: open 8aPDF and read the latest displayed material date."""
    driver = runner.legacy.make_driver()
    try:
        print("=== STEP 0: CHECKING 8aPDF WEBSITE LATEST PDF DATE ===")
        print(f"Opening website first: {WEBSITE_URL}")
        driver.get(WEBSITE_URL)
        runner.legacy.wait_ready(driver)
        time.sleep(1.8)

        body_text = ""
        try:
            body_text = driver.execute_script(
                "return (document.body && document.body.innerText) || '';"
            ) or ""
        except Exception:
            pass

        dates = parse_website_added_dates(body_text)
        if dates:
            latest = max(dates)
            print(f"Latest PDF date found on website: {latest.isoformat()}")
            print(
                "STRICT RULE LOCKED: only EduSecure attachments with a message date "
                f"AFTER {latest.isoformat()} can be uploaded. Same-date and older are skipped."
            )
            return latest

        try:
            latest = runner.legacy.latest_library_date(driver)
        except Exception:
            latest = None

        if latest:
            print(f"Latest PDF date found by fallback reader: {latest.isoformat()}")
            print(
                "STRICT RULE LOCKED: only EduSecure attachments with a message date "
                f"AFTER {latest.isoformat()} can be uploaded. Same-date and older are skipped."
            )
            return latest

        print("ERROR: Could not read the latest PDF date from the 8aPDF website.")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def existing_state_from_website(materials: List[Dict[str, Any]]):
    """Use Firestore for duplicate URLs/order, but website date for the cutoff."""
    urls, _firestore_latest, next_order = ORIGINAL_EXISTING_STATE(materials)

    if WEBSITE_LATEST_DATE is None:
        raise RuntimeError("Website latest PDF date was not established before EduSecure sync")

    effective = _WebsiteBoundaryDate(
        WEBSITE_LATEST_DATE + timedelta(days=1),
        WEBSITE_LATEST_DATE,
    )

    print(f"Website cutoff date being used: {WEBSITE_LATEST_DATE.isoformat()}")
    print("Same-date and older EduSecure messages are NOT eligible for upload.")
    return urls, effective, next_order


# Install all consolidated runtime repairs before main() starts.
runner.list_firestore_materials = list_and_cleanup_materials
runner.extract_attachment_url = extract_attachment_url
runner.legacy.make_title = make_message_title
runner.legacy.detect_subject = detect_subject_smart
runner.legacy.make_driver = make_driver_with_network_logs
runner.legacy.is_pdf_url = valid_real_attachment_url


def main() -> int:
    global WEBSITE_LATEST_DATE

    WEBSITE_LATEST_DATE = read_latest_date_from_website()
    if WEBSITE_LATEST_DATE is None:
        print(
            "Stopping safely: website latest date could not be read, "
            "so no EduSecure upload will run."
        )
        return 2

    runner.existing_state = existing_state_from_website
    return runner.main()


if __name__ == "__main__":
    sys.exit(main())
