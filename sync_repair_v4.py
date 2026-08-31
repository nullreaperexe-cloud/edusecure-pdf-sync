"""EduSecure repair v4.

Adds two final guarantees on top of v3:
1) The visible Attachment row/label is only a click target; its text is never
   used as the uploaded PDF title.
2) Uploaded titles are derived from the actual EduSecure message/homework text.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import sync as base
import sync_repair as r1
import sync_repair_v2 as r2
import sync_repair_v3 as r3


ORIGINAL_MAKE_TITLE = base.make_title


def message_title_only(text: str, pdf_url: str, number: int = 0) -> str:
    """Build a clean title from the message, never from the Attachment label."""
    raw = base.clean_text(text)
    if not raw:
        return ORIGINAL_MAKE_TITLE(text, pdf_url, number)

    # Attachment is UI chrome, not content.
    cleaned = re.sub(r"\bAttachment\b", " ", raw, flags=re.I)
    cleaned = re.sub(r"\bDownload\b|\bOpen\b|\bView\b|\bClick Here\b", " ", cleaned, flags=re.I)
    cleaned = base.clean_text(cleaned)

    # School Diary cards: the Home Work body is the most meaningful title.
    homework = re.search(
        r"\bHome\s*Work\s*:\s*(.+?)(?=\s+Class\s*Work\s*:|\s+Homework\s*:|$)",
        cleaned,
        flags=re.I,
    )
    if homework:
        candidate = base.clean_text(homework.group(1))
        # Remove a leading subject label such as "FRENCH :" only when present.
        candidate = re.sub(r"^[A-Za-z][A-Za-z .&/-]{1,30}\s*:\s*", "", candidate).strip()
        if candidate and candidate.lower() not in {"attachment", "download", "pdf"}:
            if len(candidate) > 110:
                candidate = candidate[:110].rsplit(" ", 1)[0]
            return candidate

    # Message/Circular cards: strip message type + date, then keep the real body.
    candidate = cleaned
    candidate = re.sub(r"^\s*(School\s*Diary|Circular|Message|Announcement|Notice)\s*", "", candidate, flags=re.I)
    candidate = re.sub(
        r"^\s*(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})\s*",
        "",
        candidate,
    )
    candidate = re.sub(r"\bClass\s*Work\s*:.*$", "", candidate, flags=re.I)
    candidate = base.clean_text(candidate)

    if candidate and candidate.lower() not in {"attachment", "download", "pdf"}:
        if len(candidate) > 110:
            candidate = candidate[:110].rsplit(" ", 1)[0]
        return candidate

    fallback = ORIGINAL_MAKE_TITLE(cleaned, pdf_url, number)
    if fallback.strip().lower() in {"attachment", "download", "pdf", "pdf document"}:
        fallback = base.title_from_pdf_url(pdf_url)
    return fallback


def exact_attachment_rows(driver) -> List[Dict[str, str]]:
    """Return the exact visible Attachment text plus nearby ancestor rows.

    EduSecure's Attachment control may be a styled DIV with a delegated click
    handler rather than an <a>. Clicking the child still bubbles, but v4 also
    tries compact ancestors explicitly so the screenshot-style row is covered.
    """
    js = r"""
    function visible(el) {
      if (!el) return false;
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    }
    function textOf(el) {
      return (el && (el.innerText || el.textContent || el.getAttribute('aria-label') || el.title || '') || '')
        .replace(/\s+/g,' ').trim();
    }
    function makePath(el) {
      if (!el) return '';
      if (el.id) return 'id:' + el.id;
      const parts=[]; let cur=el;
      while (cur && cur.nodeType===Node.ELEMENT_NODE && cur!==document.body) {
        let i=1, sib=cur.previousElementSibling;
        while (sib) { if (sib.tagName===cur.tagName) i++; sib=sib.previousElementSibling; }
        parts.unshift(cur.tagName.toLowerCase()+':nth-of-type('+i+')');
        cur=cur.parentElement;
      }
      return 'css:body > ' + parts.join(' > ');
    }
    function strings(el) {
      const out=[];
      if (!el || !el.getAttribute) return out;
      for (const n of ['href','src','data','data-url','data-href','data-src','formaction','onclick']) {
        const v=el.getAttribute(n)||''; if (v) out.push(v);
      }
      return out;
    }

    const labels = Array.from(document.querySelectorAll('body *')).filter(visible).filter(el => {
      const t=textOf(el).toLowerCase();
      return t === 'attachment' || t === 'attachments' || /^attachment\s*[:：]?$/.test(t);
    });

    const out=[], seen=new Set();
    function add(el, relation, label) {
      if (!el || !visible(el)) return;
      const path=makePath(el); if (!path || seen.has(path)) return; seen.add(path);
      const r=el.getBoundingClientRect();
      // Avoid page-sized wrappers; the screenshot row is compact.
      if (r.height > Math.max(360, innerHeight*0.45)) return;
      out.push({
        path,
        text:textOf(el).slice(0,250),
        labelText:textOf(label).slice(0,250),
        relation,
        score: relation==='exact-label' ? 250 : 230 - (parseInt(relation.split('-').pop()||'0',10)*8),
        tag:el.tagName||'',
        attrs: JSON.stringify(Object.fromEntries(Array.from(el.attributes||[]).map(a=>[a.name,a.value||'']))).slice(0,1800),
        onclick:el.getAttribute && (el.getAttribute('onclick')||''),
        directStrings:strings(el),
        html:(el.outerHTML||'').replace(/\s+/g,' ').slice(0,2200),
        top:Math.round(r.top), left:Math.round(r.left)
      });
    }

    for (const label of labels) {
      add(label,'exact-label',label);
      let p=label.parentElement;
      for (let depth=1; depth<=5 && p; depth++,p=p.parentElement) {
        add(p,'attachment-row-'+depth,label);
      }
    }
    return out.sort((a,b)=>b.score-a.score || a.top-b.top || a.left-b.left).slice(0,12);
    """
    try:
        return driver.execute_script(js) or []
    except Exception:
        return []


def extract_attachment_v4(driver, app_handle: str) -> Optional[str]:
    direct = base.extract_pdf_from_current_page(driver)
    if direct:
        return direct

    # Screenshot-specific exact Attachment row first, then the broader v2 scan.
    targets = exact_attachment_rows(driver)
    broad = r2.find_attachment_targets_v2(driver)
    seen = {t.get("path") for t in targets}
    for target in broad:
        if target.get("path") not in seen:
            targets.append(target)
            seen.add(target.get("path"))

    if not targets:
        targets = r1.find_attachment_targets(driver)
    if not targets:
        print("No attachment control detected on message detail page.")
        return None

    detail_url = driver.current_url
    print(f"Attachment controls detected (v4): {len(targets)}")

    for i, target in enumerate(targets, start=1):
        print(
            f"Trying attachment control {i}/{len(targets)} "
            f"relation={target.get('relation','')} score={target.get('score','')}: "
            f"{target.get('text','')[:120]!r}"
        )
        url = r3.repaired_get_pdf_url_from_attachment_v3(driver, target, app_handle)
        if url:
            print(f"Real PDF/download URL extracted: {url}")
            return url

        try:
            driver.switch_to.window(app_handle)
            if driver.current_url != detail_url:
                driver.get(detail_url)
                base.wait_ready(driver)
        except Exception:
            pass

    return None


# Apply all final overrides before running the original main flow.
base.make_driver = r1.make_driver_with_network_logs
base.is_pdf_url = r1.repaired_is_pdf_url
base.make_title = message_title_only
base.get_pdf_url_from_attachment = r3.repaired_get_pdf_url_from_attachment_v3
base.extract_pdf_from_current_message_detail = extract_attachment_v4


if __name__ == "__main__":
    base.main()
