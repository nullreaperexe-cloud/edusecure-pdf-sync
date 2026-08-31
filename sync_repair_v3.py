"""Third-stage EduSecure attachment repair.

Keeps all v1/v2 fixes and adds persistent Chrome DevTools correlation for
ASP.NET postback downloads, responseExtraInfo headers, chrome://downloads
inspection, nearby hidden/direct URL discovery, and completed-download fallback.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from selenium.common.exceptions import WebDriverException

import sync as base
import sync_repair as r1
import sync_repair_v2 as r2


DOWNLOAD_DIR = Path(r1.DOWNLOAD_DIR)


def _http_url(value: str | None, base_url: str = "") -> str:
    if not value:
        return ""
    raw = str(value).strip().strip("'\"")
    if not raw or raw.lower().startswith(("javascript:", "data:", "blob:", "mailto:", "tel:")):
        return ""
    try:
        url = urljoin(base_url, raw)
    except Exception:
        return ""
    return url if url.lower().startswith(("http://", "https://")) else ""


def _header(headers: object, name: str) -> str:
    if not isinstance(headers, dict):
        return ""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def _header_pdf_score(headers: object, mime: str = "") -> int:
    content_type = (_header(headers, "content-type") or mime or "").lower()
    disposition = _header(headers, "content-disposition").lower()
    score = 0
    if "application/pdf" in content_type:
        score += 120
    if "attachment" in disposition:
        score += 80
    if ".pdf" in disposition:
        score += 90
    return score


def _url_score(url: str, before_url: str = "") -> int:
    if not url:
        return 0
    low = url.lower()
    score = 0
    if ".pdf" in low:
        score += 100
    if r1.looks_like_attachment_url(url):
        score += 75
    if any(x in low for x in ("/upload/", "/uploads/", "/document/", "/documents/", "/files/", "/attachment/")):
        score += 55
    if before_url and url == before_url:
        score -= 35
    return score


def nearby_direct_attachment_url(driver, path: str) -> Optional[str]:
    """Search the attachment control's nearby DOM for hidden/sibling file URLs."""
    js = r"""
    const path = arguments[0];
    function getEl(path) {
      if (!path) return null;
      if (path.startsWith('id:')) return document.getElementById(path.slice(3));
      if (path.startsWith('css:')) return document.querySelector(path.slice(4));
      return null;
    }
    function abs(v) { try { return new URL(v, location.href).href; } catch(e) { return ''; } }
    const el = getEl(path);
    if (!el) return [];
    const out = [];
    const seen = new Set();
    let root = el;
    for (let i=0; i<5 && root && root.parentElement; i++) root = root.parentElement;
    root = root || el;
    const nodes = [root].concat(Array.from(root.querySelectorAll('*')));
    for (const n of nodes) {
      if (!n || !n.getAttribute) continue;
      for (const name of ['href','src','data','data-url','data-href','data-src','value','formaction','onclick']) {
        const raw = n.getAttribute(name) || '';
        if (!raw) continue;
        const vals = [raw];
        const quoted = raw.match(/['\"]([^'\"]+)['\"]/g) || [];
        for (const q of quoted) vals.push(q.slice(1,-1));
        for (const v of vals) {
          const a = abs(v);
          if (!a || seen.has(a)) continue;
          seen.add(a);
          out.push({url:a, attr:name, text:(n.innerText||n.textContent||'').replace(/\s+/g,' ').trim().slice(0,180)});
        }
      }
    }
    return out.slice(0,250);
    """
    try:
        values = driver.execute_script(js, path) or []
    except Exception:
        return None

    before = driver.current_url
    ranked: List[Tuple[int, str, str]] = []
    for item in values:
        url = _http_url(item.get("url"), before)
        if not url:
            continue
        score = _url_score(url, before)
        low = url.lower()
        if any(x in low for x in ("download", "attachment", "getfile", "filehandler")):
            score += 50
        if score >= 90:
            ranked.append((score, url, str(item.get("attr") or "")))

    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, url, attr = ranked[0]
    print(f"Nearby DOM attachment URL found ({attr}, score={score}): {url}")
    return url


def _new_download_files(before: set[str]) -> List[Path]:
    try:
        files = []
        for p in DOWNLOAD_DIR.iterdir():
            if not p.is_file():
                continue
            if p.name in before:
                continue
            if p.name.endswith(".crdownload"):
                continue
            files.append(p)
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []


def _download_snapshot() -> set[str]:
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        return {p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file()}
    except Exception:
        return set()


def chrome_download_items(driver, app_handle: str) -> List[Dict[str, str]]:
    """Read Chrome's own download model; it often exposes the original/final URL."""
    download_handle = None
    try:
        driver.switch_to.window(app_handle)
        driver.switch_to.new_window("tab")
        download_handle = driver.current_window_handle
        driver.get("chrome://downloads/")
        time.sleep(0.35)
        js = r"""
        const manager = document.querySelector('downloads-manager');
        if (!manager) return [];
        const root = manager.shadowRoot;
        const list = root && root.querySelector('#downloadsList');
        const items = (list && (list.items || list._items)) || manager.items || manager._items || [];
        return Array.from(items).map(x => ({
          url: x.url || '',
          finalUrl: x.finalUrl || '',
          fileUrl: x.fileUrl || '',
          filePath: x.filePath || '',
          fileName: x.fileName || '',
          state: String(x.state || ''),
          mimeType: x.mimeType || '',
          referrerUrl: x.referrerUrl || ''
        }));
        """
        return driver.execute_script(js) or []
    except Exception as exc:
        print(f"chrome://downloads inspection unavailable: {str(exc)[:140]}")
        return []
    finally:
        try:
            if download_handle and download_handle in driver.window_handles:
                driver.switch_to.window(download_handle)
                driver.close()
        except Exception:
            pass
        try:
            driver.switch_to.window(app_handle)
        except Exception:
            pass


def best_url_from_download_items(items: List[Dict[str, str]], before_url: str) -> Optional[str]:
    ranked: List[Tuple[int, str, str]] = []
    for item in items:
        filename = str(item.get("fileName") or "")
        mime = str(item.get("mimeType") or "").lower()
        for field in ("finalUrl", "url", "referrerUrl"):
            url = _http_url(item.get(field), before_url)
            if not url:
                continue
            score = _url_score(url, before_url)
            if filename.lower().endswith(".pdf"):
                score += 90
            if "application/pdf" in mime:
                score += 90
            if field == "finalUrl":
                score += 25
            if score > 0:
                ranked.append((score, url, f"{field}, filename={filename!r}"))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, url, why = ranked[0]
    print(f"Captured attachment URL from Chrome downloads ({why}, score={score}): {url}")
    return url


def collect_performance(driver, state: Dict[str, object], before_url: str) -> Optional[str]:
    """Persistently correlate request, response, ExtraInfo and download events."""
    requests_by_id: Dict[str, Dict[str, object]] = state.setdefault("requests", {})  # type: ignore[assignment]
    responses_by_id: Dict[str, Dict[str, object]] = state.setdefault("responses", {})  # type: ignore[assignment]
    extra_by_id: Dict[str, Dict[str, object]] = state.setdefault("extra", {})  # type: ignore[assignment]
    candidates: List[Tuple[int, str, str]] = state.setdefault("candidates", [])  # type: ignore[assignment]

    try:
        entries = driver.get_log("performance")
    except Exception:
        entries = []

    for entry in entries:
        try:
            outer = json.loads(entry.get("message", "{}"))
            message = outer.get("message", {}) or {}
            method = str(message.get("method") or "")
            params = message.get("params", {}) or {}
        except Exception:
            continue

        if method in ("Page.downloadWillBegin", "Browser.downloadWillBegin"):
            url = _http_url(params.get("url"), before_url)
            filename = str(params.get("suggestedFilename") or "")
            if url:
                score = 240 + (80 if filename.lower().endswith(".pdf") else 0)
                candidates.append((score, url, f"downloadWillBegin filename={filename!r}"))
            continue

        request_id = str(params.get("requestId") or "")

        if method == "Network.requestWillBeSent":
            req = params.get("request", {}) or {}
            url = _http_url(req.get("url"), before_url)
            record = {
                "url": url,
                "method": str(req.get("method") or "GET").upper(),
                "postData": str(req.get("postData") or ""),
                "headers": req.get("headers", {}) or {},
                "type": str(params.get("type") or ""),
                "timestamp": params.get("timestamp"),
            }
            if request_id:
                requests_by_id[request_id] = record
            redirect = params.get("redirectResponse", {}) or {}
            if redirect and url:
                redirect_headers = redirect.get("headers", {}) or {}
                score = _header_pdf_score(redirect_headers, str(redirect.get("mimeType") or ""))
                if score:
                    candidates.append((score + 70, url, "redirect response PDF headers"))
            if url and r1.looks_like_attachment_url(url):
                candidates.append((150 + _url_score(url, before_url), url, "request URL"))
            continue

        if method == "Network.responseReceived":
            resp = params.get("response", {}) or {}
            if request_id:
                responses_by_id[request_id] = resp
            url = _http_url(resp.get("url"), before_url)
            if url:
                score = _header_pdf_score(resp.get("headers", {}) or {}, str(resp.get("mimeType") or ""))
                score += _url_score(url, before_url)
                if score >= 100:
                    candidates.append((score + 80, url, "responseReceived"))
            continue

        if method == "Network.responseReceivedExtraInfo":
            if request_id:
                extra_by_id[request_id] = params.get("headers", {}) or {}
            continue

    # Correlate ExtraInfo with the original request URL. This is the important
    # ASP.NET case: POST URL can be Announcement.aspx while headers say PDF.
    for request_id, headers in list(extra_by_id.items()):
        req = requests_by_id.get(request_id, {})
        url = _http_url(req.get("url"), before_url)
        if not url:
            continue
        score = _header_pdf_score(headers)
        if score:
            method = str(req.get("method") or "")
            same = url == before_url
            candidates.append((score + 100 - (25 if same else 0), url, f"responseExtraInfo {method} PDF headers"))

    if not candidates:
        return None

    # De-dupe and choose strongest evidence.
    best_for_url: Dict[str, Tuple[int, str]] = {}
    for score, url, why in candidates:
        old = best_for_url.get(url)
        if old is None or score > old[0]:
            best_for_url[url] = (score, why)
    ranked = sorted(((score, url, why) for url, (score, why) in best_for_url.items()), reverse=True)
    score, url, why = ranked[0]
    state["best"] = (score, url, why)

    # Very strong evidence can return immediately. We keep weaker same-page
    # postback evidence around briefly so chrome://downloads can reveal a better final URL.
    if score >= 260 and (url != before_url or "downloadWillBegin" in why):
        print(f"Captured verified PDF/download URL ({why}, score={score}): {url}")
        return url
    return None


def replay_last_postback_for_redirect(driver, state: Dict[str, object], before_url: str) -> Optional[str]:
    """Replay captured ASP.NET POST only to discover a redirect/content-location URL."""
    requests_by_id = state.get("requests", {})
    if not isinstance(requests_by_id, dict):
        return None
    posts = []
    for req in requests_by_id.values():
        if not isinstance(req, dict):
            continue
        if str(req.get("method") or "").upper() != "POST":
            continue
        url = _http_url(req.get("url"), before_url)
        if not url:
            continue
        posts.append(req)
    if not posts:
        return None

    req = posts[-1]
    url = _http_url(req.get("url"), before_url)
    post_data = str(req.get("postData") or "")
    if not url or not post_data:
        return None

    try:
        session = requests.Session()
        for cookie in driver.get_cookies():
            try:
                session.cookies.set(cookie.get("name"), cookie.get("value"), domain=cookie.get("domain"), path=cookie.get("path") or "/")
            except Exception:
                pass

        headers = req.get("headers", {}) if isinstance(req.get("headers"), dict) else {}
        safe_headers = {}
        for name in ("Content-Type", "Referer", "User-Agent", "Origin"):
            value = _header(headers, name)
            if value:
                safe_headers[name] = value

        response = session.post(url, data=post_data, headers=safe_headers, allow_redirects=True, timeout=30)
        final_url = _http_url(response.url, before_url)
        location = _http_url(response.headers.get("Content-Location") or response.headers.get("Location"), final_url or before_url)
        for candidate, why in ((location, "Content-Location/Location"), (final_url, "POST replay final URL")):
            if candidate and candidate != before_url and (_url_score(candidate, before_url) >= 70 or _header_pdf_score(response.headers, response.headers.get("Content-Type", "")) > 0):
                print(f"Captured attachment URL from {why}: {candidate}")
                return candidate
    except Exception as exc:
        print(f"ASP.NET postback replay could not derive a URL: {str(exc)[:150]}")
    return None


def repaired_get_pdf_url_from_attachment_v3(driver, target: Dict[str, str], app_handle: str) -> Optional[str]:
    # Keep v2's direct control parsing first.
    current = driver.current_url
    for raw in target.get("directStrings") or []:
        candidate = r2._candidate_url(str(raw), current)
        if candidate:
            print(f"Direct attachment URL found in control: {candidate}")
            return candidate

    nearby = nearby_direct_attachment_url(driver, target.get("path", ""))
    if nearby:
        return nearby

    before_url = driver.current_url
    before_tabs = set(driver.window_handles)
    before_files = _download_snapshot()
    known_resources = r2.current_resource_urls(driver)
    r1.drain_performance_log(driver)
    r2.install_click_trace(driver)

    if not r2.real_click_control(driver, target.get("path", "")):
        print("Real attachment control click failed.")
        return None

    perf_state: Dict[str, object] = {}
    deadline = time.time() + 12.0
    download_seen = False

    while time.time() < deadline:
        captured = collect_performance(driver, perf_state, before_url)
        if captured:
            r1._close_extra_tabs_and_restore(driver, app_handle)
            return captured

        try:
            handles = set(driver.window_handles)
            new_tabs = list(handles - before_tabs)
            if new_tabs:
                driver.switch_to.window(new_tabs[-1])
                time.sleep(0.25)
                candidate = _http_url(driver.current_url, before_url)
                if candidate and (r1.looks_like_attachment_url(candidate) or ".pdf" in candidate.lower()):
                    print(f"Captured attachment URL from new tab: {candidate}")
                    r1._close_extra_tabs_and_restore(driver, app_handle)
                    return candidate
                page_pdf = base.extract_pdf_from_current_page(driver)
                if page_pdf:
                    r1._close_extra_tabs_and_restore(driver, app_handle)
                    return page_pdf
                driver.switch_to.window(app_handle)

            driver.switch_to.window(app_handle)
            candidate = r2.trace_url_candidate(driver, r2.read_click_trace(driver))
            if candidate:
                return candidate
            candidate = r2.resource_url_candidate(driver, known_resources)
            if candidate:
                return candidate
            candidate = r2.dom_attachment_url(driver)
            if candidate:
                return candidate
        except WebDriverException:
            pass

        new_files = _new_download_files(before_files)
        if new_files and not download_seen:
            download_seen = True
            print("Browser download completed: " + ", ".join(p.name for p in new_files[:3]))
            items = chrome_download_items(driver, app_handle)
            candidate = best_url_from_download_items(items, before_url)
            if candidate and candidate != before_url:
                return candidate

        time.sleep(0.25)

    # Final event drain and Chrome download inspection.
    collect_performance(driver, perf_state, before_url)
    new_files = _new_download_files(before_files)
    if new_files:
        print("Confirmed downloaded file(s): " + ", ".join(p.name for p in new_files[:3]))
        candidate = best_url_from_download_items(chrome_download_items(driver, app_handle), before_url)
        if candidate and candidate != before_url:
            return candidate

    # Try to turn an ASP.NET POSTback into its redirect/final handler URL.
    candidate = replay_last_postback_for_redirect(driver, perf_state, before_url)
    if candidate:
        return candidate

    # If headers/download evidence proves this exact URL returns the PDF, keep it
    # as a last resort. This is better than dropping a real attachment entirely.
    best = perf_state.get("best")
    if isinstance(best, tuple) and len(best) == 3:
        score, url, why = best
        if int(score) >= 200 and _http_url(str(url), before_url):
            print(f"Using verified attachment response URL as fallback ({why}, score={score}): {url}")
            return str(url)

    try:
        driver.switch_to.window(app_handle)
        print("Click trace after failed attachment attempt:")
        print(json.dumps(r2.read_click_trace(driver)[-20:], indent=2, ensure_ascii=False))
        requests_by_id = perf_state.get("requests", {})
        if isinstance(requests_by_id, dict):
            summary = []
            for req in list(requests_by_id.values())[-12:]:
                if not isinstance(req, dict):
                    continue
                summary.append({"method": req.get("method"), "url": req.get("url"), "type": req.get("type"), "hasPostData": bool(req.get("postData"))})
            print("Recent browser requests after Attachment click:")
            print(json.dumps(summary, indent=2, ensure_ascii=False))
    except Exception:
        pass

    r1._close_extra_tabs_and_restore(driver, app_handle)
    print("Attachment clicked, but no reusable download URL could be proven.")
    return None


def repaired_extract_pdf_from_current_message_detail_v3(driver, app_handle: str) -> Optional[str]:
    direct = base.extract_pdf_from_current_page(driver)
    if direct:
        return direct

    targets = r2.find_attachment_targets_v2(driver)
    if not targets:
        targets = r1.find_attachment_targets(driver)
    if not targets:
        print("No attachment control detected on message detail page.")
        return None

    detail_url = driver.current_url
    print(f"Attachment controls detected (v3): {len(targets)}")

    for i, target in enumerate(targets, start=1):
        print(
            f"Trying attachment control {i}/{len(targets)} "
            f"relation={target.get('relation','')} score={target.get('score','')}: "
            f"{target.get('text','')[:120]!r}"
        )
        url = repaired_get_pdf_url_from_attachment_v3(driver, target, app_handle)
        if url:
            print(f"Real PDF/download URL extracted: {url}")
            return url

        try:
            driver.switch_to.window(app_handle)
            if driver.current_url != detail_url:
                driver.get(detail_url)
                base.wait_ready(driver)
                time.sleep(0.7)
        except Exception:
            pass

    return None


# Preserve v1's network-enabled driver and relaxed URL validator. Replace the
# extraction layer with v3. base.main() will then POST the extracted URL to the
# configured /automation/ingest endpoint exactly like before.
base.make_driver = r1.make_driver_with_network_logs
base.is_pdf_url = r1.repaired_is_pdf_url
base.get_pdf_url_from_attachment = repaired_get_pdf_url_from_attachment_v3
base.extract_pdf_from_current_message_detail = repaired_extract_pdf_from_current_message_detail_v3


if __name__ == "__main__":
    base.main()
