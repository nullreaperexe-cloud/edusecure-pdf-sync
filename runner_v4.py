from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from selenium.common.exceptions import WebDriverException

import runner
import runner_v2
import runner_v3
import sync_repair as network


def clean(value: Any) -> str:
    return runner.clean(value)


def attachment_rows(driver) -> List[Dict[str, str]]:
    js = r"""
    function visible(el){if(!el)return false;const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>20&&r.height>10&&r.bottom>0&&r.top<innerHeight;}
    function text(el){return (el&&(el.innerText||el.textContent||'')||'').replace(/\s+/g,' ').trim();}
    function path(el){if(el.id)return 'id:'+el.id;const p=[];let c=el;while(c&&c!==document.body&&c.nodeType===1){let n=1,s=c.previousElementSibling;while(s){if(s.tagName===c.tagName)n++;s=s.previousElementSibling;}p.unshift(c.tagName.toLowerCase()+':nth-of-type('+n+')');c=c.parentElement;}return 'css:body > '+p.join(' > ');}
    const all=Array.from(document.querySelectorAll('body *')).filter(visible);
    const rows=all.filter(el=>{
      const t=text(el).toLowerCase();
      const id=(el.id||'').toLowerCase();
      const cls=String(el.className||'').toLowerCase();
      return (t==='attachment'||t==='attachments'||/^attachment\s*[:：]?$/.test(t)||cls.split(/\s+/).includes('attach')||/divpanel/.test(id)&&/attach/.test(cls+' '+t));
    });
    return rows.map(el=>{const r=el.getBoundingClientRect();return {path:path(el),text:text(el).slice(0,200),id:el.id||'',cls:String(el.className||''),top:String(Math.round(r.top)),left:String(Math.round(r.left)),width:String(Math.round(r.width)),height:String(Math.round(r.height)),html:(el.outerHTML||'').replace(/\s+/g,' ').slice(0,1800)};}).filter(x=>Number(x.height)<=180).sort((a,b)=>Number(a.top)-Number(b.top)).slice(0,12);
    """
    try:
        return driver.execute_script(js) or []
    except Exception:
        return []


def click_right_icon(driver, row: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Click the tiny right-side icon inside/next to EduSecure's Attachment row."""
    js = r"""
    const path=arguments[0];
    function getEl(p){if(!p)return null;if(p.startsWith('id:'))return document.getElementById(p.slice(3));if(p.startsWith('css:'))return document.querySelector(p.slice(4));return null;}
    function visible(el){if(!el)return false;const s=getComputedStyle(el),r=el.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&r.width>3&&r.height>3;}
    function info(el){if(!el)return '';return [el.tagName,el.id,el.className,el.getAttribute&&el.getAttribute('title'),el.getAttribute&&el.getAttribute('aria-label'),el.getAttribute&&el.getAttribute('alt'),el.getAttribute&&el.getAttribute('src'),el.getAttribute&&el.getAttribute('href'),el.getAttribute&&el.getAttribute('onclick')].filter(Boolean).join(' ').replace(/\s+/g,' ').slice(0,700);}
    const row=getEl(path);if(!row||!visible(row))return null;
    const rr=row.getBoundingClientRect();
    let parent=row.parentElement||row;
    const selector='a,button,input,img,svg,i,[onclick],[role="button"],span';
    const nodes=Array.from(parent.querySelectorAll(selector)).filter(visible).filter(el=>el!==row);
    const cand=[];
    for(const node of nodes){
      const r=node.getBoundingClientRect();
      const cy=r.top+r.height/2, ry=rr.top+rr.height/2;
      if(Math.abs(cy-ry)>Math.max(55,rr.height*2.5))continue;
      if(r.right<rr.left-30||r.left>rr.right+110)continue;
      const m=info(node).toLowerCase();
      if(/morelinks|loader\.gif|dashboard|home|menu|logout|profile|search|logo/.test(m))continue;
      let score=0;
      if(/attach|paperclip|pdf|document|file|download/.test(m))score+=100;
      if(node.matches('a,button,input,[onclick],[role="button"]'))score+=45;
      if(node.matches('img,svg,i,input[type="image"]'))score+=35;
      if(r.width<=90&&r.height<=90)score+=20;
      if((r.left+r.width/2)>rr.left+rr.width*.65)score+=35;
      if((r.left+r.width/2)>rr.left+rr.width*.82)score+=25;
      if(r.width>180||r.height>120)score-=60;
      cand.push({node,score,r,meta:info(node)});
    }
    cand.sort((a,b)=>b.score-a.score||(b.r.left+b.r.width/2)-(a.r.left+a.r.width/2));
    let target=cand.length&&cand[0].score>=40?cand[0].node:null;

    if(!target){
      const y=Math.max(1,Math.min(innerHeight-2,rr.top+rr.height/2));
      const xs=[rr.right-8,rr.right-22,rr.right-40,rr.right+10,rr.right+24].map(x=>Math.max(1,Math.min(innerWidth-2,x)));
      for(const x of xs){
        let e=document.elementFromPoint(x,y);
        if(!e)continue;
        const t=e.closest&&e.closest('a,button,input,[onclick],[role="button"]');
        target=t||e;
        const m=info(target).toLowerCase();
        if(/morelinks|dashboard|home|menu|logout|profile|search|logo/.test(m)){target=null;continue;}
        if(target)break;
      }
    }
    if(!target)return null;
    const tr=target.getBoundingClientRect();
    target.scrollIntoView({block:'center',inline:'center'});
    try{target.click();}catch(e){target.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));}
    return {meta:info(target),left:String(Math.round(tr.left)),top:String(Math.round(tr.top)),width:String(Math.round(tr.width)),height:String(Math.round(tr.height))};
    """
    try:
        return driver.execute_script(js, row.get("path", ""))
    except Exception as exc:
        print(f"Right attachment icon click error: {exc}")
        return None


def capture_after_row_icon_click(driver, app_handle: str, row: Dict[str, str]) -> Optional[str]:
    before_url = clean(driver.current_url)
    before_tabs = set(driver.window_handles)
    try:
        network.drain_performance_log(driver)
    except Exception:
        pass

    clicked = click_right_icon(driver, row)
    if not clicked:
        print("Could not find/click right-side icon for Attachment row")
        return None
    print(
        f"Clicked Attachment right icon pos=({clicked.get('left')},{clicked.get('top')}) "
        f"size={clicked.get('width')}x{clicked.get('height')} meta={clicked.get('meta','')[:220]}"
    )
    time.sleep(1.4)

    new_tabs = list(set(driver.window_handles) - before_tabs)
    if new_tabs:
        try:
            driver.switch_to.window(new_tabs[-1])
            time.sleep(0.7)
            for candidate in (clean(driver.current_url), runner.legacy.extract_pdf_from_current_page(driver)):
                verified = runner_v3.verify_pdf_url(driver, candidate)
                if verified:
                    driver.close(); driver.switch_to.window(app_handle)
                    return verified
            driver.close(); driver.switch_to.window(app_handle)
        except WebDriverException:
            try: driver.switch_to.window(app_handle)
            except Exception: pass

    try: driver.switch_to.window(app_handle)
    except Exception: pass
    now_url=clean(driver.current_url)
    if now_url!=before_url:
        verified=runner_v3.verify_pdf_url(driver,now_url)
        if verified:return verified
        try:
            driver.back(); runner.legacy.wait_ready(driver); time.sleep(.7)
        except Exception:pass

    try: captured=network.attachment_url_from_performance(driver)
    except Exception: captured=None
    return runner_v3.verify_pdf_url(driver,captured)


def extract_attachment_url_v4(driver, app_handle: str) -> Optional[str]:
    direct=runner_v3.verify_pdf_url(driver,runner.legacy.extract_pdf_from_current_page(driver))
    if direct:return direct

    rows=attachment_rows(driver)
    if rows:
        print(f"Exact Attachment rows found: {len(rows)}")
        for row in rows:
            print(f"Attachment row: id={row.get('id')} class={row.get('cls')} rect=({row.get('left')},{row.get('top')},{row.get('width')}x{row.get('height')})")
            result=capture_after_row_icon_click(driver,app_handle,row)
            if result:
                print(f"✅ Verified PDF captured from right-side Attachment icon: {result}")
                return result

    # Safe fallback: v3 message-card candidates are allowed, but every URL still
    # has to pass real PDF verification, so nav pages/images cannot upload.
    return runner_v3.extract_attachment_url_v3(driver,app_handle)


# Preserve v3 cleanup + rescan + verified URL rules and v2 message titles.
runner.list_firestore_materials=runner_v3.list_and_cleanup_materials
runner.existing_state=runner_v3.existing_state_with_rescan
runner.extract_attachment_url=extract_attachment_url_v4
runner.legacy.make_title=runner_v2.make_message_title
runner.legacy.make_driver=network.make_driver_with_network_logs

if __name__=='__main__':
    raise SystemExit(runner.main())
