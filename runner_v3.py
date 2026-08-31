from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional

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


def firestore_delete_document(document_name: str, id_token: str) -> bool:
    if not document_name:
        return False
    url = f"https://firestore.googleapis.com/v1/{document_name}"
    response = requests.delete(
        url,
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
        source = clean(item.get("source"))
        is_our_bad_upload = source == "EduSecure GitHub Sync" and BAD_URL_RE.search(url)
        if is_our_bad_upload:
            if firestore_delete_document(clean(item.get("_name")), id_token):
                removed += 1
                print(f"🧹 Removed invalid previous upload: {url}")
                continue
        kept.append(item)

    if removed:
        print(f"Invalid Firestore uploads cleaned: {removed}")
    return kept


def existing_state_with_rescan(materials: List[Dict[str, Any]]):
    urls, latest, next_order = ORIGINAL_EXISTING_STATE(materials)
    # Always re-scan the previous 2 days too. This catches a late/missed PDF
    # (e.g. Aug 29) even when a newer Aug 31 PDF is already on the website.
    if latest:
        latest = latest - timedelta(days=2)
        print(f"Rescan safety window starts: {latest.isoformat()}")
    return urls, latest, next_order


def obvious_bad_url(url: str) -> bool:
    low = clean(url).lower()
    if not low:
        return True
    if BAD_URL_RE.search(low):
        return True
    if re.search(r"\.(?:gif|png|jpe?g|webp|svg|css|js)(?:\?|$)", low, re.I):
        return True
    if any(x in low for x in ("dashboard.aspx", "/login", "morelinks.aspx")):
        return True
    return False


def verify_pdf_url(driver, url: Optional[str]) -> Optional[str]:
    """Accept only a URL that really serves a PDF/download, not a nav page/icon."""
    if not url:
        return None
    url = clean(url)
    if not re.match(r"^https?://", url, re.I) or obvious_bad_url(url):
        return None

    if re.search(r"\.pdf(?:\?|$)", url, re.I):
        return url

    session = requests.Session()
    try:
        for cookie in driver.get_cookies():
            try:
                session.cookies.set(
                    cookie.get("name"), cookie.get("value"),
                    domain=cookie.get("domain"), path=cookie.get("path") or "/"
                )
            except Exception:
                pass

        try:
            ua = driver.execute_script("return navigator.userAgent") or "Mozilla/5.0"
        except Exception:
            ua = "Mozilla/5.0"

        response = session.get(
            url,
            headers={"User-Agent": ua, "Referer": driver.current_url},
            allow_redirects=True,
            stream=True,
            timeout=20,
        )
        content_type = (response.headers.get("Content-Type") or "").lower()
        disposition = (response.headers.get("Content-Disposition") or "").lower()
        final_url = clean(response.url)
        ok = (
            "application/pdf" in content_type
            or ".pdf" in disposition
            or re.search(r"\.pdf(?:\?|$)", final_url, re.I)
        )
        response.close()
        if ok and not obvious_bad_url(final_url):
            print(f"Verified real PDF URL: {final_url} | {content_type or disposition}")
            return final_url
        print(f"Rejected non-PDF URL: {url} | content-type={content_type or 'unknown'}")
    except Exception as exc:
        print(f"PDF verification failed for {url}: {str(exc)[:140]}")
    finally:
        session.close()
    return None


def message_attachment_candidates(driver) -> List[Dict[str, str]]:
    """Find small icon controls inside the actual opened message card only."""
    js = r"""
    function visible(el) {
      if (!el) return false;
      const st=getComputedStyle(el), r=el.getBoundingClientRect();
      return st.display!=='none' && st.visibility!=='hidden' && r.width>5 && r.height>5 && r.bottom>0 && r.top<innerHeight;
    }
    function txt(el) {
      return (el && (el.innerText||el.textContent||el.getAttribute('aria-label')||el.getAttribute('title')||el.getAttribute('alt')||'') || '')
        .replace(/\s+/g,' ').trim();
    }
    function meta(el) {
      if(!el || !el.getAttribute) return '';
      return [el.id,el.className,el.getAttribute('title'),el.getAttribute('aria-label'),el.getAttribute('alt'),el.getAttribute('src'),el.getAttribute('href'),el.getAttribute('onclick'),el.getAttribute('data-url'),el.getAttribute('data-href')]
        .filter(Boolean).join(' ').toLowerCase();
    }
    function path(el) {
      if(el.id) return 'id:'+el.id;
      const parts=[]; let cur=el;
      while(cur && cur!==document.body && cur.nodeType===1){
        let n=1,s=cur.previousElementSibling; while(s){if(s.tagName===cur.tagName)n++;s=s.previousElementSibling;}
        parts.unshift(cur.tagName.toLowerCase()+':nth-of-type('+n+')'); cur=cur.parentElement;
      }
      return 'css:body > '+parts.join(' > ');
    }
    function clickable(el) {
      return el.closest && el.closest('a,button,[onclick],[role="button"],input[type="image"],[tabindex]') || el;
    }

    const seeds=Array.from(document.querySelectorAll('body *')).filter(visible).filter(el=>{
      const t=txt(el);
      return /Home\s*Work\s*:|Class\s*Work\s*:|Dear\s+Parents|Dear\s+Parent|PFA|PDF\s+attached|attached\s+below/i.test(t) && t.length>=25 && t.length<=2600;
    });

    const roots=[]; const rootSeen=new Set();
    for(const seed of seeds){
      let p=seed;
      for(let d=0;d<7 && p;d++,p=p.parentElement){
        if(!visible(p)) continue;
        const r=p.getBoundingClientRect(), t=txt(p);
        if(t.length<25 || t.length>3400 || r.width<260 || r.height<55 || r.height>800) continue;
        const key=path(p); if(rootSeen.has(key)) continue; rootSeen.add(key);
        roots.push({el:p, area:r.width*r.height, textLen:t.length});
      }
    }
    roots.sort((a,b)=>a.area-b.area || a.textLen-b.textLen);

    const out=[]; const seen=new Set();
    for(const rootInfo of roots.slice(0,12)){
      const root=rootInfo.el, rr=root.getBoundingClientRect(), rt=txt(root).toLowerCase();
      const nodes=Array.from(root.querySelectorAll('a,button,[onclick],[role="button"],input[type="image"],img,svg,i,span,div')).filter(visible);
      for(const node of nodes){
        const el=clickable(node); if(!visible(el)) continue;
        const pth=path(el); if(seen.has(pth)) continue;
        const r=el.getBoundingClientRect();
        const m=(meta(el)+' '+meta(node)+' '+txt(el).toLowerCase());
        if(/morelinks|loader\.gif|dashboard|home|menu|logout|profile|search|logo/.test(m)) continue;
        if(r.top < rr.top-4 || r.bottom > rr.bottom+4 || r.left < rr.left-4 || r.right > rr.right+4) continue;

        let score=0;
        if(/attach|paperclip|pdf|document|file|download/.test(m)) score+=140;
        if(el.matches('a,button,[onclick],[role="button"],input[type="image"]')) score+=35;
        if(node.matches('img,svg,i,input[type="image"]')) score+=30;
        if(el.querySelector && el.querySelector('img,svg,i')) score+=22;
        if(r.width<=110 && r.height<=110) score+=25;
        if((r.left+r.width/2) > rr.left + rr.width*.55) score+=22;
        if((r.top+r.height/2) > rr.top + rr.height*.42) score+=18;
        if((r.left+r.width/2) > rr.left + rr.width*.72) score+=15;
        if(/attached|attachment|pdf|worksheet|unseen passage/.test(rt)) score+=12;
        if(txt(el).length>80) score-=45;
        if(r.width>180 || r.height>140) score-=50;

        if(score>=65){
          seen.add(pth);
          out.push({path:pth,score:String(score),text:txt(el).slice(0,140),meta:m.slice(0,350),top:String(Math.round(r.top)),left:String(Math.round(r.left)),width:String(Math.round(r.width)),height:String(Math.round(r.height))});
        }
      }
    }
    return out.sort((a,b)=>Number(b.score)-Number(a.score) || Number(b.left)-Number(a.left) || Number(b.top)-Number(a.top)).slice(0,15);
    """
    try:
        return driver.execute_script(js) or []
    except Exception as exc:
        print(f"Message-card attachment scan failed: {exc}")
        return []


def try_candidate(driver, app_handle: str, target: Dict[str, str]) -> Optional[str]:
    before_url = clean(driver.current_url)
    before_tabs = set(driver.window_handles)

    try:
        network.drain_performance_log(driver)
    except Exception:
        pass

    print(
        f"Clicking candidate score={target.get('score')} pos=({target.get('left')},{target.get('top')}) "
        f"size={target.get('width')}x{target.get('height')} meta={target.get('meta','')[:160]}"
    )

    if not runner.legacy.click_path(driver, target.get("path", "")):
        return None
    time.sleep(1.25)

    new_tabs = list(set(driver.window_handles) - before_tabs)
    if new_tabs:
        try:
            driver.switch_to.window(new_tabs[-1])
            time.sleep(0.6)
            candidates = [clean(driver.current_url), runner.legacy.extract_pdf_from_current_page(driver)]
            for candidate in candidates:
                verified = verify_pdf_url(driver, candidate)
                if verified:
                    driver.close()
                    driver.switch_to.window(app_handle)
                    return verified
            driver.close()
            driver.switch_to.window(app_handle)
        except WebDriverException:
            try:
                driver.switch_to.window(app_handle)
            except Exception:
                pass

    try:
        driver.switch_to.window(app_handle)
    except Exception:
        pass

    now_url = clean(driver.current_url)
    if now_url != before_url:
        verified = verify_pdf_url(driver, now_url)
        if verified:
            return verified
        try:
            driver.back()
            runner.legacy.wait_ready(driver)
            time.sleep(0.6)
        except Exception:
            pass

    try:
        captured = network.attachment_url_from_performance(driver)
    except Exception:
        captured = None
    verified = verify_pdf_url(driver, captured)
    if verified:
        return verified
    return None


def extract_attachment_url_v3(driver, app_handle: str) -> Optional[str]:
    direct = verify_pdf_url(driver, runner.legacy.extract_pdf_from_current_page(driver))
    if direct:
        return direct

    targets = message_attachment_candidates(driver)
    if not targets:
        print("No attachment icon found inside the actual message card")
        return None

    print(f"Message-card attachment candidates: {len(targets)}")
    for target in targets:
        result = try_candidate(driver, app_handle, target)
        if result:
            print(f"✅ Real attachment URL captured after icon click: {result}")
            return result
    print("No candidate produced a verified PDF URL")
    return None


# Keep the message-title rule from v2, but replace the unsafe global icon finder.
runner.list_firestore_materials = list_and_cleanup_materials
runner.existing_state = existing_state_with_rescan
runner.extract_attachment_url = extract_attachment_url_v3
runner.legacy.make_title = runner_v2.make_message_title
runner.legacy.make_driver = network.make_driver_with_network_logs


if __name__ == "__main__":
    raise SystemExit(runner.main())
