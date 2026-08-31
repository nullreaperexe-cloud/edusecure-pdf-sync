from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

import requests
from selenium.common.exceptions import WebDriverException

import runner
import runner_v2
import sync_repair as network


BAD_URL_RE = re.compile(r"(?:/ParentApp/morelinks\.aspx(?:\?|$)|/images/loader\.gif(?:\?|$))", re.I)
ORIGINAL_LIST = runner.list_firestore_materials
ORIGINAL_EXISTING_STATE = runner.existing_state


def clean(value: Any) -> str:
    return runner.clean(value)


def strict_pdf_url(url: Optional[str]) -> bool:
    value = clean(url)
    if not re.match(r"^https?://", value, re.I):
        return False
    if BAD_URL_RE.search(value):
        return False
    return value.lower().endswith(".pdf")


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
    materials = ORIGINAL_LIST(id_token)
    kept: List[Dict[str, Any]] = []
    removed = 0

    for item in materials:
        url = clean(item.get("pdf_url"))
        source = clean(item.get("source")).lower()
        obvious_intermediate = bool(BAD_URL_RE.search(url))
        edusecure_non_pdf = "edusecure" in source and bool(url) and not strict_pdf_url(url)

        if obvious_intermediate or edusecure_non_pdf:
            if firestore_delete_document(clean(item.get("_name")), id_token):
                removed += 1
                print(f"🧹 Removed invalid automation material: {url}")
                continue
        kept.append(item)

    if removed:
        print(f"Invalid automation materials cleaned: {removed}")
    return kept


class _StrictBoundaryDate(date):
    def __new__(cls, internal_date: date, displayed_date: date):
        obj = date.__new__(cls, internal_date.year, internal_date.month, internal_date.day)
        obj._displayed_date = displayed_date
        return obj

    def isoformat(self) -> str:
        return self._displayed_date.isoformat()


def strict_existing_state(materials: List[Dict[str, Any]]):
    _, _, next_order = ORIGINAL_EXISTING_STATE(materials)
    pdf_materials = [item for item in materials if strict_pdf_url(clean(item.get("pdf_url")))]
    urls, actual_latest, _ = ORIGINAL_EXISTING_STATE(pdf_materials)

    if actual_latest:
        print(f"Website latest real PDF date: {actual_latest.isoformat()}")
        print("Strict cutoff enabled: same-date and older EduSecure messages will be skipped.")
        effective = _StrictBoundaryDate(actual_latest + timedelta(days=1), actual_latest)
        return urls, effective, next_order

    return urls, None, next_order


def exact_attachment_candidates(driver) -> List[Dict[str, str]]:
    js = r"""
    function visible(el) {
      if (!el) return false;
      const s=getComputedStyle(el), r=el.getBoundingClientRect();
      return s.display!=='none' && s.visibility!=='hidden' &&
             r.width>4 && r.height>4 && r.bottom>0 && r.top<innerHeight;
    }
    function text(el) {
      return (el && (el.innerText||el.textContent||el.getAttribute('aria-label')||
        el.getAttribute('title')||el.getAttribute('alt')||'') || '')
        .replace(/\s+/g,' ').trim();
    }
    function meta(el) {
      if(!el || !el.getAttribute) return '';
      return [el.id,el.className,el.getAttribute('title'),el.getAttribute('aria-label'),
        el.getAttribute('alt'),el.getAttribute('src'),el.getAttribute('href'),
        el.getAttribute('onclick'),el.getAttribute('data-url'),el.getAttribute('data-href')]
        .filter(Boolean).join(' ').toLowerCase();
    }
    function path(el) {
      if(!el) return '';
      if(el.id) return 'id:'+el.id;
      const p=[]; let c=el;
      while(c && c!==document.body && c.nodeType===1){
        let n=1,s=c.previousElementSibling;
        while(s){if(s.tagName===c.tagName)n++;s=s.previousElementSibling;}
        p.unshift(c.tagName.toLowerCase()+':nth-of-type('+n+')'); c=c.parentElement;
      }
      return 'css:body > '+p.join(' > ');
    }

    const raw=Array.from(document.querySelectorAll(
      'a,button,[onclick],[role="button"],input[type="image"],img,svg,i,span,div'
    )).filter(visible);
    const out=[],seen=new Set();

    for(const node of raw){
      const el=node.closest&&node.closest('a,button,[onclick],[role="button"],input[type="image"],[tabindex]')||node;
      if(!visible(el)) continue;

      const t=(text(el)+' '+text(node)).toLowerCase();
      const m=meta(el)+' '+meta(node);
      const exact=/^(attachment|attachments)\s*[:：]?$/i.test(text(el))||/^(attachment|attachments)\s*[:：]?$/i.test(text(node));
      const evidence=/attachment|paperclip|attach_file|fa-paperclip/.test(m+' '+t);

      if(!exact && !evidence) continue;
      if(/morelinks|loader\.gif|dashboard|home|menu|logout|profile|search|logo/.test(m)) continue;

      const pth=path(el); if(!pth||seen.has(pth)) continue; seen.add(pth);
      const r=el.getBoundingClientRect();
      let score=0;
      if(exact) score+=400;
      if(/attachment/.test(m+' '+t)) score+=220;
      if(/paperclip|attach_file|fa-paperclip/.test(m)) score+=180;
      if(el.matches('a,button,[onclick],[role="button"],input[type="image"]')) score+=60;
      if(r.width<=180&&r.height<=120) score+=25;

      out.push({path:pth,score:String(score),text:text(el).slice(0,160),meta:m.slice(0,450),
        top:String(Math.round(r.top)),left:String(Math.round(r.left)),
        width:String(Math.round(r.width)),height:String(Math.round(r.height))});
    }
    return out.sort((a,b)=>Number(b.score)-Number(a.score)||Number(a.top)-Number(b.top)).slice(0,8);
    """
    try:
        return driver.execute_script(js) or []
    except Exception as exc:
        print(f"Attachment detector failed: {exc}")
        return []


def strict_pdf_from_dom(driver) -> Optional[str]:
    js = r"""
    function abs(u){try{return new URL(u,location.href).href}catch(e){return ''}}
    for(const el of document.querySelectorAll('a[href],iframe[src],frame[src],embed[src],object[data],[data-url],[data-href],[data-src]')){
      for(const a of ['href','src','data','data-url','data-href','data-src']){
        const raw=el.getAttribute&&el.getAttribute(a); if(!raw) continue;
        const u=abs(raw);
        if(/^https?:/i.test(u)&&u.toLowerCase().endsWith('.pdf')) return u;
      }
    }
    return '';
    """
    try:
        value=clean(driver.execute_script(js))
        return value if strict_pdf_url(value) else None
    except Exception:
        return None


def close_extra_tabs(driver, app_handle: str) -> None:
    try:
        for handle in list(driver.window_handles):
            if handle==app_handle:
                continue
            try:
                driver.switch_to.window(handle)
                driver.close()
            except Exception:
                pass
        driver.switch_to.window(app_handle)
    except Exception:
        pass


def click_attachment_and_wait_for_final_pdf(driver, app_handle: str, target: Dict[str, str]) -> Optional[str]:
    before_url=clean(driver.current_url)
    try:
        network.drain_performance_log(driver)
    except Exception:
        pass

    print(f"Clicking REAL Attachment control score={target.get('score')} text={target.get('text','')!r}")
    if not runner.legacy.click_path(driver,target.get('path','')):
        print('Attachment click failed')
        return None

    deadline=time.time()+18.0
    seen_urls:Set[str]=set()

    while time.time()<deadline:
        try:
            handles=list(driver.window_handles)
        except Exception:
            handles=[]

        for handle in handles:
            try:
                driver.switch_to.window(handle)
                current=clean(driver.current_url)
                if current and current not in seen_urls:
                    seen_urls.add(current)
                    if current!=before_url:
                        print(f"Attachment navigation observed: {current}")

                if strict_pdf_url(current):
                    final_url=current
                    print(f"✅ FINAL .pdf URL reached after Attachment click: {final_url}")
                    if handle!=app_handle:
                        try: driver.close()
                        except Exception: pass
                    try: driver.switch_to.window(app_handle)
                    except Exception: pass
                    return final_url

                dom_pdf=strict_pdf_from_dom(driver)
                if dom_pdf:
                    print(f"✅ FINAL .pdf URL found in post-click page: {dom_pdf}")
                    if handle!=app_handle:
                        try: driver.close()
                        except Exception: pass
                    try: driver.switch_to.window(app_handle)
                    except Exception: pass
                    return dom_pdf
            except WebDriverException:
                continue

        try:
            driver.switch_to.window(app_handle)
        except Exception:
            pass
        try:
            captured=network.attachment_url_from_performance(driver)
        except Exception:
            captured=None
        if strict_pdf_url(captured):
            final_url=clean(captured)
            print(f"✅ FINAL .pdf URL captured from post-click network: {final_url}")
            close_extra_tabs(driver,app_handle)
            return final_url

        time.sleep(.25)

    print('❌ Attachment clicked, but no FINAL URL ending in .pdf appeared. Nothing will be uploaded.')
    for value in sorted(seen_urls):
        if value!=before_url:
            print(f"  intermediate: {value}")
    close_extra_tabs(driver,app_handle)
    return None


def extract_attachment_url_v4(driver, app_handle: str) -> Optional[str]:
    # Never use a pre-click href or random page URL. Only a final .pdf observed
    # AFTER clicking the real Attachment/paperclip control can be uploaded.
    targets=exact_attachment_candidates(driver)
    if not targets:
        print('No real Attachment/paperclip control found in this message')
        return None

    print(f"Real Attachment controls detected: {len(targets)}")
    detail_url=clean(driver.current_url)

    for index,target in enumerate(targets,start=1):
        print(f"Trying real Attachment control {index}/{len(targets)}")
        result=click_attachment_and_wait_for_final_pdf(driver,app_handle,target)
        if result and strict_pdf_url(result):
            return result
        try:
            driver.switch_to.window(app_handle)
            if clean(driver.current_url)!=detail_url:
                driver.get(detail_url)
                runner.legacy.wait_ready(driver)
                time.sleep(.65)
        except Exception:
            pass
    return None


runner.list_firestore_materials=list_and_cleanup_materials
runner.existing_state=strict_existing_state
runner.extract_attachment_url=extract_attachment_url_v4
runner.legacy.make_title=runner_v2.make_message_title
runner.legacy.make_driver=network.make_driver_with_network_logs
runner.legacy.is_pdf_url=strict_pdf_url

if __name__=='__main__':
    raise SystemExit(runner.main())
