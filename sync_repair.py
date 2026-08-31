"""Robust runner for EduSecure PDF sync.

This wrapper keeps the existing sync.py flow but fixes attachment detection for
EduSecure messages whose PDFs are opened/downloaded through ASP.NET handlers,
postbacks, same-tab navigation, or download URLs that do not end in .pdf.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException

import sync as base


DOWNLOAD_DIR = "/tmp/edusecure-downloads"


def looks_like_attachment_url(url: str | None) -> bool:
    if not url:
        return False
    value = str(url).strip()
    low = value.lower()
    if not low.startswith(("http://", "https://")):
        return False

    # Never mistake EduSecure message/list navigation for a PDF.
    if "dashboard.aspx" in low:
        return False
    if "announcement.aspx" in low and "download" not in low and "attachment" not in low and ".pdf" not in low:
        return False

    if ".pdf" in low:
        return True

    parsed = urlparse(low)
    haystack = f"{parsed.path}?{parsed.query}"
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
    return any(marker in haystack for marker in strong_markers)


def repaired_is_pdf_url(url: str | None) -> bool:
    return looks_like_attachment_url(url)


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

    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Page.enable", {})
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


def drain_performance_log(driver: webdriver.Chrome) -> None:
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


def attachment_url_from_performance(driver: webdriver.Chrome) -> Optional[str]:
    """Return the strongest attachment URL observed in Chrome network/page logs."""
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
            url = str(params.get("url") or "")
            filename = str(params.get("suggestedFilename") or "")
            if url.startswith(("http://", "https://")):
                score = 100
                if filename.lower().endswith(".pdf"):
                    score += 40
                best.append((score, url, f"download event filename={filename!r}"))
            continue

        if method == "Network.responseReceived":
            response = params.get("response", {}) or {}
            url = str(response.get("url") or "")
            mime = str(response.get("mimeType") or "").lower()
            headers = response.get("headers", {}) or {}
            content_type = _header_value(headers, "content-type").lower()
            disposition = _header_value(headers, "content-disposition").lower()
            if not url.startswith(("http://", "https://")):
                continue

            score = 0
            if "application/pdf" in mime or "application/pdf" in content_type:
                score += 100
            if "filename=" in disposition and ".pdf" in disposition:
                score += 90
            elif "attachment" in disposition:
                score += 60
            if ".pdf" in url.lower():
                score += 70
            if looks_like_attachment_url(url):
                score += 45
            if score:
                best.append((score, url, f"response mime={mime!r} disposition={disposition[:120]!r}"))
            continue

        if method == "Network.requestWillBeSent":
            request = params.get("request", {}) or {}
            url = str(request.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            score = 0
            if ".pdf" in url.lower():
                score += 70
            if looks_like_attachment_url(url):
                score += 45
            if score:
                best.append((score, url, "request URL"))

    if not best:
        return None

    best.sort(key=lambda item: item[0], reverse=True)
    score, url, reason = best[0]
    print(f"Captured attachment URL from browser network ({reason}, score={score}): {url}")
    return url


def find_attachment_targets(driver: webdriver.Chrome) -> List[Dict[str, str]]:
    """Find visible attachment controls, including icon/image/input based buttons."""
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    function textOf(el) {
      return (
        el.innerText || el.textContent || el.getAttribute("aria-label") ||
        el.getAttribute("title") || el.getAttribute("alt") || el.value || ""
      ).replace(/\s+/g, " ").trim();
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

    function attrsOf(el) {
      const out = {};
      if (!el || !el.attributes) return out;
      for (const a of Array.from(el.attributes)) out[a.name] = a.value || "";
      return out;
    }

    function rawUrl(el) {
      if (!el || !el.getAttribute) return "";
      const names = ["href", "src", "data", "data-url", "data-href", "data-src", "formaction"];
      for (const name of names) {
        const value = el.getAttribute(name) || "";
        if (/^https?:/i.test(value) || /^\//.test(value) || /\.pdf|download|attachment|getfile|filehandler/i.test(value)) {
          try { return new URL(value, location.href).href; } catch(e) {}
        }
      }
      return "";
    }

    const nodes = Array.from(document.querySelectorAll(
      "a, button, input, img, [onclick], [href], [src], [data-url], [data-href], [role='button'], label, svg"
    ));

    const candidates = [];
    for (const node of nodes) {
      if (!visible(node)) continue;
      const clickable = node.closest("a, button, [onclick], [role='button']") || node;
      if (!visible(clickable)) continue;

      const text = (textOf(node) + " " + textOf(clickable)).toLowerCase();
      const attrs = JSON.stringify(attrsOf(node)).toLowerCase() + " " + JSON.stringify(attrsOf(clickable)).toLowerCase();
      const hay = text + " " + attrs;

      let score = 0;
      if (/attachment|attached/.test(hay)) score += 80;
      if (/download/.test(hay)) score += 70;
      if (/\.pdf/.test(hay)) score += 100;
      if (/view\s*(file|document)|open\s*(file|document)/.test(hay)) score += 55;
      if (/paperclip|fa-paperclip|attach_file|file_download/.test(hay)) score += 50;
      if (/getfile|downloadfile|filehandler|documenthandler/.test(hay)) score += 75;
      if (!score) continue;

      const r = clickable.getBoundingClientRect();
      candidates.push({
        path: makePath(clickable),
        text: textOf(clickable).slice(0, 250),
        directUrl: rawUrl(clickable) || rawUrl(node),
        score,
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    const seen = new Set();
    return candidates
      .sort((a,b) => b.score - a.score || a.top - b.top || a.left - b.left)
      .filter(c => {
        if (!c.path || seen.has(c.path)) return false;
        seen.add(c.path);
        return true;
      })
      .slice(0, 10);
    """
    try:
        return driver.execute_script(js) or []
    except Exception:
        return []


def _close_extra_tabs_and_restore(driver: webdriver.Chrome, app_handle: str) -> None:
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


def repaired_get_pdf_url_from_attachment(
    driver: webdriver.Chrome,
    target: Dict[str, str],
    app_handle: str,
) -> Optional[str]:
    direct = target.get("directUrl") or target.get("directPdf") or ""
    if looks_like_attachment_url(direct):
        absolute = urljoin(driver.current_url, direct)
        print(f"Direct attachment URL found in element: {absolute}")
        return absolute

    before_url = driver.current_url
    before_tabs = set(driver.window_handles)
    drain_performance_log(driver)

    path = target.get("path", "")
    clicked = base.click_path_human(driver, path)
    if not clicked:
        # Original helper has a JS click fallback and useful logging.
        clicked = base.click_path(driver, path)
    if not clicked:
        print("Attachment control found but click failed.")
        return None

    deadline = time.time() + 8.0
    observed_url: Optional[str] = None

    while time.time() < deadline:
        # Network/download events are the most reliable source for ASP.NET downloads.
        captured = attachment_url_from_performance(driver)
        if captured:
            observed_url = captured
            break

        try:
            now_tabs = set(driver.window_handles)
            new_tabs = list(now_tabs - before_tabs)
            if new_tabs:
                driver.switch_to.window(new_tabs[-1])
                time.sleep(0.35)
                candidate = driver.current_url
                if looks_like_attachment_url(candidate):
                    observed_url = candidate
                    break
                page_pdf = base.extract_pdf_from_current_page(driver)
                if page_pdf:
                    observed_url = page_pdf
                    break
                driver.switch_to.window(app_handle)

            driver.switch_to.window(app_handle)
            current = driver.current_url
            if current != before_url and looks_like_attachment_url(current):
                observed_url = current
                break

            page_pdf = base.extract_pdf_from_current_page(driver)
            if page_pdf:
                observed_url = page_pdf
                break
        except WebDriverException:
            pass

        time.sleep(0.25)

    # One last log drain catches a download event arriving near the timeout.
    if not observed_url:
        observed_url = attachment_url_from_performance(driver)

    _close_extra_tabs_and_restore(driver, app_handle)

    if observed_url:
        print(f"Real PDF/download URL extracted: {observed_url}")
        return observed_url

    print("Attachment was clicked but no downloadable PDF URL was captured.")
    return None


def repaired_extract_pdf_from_current_message_detail(
    driver: webdriver.Chrome,
    app_handle: str,
) -> Optional[str]:
    # First preserve the old direct .pdf detection.
    direct = base.extract_pdf_from_current_page(driver)
    if direct:
        return direct

    targets = find_attachment_targets(driver)
    if not targets:
        # Keep the old detector as a last fallback.
        old_target = base.visible_attachment_target(driver, set())
        if old_target:
            targets = [old_target]

    if not targets:
        print("No attachment control detected on message detail page.")
        return None

    detail_url = driver.current_url
    print(f"Attachment controls detected: {len(targets)}")

    for index, target in enumerate(targets, start=1):
        print(
            f"Trying attachment control {index}/{len(targets)}: "
            f"{target.get('text', '')[:120]!r}"
        )
        url = repaired_get_pdf_url_from_attachment(driver, target, app_handle)
        if url:
            return url

        # If a failed click navigated away, reopen the message detail before the next target.
        try:
            driver.switch_to.window(app_handle)
            if driver.current_url != detail_url:
                driver.get(detail_url)
                base.wait_ready(driver)
                time.sleep(0.7)
        except Exception:
            pass

    return None


# Patch the existing module at runtime. All global lookups inside sync.py then use
# these repaired implementations, including main()'s final PDF validation.
base.make_driver = make_driver_with_network_logs
base.is_pdf_url = repaired_is_pdf_url
base.get_pdf_url_from_attachment = repaired_get_pdf_url_from_attachment
base.extract_pdf_from_current_message_detail = repaired_extract_pdf_from_current_message_detail


if __name__ == "__main__":
    base.main()
