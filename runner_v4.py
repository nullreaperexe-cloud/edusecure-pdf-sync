from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

import requests
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

import runner
import runner_v2
import sync_repair as network


BAD_URL_RE = re.compile(
    r"(?:/ParentApp/morelinks\.aspx(?:\?|$)|/images/loader\.gif(?:\?|$)|dashboard\.aspx(?:\?|$)|/login(?:\?|/|$))",
    re.I,
)
ORIGINAL_LIST = runner.list_firestore_materials
ORIGINAL_EXISTING_STATE = runner.existing_state


def clean(value: Any) -> str:
    return runner.clean(value)


def valid_real_attachment_url(url: Optional[str]) -> bool:
    """Accept any real HTTP(S) attachment URL regardless of file extension."""
    value = clean(url)
    if not re.match(r"^https?://", value, re.I):
        return False
    if BAD_URL_RE.search(value):
        return False
    low = value.lower()
    if low.startswith(("javascript:", "data:", "blob:")):
        return False
    return True


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


def list_and_cleanup_materials(id_token: str) -> List[Dict[str, Any]]:
    """Remove only known-bad intermediate URLs created by this automation."""
    materials = ORIGINAL_LIST(id_token)
    kept: List[Dict[str, Any]] = []
    removed = 0

    for item in materials:
        url = clean(item.get("pdf_url"))
        source = clean(item.get("source")).lower()
        bad_automation_record = (
            "edusecure" in source
            and bool(url)
            and bool(BAD_URL_RE.search(url))
        )

        if bad_automation_record:
            if firestore_delete_document(clean(item.get("_name")), id_token):
                removed += 1
                print(f"Removed invalid previous EduSecure automation URL: {url}")
                continue
        kept.append(item)

    if removed:
        print(f"Invalid previous EduSecure automation records cleaned: {removed}")
    return kept


class _StrictBoundaryDate(date):
    """Runner compares dates against internal date but logs website latest date."""

    def __new__(cls, internal_date: date, displayed_date: date):
        obj = date.__new__(cls, internal_date.year, internal_date.month, internal_date.day)
        obj._displayed_date = displayed_date
        return obj

    def isoformat(self) -> str:
        return self._displayed_date.isoformat()


def strict_existing_state(materials: List[Dict[str, Any]]):
    """Process only EduSecure messages strictly AFTER website latest material date."""
    _, actual_latest, next_order = ORIGINAL_EXISTING_STATE(materials)
    urls, _, _ = ORIGINAL_EXISTING_STATE(materials)

    if actual_latest:
        print(f"Website latest material date: {actual_latest.isoformat()}")
        print(
            "STRICT DATE MODE: only messages AFTER this date are eligible; "
            "same-date and older messages are skipped."
        )
        # runner.py skips msg_date < cutoff. latest+1 therefore skips latest date too.
        effective_cutoff = _StrictBoundaryDate(
            actual_latest + timedelta(days=1), actual_latest
        )
        return urls, effective_cutoff, next_order

    return urls, None, next_order


def exact_attachment_anchors(driver) -> List[Dict[str, str]]:
    """Return ONLY the real visible <a> link for the message's Attachment button."""
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
      if (/morelinks\.aspx|loader\.gif|dashboard\.aspx/i.test(href)) continue;
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
            el = driver.execute_script(
                "return document.getElementById(arguments[0]);", anchor_id
            )
            if el is not None:
                return el
        except Exception:
            pass

    if href:
        try:
            el = driver.execute_script(
                """
                const wanted = arguments[0];
                return Array.from(document.querySelectorAll('a[href]')).find(a => {
                  try { return new URL(a.href, location.href).href === wanted; }
                  catch(e) { return false; }
                }) || null;
                """,
                href,
            )
            if el is not None:
                return el
        except Exception:
            pass
    return None


def final_attachment_from_dom(driver) -> Optional[str]:
    """Find a real file/document URL on the page, regardless of extension."""
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
    """Right-click exact Attachment anchor, open that exact link in a new tab, copy final URL."""
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
        try:
            network.drain_performance_log(driver)
        except Exception:
            pass
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

                # Give redirects a short moment to settle. Then copy exactly
                # what is visible in the opened attachment tab.
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
            captured = network.attachment_url_from_performance(driver)
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

    # If Chrome downloads immediately and leaves no readable destination URL,
    # use the exact Attachment href that was explicitly opened in the new tab.
    if valid_real_attachment_url(href):
        print(f"Using exact opened Attachment link: {href}")
        close_extra_tabs(driver, app_handle)
        return href

    print("Attachment opened, but no valid real URL was captured; skipping.")
    for value in sorted(observed):
        print(f"  observed intermediate: {value}")
    close_extra_tabs(driver, app_handle)
    return None


def extract_attachment_url_v4(driver, app_handle: str) -> Optional[str]:
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


runner.list_firestore_materials = list_and_cleanup_materials
runner.existing_state = strict_existing_state
runner.extract_attachment_url = extract_attachment_url_v4
runner.legacy.make_title = runner_v2.make_message_title
runner.legacy.make_driver = network.make_driver_with_network_logs
runner.legacy.is_pdf_url = valid_real_attachment_url


if __name__ == "__main__":
    raise SystemExit(runner.main())
