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
    r"(?:/ParentApp/morelinks\.aspx(?:\?|$)|/images/loader\.gif(?:\?|$)|dashboard\.aspx(?:\?|$))",
    re.I,
)
ORIGINAL_LIST = runner.list_firestore_materials
ORIGINAL_EXISTING_STATE = runner.existing_state


def clean(value: Any) -> str:
    return runner.clean(value)


def strict_pdf_url(url: Optional[str]) -> bool:
    """Only a normal HTTP(S) URL whose path literally ends in .pdf is accepted."""
    value = clean(url)
    if not re.match(r"^https?://", value, re.I):
        return False
    if BAD_URL_RE.search(value):
        return False
    return value.lower().split("#", 1)[0].split("?", 1)[0].endswith(".pdf")


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
    """Remove only invalid records previously created by this EduSecure automation."""
    materials = ORIGINAL_LIST(id_token)
    kept: List[Dict[str, Any]] = []
    removed = 0

    for item in materials:
        url = clean(item.get("pdf_url"))
        source = clean(item.get("source")).lower()
        bad_automation_record = (
            "edusecure" in source
            and bool(url)
            and (bool(BAD_URL_RE.search(url)) or not strict_pdf_url(url))
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
    """Runner compares dates against this internal date but logs the website date."""

    def __new__(cls, internal_date: date, displayed_date: date):
        obj = date.__new__(cls, internal_date.year, internal_date.month, internal_date.day)
        obj._displayed_date = displayed_date
        return obj

    def isoformat(self) -> str:
        return self._displayed_date.isoformat()


def strict_existing_state(materials: List[Dict[str, Any]]):
    """Only process EduSecure messages strictly AFTER the website's latest real PDF date."""
    _, _, next_order = ORIGINAL_EXISTING_STATE(materials)
    real_pdf_materials = [
        item for item in materials if strict_pdf_url(clean(item.get("pdf_url")))
    ]
    urls, actual_latest, _ = ORIGINAL_EXISTING_STATE(real_pdf_materials)

    if actual_latest:
        print(f"Website latest real PDF date: {actual_latest.isoformat()}")
        print(
            "STRICT DATE MODE: only messages AFTER this date are eligible; "
            "same-date and older messages are skipped."
        )
        # runner.py skips msg_date < cutoff.  Giving it latest+1 therefore also
        # skips the website's latest date itself, exactly as requested.
        effective_cutoff = _StrictBoundaryDate(
            actual_latest + timedelta(days=1), actual_latest
        )
        return urls, effective_cutoff, next_order

    return urls, None, next_order


def exact_attachment_anchors(driver) -> List[Dict[str, str]]:
    """Return ONLY the real visible <a> link for the message's Attachment button.

    EduSecure's real control observed in logs is like:
      ..._HyperLink1  text='Attachment'
    Parent DIVs, wrappers, loaders and generic panels are deliberately ignored.
    """
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


def strict_pdf_from_dom(driver) -> Optional[str]:
    js = r"""
    function abs(u){try{return new URL(u,location.href).href}catch(e){return ''}}
    for(const el of document.querySelectorAll('a[href],iframe[src],frame[src],embed[src],object[data]')){
      for(const attr of ['href','src','data']){
        const raw=el.getAttribute&&el.getAttribute(attr); if(!raw) continue;
        const u=abs(raw);
        const clean=u.split('#')[0].split('?')[0].toLowerCase();
        if(/^https?:/i.test(u) && clean.endsWith('.pdf')) return u;
      }
    }
    return '';
    """
    try:
        value = clean(driver.execute_script(js))
        return value if strict_pdf_url(value) else None
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
    """Right-click the exact Attachment anchor, then open THAT link in a new tab.

    Chrome's native context-menu UI is not exposed to WebDriver in headless
    GitHub Actions.  We still perform the real right-click/contextmenu on the
    exact <a>, then perform the browser-equivalent 'Open link in new tab' by
    opening that exact anchor href in a Selenium-created tab.  Crucially, the
    href is NEVER uploaded directly: the new tab/network must yield a final
    URL ending in .pdf.
    """
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

    # Real Selenium context click on the exact <a>.
    try:
        ActionChains(driver).move_to_element(element).context_click(element).perform()
        time.sleep(0.25)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
    except Exception as exc:
        # Dispatching a contextmenu event is the fallback if native context-click
        # is unavailable in the current headless Chrome build.
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

    # Read the href from the exact element only AFTER the right-click.
    try:
        href = clean(element.get_attribute("href"))
    except Exception:
        href = clean(anchor.get("href"))

    if not re.match(r"^https?://", href, re.I) or BAD_URL_RE.search(href):
        print(f"Rejected Attachment href before opening new tab: {href}")
        return None

    print("Open link in new tab: exact Attachment href")

    # Browser-equivalent of context-menu -> Open link in new tab.
    try:
        driver.switch_to.new_window("tab")
        pdf_tab = driver.current_window_handle
        try:
            network.drain_performance_log(driver)
        except Exception:
            pass
        driver.get(href)
    except Exception as exc:
        print(f"Could not open Attachment href in new tab: {str(exc)[:150]}")
        close_extra_tabs(driver, app_handle)
        return None

    deadline = time.time() + 18.0
    observed: Set[str] = set()

    while time.time() < deadline:
        try:
            driver.switch_to.window(pdf_tab)
            current = clean(driver.current_url)
            if current and current not in observed:
                observed.add(current)
                print(f"New-tab URL observed: {current}")

            if strict_pdf_url(current):
                final_url = current
                print(f"FINAL .pdf URL copied from new tab: {final_url}")
                close_extra_tabs(driver, app_handle)
                return final_url

            dom_pdf = strict_pdf_from_dom(driver)
            if dom_pdf:
                print(f"FINAL .pdf URL copied from new-tab viewer: {dom_pdf}")
                close_extra_tabs(driver, app_handle)
                return dom_pdf
        except WebDriverException:
            pass

        # If Chrome downloads/redirects instead of displaying the PDF, capture
        # only a post-open network URL that itself ends in .pdf.
        try:
            captured = network.attachment_url_from_performance(driver)
        except Exception:
            captured = None
        if strict_pdf_url(captured):
            final_url = clean(captured)
            print(f"FINAL .pdf URL copied from post-open network: {final_url}")
            close_extra_tabs(driver, app_handle)
            return final_url

        # If navigation started a download and Chrome leaves about:blank, the
        # exact opened href is acceptable ONLY when it itself is a strict .pdf.
        if strict_pdf_url(href):
            print(f"FINAL .pdf URL copied from the exact opened Attachment link: {href}")
            close_extra_tabs(driver, app_handle)
            return href

        time.sleep(0.25)

    print("Attachment was opened in a new tab but no final URL ending in .pdf appeared; skipping.")
    for value in sorted(observed):
        print(f"  observed intermediate: {value}")
    close_extra_tabs(driver, app_handle)
    return None


def extract_attachment_url_v4(driver, app_handle: str) -> Optional[str]:
    """Only exact Attachment anchors are eligible; no generic icon/page scanning."""
    anchors = exact_attachment_anchors(driver)
    if not anchors:
        print("No exact Attachment <a> link found in this message")
        return None

    # Normally there is one real HyperLink1.  If more exist, try the strongest
    # exact anchor first and never fall back to generic DIVs/icons.
    for index, anchor in enumerate(anchors, start=1):
        print(f"Trying exact Attachment anchor {index}/{len(anchors)}")
        result = right_click_open_link_in_new_tab(driver, app_handle, anchor)
        if result and strict_pdf_url(result):
            print(f"Verified uploadable PDF link: {result}")
            return result

    print("Exact Attachment link(s) found, but none produced a strict .pdf URL")
    return None


runner.list_firestore_materials = list_and_cleanup_materials
runner.existing_state = strict_existing_state
runner.extract_attachment_url = extract_attachment_url_v4
runner.legacy.make_title = runner_v2.make_message_title
runner.legacy.make_driver = network.make_driver_with_network_logs
runner.legacy.is_pdf_url = strict_pdf_url


if __name__ == "__main__":
    raise SystemExit(runner.main())
