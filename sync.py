"""
EduSecure Flexible PDF Uploader V29
-----------------------------------------
Fixes from V7:
1) Local admin.html now opens using Selenium direct tab:
      driver.switch_to.new_window("tab")
      driver.get("file:///C:/Users/Kanish/Downloads/admin.html")
   Not using window.open from EduSecure, because Chrome can block file:// from HTTPS page.

2) After PDF URL copy:
      open admin.html -> manual login -> upload -> back to EduSecure -> next PDF

3) Admin form detection improved:
   - waits for Add PDF form
   - scrolls page to top
   - finds fields by label/placeholder:
     Title *, Subject *, Description *, PDF Link *
   - finds Add PDF button

4) It processes visible Attachment buttons and scrolls until end.

Install:
    pip install selenium

Run:
    python edusecure_flexible_pdf_uploader_v29.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import unquote, urlparse

import requests

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


START_URL = "https://edusecure.org/ManavMangal88/ParentApp/Dashboard.aspx"
PUBLIC_PDF_SITE = "https://8apdf.xo.je/"
ADMIN_URL = "file:///C:/Users/Kanish/Downloads/admin.html"

# Auto-login credentials. Keep this file private.
EDUSECURE_USERNAME = os.environ.get("EDUSECURE_USERNAME", "")
EDUSECURE_PASSWORD = os.environ.get("EDUSECURE_PASSWORD", "")
INGEST_URL = os.environ.get("INGEST_URL", "https://eightapdf-study-library.nullreaper-exe.chatgpt.site/automation/ingest")
AUTOMATION_TOKEN = os.environ.get("AUTOMATION_TOKEN", "")

# Persistent memory files:
# - uploaded URLs are saved here so next run will not upload same PDF again.
STATE_FILE_NAME = "edusecure_uploaded_pdf_state_v16.json"

# Old output files are also loaded once, so PDFs uploaded by v15/v13/v12 are skipped.
OLD_OUTPUT_FILES = [
    "uploaded_missing_pdfs_v23.json",
    "uploaded_portal_date_message_click_v22.json",
    "uploaded_today_every_message_click_v21.json",
    "uploaded_today_message_click_v20.json",
    "uploaded_today_dashboard_v19.json",
    "uploaded_today_list_page_v18.json",
    "uploaded_today_announcement_v17.json",
    "uploaded_announcement_full_v16.json",
    "uploaded_announcement_list_v15.json",
    "uploaded_circular_strict_v13.json",
    "uploaded_circular_pdfs_v12.json",
    "uploaded_edusecure_pdfs.json",
    "uploaded_circular_pdfs.json",
]

MAX_UPLOADS = 5000
MAX_NO_NEW_SCROLLS = 999

# Today-only mode:
# Script sirf aaj ki date wale Dashboard/Circular cards upload karega.
TODAY = date.today()
TODAY_DATE_LABELS = [
    TODAY.strftime("%b %d, %Y"),                 # Jul 11, 2026
    f"{TODAY.strftime('%b')} {TODAY.day}, {TODAY.year}",   # Jul 11, 2026 without leading zero
    f"{TODAY.strftime('%B')} {TODAY.day}, {TODAY.year}",   # July 11, 2026
]

# If no today-card appears after multiple scrolls and page shows older dates, stop early.
STOP_AFTER_NO_TODAY_SCROLLS = 10

WAIT_AFTER_CLICK = 1.25
WAIT_AFTER_SCROLL = 0.85
WAIT_AFTER_ADMIN_ADD = 1.25

DEFAULT_SUBJECT = "Circular"

# Exact CSS selectors optional. Blank rakho pehle.
# Agar fir bhi admin form detect na ho, screenshot bhej dena.
ADMIN_SELECTORS = {
    "title": "",
    "subject": "",
    "description": "",
    "link": "",
    "button": "",
}


def make_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return webdriver.Chrome(options=options)


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:450]


def is_pdf_url(url: str | None) -> bool:
    return bool(url and ".pdf" in url.lower())


def title_from_pdf_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name or "PDF Document")
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = name.replace("_", " ").replace("-", " ")
    return clean_text(name).title() or "PDF Document"


def extract_date(text: str) -> str:
    match = re.search(r"\b[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\b", text)
    return match.group(0) if match else ""


def detect_subject(text: str, default: str = DEFAULT_SUBJECT) -> str:
    t = clean_text(text)

    # School Diary style:
    # Home Work : Mathematics: Kindly find attached PDF...
    m = re.search(r"Home\s*Work\s*:?\s*([A-Za-z][A-Za-z\s&.-]{1,35})\s*:", t, flags=re.I)
    if m:
        return clean_text(m.group(1)).title()

    # Common subject words anywhere in extracted text
    m = re.search(
        r"\b(Mathematics|Maths|Science|English|Hindi|Punjabi|Computer|History|Geography|SST|Social Science|French|GK|EVS)\b",
        t,
        flags=re.I,
    )
    if m:
        return clean_text(m.group(1)).title()

    # If no exact subject found, keep Circular for Circular posts, School Diary for diary posts
    if re.search(r"\bSchool\s*Diary\b", t, flags=re.I):
        return "School Diary"

    if re.search(r"\bCircular\b", t, flags=re.I):
        return "Circular"

    return default


def make_title(text: str, pdf_url: str, number: int = 0) -> str:
    text = clean_text(text)
    date = extract_date(text)

    title = text
    title = re.sub(r"\bLearning Planner\b|\bToday's Learning Planner\b|\bView\b", "", title, flags=re.I)
    title = re.sub(r"\bSchool Diary\b|\bCircular\b", "", title, flags=re.I)
    title = re.sub(r"\bHome Work\s*:?\b|\bClass Work\s*:?\b", "", title, flags=re.I)
    title = re.sub(r"\bAttachment\b|\bAttach\b", "", title, flags=re.I)
    title = re.sub(r"\bDownload\b|\bOpen\b|\bClick Here\b", "", title, flags=re.I)
    title = re.sub(r"Manav\s+Mangal\s+SMART\s+SCHOOL-?88", "", title, flags=re.I)
    title = re.sub(r"https?://\S+", "", title)
    title = clean_text(title)

    if date:
        title = clean_text(title.replace(date, ""))

    if not title or len(title) < 3:
        title = title_from_pdf_url(pdf_url)

    if len(title) > 95:
        title = title[:95].rsplit(" ", 1)[0]

    if date and date not in title:
        title = f"{title} ({date})"

    return clean_text(title) or f"PDF {number}".strip()


def make_description(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"\bAttachment\b|\bAttach\b", "", text, flags=re.I)
    text = re.sub(r"\bView\b|\bDownload\b|\bOpen\b|\bClick Here\b", "", text, flags=re.I)
    text = re.sub(r"Manav\s+Mangal\s+SMART\s+SCHOOL-?88", "", text, flags=re.I)
    text = re.sub(r"https?://\S+", "", text)
    text = clean_text(text)
    return text[:250] if text else "PDF attachment"


def wait_ready(driver: webdriver.Chrome, timeout: int = 12) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
    except Exception:
        pass


def save_debug_screenshot(driver: webdriver.Chrome, name: str) -> None:
    try:
        driver.save_screenshot(name)
        print(f"Debug screenshot saved: {Path(name).resolve()}")
    except Exception:
        pass



# -----------------------------
# AUTO LOGIN HELPERS
# -----------------------------

def fill_login_field(driver: webdriver.Chrome, element: WebElement, value: str) -> bool:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", element)
        time.sleep(0.15)
        element.click()
        time.sleep(0.08)
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.BACKSPACE)
        element.send_keys(value)
        return True
    except Exception:
        try:
            driver.execute_script(
                """
                const el = arguments[0], value = arguments[1];
                el.focus();
                const proto = el.tagName.toLowerCase() === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
                setter.call(el, value);
                el.dispatchEvent(new Event("input", { bubbles: true }));
                el.dispatchEvent(new Event("change", { bubbles: true }));
                """,
                element,
                value,
            )
            return True
        except Exception:
            return False


def click_best_login_button(driver: webdriver.Chrome) -> bool:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const buttons = Array.from(document.querySelectorAll("button, input[type='submit'], input[type='button'], a[role='button']"))
      .filter(visible);

    const best = buttons.find(btn => {
      const t = (btn.innerText || btn.value || btn.getAttribute("aria-label") || "").toLowerCase();
      return /login|log in|sign in|submit|continue/.test(t) && !/logout|clear|cancel/.test(t);
    }) || buttons[0];

    if (!best) return false;
    best.scrollIntoView({block:"center", inline:"center"});
    best.click();
    return true;
    """
    try:
        return bool(driver.execute_script(js))
    except Exception:
        return False


def auto_login_edusecure(driver: webdriver.Chrome) -> bool:
    """
    EduSecure login screen par username/password auto-fill + login.
    Agar already logged in hai to True.
    """
    wait_ready(driver)
    time.sleep(1.0)

    try:
        if "login" not in driver.current_url.lower():
            text = clean_text(driver.find_element(By.TAG_NAME, "body").text)
            if "Circular" in text or "School Diary" in text or "Dashboard" in text:
                print("EduSecure already logged in ✅")
                return True
    except Exception:
        pass

    print("Trying EduSecure auto-login...")

    try:
        WebDriverWait(driver, 20).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "input")) >= 2)
    except TimeoutException:
        print("EduSecure login inputs not found.")
        return False

    inputs = []
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "input"):
            if not el.is_displayed():
                continue
            typ = (el.get_attribute("type") or "").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            inputs.append(el)
    except Exception:
        return False

    if not inputs:
        print("No visible EduSecure login input found.")
        return False

    password_el = None
    username_el = None

    for el in inputs:
        if (el.get_attribute("type") or "").lower() == "password":
            password_el = el
            break

    # username is usually the visible text/email input before password
    for el in inputs:
        if el == password_el:
            continue
        username_el = el
        break

    if username_el is None or password_el is None:
        print("EduSecure username/password boxes not detected.")
        return False

    ok_user = fill_login_field(driver, username_el, EDUSECURE_USERNAME)
    ok_pass = fill_login_field(driver, password_el, EDUSECURE_PASSWORD)

    if not (ok_user and ok_pass):
        print("EduSecure login fill failed.")
        return False

    if not click_best_login_button(driver):
        try:
            password_el.send_keys(Keys.ENTER)
        except Exception:
            pass

    time.sleep(3.0)
    wait_ready(driver)

    try:
        text = clean_text(driver.find_element(By.TAG_NAME, "body").text)
        if "login" not in driver.current_url.lower() and ("Circular" in text or "School Diary" in text or "Dashboard" in text or "Learning Planner" in text):
            print("EduSecure auto-login done ✅")
            return True
    except Exception:
        pass

    print("EduSecure auto-login may have failed.")
    return False


def admin_form_available(driver: webdriver.Chrome, quick_timeout: int = 4) -> bool:
    form = wait_for_admin_form(driver, timeout=quick_timeout)
    return bool(form.get("title") and form.get("subject") and form.get("description") and form.get("link") and form.get("button"))


def auto_login_admin(driver: webdriver.Chrome) -> bool:
    """
    admin.html login form auto-fill. Agar already logged in / form visible hai to True.
    """
    wait_ready(driver)
    time.sleep(0.8)

    if admin_form_available(driver, quick_timeout=3):
        print("Admin already logged in / Add PDF form visible ✅")
        return True

    print("Trying admin auto-login...")

    try:
        WebDriverWait(driver, 15).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "input")) >= 2)
    except TimeoutException:
        print("Admin login inputs not found.")
        return False

    inputs = []
    try:
        for el in driver.find_elements(By.CSS_SELECTOR, "input"):
            if not el.is_displayed():
                continue
            typ = (el.get_attribute("type") or "").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            inputs.append(el)
    except Exception:
        return False

    email_el = None
    password_el = None

    for el in inputs:
        typ = (el.get_attribute("type") or "").lower()
        info = " ".join([
            el.get_attribute("placeholder") or "",
            el.get_attribute("name") or "",
            el.get_attribute("id") or "",
            el.get_attribute("aria-label") or "",
        ]).lower()

        if typ == "password" or "password" in info:
            password_el = el
        elif email_el is None and (typ in ("email", "text") or "email" in info or "user" in info):
            email_el = el

    if email_el is None:
        # first non-password input
        for el in inputs:
            if el != password_el:
                email_el = el
                break

    if password_el is None:
        for el in inputs:
            if (el.get_attribute("type") or "").lower() == "password":
                password_el = el
                break

    if email_el is None or password_el is None:
        print("Admin email/password boxes not detected.")
        return False

    ok_email = fill_login_field(driver, email_el, ADMIN_EMAIL)
    ok_pass = fill_login_field(driver, password_el, ADMIN_PASSWORD)

    if not (ok_email and ok_pass):
        print("Admin login fill failed.")
        return False

    if not click_best_login_button(driver):
        try:
            password_el.send_keys(Keys.ENTER)
        except Exception:
            pass

    time.sleep(3.0)
    wait_ready(driver)

    if admin_form_available(driver, quick_timeout=10):
        print("Admin auto-login done ✅")
        return True

    print("Admin auto-login may have failed.")
    return False


# -----------------------------
# EDUSECURE APP SIDE
# -----------------------------

def click_circular_section(driver: webdriver.Chrome) -> bool:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    function txt(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "").replace(/\s+/g, " ").trim();
    }

    const candidates = Array.from(document.querySelectorAll("a, button, div, span, li"))
      .filter(visible)
      .map(el => {
        const text = txt(el);
        let score = 0;
        if (/^Circular$/i.test(text)) score = 100;
        else if (/\bCircular\b/i.test(text)) score = 60;
        return {el, text, score};
      })
      .filter(x => x.score > 0)
      .sort((a,b) => b.score - a.score);

    if (!candidates.length) return false;

    const raw = candidates[0].el;
    const target = raw.closest("a, button, [onclick], li, .card, .item, .list-group-item") || raw;
    target.scrollIntoView({block:"center", inline:"center"});
    target.click();
    return true;
    """
    try:
        ok = bool(driver.execute_script(js))
        if ok:
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
        return ok
    except JavascriptException:
        return False


def click_app_back_arrow(driver: webdriver.Chrome) -> bool:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const candidates = Array.from(document.querySelectorAll("a, button, i, span, div"))
      .filter(visible)
      .map(el => {
        const r = el.getBoundingClientRect();
        const txt = (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "").trim();
        const cls = String(el.className || "");
        const score =
          (r.top < 120 ? 20 : 0) +
          (r.left < 220 ? 20 : 0) +
          (/back|arrow|chevron|left|return/i.test(txt + " " + cls) ? 45 : 0) +
          (txt === "←" || txt === "‹" || txt === "❮" ? 60 : 0) +
          (r.width >= 15 && r.width <= 110 && r.height >= 15 && r.height <= 110 ? 15 : 0);
        return {el, r, txt, score};
      })
      .filter(x => x.score >= 35)
      .sort((a,b) => b.score - a.score || a.r.left - b.r.left);

    if (candidates.length) {
      const target = candidates[0].el.closest("a, button, [onclick]") || candidates[0].el;
      target.click();
      return true;
    }

    // Hard fallback top-left arrow coordinate
    const elAt = document.elementFromPoint(130, 52) || document.elementFromPoint(125, 50) || document.elementFromPoint(105, 52);
    if (elAt) {
      const target = elAt.closest("a, button, [onclick]") || elAt;
      target.click();
      return true;
    }

    return false;
    """
    try:
        ok = bool(driver.execute_script(js))
        if ok:
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
        return ok
    except JavascriptException:
        return False


def extract_pdf_from_current_page(driver: webdriver.Chrome) -> Optional[str]:
    js = r"""
    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    if (location.href.toLowerCase().includes(".pdf")) return location.href;

    for (const a of document.querySelectorAll("a[href], area[href]")) {
      const href = abs(a.getAttribute("href"));
      if (href && href.toLowerCase().includes(".pdf")) return href;
    }

    for (const el of document.querySelectorAll("iframe[src], frame[src], embed[src], object[data]")) {
      const raw = el.getAttribute("src") || el.getAttribute("data");
      const href = abs(raw);
      if (href && href.toLowerCase().includes(".pdf")) return href;
    }

    for (const el of document.querySelectorAll("[onclick]")) {
      const onclick = el.getAttribute("onclick") || "";
      const matches = onclick.match(/(?:https?:)?\/\/[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?|[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?/ig) || [];
      if (matches.length) {
        const href = abs(matches[0]);
        if (href) return href;
      }
    }

    return null;
    """
    try:
        return driver.execute_script(js)
    except JavascriptException:
        return None


def visible_attachment_target(driver: webdriver.Chrome, processed_cards: Set[str]) -> Optional[Dict[str, str]]:
    processed = list(processed_cards)
    js = r"""
    const processed = arguments[0] || [];

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "").replace(/\s+/g, " ").trim();
    }

    function makePath(el) {
      if (el.id) return "id:" + el.id;
      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function fp(text) {
      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b/ig, "")
        .replace(/https?:\/\/\S+/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 450);
    }

    function cardText(el) {
      let best = el;
      let p = el;
      for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
        const t = textOf(p);
        const r = p.getBoundingClientRect();

        if (
          t.length >= 12 &&
          t.length <= 1700 &&
          r.width >= 180 &&
          r.height >= 35 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }
      return textOf(best).slice(0, 1500);
    }

    function directPdfFrom(el) {
      const href = el.getAttribute && el.getAttribute("href");
      if (href) {
        const u = abs(href);
        if (u && u.toLowerCase().includes(".pdf")) return u;
      }

      const onclick = el.getAttribute && el.getAttribute("onclick");
      if (onclick) {
        const matches = onclick.match(/(?:https?:)?\/\/[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?|[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?/ig) || [];
        if (matches.length) {
          const u = abs(matches[0]);
          if (u) return u;
        }
      }

      return null;
    }

    const raw = Array.from(document.querySelectorAll("a, button, [onclick], div, span"))
      .filter(visible)
      .filter(el => {
        const txt = textOf(el).toLowerCase();
        const href = (el.getAttribute("href") || "").toLowerCase();
        const onclick = (el.getAttribute("onclick") || "").toLowerCase();
        return txt.includes("attachment") || txt.includes("attach") || href.includes(".pdf") || onclick.includes(".pdf");
      })
      .map(el => el.closest("a, button, [onclick]") || el)
      .filter(visible);

    const unique = Array.from(new Set(raw))
      .sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    for (const el of unique) {
      const text = cardText(el);
      const f = fp(text);
      if (!f) continue;
      if (processed.includes(f)) continue;

      return {
        path: makePath(el),
        text,
        fp: f,
        directPdf: directPdfFrom(el),
        top: Math.round(el.getBoundingClientRect().top),
        currentUrl: location.href
      };
    }

    return null;
    """
    try:
        return driver.execute_script(js, processed)
    except JavascriptException:
        return None


def click_path(driver: webdriver.Chrome, path: str) -> bool:
    js = r"""
    const path = arguments[0];

    function getEl(path) {
      if (!path) return null;
      if (path.startsWith("id:")) return document.getElementById(path.slice(3));
      if (path.startsWith("css:")) return document.querySelector(path.slice(4));
      return null;
    }

    const el = getEl(path);
    if (!el) return false;

    el.scrollIntoView({block:"center", inline:"center"});
    el.click();
    return true;
    """
    try:
        clicked = bool(driver.execute_script(js, path))
        if clicked:
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
        return clicked
    except JavascriptException:
        return False


def get_pdf_url_from_attachment(driver: webdriver.Chrome, target: Dict[str, str], app_handle: str) -> Optional[str]:
    direct_pdf = target.get("directPdf")
    if is_pdf_url(direct_pdf):
        print("Direct PDF href found.")
        return direct_pdf

    before_tabs = set(driver.window_handles)

    if not click_path(driver, target.get("path", "")):
        print("❌ Attachment click failed.")
        return None

    time.sleep(0.7)

    after_tabs = set(driver.window_handles)
    new_tabs = list(after_tabs - before_tabs)

    if new_tabs:
        try:
            driver.switch_to.window(new_tabs[-1])
            time.sleep(0.9)
            pdf_url = driver.current_url
            if not is_pdf_url(pdf_url):
                pdf_url = extract_pdf_from_current_page(driver)
            driver.close()
            driver.switch_to.window(app_handle)
            return pdf_url if is_pdf_url(pdf_url) else None
        except WebDriverException:
            try:
                driver.switch_to.window(app_handle)
            except Exception:
                pass
            return None

    if is_pdf_url(driver.current_url):
        pdf_url = driver.current_url
        try:
            driver.back()
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
        except Exception:
            pass
        return pdf_url

    pdf_inside = extract_pdf_from_current_page(driver)
    if is_pdf_url(pdf_inside):
        return pdf_inside

    return None


def restore_app_after_pdf(driver: webdriver.Chrome, app_handle: str) -> None:
    try:
        for h in list(driver.window_handles):
            if h != app_handle:
                driver.switch_to.window(h)
                if is_pdf_url(driver.current_url):
                    driver.close()
        driver.switch_to.window(app_handle)
    except WebDriverException:
        try:
            driver.switch_to.window(app_handle)
        except Exception:
            pass

    try:
        if is_pdf_url(driver.current_url):
            driver.back()
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
    except Exception:
        pass


def app_current_text(driver: webdriver.Chrome) -> str:
    try:
        return clean_text(driver.find_element(By.TAG_NAME, "body").text)
    except Exception:
        return ""


def scroll_down_app(driver: webdriver.Chrome) -> Dict[str, object]:
    result = {"moved": False, "methods": []}

    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    function tryScroll(el, amount, label) {
      if (!el) return null;
      try {
        const before = el.scrollTop || 0;
        el.scrollTop = Math.min(before + amount, el.scrollHeight || before + amount);
        const after = el.scrollTop || 0;
        if (after !== before) return {moved:true, label, before, after, tag:el.tagName || "document"};
      } catch(e) {}
      return null;
    }

    const amount = Math.floor(window.innerHeight * 0.82);

    const points = [
      [Math.floor(window.innerWidth / 2), Math.floor(window.innerHeight * 0.78)],
      [Math.floor(window.innerWidth / 2), Math.floor(window.innerHeight * 0.55)],
      [Math.floor(window.innerWidth * 0.35), Math.floor(window.innerHeight * 0.78)]
    ];

    for (const [x,y] of points) {
      let el = document.elementFromPoint(x,y);
      for (let i=0; i<14 && el; i++, el = el.parentElement) {
        if ((el.scrollHeight - el.clientHeight) > 35) {
          const res = tryScroll(el, amount, "ancestorFromPoint");
          if (res) return res;
        }
      }
    }

    const containers = Array.from(document.querySelectorAll("*"))
      .filter(visible)
      .filter(el => (el.scrollHeight - el.clientHeight) > 35)
      .map(el => {
        const txt = (el.innerText || el.textContent || "").slice(0, 3000);
        return {el, hasContent:/Circular|Attachment|Attach|School Diary|Learning Planner/i.test(txt), max:el.scrollHeight - el.clientHeight};
      })
      .sort((a,b) => {
        if (a.hasContent !== b.hasContent) return a.hasContent ? -1 : 1;
        return b.max - a.max;
      });

    for (const c of containers) {
      const res = tryScroll(c.el, amount, "scrollContainer");
      if (res) return res;
    }

    const beforeY = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
    window.scrollBy(0, amount);
    const afterY = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
    if (afterY !== beforeY) return {moved:true, label:"window", before:beforeY, after:afterY, tag:"window"};

    return {moved:false, label:"none", before:beforeY, after:afterY, tag:"none"};
    """
    try:
        js_res = driver.execute_script(js) or {}
        result["methods"].append(js_res)
        if js_res.get("moved"):
            result["moved"] = True
            time.sleep(WAIT_AFTER_SCROLL)
            return result
    except JavascriptException:
        pass

    try:
        ActionChains(driver).scroll_by_amount(0, 760).perform()
        result["methods"].append({"label": "seleniumWheel", "moved": True})
        result["moved"] = True
        time.sleep(WAIT_AFTER_SCROLL)
        return result
    except Exception as e:
        result["methods"].append({"label": "seleniumWheelFailed", "error": str(e)[:80]})

    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
        result["methods"].append({"label": "pageDown", "moved": True})
        result["moved"] = True
        time.sleep(WAIT_AFTER_SCROLL)
        return result
    except Exception as e:
        result["methods"].append({"label": "pageDownFailed", "error": str(e)[:80]})

    return result


# -----------------------------
# ADMIN SIDE
# -----------------------------

FIND_ADMIN_ELEMENTS_JS = r"""
const selectors = arguments[0] || {};

function visible(el) {
  if (!el) return false;
  const st = window.getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
}

function q(sel) {
  if (!sel) return null;
  try {
    const el = document.querySelector(sel);
    return visible(el) ? el : null;
  } catch(e) {
    return null;
  }
}

function fieldInfo(el) {
  return [
    el.id,
    el.name,
    el.placeholder,
    el.getAttribute("aria-label"),
    el.getAttribute("data-field"),
    el.getAttribute("data-name"),
    el.className
  ].filter(Boolean).join(" ").toLowerCase();
}

function findAddPdfBox() {
  const all = Array.from(document.querySelectorAll("form, section, article, div"))
    .filter(visible)
    .map(el => {
      const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      const fields = Array.from(el.querySelectorAll("input, textarea, select")).filter(visible);
      const buttons = Array.from(el.querySelectorAll("button, input[type='submit'], input[type='button']")).filter(visible);
      let score = 0;
      if (/Add PDF/i.test(txt)) score += 50;
      if (/Title\s*\*/i.test(txt)) score += 20;
      if (/Subject\s*\*/i.test(txt)) score += 20;
      if (/Description\s*\*/i.test(txt)) score += 20;
      if (/PDF Link\s*\*/i.test(txt)) score += 20;
      if (fields.length >= 4) score += 20;
      if (buttons.some(b => /add\s*pdf/i.test(b.innerText || b.value || ""))) score += 30;
      // Prefer smaller left card, not whole page.
      if (txt.length < 2000) score += 10;
      return {el, txt, fields, buttons, score};
    })
    .filter(x => x.score >= 60)
    .sort((a, b) => b.score - a.score || a.txt.length - b.txt.length);

  return all.length ? all[0].el : document;
}

const scope = findAddPdfBox();

function scopedFields() {
  const all = Array.from(scope.querySelectorAll("input, textarea, select"))
    .filter(visible)
    .filter(el => {
      const type = (el.type || "").toLowerCase();
      const info = fieldInfo(el);
      if (["hidden", "submit", "button", "checkbox", "radio", "password", "email"].includes(type)) return false;
      if (/search/.test(info)) return false;
      return true;
    });

  // DOM order is the safest for this admin form:
  // Title input, Subject input, Description textarea, PDF Link input
  return all;
}

function findButton() {
  if (selectors.button) {
    const exact = q(selectors.button);
    if (exact) return exact;
  }

  const btns = Array.from(scope.querySelectorAll("button, input[type='submit'], input[type='button'], a[role='button']"))
    .filter(visible);

  return btns.find(btn => {
    const t = (btn.innerText || btn.value || btn.getAttribute("aria-label") || "").toLowerCase();
    return /add\s*pdf/.test(t);
  }) || btns.find(btn => {
    const t = (btn.innerText || btn.value || btn.getAttribute("aria-label") || "").toLowerCase();
    return /add|save|submit|create|upload/.test(t) && !/logout|delete|edit|open|clear/.test(t);
  }) || null;
}

const fields = scopedFields();
const inputs = fields.filter(el => el.tagName.toLowerCase() !== "textarea");
const textareas = fields.filter(el => el.tagName.toLowerCase() === "textarea");

// Exact selector override first
const titleExact = selectors.title ? q(selectors.title) : null;
const subjectExact = selectors.subject ? q(selectors.subject) : null;
const descExact = selectors.description ? q(selectors.description) : null;
const linkExact = selectors.link ? q(selectors.link) : null;

// Find PDF link by placeholder/hint, else last input in Add PDF box
const linkByHint = inputs.find(el => /https|file\.pdf|pdf\s*link|url|link/.test(fieldInfo(el)));
const titleByHint = inputs.find(el => /enter\s*pdf\s*title|pdf\s*title|title/.test(fieldInfo(el)) && el !== linkByHint);
const subjectByHint = inputs.find(el => /example:\s*maths|subject|category/.test(fieldInfo(el)) && el !== linkByHint && el !== titleByHint);

const titleEl = titleExact || titleByHint || inputs[0] || null;
const subjectEl = subjectExact || subjectByHint || inputs.find(el => el !== titleEl && el !== linkByHint) || inputs[1] || null;
const descEl = descExact || textareas[0] || null;
const linkEl = linkExact || linkByHint || inputs[inputs.length - 1] || null;

return {
  url: location.href,
  bodyText: (document.body.innerText || "").slice(0, 700),
  debugFields: fields.map(el => ({
    tag: el.tagName,
    type: el.type || "",
    placeholder: el.placeholder || "",
    id: el.id || "",
    name: el.name || "",
    info: fieldInfo(el)
  })),
  title: titleEl,
  subject: subjectEl,
  description: descEl,
  link: linkEl,
  button: findButton()
};
"""


def wait_for_admin_form(driver: webdriver.Chrome, timeout: int = 60) -> Dict[str, Optional[WebElement]]:
    """
    Wait until admin form is visible after manual login.
    """
    end = time.time() + timeout
    last_result = {}

    while time.time() < end:
        try:
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass

        try:
            result = driver.execute_script(FIND_ADMIN_ELEMENTS_JS, ADMIN_SELECTORS) or {}
            last_result = result

            if result.get("title") and result.get("subject") and result.get("description") and result.get("link") and result.get("button"):
                return result
        except Exception:
            pass

        time.sleep(0.7)

    return last_result


def set_field_value(driver: webdriver.Chrome, element: WebElement, value: str, field_name: str = "") -> bool:
    """
    Har field ko pehle click karega, Ctrl+A karega, clear karega, phir type karega.
    Agar normal typing fail hui to JS native setter fallback use karega.
    """
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", element)
        time.sleep(0.15)
        element.click()
        time.sleep(0.10)
        element.send_keys(Keys.CONTROL, "a")
        time.sleep(0.05)
        element.send_keys(Keys.BACKSPACE)
        time.sleep(0.05)
        element.send_keys(value)

        # Verify actual value
        actual = element.get_attribute("value") or ""
        if actual.strip() == str(value).strip():
            return True
    except Exception:
        pass

    # JS fallback
    try:
        driver.execute_script(
            """
            const el = arguments[0], value = arguments[1];
            const tag = el.tagName.toLowerCase();

            el.scrollIntoView({block:'center', inline:'center'});
            el.focus();

            if (tag === "select") {
              let found = false;
              for (const opt of el.options) {
                if ((opt.value || "").toLowerCase() === String(value).toLowerCase() ||
                    (opt.text || "").toLowerCase() === String(value).toLowerCase()) {
                  el.value = opt.value;
                  found = true;
                  break;
                }
              }
              if (!found) {
                const opt = document.createElement("option");
                opt.value = value;
                opt.text = value;
                el.add(opt);
                el.value = value;
              }
            } else {
              const proto = tag === "textarea" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
              setter.call(el, value);
            }

            el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
            """,
            element,
            value,
        )
        actual = element.get_attribute("value") or ""
        return actual.strip() == str(value).strip()
    except Exception:
        print(f"❌ Could not fill field: {field_name}")
        return False


def click_admin_add(driver: webdriver.Chrome, button: WebElement) -> bool:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", button)
        time.sleep(0.2)
        try:
            button.click()
        except (ElementClickInterceptedException, WebDriverException):
            driver.execute_script("arguments[0].click();", button)
        return True
    except Exception:
        return False


def open_admin_tab_direct(driver: webdriver.Chrome, app_handle: str) -> str:
    """
    Opens admin.html directly and auto-logs in.
    """
    print(f"\nOpening admin panel directly: {ADMIN_URL}")

    driver.switch_to.new_window("tab")
    admin_handle = driver.current_window_handle
    driver.get(ADMIN_URL)
    wait_ready(driver)
    time.sleep(1.2)

    print(f"Admin tab URL now: {driver.current_url}")

    if not auto_login_admin(driver):
        input(
            "\nAdmin auto-login failed ya extra step aa gaya.\n"
            "Manually login/unlock karlo, Add PDF form visible ho jaaye to Enter press karo..."
        )

    form = wait_for_admin_form(driver, timeout=60)
    missing = [k for k in ["title", "subject", "description", "link", "button"] if not form.get(k)]

    if missing:
        print(f"❌ Admin form still missing after wait: {missing}")
        print(f"Current admin URL: {form.get('url') or driver.current_url}")
        print("Page text preview:")
        print((form.get("bodyText") or "")[:500])
        print("Detected fields debug:")
        print(json.dumps(form.get("debugFields") or [], indent=2, ensure_ascii=False))
        save_debug_screenshot(driver, "debug_admin_missing_fields.png")
        print("debug_admin_missing_fields.png saved. Is screenshot ko bhej dena.")
    else:
        print("✅ Admin form detected.")

    driver.switch_to.window(app_handle)
    return admin_handle


def upload_one_pdf_to_admin(driver: webdriver.Chrome, admin_handle: str, item: Dict[str, str]) -> bool:
    driver.switch_to.window(admin_handle)
    wait_ready(driver)
    time.sleep(0.35)

    form = wait_for_admin_form(driver, timeout=20)
    missing = [k for k in ["title", "subject", "description", "link", "button"] if not form.get(k)]

    if missing:
        print(f"❌ Admin form fields missing: {missing}")
        print(f"Current admin URL: {form.get('url') or driver.current_url}")
        print("Page text preview:")
        print((form.get("bodyText") or "")[:500])
        print("Detected fields debug:")
        print(json.dumps(form.get("debugFields") or [], indent=2, ensure_ascii=False))
        save_debug_screenshot(driver, "debug_admin_missing_fields.png")
        return False

    title_value = item.get("title", "PDF")
    subject_value = item.get("subject", DEFAULT_SUBJECT) or DEFAULT_SUBJECT
    description_value = item.get("description", title_value) or title_value
    link_value = item.get("url", "")

    # Safety: title/description me PDF URL kabhi nahi daalna.
    if is_pdf_url(title_value):
        title_value = title_from_pdf_url(link_value)
    if is_pdf_url(description_value):
        description_value = title_value

    print("Filling admin form EXACT mapping:")
    print(f"  Title box       -> {title_value}")
    print(f"  Subject box     -> {subject_value}")
    print(f"  Description box -> {description_value}")
    print(f"  PDF Link box    -> {link_value}")

    ok = True
    ok = set_field_value(driver, form["title"], title_value, "Title") and ok
    ok = set_field_value(driver, form["subject"], subject_value, "Subject") and ok
    ok = set_field_value(driver, form["description"], description_value, "Description") and ok
    ok = set_field_value(driver, form["link"], link_value, "PDF Link") and ok

    if not ok:
        print("❌ Admin fields fill failed.")
        save_debug_screenshot(driver, "debug_admin_fill_failed.png")
        return False

    if not click_admin_add(driver, form["button"]):
        print("❌ Admin Add button click failed.")
        save_debug_screenshot(driver, "debug_admin_button_failed.png")
        return False

    time.sleep(WAIT_AFTER_ADMIN_ADD)
    print("✅ Uploaded to admin")
    return True



# -----------------------------
# TODAY PDF FROM NORMAL LIST PAGE ONLY
# -----------------------------

def find_next_today_attachment_on_list_page(driver: webdriver.Chrome, processed_items: Set[str], today_labels: List[str]) -> Optional[Dict[str, str]]:
    """
    Normal dashboard/home page par jo visible aaj ki date ka Attachment/PDF button/link hai,
    usko return karega. Card/Circular detail click nahi karega.
    """
    processed = list(processed_items)

    js = r"""
    const processed = arguments[0] || [];
    const todayLabels = (arguments[1] || []).map(x => String(x).toLowerCase());

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function fp(text) {
      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b/ig, "")
        .replace(/https?:\/\/\S+/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 600);
    }

    function makePath(el) {
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function directPdfFrom(el) {
      const href = el.getAttribute && el.getAttribute("href");
      if (href) {
        const u = abs(href);
        if (u && u.toLowerCase().includes(".pdf")) return u;
      }

      const onclick = el.getAttribute && el.getAttribute("onclick");
      if (onclick) {
        const matches = onclick.match(/(?:https?:)?\/\/[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?|[^'"<>\s]+?\.pdf(?:\?[^'"<>\s]*)?/ig) || [];
        if (matches.length) {
          const u = abs(matches[0]);
          if (u) return u;
        }
      }

      return null;
    }

    function cardForAttachment(el) {
      let best = el;
      let p = el;

      for (let i = 0; i < 14 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const txtLower = txt.toLowerCase();
        const r = p.getBoundingClientRect();
        const isToday = todayLabels.some(label => txtLower.includes(label));

        if (
          isToday &&
          txt.length >= 20 &&
          txt.length <= 2200 &&
          r.width >= 180 &&
          r.height >= 40 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }

      return best;
    }

    const rawTargets = Array.from(document.querySelectorAll("a, button, [onclick], div, span"))
      .filter(visible)
      .filter(el => {
        const txt = textOf(el).toLowerCase();
        const href = (el.getAttribute("href") || "").toLowerCase();
        const onclick = (el.getAttribute("onclick") || "").toLowerCase();
        return txt.includes("attachment") ||
               txt.includes("attach") ||
               txt.includes("pdf") ||
               href.includes(".pdf") ||
               onclick.includes(".pdf");
      })
      .map(el => el.closest("a, button, [onclick]") || el)
      .filter(visible);

    const uniqueTargets = Array.from(new Set(rawTargets))
      .sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

    const candidates = [];

    for (const target of uniqueTargets) {
      const card = cardForAttachment(target);
      const cardText = textOf(card);
      const lower = cardText.toLowerCase();

      const isToday = todayLabels.some(label => lower.includes(label));
      if (!isToday) continue;

      const f = fp(cardText + " " + textOf(target) + " " + (directPdfFrom(target) || ""));
      if (!f || processed.includes(f)) continue;

      const r = target.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 1800),
        fp: f,
        path: makePath(target),
        directPdf: directPdfFrom(target),
        top: Math.round(r.top),
        left: Math.round(r.left),
        targetText: textOf(target).slice(0, 200)
      });
    }

    return candidates.sort((a,b) => a.top - b.top || a.left - b.left)[0] || null;
    """

    try:
        return driver.execute_script(js, processed, today_labels)
    except JavascriptException:
        return None


def get_pdf_url_from_list_attachment(driver: webdriver.Chrome, target: Dict[str, str], app_handle: str) -> Optional[str]:
    """
    Normal list page ke visible Attachment/PDF ko handle karega.
    Direct href ho to direct URL lega, warna sirf attachment button click karega.
    Circular/card/detail click nahi karega.
    """
    direct_pdf = target.get("directPdf")
    if is_pdf_url(direct_pdf):
        print("Direct PDF href found on list page.")
        return direct_pdf

    return get_pdf_url_from_attachment(driver, target, app_handle)




# -----------------------------
# PORTAL DATE TARGET HELPERS
# -----------------------------

def get_portal_visible_date_labels(driver: webdriver.Chrome) -> List[str]:
    """
    Dashboard par jo visible date cards me dikhegi, usme se top/latest visible date target banegi.
    Screenshot jaisa: Jul 09, 2026
    """
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    const els = Array.from(document.querySelectorAll("body *")).filter(visible);
    const found = [];

    for (const el of els) {
      const txt = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (!txt || txt.length > 120) continue;

      const matches = txt.match(/\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/g) || [];
      for (const m of matches) {
        const r = el.getBoundingClientRect();
        found.push({date:m, top:Math.round(r.top), left:Math.round(r.left)});
      }
    }

    const seen = new Set();
    return found
      .sort((a,b) => a.top - b.top || a.left - b.left)
      .map(x => x.date)
      .filter(d => {
        const k = d.toLowerCase();
        if (seen.has(k)) return false;
        seen.add(k);
        return true;
      });
    """
    try:
        dates = driver.execute_script(js) or []
        return [str(d) for d in dates if d]
    except Exception:
        return []


def build_date_label_variants(date_label: str) -> List[str]:
    """
    Jul 09, 2026 -> Jul 09, 2026 + Jul 9, 2026
    """
    labels = []
    clean = clean_text(date_label)
    if clean:
        labels.append(clean)
        m = re.match(r"^([A-Za-z]{3,8})\s+0?(\d{1,2}),\s+(\d{4})$", clean)
        if m:
            mon, day, year = m.groups()
            labels.append(f"{mon} {int(day)}, {year}")
            labels.append(f"{mon} {int(day):02d}, {year}")

    # Keep system today as fallback too
    for label in TODAY_DATE_LABELS:
        if label not in labels:
            labels.append(label)

    # Deduplicate
    out = []
    seen = set()
    for x in labels:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def choose_target_date_labels_from_portal(driver: webdriver.Chrome) -> List[str]:
    """
    Portal ke top visible date ko target banata hai.
    Isse screenshot wali Jul 09, 2026 bhi click hogi, chahe system date Jul 11 ho.
    """
    visible_dates = get_portal_visible_date_labels(driver)
    if visible_dates:
        labels = build_date_label_variants(visible_dates[0])
        print(f"Portal target date selected: {visible_dates[0]}")
        print(f"Date labels used: {labels}")
        return labels

    print(f"No visible portal date found, fallback to system today labels: {TODAY_DATE_LABELS}")
    return TODAY_DATE_LABELS


# -----------------------------
# TODAY MESSAGE CLICK FLOW
# -----------------------------

def find_next_today_message_card_on_dashboard(driver: webdriver.Chrome, processed_cards: Set[str], target_date_labels: List[str]) -> Optional[Dict[str, str]]:
    """
    Normal dashboard/home page par target date ka next visible message card dhoondhta hai.
    It prefers real detail URLs like:
    Announcement.aspx?Id=19284&&Type=homework
    """
    processed = list(processed_cards)

    js = r"""
    const processed = arguments[0] || [];
    const targetLabels = (arguments[1] || []).map(x => String(x).toLowerCase());

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function fp(text, url) {
      return ((text || "") + " " + (url || ""))
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b|\bpay now\b/ig, "")
        .replace(/https?:\/\/\S+/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 900);
    }

    function makePath(el) {
      if (!el) return "";
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function dateMatch(text) {
      const lower = (text || "").toLowerCase();
      return targetLabels.some(label => lower.includes(label));
    }

    function detailUrlFrom(el) {
      if (!el) return null;

      // Direct href
      if (el.getAttribute) {
        const href = el.getAttribute("href") || "";
        if (/Announcement\.aspx\?Id=/i.test(href) || /Type=homework/i.test(href)) {
          return abs(href);
        }

        const onclick = el.getAttribute("onclick") || "";
        const m = onclick.match(/(?:https?:\/\/[^'"<>\s]+)?\/?ManavMangal88\/ParentApp\/Announcement\.aspx\?Id=[^'"<>\s]+|Announcement\.aspx\?Id=[^'"<>\s]+/i);
        if (m) return abs(m[0]);

        const m2 = onclick.match(/(?:location\.href|window\.location|open)\s*\(?\s*['"]([^'"]*Announcement\.aspx\?Id=[^'"]+)['"]/i);
        if (m2) return abs(m2[1]);
      }

      // Check clickable ancestors
      let p = el;
      for (let i=0; i<10 && p; i++, p=p.parentElement) {
        if (p.getAttribute) {
          const href = p.getAttribute("href") || "";
          if (/Announcement\.aspx\?Id=/i.test(href) || /Type=homework/i.test(href)) return abs(href);

          const onclick = p.getAttribute("onclick") || "";
          const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
          if (m) return abs(m[0]);
        }
      }

      // Check children
      const found = el.querySelector && el.querySelector('a[href*="Announcement.aspx?Id="], a[href*="Type=homework"], [onclick*="Announcement.aspx?Id="]');
      if (found) return detailUrlFrom(found);

      return null;
    }

    function compactCardFor(el) {
      let best = el;
      let p = el;

      for (let i = 0; i < 15 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const r = p.getBoundingClientRect();

        if (
          dateMatch(txt) &&
          txt.length >= 15 &&
          txt.length <= 3200 &&
          r.width >= 170 &&
          r.height >= 35 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }

      return best;
    }

    function clickableFor(card) {
      const link = card.querySelector && card.querySelector('a[href*="Announcement.aspx?Id="], a[href*="Type=homework"], [onclick*="Announcement.aspx?Id="]');
      return link || card.closest("a, button, [onclick]") || card.querySelector("a, button, [onclick]") || card;
    }

    const candidates = [];

    // First pass: real detail links/onclicks
    const linkNodes = Array.from(document.querySelectorAll('a[href*="Announcement.aspx?Id="], a[href*="Type=homework"], [onclick*="Announcement.aspx?Id="]'))
      .filter(visible);

    for (const node of linkNodes) {
      const card = compactCardFor(node);
      const cardText = textOf(card);
      if (!dateMatch(cardText)) continue;

      const url = detailUrlFrom(node) || detailUrlFrom(card);
      const f = fp(cardText, url);
      if (!f || processed.includes(f)) continue;

      const r = card.getBoundingClientRect();
      candidates.push({
        text: cardText.slice(0, 2400),
        fp: f,
        path: makePath(node),
        cardPath: makePath(card),
        detailUrl: url,
        top: Math.round(r.top),
        left: Math.round(r.left),
        targetText: textOf(node).slice(0, 250)
      });
    }

    // Fallback pass: any visible node with target date
    const nodes = Array.from(document.querySelectorAll("a, button, [onclick], div, li, tr, section, article"))
      .filter(visible);

    for (const el of nodes) {
      const rawText = textOf(el);
      if (!rawText || !dateMatch(rawText)) continue;
      if (rawText.length > 3600) continue;

      const card = compactCardFor(el);
      const cardText = textOf(card);
      if (!dateMatch(cardText)) continue;

      const target = clickableFor(card);
      if (!visible(target)) continue;

      const url = detailUrlFrom(target) || detailUrlFrom(card);
      const f = fp(cardText, url);
      if (!f || processed.includes(f)) continue;

      const r = card.getBoundingClientRect();
      candidates.push({
        text: cardText.slice(0, 2400),
        fp: f,
        path: makePath(target),
        cardPath: makePath(card),
        detailUrl: url,
        top: Math.round(r.top),
        left: Math.round(r.left),
        targetText: textOf(target).slice(0, 250)
      });
    }

    const map = new Map();
    for (const c of candidates) {
      if (!map.has(c.fp)) map.set(c.fp, c);
    }

    return Array.from(map.values()).sort((a,b) => a.top - b.top || a.left - b.left)[0] || null;
    """

    try:
        return driver.execute_script(js, processed, target_date_labels)
    except JavascriptException:
        return None



def click_path_human(driver: webdriver.Chrome, path: str) -> bool:
    """
    JS click + Selenium ActionChains fallback. Kuch cards JS click se open nahi hote.
    """
    if click_path(driver, path):
        return True

    try:
        js = """
        const path = arguments[0];
        function getEl(path) {
          if (!path) return null;
          if (path.startsWith("id:")) return document.getElementById(path.slice(3));
          if (path.startsWith("css:")) return document.querySelector(path.slice(4));
          return null;
        }
        const el = getEl(path);
        if (!el) return null;
        el.scrollIntoView({block:"center", inline:"center"});
        return el;
        """
        el = driver.execute_script(js, path)
        if el:
            ActionChains(driver).move_to_element(el).pause(0.15).click().perform()
            time.sleep(WAIT_AFTER_CLICK)
            wait_ready(driver)
            return True
    except Exception:
        pass

    return False


def open_message_card_and_wait(driver: webdriver.Chrome, card: Dict[str, str]) -> bool:
    """
    Dashboard message card open karega.
    Prefer detailUrl from actual link shown in Chrome status bar.
    """
    before_url = driver.current_url
    detail_url = card.get("detailUrl")

    if detail_url and "announcement.aspx?id=" in detail_url.lower():
        print(f"Opening message detail URL: {detail_url}")
        driver.get(detail_url)
        wait_ready(driver)
        time.sleep(1.0)
        return driver.current_url != before_url or "announcement.aspx?id=" in driver.current_url.lower()

    before_text = ""
    try:
        before_text = app_current_text(driver)[:800]
    except Exception:
        pass

    clicked = click_path_human(driver, card.get("path", ""))
    if not clicked and card.get("cardPath"):
        clicked = click_path_human(driver, card.get("cardPath", ""))

    if not clicked:
        return False

    time.sleep(1.0)

    end = time.time() + 8
    while time.time() < end:
        try:
            current_url = driver.current_url
            body = app_current_text(driver)
            url_changed = current_url != before_url
            text_changed = body[:800] != before_text

            if url_changed or text_changed:
                time.sleep(0.5)
                return True
        except Exception:
            pass
        time.sleep(0.35)

    return True



def extract_pdf_from_current_message_detail(driver: webdriver.Chrome, app_handle: str) -> Optional[str]:
    """
    Message ke andar PDF/Attachment find karega.
    Detail page par direct link, iframe, onclick, ya visible Attachment button sab check.
    """
    direct = extract_pdf_from_current_page(driver)
    if is_pdf_url(direct):
        return direct

    target = visible_attachment_target(driver, set())
    if not target:
        return None

    return get_pdf_url_from_attachment(driver, target, app_handle)


def return_to_dashboard(driver: webdriver.Chrome) -> None:
    """
    Message detail / PDF / other URL se normal dashboard par wapas.
    """
    try:
        if "dashboard.aspx" in driver.current_url.lower():
            return

        # First try app back arrow because EduSecure app uses internal back.
        if click_app_back_arrow(driver):
            time.sleep(WAIT_AFTER_CLICK)

        if "dashboard.aspx" not in driver.current_url.lower():
            try:
                driver.back()
                time.sleep(WAIT_AFTER_CLICK)
                wait_ready(driver)
            except Exception:
                pass

        if "dashboard.aspx" not in driver.current_url.lower():
            driver.get(START_URL)
            wait_ready(driver)
            time.sleep(0.8)
    except Exception:
        try:
            driver.get(START_URL)
            wait_ready(driver)
            time.sleep(0.8)
        except Exception:
            pass


# -----------------------------
# V23: MISSING-PDF SYNC HELPERS
# -----------------------------

def normalize_pdf_url(url: str | None) -> str:
    if not url:
        return ""
    return clean_text(url).strip().lower()


def scrape_existing_admin_pdf_urls(driver: webdriver.Chrome) -> Set[str]:
    """
    admin.html par already existing PDF URLs collect karta hai.
    """
    js = r"""
    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return u || ""; }
    }

    const found = new Set();

    function addCandidate(v) {
      if (!v) return;
      const s = String(v).trim();
      if (!s) return;

      const matches = s.match(/https?:\/\/[^\s"'<>]+?\.pdf(?:\?[^\s"'<>]*)?/ig) || [];
      for (const m of matches) found.add(m);

      if (/\.pdf(?:\?|$)/i.test(s) && !/^javascript:/i.test(s)) {
        try { found.add(abs(s)); } catch(e) {}
      }
    }

    for (const el of document.querySelectorAll("a[href], iframe[src], embed[src], object[data], input[value], textarea")) {
      addCandidate(el.getAttribute("href"));
      addCandidate(el.getAttribute("src"));
      addCandidate(el.getAttribute("data"));
      addCandidate(el.value);
      addCandidate(el.textContent);
    }

    addCandidate(document.body ? document.body.innerText : "");
    addCandidate(document.documentElement ? document.documentElement.innerHTML : "");

    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        addCandidate(localStorage.getItem(k));
      }
    } catch(e) {}

    try {
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        addCandidate(sessionStorage.getItem(k));
      }
    } catch(e) {}

    return Array.from(found);
    """

    try:
        urls = driver.execute_script(js) or []
        return {normalize_pdf_url(u) for u in urls if is_pdf_url(u)}
    except Exception:
        return set()


def find_next_message_card_on_dashboard(driver: webdriver.Chrome, processed_cards: Set[str]) -> Optional[Dict[str, str]]:
    """
    Dashboard par next visible message card dhoondhta hai.
    No date filter. Newest/top messages pehle process honge.
    """
    processed = list(processed_cards)

    js = r"""
    const processed = arguments[0] || [];

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function makePath(el) {
      if (!el) return "";
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function detailUrlFrom(el) {
      if (!el) return null;

      if (el.getAttribute) {
        const href = el.getAttribute("href") || "";
        if (/Announcement\.aspx\?Id=/i.test(href)) return abs(href);

        const onclick = el.getAttribute("onclick") || "";
        const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
        if (m) return abs(m[0]);
      }

      let p = el;
      for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
        if (!p.getAttribute) continue;

        const href = p.getAttribute("href") || "";
        if (/Announcement\.aspx\?Id=/i.test(href)) return abs(href);

        const onclick = p.getAttribute("onclick") || "";
        const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
        if (m) return abs(m[0]);
      }

      const child = el.querySelector && el.querySelector('a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]');
      if (child) return detailUrlFrom(child);

      return null;
    }

    function fp(text, detailUrl) {
      if (detailUrl) return "url:" + detailUrl.toLowerCase();

      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b|\bpay now\b/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 1000);
    }

    function compactCardFor(el) {
      let best = el;
      let p = el;

      for (let i = 0; i < 14 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const r = p.getBoundingClientRect();

        if (
          txt.length >= 12 &&
          txt.length <= 3200 &&
          r.width >= 170 &&
          r.height >= 35 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }

      return best;
    }

    const candidates = [];

    const detailNodes = Array.from(
      document.querySelectorAll('a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]')
    ).filter(visible);

    for (const node of detailNodes) {
      const url = detailUrlFrom(node);
      if (!url) continue;

      const card = compactCardFor(node);
      const cardText = textOf(card);
      const f = fp(cardText, url);

      if (!f || processed.includes(f)) continue;

      const r = card.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 2500),
        fp: f,
        path: makePath(node),
        cardPath: makePath(card),
        detailUrl: url,
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    const map = new Map();
    for (const c of candidates) {
      if (!map.has(c.fp)) map.set(c.fp, c);
    }

    return Array.from(map.values())
      .sort((a,b) => a.top - b.top || a.left - b.left)[0] || null;
    """

    try:
        return driver.execute_script(js, processed)
    except JavascriptException:
        return None


def open_message_detail_v23(driver: webdriver.Chrome, card: Dict[str, str]) -> bool:
    before_url = driver.current_url
    detail_url = card.get("detailUrl")

    if detail_url and "announcement.aspx?id=" in detail_url.lower():
        print(f"Opening message detail: {detail_url}")
        driver.get(detail_url)
        wait_ready(driver)
        time.sleep(0.9)
        return "announcement.aspx?id=" in driver.current_url.lower()

    if click_path_human(driver, card.get("path", "")):
        time.sleep(0.8)
        return driver.current_url != before_url or "announcement.aspx?id=" in driver.current_url.lower()

    if card.get("cardPath") and click_path_human(driver, card.get("cardPath", "")):
        time.sleep(0.8)
        return driver.current_url != before_url or "announcement.aspx?id=" in driver.current_url.lower()

    return False


def scroll_dashboard(driver: webdriver.Chrome) -> Dict[str, object]:
    return scroll_announcement_list_until_move(driver)



# -----------------------------
# V24: PUBLIC WEBSITE ANALYSIS + EVERY MESSAGE CLICK
# -----------------------------

def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(clean_text(url))
        scheme = (p.scheme or "https").lower()
        host = (p.netloc or "").lower()
        path = p.path or "/"
        query = p.query or ""
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:
        return clean_text(url).lower()


def pdf_keys(url: str | None) -> Set[str]:
    """
    Compare PDFs by full URL + filename.
    EduSecure PDF filenames are unique-looking hashes, so filename comparison
    also catches the same PDF if website stores the same file with URL variations.
    """
    keys: Set[str] = set()
    if not url or not is_pdf_url(url):
        return keys

    u = canonical_url(url).lower()
    if u:
        keys.add("url:" + u)

    try:
        from urllib.parse import urlsplit, unquote
        name = Path(unquote(urlsplit(url).path)).name.lower().strip()
        if name.endswith(".pdf"):
            keys.add("file:" + name)
    except Exception:
        pass

    return keys


def generic_scroll_down(driver: webdriver.Chrome) -> Dict[str, object]:
    """
    Generic scroll for 8apdf.xo.je or any normal page.
    """
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const amount = Math.max(500, Math.floor(window.innerHeight * 0.82));

    const containers = Array.from(document.querySelectorAll("*"))
      .filter(visible)
      .filter(el => (el.scrollHeight - el.clientHeight) > 30)
      .map(el => ({
        el,
        max: el.scrollHeight - el.clientHeight,
        top: el.scrollTop || 0
      }))
      .sort((a,b) => b.max - a.max);

    for (const c of containers) {
      const before = c.el.scrollTop || 0;
      const max = Math.max(0, c.el.scrollHeight - c.el.clientHeight);
      c.el.scrollTop = Math.min(max, before + amount);
      const after = c.el.scrollTop || 0;
      if (after !== before) {
        return {
          moved: true,
          source: c.el.tagName,
          before,
          after,
          max,
          atBottom: after >= max - 8
        };
      }
    }

    const before = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
    const max = Math.max(
      0,
      Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0) -
      (window.innerHeight || document.documentElement.clientHeight || 0)
    );
    window.scrollBy(0, amount);
    const after = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;

    return {
      moved: after !== before,
      source: "window",
      before,
      after,
      max,
      atBottom: after >= max - 8
    };
    """

    try:
        result = driver.execute_script(js) or {}
        time.sleep(0.45)
        return result
    except Exception:
        return {"moved": False, "atBottom": False}


def scan_public_page_dom(driver: webdriver.Chrome, site_host: str) -> Dict[str, List[str]]:
    """
    Extract PDF URLs and same-site internal links from current public page.
    Also scans inline HTML, localStorage and sessionStorage.
    """
    js = r"""
    const siteHost = String(arguments[0] || "").toLowerCase();

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    const pdfs = new Set();
    const links = new Set();

    function addTextForPdfs(v) {
      if (!v) return;
      const s = String(v);
      const matches = s.match(/https?:\/\/[^\s"'<>\\]+?\.pdf(?:\?[^\s"'<>\\]*)?/ig) || [];
      for (const m of matches) pdfs.add(m);

      const rel = s.match(/(?:\/|\.\/|\.\.\/)[^\s"'<>\\]+?\.pdf(?:\?[^\s"'<>\\]*)?/ig) || [];
      for (const m of rel) {
        const u = abs(m);
        if (u) pdfs.add(u);
      }
    }

    for (const a of document.querySelectorAll("a[href]")) {
      const u = abs(a.getAttribute("href"));
      if (!u) continue;

      try {
        const parsed = new URL(u);
        if (u.toLowerCase().includes(".pdf")) {
          pdfs.add(u);
        } else if (
          parsed.hostname.toLowerCase() === siteHost &&
          /^https?:$/i.test(parsed.protocol)
        ) {
          parsed.hash = "";
          links.add(parsed.href);
        }
      } catch(e) {}
    }

    for (const el of document.querySelectorAll("[src], [data], [onclick], [value]")) {
      addTextForPdfs(el.getAttribute("src"));
      addTextForPdfs(el.getAttribute("data"));
      addTextForPdfs(el.getAttribute("onclick"));
      addTextForPdfs(el.getAttribute("value"));
    }

    addTextForPdfs(document.documentElement ? document.documentElement.innerHTML : "");
    addTextForPdfs(document.body ? document.body.innerText : "");

    try {
      for (let i = 0; i < localStorage.length; i++) {
        addTextForPdfs(localStorage.getItem(localStorage.key(i)));
      }
    } catch(e) {}

    try {
      for (let i = 0; i < sessionStorage.length; i++) {
        addTextForPdfs(sessionStorage.getItem(sessionStorage.key(i)));
      }
    } catch(e) {}

    return {
      pdfs: Array.from(pdfs),
      links: Array.from(links)
    };
    """

    try:
        result = driver.execute_script(js, site_host) or {}
        return {
            "pdfs": list(result.get("pdfs") or []),
            "links": list(result.get("links") or []),
        }
    except Exception:
        return {"pdfs": [], "links": []}


def should_crawl_public_url(url: str, site_host: str) -> bool:
    try:
        from urllib.parse import urlsplit
        p = urlsplit(url)
        if p.netloc.lower() != site_host.lower():
            return False

        low_path = (p.path or "/").lower()
        if any(x in low_path for x in ["/admin", "/logout", "/login", "/signin", "/signup", "/register"]):
            return False

        if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|ico|woff2?|ttf|zip|rar|mp4|mp3|pdf)$", low_path):
            return False

        return True
    except Exception:
        return False


def analyze_entire_public_pdf_site(driver: webdriver.Chrome, max_pages: int = 120) -> Set[str]:
    """
    First step of V24:
    Opens 8apdf.xo.je and crawls same-site pages, scrolls each page,
    and builds the set of PDFs already present on the website.
    """
    from urllib.parse import urlsplit

    print(f"\n=== STEP 1: ANALYZING PUBLIC WEBSITE ===")
    print(f"Opening: {PUBLIC_PDF_SITE}")

    site_host = urlsplit(PUBLIC_PDF_SITE).netloc.lower()
    queue = [PUBLIC_PDF_SITE]
    visited: Set[str] = set()
    all_pdfs: Set[str] = set()

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        can = canonical_url(url)
        if not can or can in visited:
            continue
        if not should_crawl_public_url(can, site_host):
            continue

        visited.add(can)
        print(f"[Website scan {len(visited)}] {can}")

        try:
            driver.get(can)
            wait_ready(driver)
            time.sleep(0.8)
        except Exception as e:
            print(f"  Could not open page: {str(e)[:120]}")
            continue

        bottom_hits = 0
        for _ in range(40):
            scanned = scan_public_page_dom(driver, site_host)

            for p in scanned["pdfs"]:
                if is_pdf_url(p):
                    all_pdfs.add(canonical_url(p))

            for link in scanned["links"]:
                c = canonical_url(link)
                if c and c not in visited and c not in queue and should_crawl_public_url(c, site_host):
                    queue.append(c)

            scroll = generic_scroll_down(driver)

            if scroll.get("atBottom") and not scroll.get("moved"):
                bottom_hits += 1
            elif scroll.get("atBottom"):
                bottom_hits += 1
            else:
                bottom_hits = 0

            if bottom_hits >= 3:
                break

        print(f"  PDFs found so far: {len(all_pdfs)} | queued pages: {len(queue)}")

    print(f"\nWebsite analysis complete.")
    print(f"Pages analyzed: {len(visited)}")
    print(f"Existing PDF URLs found: {len(all_pdfs)}")

    return all_pdfs


def get_dashboard_scroll_position(driver: webdriver.Chrome) -> float:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const containers = Array.from(document.querySelectorAll("*"))
      .filter(visible)
      .filter(el => (el.scrollHeight - el.clientHeight) > 30)
      .map(el => {
        const txt = (el.innerText || el.textContent || "").slice(0, 6000);
        return {
          el,
          score: (/School Diary|Circular|Learning Planner|Class Work|Home Work/i.test(txt) ? 1000000 : 0) +
                 (el.scrollHeight - el.clientHeight)
        };
      })
      .sort((a,b) => b.score - a.score);

    if (containers.length) return containers[0].el.scrollTop || 0;
    return window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
    """
    try:
        return float(driver.execute_script(js) or 0)
    except Exception:
        return 0.0


def restore_dashboard_scroll_position(driver: webdriver.Chrome, position: float) -> None:
    js = r"""
    const position = Number(arguments[0] || 0);

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const containers = Array.from(document.querySelectorAll("*"))
      .filter(visible)
      .filter(el => (el.scrollHeight - el.clientHeight) > 30)
      .map(el => {
        const txt = (el.innerText || el.textContent || "").slice(0, 6000);
        return {
          el,
          score: (/School Diary|Circular|Learning Planner|Class Work|Home Work/i.test(txt) ? 1000000 : 0) +
                 (el.scrollHeight - el.clientHeight)
        };
      })
      .sort((a,b) => b.score - a.score);

    if (containers.length) {
      containers[0].el.scrollTop = position;
      return containers[0].el.scrollTop || 0;
    }

    window.scrollTo(0, position);
    return window.scrollY || 0;
    """
    try:
        driver.execute_script(js, position)
        time.sleep(0.45)
    except Exception:
        pass


def find_next_visible_dashboard_message_v24(driver: webdriver.Chrome, processed_detail_urls: Set[str]) -> Optional[Dict[str, str]]:
    """
    Find the next visible EduSecure message by its real Announcement.aspx?Id=... link.
    No date filter. Every message is checked.
    """
    processed = [canonical_url(x).lower() for x in processed_detail_urls]

    js = r"""
    const processed = new Set(arguments[0] || []);

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function visibleRect(el) {
      if (!el) return null;
      const st = window.getComputedStyle(el);
      if (st.display === "none" || st.visibility === "hidden") return null;

      let r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) return r;

      for (const child of el.querySelectorAll("*")) {
        const cr = child.getBoundingClientRect();
        if (cr.width > 0 && cr.height > 0) return cr;
      }
      return null;
    }

    function textOf(el) {
      return (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    }

    function detailUrl(el) {
      const href = el.getAttribute("href") || "";
      if (/Announcement\.aspx\?Id=/i.test(href)) return abs(href);

      const onclick = el.getAttribute("onclick") || "";
      const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
      if (m) return abs(m[0]);

      return null;
    }

    const raw = Array.from(
      document.querySelectorAll('a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]')
    );

    const items = [];

    for (const el of raw) {
      const url = detailUrl(el);
      if (!url) continue;

      const key = url.toLowerCase();
      if (processed.has(key)) continue;

      const r = visibleRect(el);
      if (!r) continue;
      if (r.bottom <= 0 || r.top >= window.innerHeight) continue;

      let card = el;
      let p = el;
      for (let i=0; i<10 && p; i++, p=p.parentElement) {
        const txt = textOf(p);
        const pr = p.getBoundingClientRect();
        if (
          txt.length >= 8 &&
          txt.length <= 3000 &&
          pr.width >= 150 &&
          pr.height >= 25
        ) {
          card = p;
        }
      }

      items.push({
        detailUrl: url,
        text: textOf(card).slice(0, 2500),
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    const map = new Map();
    for (const item of items) {
      const k = item.detailUrl.toLowerCase();
      if (!map.has(k)) map.set(k, item);
    }

    return Array.from(map.values())
      .sort((a,b) => a.top - b.top || a.left - b.left)[0] || null;
    """

    try:
        return driver.execute_script(js, processed)
    except Exception:
        return None


def click_dashboard_message_by_url(driver: webdriver.Chrome, detail_url: str) -> bool:
    """
    Actually clicks the matching message link on Dashboard.
    Direct URL navigation is only a fallback.
    """
    js = r"""
    const targetUrl = String(arguments[0] || "");

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    const candidates = Array.from(
      document.querySelectorAll('a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]')
    );

    for (const el of candidates) {
      let u = null;

      const href = el.getAttribute("href") || "";
      if (href) u = abs(href);

      if (!u) {
        const onclick = el.getAttribute("onclick") || "";
        const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
        if (m) u = abs(m[0]);
      }

      if (u && u === targetUrl) {
        el.scrollIntoView({block:"center", inline:"center"});
        el.click();
        return true;
      }
    }

    return false;
    """

    before = driver.current_url

    try:
        clicked = bool(driver.execute_script(js, detail_url))
    except Exception:
        clicked = False

    if clicked:
        end = time.time() + 8
        while time.time() < end:
            try:
                if driver.current_url != before and "announcement.aspx?id=" in driver.current_url.lower():
                    wait_ready(driver)
                    time.sleep(0.55)
                    return True
            except Exception:
                pass
            time.sleep(0.25)

    # Fallback: opening the same detail URL gives the same message detail page.
    try:
        print("  Click fallback -> opening exact message URL.")
        driver.get(detail_url)
        wait_ready(driver)
        time.sleep(0.7)
        return "announcement.aspx?id=" in driver.current_url.lower()
    except Exception:
        return False


def dashboard_scroll_v24(driver: webdriver.Chrome) -> Dict[str, object]:
    """
    Scroll the EduSecure dashboard's real message container.
    """
    return scroll_announcement_list_until_move(driver)



# -----------------------------
# V25: NAME ENTRY + REAL MESSAGE CLICK
# -----------------------------

PUBLIC_SITE_NAME = "xyz"


def enter_public_site_name_once_v28(driver: webdriver.Chrome) -> bool:
    """
    EXACT 8apdf.xo.je name screen handling.

    Screen:
      input placeholder = "Write your name"
      button text       = "Continue"

    Flow:
      enter xyz
      click Continue
      sleep 5 seconds
      then analysis may start
    """
    print("Waiting for 8apdf name screen...")

    name_input = None

    # Wait up to 25 seconds for the exact name input.
    end_time = time.time() + 25

    while time.time() < end_time:
        try:
            candidates = driver.find_elements(
                By.XPATH,
                "//input[@placeholder='Write your name' or "
                "contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'write your name') or "
                "contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'your name')]"
            )

            for el in candidates:
                if el.is_displayed() and el.is_enabled():
                    name_input = el
                    break

            if name_input:
                break

        except Exception:
            pass

        time.sleep(0.25)

    # If exact placeholder not found, try visible non-search text input.
    if name_input is None:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, "input"):
                if not el.is_displayed() or not el.is_enabled():
                    continue

                typ = (el.get_attribute("type") or "text").lower()
                placeholder = (el.get_attribute("placeholder") or "").lower()

                if typ in ("hidden", "password", "submit", "button", "checkbox", "radio", "search"):
                    continue

                if "search" in placeholder:
                    continue

                name_input = el
                break
        except Exception:
            pass

    if name_input is None:
        print("❌ 'Write your name' input not found.")
        return False

    print("✅ Name input found.")
    print("Typing: xyz")

    # Fill using real keyboard interaction first.
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
            name_input
        )
        time.sleep(0.15)

        name_input.click()
        time.sleep(0.1)

        name_input.send_keys(Keys.CONTROL, "a")
        name_input.send_keys(Keys.BACKSPACE)
        name_input.send_keys(PUBLIC_SITE_NAME)

    except Exception:
        # Native value setter fallback.
        try:
            driver.execute_script(
                """
                const el = arguments[0], value = arguments[1];

                el.focus();

                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype,
                    "value"
                ).set;

                setter.call(el, value);

                el.dispatchEvent(new Event("input", {
                    bubbles: true
                }));

                el.dispatchEvent(new Event("change", {
                    bubbles: true
                }));

                el.dispatchEvent(new KeyboardEvent("keyup", {
                    bubbles: true,
                    key: "x"
                }));
                """,
                name_input,
                PUBLIC_SITE_NAME,
            )
        except Exception as exc:
            print(f"❌ Could not type name: {exc}")
            return False

    # Verify actual field value BEFORE Continue.
    try:
        actual_value = (name_input.get_attribute("value") or "").strip()
    except Exception:
        actual_value = ""

    if actual_value.lower() != PUBLIC_SITE_NAME.lower():
        # One stronger JS retry.
        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const value = arguments[1];

                el.value = value;

                el.dispatchEvent(new InputEvent("input", {
                    bubbles: true,
                    inputType: "insertText",
                    data: value
                }));

                el.dispatchEvent(new Event("change", {
                    bubbles: true
                }));
                """,
                name_input,
                PUBLIC_SITE_NAME
            )

            actual_value = (name_input.get_attribute("value") or "").strip()
        except Exception:
            pass

    if actual_value.lower() != PUBLIC_SITE_NAME.lower():
        print(f"❌ Name field verification failed. Current value: {actual_value!r}")
        return False

    print("✅ xyz entered successfully.")

    # Find exact Continue button.
    continue_button = None
    end_time = time.time() + 15

    while time.time() < end_time:
        try:
            candidates = driver.find_elements(
                By.XPATH,
                "//button[normalize-space()='Continue'] | "
                "//input[@type='submit' and @value='Continue'] | "
                "//*[@role='button' and normalize-space()='Continue']"
            )

            for el in candidates:
                if el.is_displayed() and el.is_enabled():
                    continue_button = el
                    break

            if continue_button:
                break

        except Exception:
            pass

        time.sleep(0.2)

    if continue_button is None:
        print("❌ Continue button not found.")
        return False

    print("✅ Continue button found. Clicking...")

    clicked = False

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
            continue_button
        )
        time.sleep(0.1)
        continue_button.click()
        clicked = True
    except Exception:
        try:
            ActionChains(driver).move_to_element(
                continue_button
            ).pause(0.1).click().perform()
            clicked = True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", continue_button)
                clicked = True
            except Exception:
                pass

    if not clicked:
        print("❌ Continue button click failed.")
        return False

    print("✅ Continue clicked.")
    print("⏳ Waiting exactly 5 seconds. NO browser action during this wait...")

    # IMPORTANT:
    # Nothing browser-related happens during these five seconds.
    time.sleep(5.0)

    print("✅ 5 seconds completed. Website analysis can start now.")
    return True



def analyze_entire_public_pdf_site_v28(driver: webdriver.Chrome, max_pages: int = 120) -> Set[str]:
    """
    V28 flow:
    1. Open 8apdf.xo.je.
    2. Enter xyz ONLY on initial name-entry screen.
    3. Wait 5 seconds.
    4. Never touch the library search box.
    5. Crawl/scroll same-site pages and collect PDF URLs.
    """
    from urllib.parse import urlsplit

    print("\n=== STEP 1: ANALYZING 8apdf.xo.je ===")
    driver.get(PUBLIC_PDF_SITE)
    wait_ready(driver)
    time.sleep(0.8)

    if not enter_public_site_name_once_v28(driver):
        print("⚠️ Could not auto-submit initial name. Continuing current page.")

    root_url = driver.current_url or PUBLIC_PDF_SITE
    site_host = urlsplit(PUBLIC_PDF_SITE).netloc.lower()

    queue = [root_url]

    root_public = canonical_url(PUBLIC_PDF_SITE)
    if root_public and root_public not in [canonical_url(root_url)]:
        queue.append(PUBLIC_PDF_SITE)

    visited: Set[str] = set()
    all_pdfs: Set[str] = set()

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        can = canonical_url(url)

        if not can or can in visited:
            continue

        if not should_crawl_public_url(can, site_host):
            continue

        visited.add(can)
        print(f"[Website scan {len(visited)}] {can}")

        try:
            # Do not reload the current root page unnecessarily after name submit,
            # otherwise it may return to the entry screen.
            if canonical_url(driver.current_url) != can:
                driver.get(can)
                wait_ready(driver)
                time.sleep(0.7)
        except Exception as e:
            print(f"  Could not open page: {str(e)[:120]}")
            continue

        # IMPORTANT: no name-entry helper here.
        # Search inputs are never touched during crawl.

        bottom_hits = 0

        for _ in range(50):
            scanned = scan_public_page_dom(driver, site_host)

            for p in scanned["pdfs"]:
                if is_pdf_url(p):
                    all_pdfs.add(canonical_url(p))

            for link in scanned["links"]:
                c = canonical_url(link)

                if (
                    c
                    and c not in visited
                    and c not in queue
                    and should_crawl_public_url(c, site_host)
                ):
                    queue.append(c)

            scroll = generic_scroll_down(driver)

            if scroll.get("atBottom") and not scroll.get("moved"):
                bottom_hits += 1
            elif scroll.get("atBottom"):
                bottom_hits += 1
            else:
                bottom_hits = 0

            if bottom_hits >= 3:
                break

        print(
            f"  PDFs found so far: {len(all_pdfs)} | "
            f"queued pages: {len(queue)}"
        )

    print("\nWebsite analysis complete.")
    print(f"Pages analyzed: {len(visited)}")
    print(f"Existing PDF URLs found: {len(all_pdfs)}")

    return all_pdfs



def get_visible_message_cards_v25(driver: webdriver.Chrome, processed_fps: Set[str]) -> List[Dict[str, str]]:
    """
    Returns ALL visible dashboard message cards in top-to-bottom order.
    It does not require an href. It identifies real cards using EduSecure message content
    and stores the actual card element path + best clickable path.
    """
    processed = list(processed_fps)

    js = r"""
    const processed = new Set(arguments[0] || []);

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function makePath(el) {
      if (!el) return "";
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function abs(u) {
      try { return new URL(u, location.href).href; } catch(e) { return null; }
    }

    function detailUrlFrom(el) {
      if (!el) return null;

      let nodes = [el];
      let p = el.parentElement;
      for (let i=0; i<8 && p; i++, p=p.parentElement) nodes.push(p);

      if (el.querySelectorAll) {
        nodes = nodes.concat(Array.from(el.querySelectorAll("a[href], [onclick]")));
      }

      for (const n of nodes) {
        if (!n || !n.getAttribute) continue;

        const href = n.getAttribute("href") || "";
        if (/Announcement\.aspx\?Id=/i.test(href)) return abs(href);

        const onclick = n.getAttribute("onclick") || "";
        const m = onclick.match(/Announcement\.aspx\?Id=[^'"<>\s]+/i);
        if (m) return abs(m[0]);
      }

      return null;
    }

    function clickTargetFor(card) {
      const direct = card.querySelector &&
        card.querySelector('a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]');

      if (direct) return direct;

      let p = card;
      for (let i=0; i<5 && p; i++, p=p.parentElement) {
        if (p.matches && p.matches("a, button, [onclick]")) return p;
      }

      const child = card.querySelector && card.querySelector("a, button, [onclick]");
      return child || card;
    }

    function fingerprint(text, url) {
      if (url) return "url:" + url.toLowerCase();

      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b|\bpay now\b/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 1200);
    }

    // Candidate card wrappers: visible blocks containing likely message structure.
    const blocks = Array.from(
      document.querySelectorAll("div, li, tr, section, article, a, button, [onclick]")
    ).filter(visible);

    const candidates = [];

    for (const el of blocks) {
      const txt = textOf(el);
      if (!txt || txt.length < 8 || txt.length > 3000) continue;

      const hasDate = /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(txt);
      const hasMessageType = /School Diary|Circular|Home Work|Class Work|Homework|Announcement|Notice|Message/i.test(txt);
      const hasDetail = !!detailUrlFrom(el);

      if (!hasDate && !hasMessageType && !hasDetail) continue;

      const r = el.getBoundingClientRect();

      // Avoid huge page wrappers.
      if (r.height > window.innerHeight * 0.95 && txt.length > 1200) continue;

      // Choose the smallest meaningful parent around this element.
      let card = el;
      let p = el;
      for (let i=0; i<8 && p; i++, p=p.parentElement) {
        const pt = textOf(p);
        const pr = p.getBoundingClientRect();

        if (
          pt.length >= 8 &&
          pt.length <= 2200 &&
          pr.width >= 180 &&
          pr.height >= 35 &&
          pr.height <= Math.max(650, window.innerHeight * 0.85) &&
          pr.bottom > 0 &&
          pr.top < window.innerHeight
        ) {
          const pHasDate = /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(pt);
          const pHasType = /School Diary|Circular|Home Work|Class Work|Homework|Announcement|Notice|Message/i.test(pt);
          if (pHasDate || pHasType || detailUrlFrom(p)) card = p;
        }
      }

      const cardText = textOf(card);
      const url = detailUrlFrom(card);
      const fp = fingerprint(cardText, url);

      if (!fp || processed.has(fp)) continue;

      const target = clickTargetFor(card);
      if (!visible(target)) continue;

      const cr = card.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 2400),
        fp,
        cardPath: makePath(card),
        clickPath: makePath(target),
        detailUrl: url || "",
        top: Math.round(cr.top),
        left: Math.round(cr.left)
      });
    }

    // Dedupe. Real detail URL wins; otherwise fingerprint.
    const map = new Map();

    for (const c of candidates) {
      const key = c.detailUrl ? "url:" + c.detailUrl.toLowerCase() : c.fp;
      if (!map.has(key)) {
        map.set(key, c);
      } else {
        const old = map.get(key);
        // Prefer smaller/upper card.
        if (c.top < old.top) map.set(key, c);
      }
    }

    return Array.from(map.values())
      .sort((a,b) => a.top - b.top || a.left - b.left)
      .slice(0, 50);
    """

    try:
        return driver.execute_script(js, processed) or []
    except Exception:
        return []


def get_element_from_path_v25(driver: webdriver.Chrome, path: str):
    js = r"""
    const path = arguments[0];

    if (!path) return null;
    if (path.startsWith("id:")) return document.getElementById(path.slice(3));
    if (path.startsWith("css:")) return document.querySelector(path.slice(4));
    return null;
    """

    try:
        return driver.execute_script(js, path)
    except Exception:
        return None


def real_click_message_v25(driver: webdriver.Chrome, message: Dict[str, str]) -> bool:
    """
    REALLY clicks the message card.
    Attempts:
    1. Selenium click on best clickable element
    2. ActionChains center click on card
    3. JS mouse/pointer event sequence on card
    Direct driver.get(detail URL is NOT used here.
    """
    before_url = driver.current_url
    before_text = app_current_text(driver)[:900]

    # Attempt 1: Selenium click target
    target = get_element_from_path_v25(driver, message.get("clickPath", ""))

    if target:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                target,
            )
            time.sleep(0.15)
            target.click()
        except Exception:
            try:
                ActionChains(driver).move_to_element(target).pause(0.15).click().perform()
            except Exception:
                pass

    # Wait for real navigation/change.
    end = time.time() + 4
    while time.time() < end:
        try:
            if driver.current_url != before_url:
                wait_ready(driver)
                time.sleep(0.5)
                return True

            now_text = app_current_text(driver)[:900]
            if now_text != before_text and "Dashboard" not in driver.current_url:
                time.sleep(0.4)
                return True
        except Exception:
            pass

        time.sleep(0.25)

    # Attempt 2: click card container with ActionChains
    card = get_element_from_path_v25(driver, message.get("cardPath", ""))

    if card:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                card,
            )
            time.sleep(0.15)
            ActionChains(driver).move_to_element(card).pause(0.2).click().perform()
        except Exception:
            pass

    end = time.time() + 4
    while time.time() < end:
        try:
            if driver.current_url != before_url:
                wait_ready(driver)
                time.sleep(0.5)
                return True
        except Exception:
            pass
        time.sleep(0.25)

    # Attempt 3: full mouse/pointer event on card
    try:
        js = r"""
        const path = arguments[0];

        function getEl(path) {
          if (!path) return null;
          if (path.startsWith("id:")) return document.getElementById(path.slice(3));
          if (path.startsWith("css:")) return document.querySelector(path.slice(4));
          return null;
        }

        const el = getEl(path);
        if (!el) return false;

        el.scrollIntoView({block:"center", inline:"center"});

        const events = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];

        for (const type of events) {
          el.dispatchEvent(new MouseEvent(type, {
            bubbles: true,
            cancelable: true,
            view: window,
            button: 0
          }));
        }

        return true;
        """
        driver.execute_script(js, message.get("cardPath", ""))
    except Exception:
        pass

    end = time.time() + 4
    while time.time() < end:
        try:
            if driver.current_url != before_url:
                wait_ready(driver)
                time.sleep(0.5)
                return True
        except Exception:
            pass
        time.sleep(0.25)

    print("❌ Real click did not open this message.")
    return False


def return_dashboard_and_restore_v25(driver: webdriver.Chrome, app_handle: str, position: float) -> None:
    """
    Back from message detail to dashboard and restore same scroll position.
    """
    driver.switch_to.window(app_handle)
    restore_app_after_pdf(driver, app_handle)

    try:
        if "dashboard.aspx" not in driver.current_url.lower():
            # First use app/back if possible.
            if not click_app_back_arrow(driver):
                try:
                    driver.back()
                    time.sleep(WAIT_AFTER_CLICK)
                    wait_ready(driver)
                except Exception:
                    pass

        if "dashboard.aspx" not in driver.current_url.lower():
            driver.get(START_URL)
            wait_ready(driver)
            time.sleep(0.6)
    except Exception:
        driver.get(START_URL)
        wait_ready(driver)
        time.sleep(0.6)

    restore_dashboard_scroll_position(driver, position)



# -----------------------------
# V29: CLI FILTER MODES
# -----------------------------

def parse_user_date(value: str) -> date:
    """
    Accepts:
      DD-MM-YYYY
      DD/MM/YYYY
      YYYY-MM-DD
      DD-MM-YY
    """
    value = clean_text(value)

    formats = [
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
        "%d-%m-%y",
        "%d/%m/%y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    raise ValueError(
        "Date format invalid. Use DD-MM-YYYY, e.g. 16-08-2026."
    )


def extract_message_date(text: str) -> Optional[date]:
    """
    EduSecure card/detail text example:
      Jul 09, 2026
      July 9, 2026
    """
    text = clean_text(text)

    patterns = [
        (r"\b([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})\b", "%b %d %Y"),
        (r"\b([A-Z][a-z]{3,8})\s+(\d{1,2}),\s+(\d{4})\b", "%B %d %Y"),
    ]

    for pattern, fmt in patterns:
        m = re.search(pattern, text)
        if not m:
            continue

        raw = f"{m.group(1)} {m.group(2)} {m.group(3)}"

        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    return None


def choose_cli_mode() -> Dict[str, object]:
    """
    CMD menu:
      1 = date range
      2 = last N days
      3 = first N messages
    """
    print("\n==============================================")
    print("SELECT PDF UPLOAD MODE")
    print("==============================================")
    print("1. Date Range")
    print("   Example: 10-08-2026 to 16-08-2026")
    print("")
    print("2. Last N Days")
    print("   Example: 7 = today + previous 6 days")
    print("")
    print("3. First N Messages")
    print("   Example: 7 = top/latest 7 dashboard messages")
    print("==============================================")

    while True:
        choice = input("\nSelect option (1/2/3): ").strip()

        if choice == "1":
            while True:
                try:
                    start = parse_user_date(
                        input("Starting date (DD-MM-YYYY): ").strip()
                    )
                    end = parse_user_date(
                        input("Latest/End date (DD-MM-YYYY): ").strip()
                    )

                    if start > end:
                        print("Starting date latest/end date se badi nahi ho sakti.")
                        continue

                    print(f"Selected Date Range: {start} -> {end}")

                    return {
                        "mode": "date_range",
                        "start_date": start,
                        "end_date": end,
                    }

                except ValueError as e:
                    print(e)

        if choice == "2":
            while True:
                raw = input("How many days? Example 7: ").strip()

                try:
                    days = int(raw)

                    if days <= 0:
                        raise ValueError

                    today = date.today()
                    start = today - timedelta(days=days - 1)

                    print(
                        f"Selected Last {days} Days: "
                        f"{start} -> {today}"
                    )

                    return {
                        "mode": "last_days",
                        "days": days,
                        "start_date": start,
                        "end_date": today,
                    }

                except ValueError:
                    print("Please enter a positive number, e.g. 7.")

        if choice == "3":
            while True:
                raw = input(
                    "How many top/latest messages? Example 7: "
                ).strip()

                try:
                    count = int(raw)

                    if count <= 0:
                        raise ValueError

                    print(f"Selected First {count} Messages.")

                    return {
                        "mode": "message_count",
                        "message_limit": count,
                    }

                except ValueError:
                    print("Please enter a positive number, e.g. 7.")

        print("Please select 1, 2 or 3.")


def message_matches_filter(
    message_text: str,
    settings: Dict[str, object],
    messages_opened: int,
) -> bool:
    mode = settings.get("mode")

    if mode == "message_count":
        limit = int(settings.get("message_limit", 0))
        return messages_opened < limit

    msg_date = extract_message_date(message_text)

    if msg_date is None:
        # Date modes should not open an undated card.
        return False

    start = settings.get("start_date")
    end = settings.get("end_date")

    if isinstance(start, date) and isinstance(end, date):
        return start <= msg_date <= end

    return False


def date_is_older_than_filter(message_text: str, settings: Dict[str, object]) -> bool:
    """
    Since dashboard is newest -> oldest, once we see a date older than start_date
    in date-based modes, we can eventually stop after confirming no matching messages.
    """
    if settings.get("mode") == "message_count":
        return False

    start = settings.get("start_date")

    if not isinstance(start, date):
        return False

    msg_date = extract_message_date(message_text)

    if msg_date is None:
        return False

    return msg_date < start


def find_visible_dashboard_messages_v29(
    driver: webdriver.Chrome,
    processed_fps: Set[str],
) -> List[Dict[str, str]]:
    """
    Find all visible message cards in top-to-bottom order.
    Uses real EduSecure Announcement.aspx?Id=... links when available.
    """
    processed = list(processed_fps)

    js = r"""
    const processed = new Set(arguments[0] || []);

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();

      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function abs(u) {
      try {
        return new URL(u, location.href).href;
      } catch(e) {
        return null;
      }
    }

    function textOf(el) {
      return (
        el.innerText ||
        el.textContent ||
        el.getAttribute("aria-label") ||
        el.title ||
        ""
      ).replace(/\s+/g, " ").trim();
    }

    function detailUrlFrom(el) {
      if (!el) return null;

      let nodes = [el];

      let p = el.parentElement;
      for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
        nodes.push(p);
      }

      if (el.querySelectorAll) {
        nodes = nodes.concat(
          Array.from(
            el.querySelectorAll(
              'a[href], [onclick]'
            )
          )
        );
      }

      for (const n of nodes) {
        if (!n || !n.getAttribute) continue;

        const href = n.getAttribute("href") || "";

        if (/Announcement\.aspx\?Id=/i.test(href)) {
          return abs(href);
        }

        const onclick = n.getAttribute("onclick") || "";

        const m = onclick.match(
          /Announcement\.aspx\?Id=[^'"<>\s]+/i
        );

        if (m) {
          return abs(m[0]);
        }
      }

      return null;
    }

    function makePath(el) {
      if (!el) return "";

      if (el.id) {
        return "id:" + el.id;
      }

      const parts = [];
      let cur = el;

      while (
        cur &&
        cur.nodeType === Node.ELEMENT_NODE &&
        cur !== document.body
      ) {
        let index = 1;
        let sib = cur.previousElementSibling;

        while (sib) {
          if (sib.tagName === cur.tagName) {
            index++;
          }

          sib = sib.previousElementSibling;
        }

        parts.unshift(
          cur.tagName.toLowerCase() +
          ":nth-of-type(" + index + ")"
        );

        cur = cur.parentElement;
      }

      return "css:body > " + parts.join(" > ");
    }

    function fingerprint(text, detailUrl) {
      if (detailUrl) {
        return "url:" + detailUrl.toLowerCase();
      }

      return (text || "")
        .replace(/\s+/g, " ")
        .replace(
          /\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b|\bpay now\b/ig,
          ""
        )
        .trim()
        .toLowerCase()
        .slice(0, 1200);
    }

    function cardFor(el) {
      let best = el;
      let p = el;

      for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const r = p.getBoundingClientRect();

        const hasDate =
          /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(txt);

        const hasType =
          /School Diary|Circular|Home Work|Class Work|Homework|Announcement|Notice|Message/i.test(txt);

        if (
          txt.length >= 8 &&
          txt.length <= 2500 &&
          r.width >= 170 &&
          r.height >= 30 &&
          r.height <= Math.max(700, window.innerHeight * 0.90) &&
          r.bottom > 0 &&
          r.top < window.innerHeight &&
          (hasDate || hasType || detailUrlFrom(p))
        ) {
          best = p;
        }
      }

      return best;
    }

    const candidates = [];

    // Strong pass: actual detail links.
    const detailNodes = Array.from(
      document.querySelectorAll(
        'a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]'
      )
    );

    for (const node of detailNodes) {
      const url = detailUrlFrom(node);

      if (!url) continue;

      let card = cardFor(node);

      const cardText = textOf(card);
      const fp = fingerprint(cardText, url);

      if (!fp || processed.has(fp)) continue;

      let r = node.getBoundingClientRect();

      if (!visible(node)) {
        r = card.getBoundingClientRect();

        if (
          r.bottom <= 0 ||
          r.top >= window.innerHeight
        ) {
          continue;
        }
      }

      candidates.push({
        text: cardText.slice(0, 2400),
        fp,
        detailUrl: url,
        clickPath: makePath(node),
        cardPath: makePath(card),
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    // Fallback pass: message-looking cards.
    const blocks = Array.from(
      document.querySelectorAll(
        "div, li, tr, section, article, a, button, [onclick]"
      )
    ).filter(visible);

    for (const el of blocks) {
      const txt = textOf(el);

      if (!txt || txt.length < 8 || txt.length > 3000) {
        continue;
      }

      const hasDate =
        /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(txt);

      const hasType =
        /School Diary|Circular|Home Work|Class Work|Homework|Announcement|Notice|Message/i.test(txt);

      if (!hasDate && !hasType) {
        continue;
      }

      const card = cardFor(el);
      const cardText = textOf(card);
      const url = detailUrlFrom(card);

      const fp = fingerprint(cardText, url);

      if (!fp || processed.has(fp)) {
        continue;
      }

      const target =
        (
          card.querySelector &&
          card.querySelector(
            'a[href*="Announcement.aspx?Id="], [onclick*="Announcement.aspx?Id="]'
          )
        ) ||
        card.closest("a, button, [onclick]") ||
        card.querySelector("a, button, [onclick]") ||
        card;

      if (!visible(target)) {
        continue;
      }

      const r = card.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 2400),
        fp,
        detailUrl: url || "",
        clickPath: makePath(target),
        cardPath: makePath(card),
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    const map = new Map();

    for (const c of candidates) {
      const key =
        c.detailUrl
          ? "url:" + c.detailUrl.toLowerCase()
          : c.fp;

      if (!map.has(key)) {
        map.set(key, c);
      }
    }

    return Array.from(map.values())
      .sort(
        (a, b) =>
          a.top - b.top ||
          a.left - b.left
      )
      .slice(0, 80);
    """

    try:
        return driver.execute_script(js, processed) or []
    except Exception:
        return []


def real_click_message_v29(
    driver: webdriver.Chrome,
    message: Dict[str, str],
) -> bool:
    """
    Actually open the message.
    Real click first. Direct URL only fallback.
    """
    before_url = driver.current_url

    click_path_value = message.get("clickPath", "")

    target = get_element_from_path_v25(
        driver,
        click_path_value
    )

    if target:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                target
            )

            time.sleep(0.12)

            ActionChains(driver) \
                .move_to_element(target) \
                .pause(0.12) \
                .click() \
                .perform()

        except Exception:
            try:
                target.click()
            except Exception:
                pass

    end = time.time() + 5

    while time.time() < end:
        try:
            if driver.current_url != before_url:
                wait_ready(driver)
                time.sleep(0.45)
                return True
        except Exception:
            pass

        time.sleep(0.25)

    # Card click fallback.
    card = get_element_from_path_v25(
        driver,
        message.get("cardPath", "")
    )

    if card:
        try:
            ActionChains(driver) \
                .move_to_element(card) \
                .pause(0.12) \
                .click() \
                .perform()
        except Exception:
            pass

    end = time.time() + 4

    while time.time() < end:
        try:
            if driver.current_url != before_url:
                wait_ready(driver)
                time.sleep(0.45)
                return True
        except Exception:
            pass

        time.sleep(0.25)

    # Final fallback: exact detail URL.
    detail_url = message.get("detailUrl", "")

    if detail_url and "announcement.aspx?id=" in detail_url.lower():
        try:
            print("Click fallback -> opening exact message URL.")
            driver.get(detail_url)
            wait_ready(driver)
            time.sleep(0.55)

            return "announcement.aspx?id=" in driver.current_url.lower()

        except Exception:
            pass

    return False


def load_uploaded_pdf_memory_v29() -> Set[str]:
    """
    Use old state/output JSON files as duplicate memory.
    """
    state = load_previous_state()

    urls = set()

    for u in state.get("uploaded_urls", set()):
        if is_pdf_url(u):
            urls.add(normalize_pdf_url(u))

    for item in state.get("uploaded_items", []):
        if isinstance(item, dict):
            u = item.get("url", "")

            if is_pdf_url(u):
                urls.add(normalize_pdf_url(u))

    print(
        f"Loaded duplicate memory: "
        f"{len(urls)} PDF URLs."
    )

    return urls



# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# STRICT CIRCULAR FLOW HELPERS
# -----------------------------

def is_circular_detail_page(driver: webdriver.Chrome) -> bool:
    """
    True only when current page is a Circular detail page.
    Attachments on normal/home/list page are ignored.
    """
    try:
        url = driver.current_url.lower()
        text = app_current_text(driver)
        return ("announcement.aspx?id=" in url and "Circular" in text and "Attachment" in text)
    except Exception:
        return False


def find_next_circular_card_only(driver: webdriver.Chrome, processed_cards: Set[str]) -> Optional[Dict[str, str]]:
    """
    Current EduSecure list/dashboard/feed par sirf next visible Circular card dhoondhta hai.
    School Diary / Learning Planner attachments ignore.
    """
    processed = list(processed_cards)

    js = r"""
    const processed = arguments[0] || [];

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function fp(text) {
      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b/ig, "")
        .replace(/https?:\/\/\S+/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 520);
    }

    function makePath(el) {
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function cardFor(el) {
      let best = el;
      let p = el;
      for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const r = p.getBoundingClientRect();
        const hasCircular = /\bCircular\b/i.test(txt);
        const hasDate = /\b[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\b/.test(txt);

        if (
          hasCircular &&
          hasDate &&
          txt.length >= 25 &&
          txt.length <= 1700 &&
          r.width >= 180 &&
          r.height >= 45 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }
      return best;
    }

    const possible = Array.from(document.querySelectorAll("a, button, [onclick], div, li, tr, section, article"))
      .filter(visible);

    const candidates = [];

    for (const el of possible) {
      const txt = textOf(el);
      if (!txt) continue;

      const hasCircular = /\bCircular\b/i.test(txt);
      const hasDate = /\b[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}\b/.test(txt);

      if (!hasCircular || !hasDate) continue;
      if (/\bSchool\s*Diary\b/i.test(txt)) continue;
      if (/\bLearning\s*Planner\b/i.test(txt)) continue;
      if (txt.length > 1800) continue;

      const card = cardFor(el);
      const cardText = textOf(card);
      const f = fp(cardText);
      if (!f || processed.includes(f)) continue;

      const target = card.closest("a, button, [onclick]") || card.querySelector("a, button, [onclick]") || card;
      if (!visible(target)) continue;

      const r = card.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 1500),
        fp: f,
        path: makePath(target),
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    const map = new Map();
    for (const c of candidates) {
      if (!map.has(c.fp)) map.set(c.fp, c);
    }

    return Array.from(map.values()).sort((a, b) => a.top - b.top || a.left - b.left)[0] || null;
    """

    try:
        return driver.execute_script(js, processed)
    except JavascriptException:
        return None



def find_next_today_circular_card_only(driver: webdriver.Chrome, processed_cards: Set[str], today_labels: List[str]) -> Optional[Dict[str, str]]:
    """
    Sirf aaj ki date wale Circular/Dashboard card return karega.
    Older dates visible honge to bhi click/upload nahi karega.
    """
    processed = list(processed_cards)

    js = r"""
    const processed = arguments[0] || [];
    const todayLabels = (arguments[1] || []).map(x => String(x).toLowerCase());

    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" &&
             st.visibility !== "hidden" &&
             r.width > 0 &&
             r.height > 0 &&
             r.bottom > 0 &&
             r.top < window.innerHeight;
    }

    function textOf(el) {
      return (el.innerText || el.textContent || el.getAttribute("aria-label") || el.title || "")
        .replace(/\s+/g, " ")
        .trim();
    }

    function fp(text) {
      return (text || "")
        .replace(/\s+/g, " ")
        .replace(/\battachment\b|\battach\b|\bview\b|\bdownload\b|\bopen\b|\bmore\b/ig, "")
        .replace(/https?:\/\/\S+/ig, "")
        .trim()
        .toLowerCase()
        .slice(0, 520);
    }

    function makePath(el) {
      if (el.id) return "id:" + el.id;

      const parts = [];
      let cur = el;
      while (cur && cur.nodeType === Node.ELEMENT_NODE && cur !== document.body) {
        let index = 1;
        let sib = cur.previousElementSibling;
        while (sib) {
          if (sib.tagName === cur.tagName) index++;
          sib = sib.previousElementSibling;
        }
        parts.unshift(cur.tagName.toLowerCase() + ":nth-of-type(" + index + ")");
        cur = cur.parentElement;
      }
      return "css:body > " + parts.join(" > ");
    }

    function cardFor(el) {
      let best = el;
      let p = el;
      for (let i = 0; i < 10 && p; i++, p = p.parentElement) {
        const txt = textOf(p);
        const r = p.getBoundingClientRect();
        const hasDate = /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(txt);

        if (
          hasDate &&
          txt.length >= 20 &&
          txt.length <= 1700 &&
          r.width >= 180 &&
          r.height >= 45 &&
          r.bottom > 0 &&
          r.top < window.innerHeight
        ) {
          best = p;
        }
      }
      return best;
    }

    const possible = Array.from(document.querySelectorAll("a, button, [onclick], div, li, tr, section, article"))
      .filter(visible);

    const candidates = [];
    const visibleDates = [];

    for (const el of possible) {
      const rawTxt = textOf(el);
      if (!rawTxt) continue;

      const txtLower = rawTxt.toLowerCase();
      const hasDate = /\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/.test(rawTxt);
      if (!hasDate) continue;

      const isToday = todayLabels.some(label => txtLower.includes(label));
      if (!isToday) {
        const m = rawTxt.match(/\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b/);
        if (m) visibleDates.push(m[0]);
        continue;
      }

      if (/\bSchool\s*Diary\b/i.test(rawTxt)) continue;
      if (/\bLearning\s*Planner\b/i.test(rawTxt)) continue;
      if (rawTxt.length > 1800) continue;

      const card = cardFor(el);
      const cardText = textOf(card);
      const cardTextLower = cardText.toLowerCase();

      const cardIsToday = todayLabels.some(label => cardTextLower.includes(label));
      if (!cardIsToday) continue;

      const f = fp(cardText);
      if (!f || processed.includes(f)) continue;

      const target = card.closest("a, button, [onclick]") || card.querySelector("a, button, [onclick]") || card;
      if (!visible(target)) continue;

      const r = card.getBoundingClientRect();

      candidates.push({
        text: cardText.slice(0, 1500),
        fp: f,
        path: makePath(target),
        top: Math.round(r.top),
        left: Math.round(r.left),
        todayLabels: todayLabels,
        visibleOlderDates: visibleDates.slice(0, 8)
      });
    }

    const map = new Map();
    for (const c of candidates) {
      if (!map.has(c.fp)) map.set(c.fp, c);
    }

    const chosen = Array.from(map.values()).sort((a, b) => a.top - b.top || a.left - b.left)[0] || null;
    if (chosen) return chosen;

    return {
      noTodayCard: true,
      text: "",
      fp: "",
      path: "",
      top: 0,
      left: 0,
      todayLabels: todayLabels,
      visibleOlderDates: visibleDates.slice(0, 8)
    };
    """

    try:
        result = driver.execute_script(js, processed, today_labels)
        if result and result.get("noTodayCard"):
            return None
        return result
    except JavascriptException:
        return None


def detail_text_is_today(detail_text: str) -> bool:
    """
    Safety check: detail page bhi aaj ki date ka hona chahiye.
    """
    t = clean_text(detail_text).lower()
    return any(label.lower() in t for label in TODAY_DATE_LABELS)


def open_circular_detail_from_card(driver: webdriver.Chrome, card: Dict[str, str]) -> bool:
    before_url = driver.current_url

    if not click_path(driver, card.get("path", "")):
        return False

    end = time.time() + 10
    while time.time() < end:
        try:
            if is_circular_detail_page(driver):
                return True
            if driver.current_url != before_url and "announcement.aspx?id=" in driver.current_url.lower():
                time.sleep(0.7)
                return True
        except Exception:
            pass
        time.sleep(0.35)

    return is_circular_detail_page(driver)


def extract_pdf_from_current_circular_detail_only(driver: webdriver.Chrome, app_handle: str) -> Optional[str]:
    """
    Normal/list page ke attachments ignore. Sirf Circular detail page par PDF link extract.
    """
    if not is_circular_detail_page(driver):
        return None

    target = visible_attachment_target(driver, set())
    if not target:
        return extract_pdf_from_current_page(driver)

    return get_pdf_url_from_attachment(driver, target, app_handle)


# -----------------------------
# MAIN
# -----------------------------

def is_announcement_list_page(driver: webdriver.Chrome) -> bool:
    """
    True only for dashboard/home page:
    https://edusecure.org/ManavMangal88/ParentApp/Dashboard.aspx?Type=Dashboard
    """
    try:
        url = driver.current_url.lower()
        return "announcement.aspx" in url and "type=announcement" in url and "id=" not in url
    except Exception:
        return False


def force_open_announcement_list(driver: webdriver.Chrome) -> None:
    """
    Agar app back ke baad dashboard/normal page par chala jaye,
    forcefully dashboard/home URL open karo.
    """
    try:
        if not is_announcement_list_page(driver) and not is_circular_detail_page(driver):
            print("Not on dashboard/home. Opening dashboard/home URL again...")
            driver.get(START_URL)
            wait_ready(driver)
            time.sleep(1.0)
    except Exception:
        pass


def get_state_file_paths() -> List[Path]:
    """
    Current working directory + script folder dono jagah state/output files check karega.
    """
    paths: List[Path] = []

    try:
        paths.append(Path.cwd() / STATE_FILE_NAME)
    except Exception:
        pass

    try:
        paths.append(Path(__file__).resolve().parent / STATE_FILE_NAME)
    except Exception:
        pass

    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for p in paths:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def load_json_file(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def load_previous_state() -> Dict[str, object]:
    """
    Previously uploaded PDFs load karega:
    - v16 state file
    - old output JSON files from v15/v13/v12 etc.
    """
    uploaded_urls: Set[str] = set()
    processed_cards: Set[str] = set()
    uploaded_items: List[Dict[str, str]] = []

    search_dirs = []
    try:
        search_dirs.append(Path.cwd())
    except Exception:
        pass
    try:
        search_dirs.append(Path(__file__).resolve().parent)
    except Exception:
        pass

    # Load v16 state
    for state_path in get_state_file_paths():
        data = load_json_file(state_path)
        if isinstance(data, dict):
            for u in data.get("uploaded_urls", []):
                if is_pdf_url(u):
                    uploaded_urls.add(u)
            for f in data.get("processed_card_fingerprints", []):
                if f:
                    processed_cards.add(str(f))
            for item in data.get("uploaded_items", []):
                if isinstance(item, dict):
                    url = item.get("url", "")
                    if is_pdf_url(url):
                        uploaded_urls.add(url)
                    uploaded_items.append(item)

    # Load old output files
    checked_paths = set()
    for d in search_dirs:
        for name in OLD_OUTPUT_FILES:
            p = d / name
            key = str(p).lower()
            if key in checked_paths:
                continue
            checked_paths.add(key)

            data = load_json_file(p)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        url = item.get("url", "")
                        if is_pdf_url(url):
                            uploaded_urls.add(url)
                            uploaded_items.append(item)

    # Deduplicate items by URL
    dedup_items = []
    seen_urls = set()
    for item in uploaded_items:
        url = item.get("url", "")
        if is_pdf_url(url) and url not in seen_urls:
            seen_urls.add(url)
            dedup_items.append(item)

    print(f"Loaded memory: {len(uploaded_urls)} already uploaded PDF URLs.")
    return {
        "uploaded_urls": uploaded_urls,
        "processed_card_fingerprints": processed_cards,
        "uploaded_items": dedup_items,
    }


def save_state(uploaded_items: List[Dict[str, str]], processed_urls: Set[str], processed_cards: Set[str]) -> None:
    """
    Har successful upload / duplicate detection ke baad state save karega.
    """
    data = {
        "uploaded_urls": sorted(processed_urls),
        "processed_card_fingerprints": sorted(processed_cards),
        "uploaded_items": uploaded_items,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    for p in get_state_file_paths():
        try:
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def get_announcement_scroll_state(driver: webdriver.Chrome) -> Dict[str, object]:
    """
    dashboard/home ke actual scroll container ka state return karta hai.
    Stop sirf tab hoga jab:
    - koi new card nahi mil raha
    - scroll bottom par hai
    - multiple bottom checks fail ho chuke
    """
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    const containers = Array.from(document.querySelectorAll("*"))
      .filter(visible)
      .filter(el => (el.scrollHeight - el.clientHeight) > 30)
      .map(el => {
        const txt = (el.innerText || el.textContent || "").slice(0, 5000);
        const hasDashboards = /Circular|Dashboard|Attachment/i.test(txt);
        const maxScroll = Math.max(0, el.scrollHeight - el.clientHeight);
        return {
          el,
          hasDashboards,
          scrollTop: el.scrollTop || 0,
          scrollHeight: el.scrollHeight || 0,
          clientHeight: el.clientHeight || 0,
          maxScroll,
          tag: el.tagName
        };
      })
      .sort((a,b) => {
        if (a.hasDashboards !== b.hasDashboards) return a.hasDashboards ? -1 : 1;
        return b.maxScroll - a.maxScroll;
      });

    let c = containers[0] || null;

    if (!c) {
      const scrollTop = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
      const scrollHeight = Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0);
      const clientHeight = window.innerHeight || document.documentElement.clientHeight || 0;
      const maxScroll = Math.max(0, scrollHeight - clientHeight);
      return {
        source: "window",
        scrollTop,
        scrollHeight,
        clientHeight,
        maxScroll,
        atBottom: scrollTop >= maxScroll - 8
      };
    }

    return {
      source: c.tag,
      scrollTop: c.scrollTop,
      scrollHeight: c.scrollHeight,
      clientHeight: c.clientHeight,
      maxScroll: c.maxScroll,
      atBottom: c.scrollTop >= c.maxScroll - 8
    };
    """
    try:
        return driver.execute_script(js) or {}
    except Exception:
        return {}


def scroll_announcement_list_until_move(driver: webdriver.Chrome) -> Dict[str, object]:
    """
    Strong scroll, and returns before/after state for real bottom checking.
    """
    before = get_announcement_scroll_state(driver)
    scroll_result = scroll_down_app(driver)
    time.sleep(0.6)
    after = get_announcement_scroll_state(driver)

    moved = False
    try:
        moved = abs(float(after.get("scrollTop", 0)) - float(before.get("scrollTop", 0))) > 4
    except Exception:
        moved = bool(scroll_result.get("moved"))

    return {
        "moved": moved,
        "before": before,
        "after": after,
        "raw": scroll_result,
        "atBottom": bool(after.get("atBottom")),
    }


# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

# -----------------------------
# MAIN
# -----------------------------

def upload_to_endpoint(item: Dict[str, str]) -> bool:
    if not AUTOMATION_TOKEN:
        raise RuntimeError("AUTOMATION_TOKEN is missing")
    response = requests.post(
        INGEST_URL,
        headers={"Authorization": f"Bearer {AUTOMATION_TOKEN}", "Content-Type": "application/json"},
        json=item,
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return True


def main() -> None:
    if not EDUSECURE_USERNAME or not EDUSECURE_PASSWORD:
        raise RuntimeError("EDUSECURE_USERNAME and EDUSECURE_PASSWORD are required")

    # Incremental safety boundary: only messages newer than the existing library's
    # latest date are eligible. Override SYNC_AFTER deliberately when the library advances.
    after_raw = os.environ.get("SYNC_AFTER", "30-08-2026")
    before_raw = os.environ.get("SYNC_BEFORE", "")
    after_date = parse_user_date(after_raw)
    before_date = parse_user_date(before_raw) if before_raw else date.today()
    if before_date < after_date:
        raise RuntimeError("SYNC_BEFORE cannot be earlier than SYNC_AFTER")

    driver = make_driver()
    processed_message_fps: Set[str] = set()
    uploaded_pdf_memory: Set[str] = set()
    messages_opened = 0
    uploaded_items: List[Dict[str, str]] = []
    bottom_confirmations = 0
    older_date_confirmations = 0

    try:
        print("Opening EduSecure Dashboard...")
        driver.get(START_URL)
        if not auto_login_edusecure(driver):
            raise RuntimeError("EduSecure auto-login failed")
        driver.get(START_URL)
        wait_ready(driver)
        app_handle = driver.current_window_handle
        restore_dashboard_scroll_position(driver, 0)

        while messages_opened < MAX_UPLOADS:
            driver.switch_to.window(app_handle)
            restore_app_after_pdf(driver, app_handle)
            if "dashboard.aspx" not in driver.current_url.lower():
                driver.get(START_URL)
                wait_ready(driver)

            visible_messages = find_visible_dashboard_messages_v29(driver, processed_message_fps)
            if not visible_messages:
                scroll_result = dashboard_scroll_v24(driver)
                print(f"Dashboard scroll: {scroll_result}")
                if scroll_result.get("atBottom") and not scroll_result.get("moved"):
                    bottom_confirmations += 1
                else:
                    bottom_confirmations = 0
                if bottom_confirmations >= 6:
                    break
                continue

            message = visible_messages[0]
            message_text = clean_text(message.get("text", ""))
            fp = message.get("fp") or fingerprint(message_text)
            if fp:
                processed_message_fps.add(fp)

            msg_date = extract_message_date(message_text)
            if msg_date is None:
                print("Undated message -> skip")
                continue
            if msg_date > before_date:
                continue
            if msg_date <= after_date:
                older_date_confirmations += 1
                print(f"Reached existing cutoff with {msg_date}; skipping")
                if older_date_confirmations >= 8:
                    break
                continue
            older_date_confirmations = 0

            saved_position = get_dashboard_scroll_position(driver)
            print(f"Opening new message dated {msg_date}: {message_text[:180]}")
            if not real_click_message_v29(driver, message):
                print("Message could not be opened")
                restore_dashboard_scroll_position(driver, saved_position)
                continue
            messages_opened += 1
            detail_text = app_current_text(driver)
            pdf_url = extract_pdf_from_current_message_detail(driver, app_handle)
            driver.switch_to.window(app_handle)
            restore_app_after_pdf(driver, app_handle)
            if not is_pdf_url(pdf_url):
                print("No PDF attachment in this message")
                return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            normalized_pdf = normalize_pdf_url(pdf_url)
            if normalized_pdf in uploaded_pdf_memory:
                return_dashboard_and_restore_v25(driver, app_handle, saved_position)
                continue

            title = make_title(detail_text or message_text, pdf_url, len(uploaded_items) + 1)
            item = {
                "title": title,
                "subject": detect_subject(detail_text or message_text),
                "description": make_description(detail_text or message_text),
                "pdf_url": pdf_url,
            }
            print(f"Uploading: {title}")
            try:
                upload_to_endpoint(item)
                uploaded_items.append(item)
                uploaded_pdf_memory.add(normalized_pdf)
                print("Upload successful")
            except Exception as exc:
                print(f"Upload failed: {exc}")
            return_dashboard_and_restore_v25(driver, app_handle, saved_position)

        print(f"Completed. New PDFs uploaded: {len(uploaded_items)}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
