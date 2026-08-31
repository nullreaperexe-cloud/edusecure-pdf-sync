from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Dict, Optional

from selenium.common.exceptions import WebDriverException

import runner


def clean(value) -> str:
    return runner.clean(value)


def make_message_title(text: str, pdf_url: str, number: int = 0) -> str:
    """Use the actual EduSecure message/homework text as the PDF title."""
    raw = clean(text)

    # Prefer the Homework content shown inside School Diary messages.
    hw = re.search(r"Home\s*Work\s*:\s*(.*?)(?=\s*Class\s*Work\s*:|$)", raw, flags=re.I)
    cw = re.search(r"Class\s*Work\s*:\s*(.*)$", raw, flags=re.I)

    candidate = ""
    if hw:
        candidate = clean(hw.group(1))
        if candidate in {"-", "--", "nil", "none"}:
            candidate = ""
    if not candidate and cw:
        candidate = clean(cw.group(1))
    if not candidate:
        candidate = raw

    # Remove UI/date wrappers and attachment words, but keep the real message wording.
    candidate = re.sub(r"^(School\s*Diary|Circular|Message)\s+", "", candidate, flags=re.I)
    candidate = re.sub(r"\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b", "", candidate)
    candidate = re.sub(r"\bAttachment\b|\bAttach\b|\bDownload\b|\bOpen\b|\bClick\s+Here\b", "", candidate, flags=re.I)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -:|,")

    if not candidate or candidate.lower() in {"attachment", "pdf", "document"}:
        candidate = runner.legacy.title_from_pdf_url(pdf_url)

    if len(candidate) > 125:
        candidate = candidate[:125].rsplit(" ", 1)[0]

    return candidate or f"PDF {number}".strip()


def find_attachment_icon_target(driver) -> Optional[Dict[str, str]]:
    """
    EduSecure detail view uses a tiny attachment icon at the lower-right.
    It often has no visible 'Attachment' text, so detect icon/clickable metadata
    plus its lower-right geometry instead of relying on text only.
    """
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return st.display !== 'none' && st.visibility !== 'hidden' &&
             r.width > 5 && r.height > 5 && r.bottom > 0 && r.top < innerHeight;
    }
    function text(el) {
      return (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
    }
    function info(el) {
      const bits = [
        el.id, el.className, el.getAttribute && el.getAttribute('title'),
        el.getAttribute && el.getAttribute('aria-label'),
        el.getAttribute && el.getAttribute('alt'),
        el.getAttribute && el.getAttribute('src'),
        el.getAttribute && el.getAttribute('href'),
        el.getAttribute && el.getAttribute('onclick'),
        el.getAttribute && el.getAttribute('data-url'),
        el.getAttribute && el.getAttribute('data-href')
      ].filter(Boolean).join(' ');
      return String(bits).toLowerCase();
    }
    function path(el) {
      if (el.id) return 'id:' + el.id;
      const parts=[]; let cur=el;
      while(cur && cur !== document.body && cur.nodeType===1) {
        let n=1, s=cur.previousElementSibling;
        while(s){ if(s.tagName===cur.tagName) n++; s=s.previousElementSibling; }
        parts.unshift(cur.tagName.toLowerCase()+':nth-of-type('+n+')');
        cur=cur.parentElement;
      }
      return 'css:body > ' + parts.join(' > ');
    }
    function clickable(el) {
      return el.closest('a,button,[onclick],[role="button"],[tabindex]') || el;
    }

    const raw = Array.from(document.querySelectorAll(
      'a,button,[onclick],[role="button"],img,svg,i,span,div'
    )).filter(visible);

    const seen = new Set();
    const scored = [];

    for (const node of raw) {
      const el = clickable(node);
      if (!visible(el) || seen.has(el)) continue;
      seen.add(el);

      const r = el.getBoundingClientRect();
      const t = text(el);
      const meta = info(el) + ' ' + info(node);
      let score = 0;

      if (/attach|attachment|paperclip|pdf|document|file|download/.test(meta + ' ' + t.toLowerCase())) score += 120;
      if (el.matches('a,button,[onclick],[role="button"]')) score += 28;
      if (el.querySelector && el.querySelector('img,svg,i')) score += 22;
      if (node.matches && node.matches('img,svg,i')) score += 18;

      // The EduSecure attachment control in the detail screen is a small icon
      // in the lower-right area (as shown in the real UI screenshot).
      if (r.width <= 100 && r.height <= 100) score += 18;
      if (r.left >= innerWidth * 0.55) score += 20;
      if (r.top >= innerHeight * 0.48) score += 16;
      if (r.right >= innerWidth * 0.70) score += 12;

      // Avoid navigation/back/menu controls.
      if (/back|arrow|chevron|home|menu|search|close|logout|profile/.test(meta + ' ' + t.toLowerCase())) score -= 140;
      if (t.length > 90) score -= 35;
      if (r.top < 135) score -= 50;

      if (score >= 55) {
        scored.push({
          el, score,
          path: path(el),
          meta: meta.slice(0,400),
          text: t.slice(0,180),
          top: Math.round(r.top), left: Math.round(r.left),
          width: Math.round(r.width), height: Math.round(r.height)
        });
      }
    }

    scored.sort((a,b) => b.score - a.score || b.left - a.left || b.top - a.top);
    if (!scored.length) return null;
    const x = scored[0];
    return {
      path:x.path,
      score:String(x.score),
      meta:x.meta,
      text:x.text,
      top:String(x.top), left:String(x.left),
      width:String(x.width), height:String(x.height)
    };
    """
    try:
        result = driver.execute_script(js)
        if result:
            print(
                "Attachment icon target found -> "
                f"score={result.get('score')} pos=({result.get('left')},{result.get('top')}) "
                f"size={result.get('width')}x{result.get('height')} meta={result.get('meta','')[:160]}"
            )
        return result
    except Exception as exc:
        print(f"Attachment icon detection error: {exc}")
        return None


def resolve_target_url(driver, target: Dict[str, str]) -> Optional[str]:
    # Reuse V1 resolver first.
    direct = runner.resolve_target_url(driver, target)
    if runner.valid_attachment_url(direct, driver.current_url):
        return direct

    # Extra support for JS/postback controls where the clickable parent stores
    # the real endpoint in uncommon data attributes.
    js = r"""
    const path = arguments[0];
    function getEl(p){
      if(!p) return null;
      if(p.startsWith('id:')) return document.getElementById(p.slice(3));
      if(p.startsWith('css:')) return document.querySelector(p.slice(4));
      return null;
    }
    function abs(u){ try{return new URL(u,location.href).href}catch(e){return ''} }
    let el=getEl(path); if(!el) return '';
    el=el.closest('a,button,[onclick],[role="button"]')||el;
    const attrs=['href','src','data-url','data-href','data-src','data-file','data-pdf','data-download','formaction'];
    const nodes=[el,...Array.from(el.querySelectorAll?el.querySelectorAll('*'):[])];
    for(const n of nodes){
      for(const a of attrs){
        const raw=n.getAttribute&&n.getAttribute(a);
        if(raw && raw!=='#' && !/^javascript:/i.test(raw)){
          const u=abs(raw); if(/^https?:/i.test(u)) return u;
        }
      }
    }
    return '';
    """
    try:
        value = clean(driver.execute_script(js, target.get("path", "")))
        return value or None
    except Exception:
        return None


def extract_attachment_url_v2(driver, app_handle: str) -> Optional[str]:
    # Real direct PDF URLs, if present, still win.
    direct = runner.legacy.extract_pdf_from_current_page(driver)
    if runner.valid_attachment_url(direct):
        return clean(direct)

    # First try old semantic Attachment detector, then the icon-specific detector.
    target = runner.legacy.visible_attachment_target(driver, set())
    if not target:
        target = find_attachment_icon_target(driver)
    if not target:
        print("No attachment icon/control detected in opened message")
        return None

    before_url = driver.current_url
    href = resolve_target_url(driver, target)
    if runner.valid_attachment_url(href, before_url):
        print(f"Attachment URL resolved before click: {href}")
        return clean(href)

    before_tabs = set(driver.window_handles)
    if not runner.legacy.click_path(driver, target.get("path", "")):
        print("Attachment icon click failed")
        return None

    # Give EduSecure enough time to open the attachment endpoint/tab.
    time.sleep(1.4)
    after_tabs = set(driver.window_handles)
    new_tabs = list(after_tabs - before_tabs)

    if new_tabs:
        try:
            driver.switch_to.window(new_tabs[-1])
            time.sleep(1.0)
            current = clean(driver.current_url)
            nested = runner.legacy.extract_pdf_from_current_page(driver)
            candidate = clean(nested) if runner.valid_attachment_url(nested) else current
            result = candidate if runner.valid_attachment_url(candidate, before_url) else None
            print(f"Attachment opened in new tab: {result or current}")
            driver.close()
            driver.switch_to.window(app_handle)
            return result
        except WebDriverException:
            try:
                driver.switch_to.window(app_handle)
            except Exception:
                pass
            return None

    current = clean(driver.current_url)
    nested = runner.legacy.extract_pdf_from_current_page(driver)
    candidate = clean(nested) if runner.valid_attachment_url(nested) else current
    if runner.valid_attachment_url(candidate, before_url):
        result = candidate
        print(f"Attachment opened in current tab: {result}")
        try:
            driver.back()
            runner.legacy.wait_ready(driver)
            time.sleep(0.8)
        except Exception:
            pass
        return result

    print("Attachment icon was clicked but no new attachment URL was captured")
    return None


# Monkey-patch the V1 runner so its proven scan/Firestore logic stays unchanged,
# while attachment detection and title generation use the corrected behavior.
runner.extract_attachment_url = extract_attachment_url_v2
runner.legacy.make_title = make_message_title


if __name__ == "__main__":
    raise SystemExit(runner.main())
