"""Second-stage repair for EduSecure attachment extraction.

This layer fixes the remaining case where the message detail visibly contains
"Attachment" but the label itself is not the real clickable control.  It finds
nearby sibling/parent/descendant controls, instruments window.open/form submits,
and watches Chrome network/resource activity after the real click.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains

import sync as base
import sync_repair as r1


def _candidate_url(raw: str | None, current_url: str) -> Optional[str]:
    if not raw:
        return None
    value = str(raw).strip().strip("'\"")
    if not value or value.lower().startswith(("javascript:", "mailto:", "tel:")):
        return None
    try:
        absolute = urljoin(current_url, value)
    except Exception:
        return None
    if r1.looks_like_attachment_url(absolute):
        return absolute
    return None


def find_attachment_targets_v2(driver) -> List[Dict[str, str]]:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== "none" && st.visibility !== "hidden" && r.width > 0 && r.height > 0;
    }

    function textOf(el) {
      if (!el) return "";
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

    function isClickable(el) {
      if (!el || !visible(el)) return false;
      if (el.matches && el.matches("a[href], button, input[type='button'], input[type='submit'], input[type='image'], [onclick], [role='button']")) return true;
      const st = window.getComputedStyle(el);
      return st.cursor === "pointer";
    }

    function directStrings(el) {
      const values = [];
      if (!el || !el.getAttribute) return values;
      const attrs = ["href", "src", "data", "data-url", "data-href", "data-src", "formaction", "onclick"];
      for (const name of attrs) {
        const v = el.getAttribute(name) || "";
        if (v) values.push(v);
      }
      return values;
    }

    function scoreControl(el, label) {
      if (!el) return -1;
      const txt = (textOf(el) + " " + JSON.stringify(attrsOf(el))).toLowerCase();
      let score = 0;
      if (isClickable(el)) score += 50;
      if (/attachment|attached/.test(txt)) score += 80;
      if (/download/.test(txt)) score += 75;
      if (/\.pdf/.test(txt)) score += 100;
      if (/__dopostback|webform_dopostback|postback/.test(txt)) score += 65;
      if (/getfile|downloadfile|filehandler|documenthandler/.test(txt)) score += 80;
      if (/paperclip|attach_file|file_download|fa-paperclip/.test(txt)) score += 45;
      if (el === label) score -= 20;
      return score;
    }

    const labels = Array.from(document.querySelectorAll("body *"))
      .filter(visible)
      .filter(el => {
        const t = textOf(el).toLowerCase();
        const a = JSON.stringify(attrsOf(el)).toLowerCase();
        return /attachment|attached/.test(t + " " + a) || /\.pdf|downloadfile|filehandler/.test(a);
      });

    const results = [];
    const seen = new Set();

    function add(control, label, relation) {
      if (!control || !visible(control)) return;
      const path = makePath(control);
      if (!path || seen.has(path)) return;
      seen.add(path);
      const r = control.getBoundingClientRect();
      const attrs = attrsOf(control);
      const strings = directStrings(control);
      results.push({
        path,
        text: textOf(control).slice(0, 300),
        labelText: textOf(label).slice(0, 300),
        relation,
        score: scoreControl(control, label),
        tag: control.tagName || "",
        attrs: JSON.stringify(attrs).slice(0, 1800),
        onclick: control.getAttribute && (control.getAttribute("onclick") || ""),
        directStrings: strings.slice(0, 10),
        html: (control.outerHTML || "").replace(/\s+/g, " ").slice(0, 2200),
        top: Math.round(r.top),
        left: Math.round(r.left)
      });
    }

    for (const label of labels) {
      // Label itself if it truly acts like a control.
      if (isClickable(label)) add(label, label, "label-self");

      // Nearest clickable ancestor.
      const ancestor = label.closest && label.closest("a[href], button, input[type='button'], input[type='submit'], input[type='image'], [onclick], [role='button']");
      if (ancestor) add(ancestor, label, "ancestor");

      // Clickable descendants of the label/container.
      if (label.querySelectorAll) {
        for (const child of Array.from(label.querySelectorAll("a[href], button, input[type='button'], input[type='submit'], input[type='image'], [onclick], [role='button']"))) {
          add(child, label, "descendant");
        }
      }

      // Critical EduSecure case: "Attachment" text can be one sibling while
      // the actual icon/button is another child of the same wrapper.
      let p = label.parentElement;
      for (let depth = 1; depth <= 5 && p; depth++, p = p.parentElement) {
        const controls = Array.from(p.querySelectorAll("a[href], button, input[type='button'], input[type='submit'], input[type='image'], [onclick], [role='button'], img[src]"))
          .filter(visible);
        for (const control of controls) {
          const cr = control.getBoundingClientRect();
          const lr = label.getBoundingClientRect();
          const dx = Math.abs((cr.left + cr.width / 2) - (lr.left + lr.width / 2));
          const dy = Math.abs((cr.top + cr.height / 2) - (lr.top + lr.height / 2));
          // Keep nearby controls only so a whole page wrapper cannot introduce random buttons.
          if (dx < 700 && dy < 320) add(control, label, "nearby-parent-" + depth);
        }
      }
    }

    return results
      .sort((a,b) => b.score - a.score || a.top - b.top || a.left - b.left)
      .slice(0, 20);
    """
    try:
        return driver.execute_script(js) or []
    except Exception as exc:
        print(f"Attachment DOM scan failed: {exc}")
        return []


def install_click_trace(driver) -> None:
    js = r"""
    try {
      window.__edusecureTrace = [];
      const push = (kind, value) => {
        try { window.__edusecureTrace.push({kind, value: String(value || ""), at: Date.now()}); } catch(e) {}
      };

      if (!window.__edusecureOriginalOpen) {
        window.__edusecureOriginalOpen = window.open;
        window.open = function() {
          push("window.open", arguments[0] || "");
          return window.__edusecureOriginalOpen.apply(this, arguments);
        };
      }

      document.addEventListener("click", function(ev) {
        const el = ev.target && (ev.target.closest ? ev.target.closest("a,button,input,[onclick],[role='button']") : ev.target);
        if (!el) return;
        const info = {
          tag: el.tagName || "",
          text: (el.innerText || el.textContent || el.value || "").replace(/\s+/g," ").trim().slice(0,300),
          href: el.getAttribute && (el.getAttribute("href") || ""),
          onclick: el.getAttribute && (el.getAttribute("onclick") || ""),
          id: el.id || ""
        };
        push("click", JSON.stringify(info));
      }, true);

      document.addEventListener("submit", function(ev) {
        const f = ev.target;
        push("submit", JSON.stringify({action:(f && f.action)||"", method:(f && f.method)||"", id:(f && f.id)||""}));
      }, true);

      window.addEventListener("beforeunload", function() { push("beforeunload", location.href); });
    } catch(e) {}
    return true;
    """
    try:
        driver.execute_script(js)
    except Exception:
        pass


def read_click_trace(driver) -> List[Dict[str, str]]:
    try:
        return driver.execute_script("return window.__edusecureTrace || [];") or []
    except Exception:
        return []


def trace_url_candidate(driver, trace: List[Dict[str, str]]) -> Optional[str]:
    current = driver.current_url
    for item in reversed(trace):
        kind = str(item.get("kind") or "")
        value = str(item.get("value") or "")
        if kind == "window.open":
            candidate = _candidate_url(value, current)
            if candidate:
                print(f"Captured attachment URL from window.open: {candidate}")
                return candidate

        if kind == "click":
            try:
                data = json.loads(value)
            except Exception:
                data = {}
            for raw in (data.get("href"), data.get("onclick")):
                if not raw:
                    continue
                # Direct attribute value.
                candidate = _candidate_url(raw, current)
                if candidate:
                    print(f"Captured attachment URL from clicked element: {candidate}")
                    return candidate
                # URLs quoted inside onclick JS.
                matches = re.findall(r"['\"]([^'\"]+(?:\.pdf|download[^'\"]*|attachment[^'\"]*|getfile[^'\"]*|filehandler[^'\"]*))['\"]", str(raw), flags=re.I)
                for match in matches:
                    candidate = _candidate_url(match, current)
                    if candidate:
                        print(f"Captured attachment URL from onclick: {candidate}")
                        return candidate
    return None


def resource_url_candidate(driver, known_urls: set[str]) -> Optional[str]:
    js = r"""
    return performance.getEntriesByType("resource").map(e => e.name).filter(Boolean);
    """
    try:
        urls = driver.execute_script(js) or []
    except Exception:
        return None
    for raw in reversed(urls):
        url = str(raw)
        if url in known_urls:
            continue
        if r1.looks_like_attachment_url(url):
            print(f"Captured attachment URL from resource timing: {url}")
            return url
    return None


def current_resource_urls(driver) -> set[str]:
    try:
        urls = driver.execute_script("return performance.getEntriesByType('resource').map(e => e.name).filter(Boolean);") or []
        return {str(x) for x in urls}
    except Exception:
        return set()


def dom_attachment_url(driver) -> Optional[str]:
    js = r"""
    function abs(u) { try { return new URL(u, location.href).href; } catch(e) { return ""; } }
    const out = [];
    for (const el of document.querySelectorAll("a[href], iframe[src], frame[src], embed[src], object[data], [data-url], [data-href], [onclick]")) {
      for (const name of ["href","src","data","data-url","data-href","onclick"]) {
        const raw = el.getAttribute(name) || "";
        if (!raw) continue;
        if (name !== "onclick") out.push(abs(raw));
        const ms = raw.match(/['\"]([^'\"]+(?:\.pdf|download[^'\"]*|attachment[^'\"]*|getfile[^'\"]*|filehandler[^'\"]*))['\"]/ig) || [];
        for (const m of ms) out.push(abs(m.slice(1,-1)));
      }
    }
    return out.filter(Boolean);
    """
    try:
        values = driver.execute_script(js) or []
    except Exception:
        return None
    for raw in reversed(values):
        if r1.looks_like_attachment_url(str(raw)):
            print(f"Captured attachment URL from changed DOM: {raw}")
            return str(raw)
    return None


def real_click_control(driver, path: str) -> bool:
    el = base.get_element_from_path_v25(driver, path)
    if not el:
        return False
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", el)
        time.sleep(0.15)
    except Exception:
        pass

    # Prefer a physical Selenium pointer click.
    try:
        ActionChains(driver).move_to_element(el).pause(0.15).click().perform()
        return True
    except Exception:
        pass
    try:
        el.click()
        return True
    except Exception:
        pass
    try:
        driver.execute_script(
            """
            const el=arguments[0];
            for (const t of ['pointerdown','mousedown','pointerup','mouseup','click']) {
              el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window,button:0}));
            }
            """,
            el,
        )
        return True
    except Exception:
        return False


def repaired_get_pdf_url_from_attachment_v2(driver, target: Dict[str, str], app_handle: str) -> Optional[str]:
    print("Attachment target debug:")
    print(json.dumps({
        "relation": target.get("relation"),
        "tag": target.get("tag"),
        "text": target.get("text"),
        "labelText": target.get("labelText"),
        "onclick": target.get("onclick"),
        "directStrings": target.get("directStrings"),
        "attrs": target.get("attrs"),
        "html": target.get("html"),
    }, indent=2, ensure_ascii=False))

    current = driver.current_url
    for raw in target.get("directStrings") or []:
        candidate = _candidate_url(str(raw), current)
        if candidate:
            print(f"Direct attachment URL found in control: {candidate}")
            return candidate
        matches = re.findall(r"['\"]([^'\"]+(?:\.pdf|download[^'\"]*|attachment[^'\"]*|getfile[^'\"]*|filehandler[^'\"]*))['\"]", str(raw), flags=re.I)
        for match in matches:
            candidate = _candidate_url(match, current)
            if candidate:
                print(f"Attachment URL parsed from control JS: {candidate}")
                return candidate

    before_url = driver.current_url
    before_tabs = set(driver.window_handles)
    known_resources = current_resource_urls(driver)
    r1.drain_performance_log(driver)
    install_click_trace(driver)

    if not real_click_control(driver, target.get("path", "")):
        print("Real attachment control click failed.")
        return None

    deadline = time.time() + 10
    while time.time() < deadline:
        captured = r1.attachment_url_from_performance(driver)
        if captured:
            r1._close_extra_tabs_and_restore(driver, app_handle)
            return captured

        try:
            handles = set(driver.window_handles)
            new_tabs = list(handles - before_tabs)
            if new_tabs:
                driver.switch_to.window(new_tabs[-1])
                time.sleep(0.25)
                candidate = driver.current_url
                if r1.looks_like_attachment_url(candidate):
                    print(f"Captured attachment URL from new tab: {candidate}")
                    r1._close_extra_tabs_and_restore(driver, app_handle)
                    return candidate
                page_pdf = base.extract_pdf_from_current_page(driver)
                if page_pdf:
                    r1._close_extra_tabs_and_restore(driver, app_handle)
                    return page_pdf
                driver.switch_to.window(app_handle)

            driver.switch_to.window(app_handle)
            if driver.current_url != before_url and r1.looks_like_attachment_url(driver.current_url):
                candidate = driver.current_url
                print(f"Captured attachment URL from navigation: {candidate}")
                return candidate

            trace = read_click_trace(driver)
            candidate = trace_url_candidate(driver, trace)
            if candidate:
                return candidate

            candidate = resource_url_candidate(driver, known_resources)
            if candidate:
                return candidate

            candidate = dom_attachment_url(driver)
            if candidate:
                return candidate
        except WebDriverException:
            pass

        time.sleep(0.25)

    # Print trace even on failure; this is crucial if EduSecure uses a custom JS postback.
    try:
        driver.switch_to.window(app_handle)
        trace = read_click_trace(driver)
        print("Click trace after failed attachment attempt:")
        print(json.dumps(trace[-20:], indent=2, ensure_ascii=False))
        print(f"URL after click: {driver.current_url}")
    except Exception:
        pass

    r1._close_extra_tabs_and_restore(driver, app_handle)
    return None


def repaired_extract_pdf_from_current_message_detail_v2(driver, app_handle: str) -> Optional[str]:
    direct = base.extract_pdf_from_current_page(driver)
    if direct:
        return direct

    targets = find_attachment_targets_v2(driver)
    if not targets:
        targets = r1.find_attachment_targets(driver)
    if not targets:
        print("No attachment control detected on message detail page.")
        return None

    detail_url = driver.current_url
    print(f"Attachment controls detected (v2): {len(targets)}")

    for i, target in enumerate(targets, start=1):
        print(
            f"Trying attachment control {i}/{len(targets)} "
            f"relation={target.get('relation','')} score={target.get('score','')}: "
            f"{target.get('text','')[:120]!r}"
        )
        url = repaired_get_pdf_url_from_attachment_v2(driver, target, app_handle)
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


# Keep v1 driver/network setup and relaxed URL validator, replace only the
# remaining attachment-target/click layer.
base.make_driver = r1.make_driver_with_network_logs
base.is_pdf_url = r1.repaired_is_pdf_url
base.get_pdf_url_from_attachment = repaired_get_pdf_url_from_attachment_v2
base.extract_pdf_from_current_message_detail = repaired_extract_pdf_from_current_message_detail_v2


if __name__ == "__main__":
    base.main()
