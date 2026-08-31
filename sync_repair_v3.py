"""EduSecure attachment repair v3.

Exact browser flow:
1. Open the EduSecure message detail.
2. Click the visible `Attachment` link/control for real.
3. Capture the exact URL opened in the new tab/window.
4. Upload that URL to the 8aPDF ingest endpoint.

EduSecure homework attachments are not always `.pdf`; teachers also upload
JPG/JPEG/PNG study sheets.  Those are valid study attachments and must not be
dropped merely because the URL does not end in `.pdf`.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from selenium.common.exceptions import WebDriverException

import sync as base
import sync_repair as r1
import sync_repair_v2 as r2


STUDY_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp")


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_study_attachment_url(url: str | None) -> bool:
    """True only for a reusable EduSecure study attachment/download URL."""
    if not url:
        return False

    value = clean(url)
    low = value.lower()
    if not low.startswith(("http://", "https://")):
        return False

    parsed = urlparse(low)
    path = parsed.path.lower()

    # Real files teachers attach to homework/circulars.
    if any(path.endswith(ext) for ext in STUDY_EXTENSIONS):
        return True

    # EduSecure's normal attachment directory can contain extension-less files.
    if "/studentinfo/homework/" in path or "/studentinfo/attachment" in path:
        return True

    # Download handlers without .pdf suffix.
    if r1.looks_like_attachment_url(value):
        return True

    return False


def resolve_url(raw: str | None, current_url: str) -> str:
    if not raw:
        return ""
    value = clean(raw).strip("'\"")
    if not value or value.lower().startswith(("javascript:", "data:", "blob:")):
        return ""
    try:
        return urljoin(current_url, value)
    except Exception:
        return ""


def derive_message_title(text: str, attachment_url: str = "", number: int = 0) -> str:
    """Build title from the actual message, never from the word Attachment."""
    raw = clean(text)

    # School Diary: Home Work text is the clearest human title.
    match = re.search(
        r"Home\s*Work\s*:?\s*(.*?)(?=\s*Class\s*Work\s*:?|\s*Attachment\b|$)",
        raw,
        flags=re.I,
    )
    if match:
        title = clean(match.group(1))
        if title and title.lower() not in {"attachment", "attach", "download"}:
            return title[:140].rstrip(" -:,")

    # Message/Circular fallback: remove UI/type/date labels and retain message text.
    title = raw
    title = re.sub(r"\b(?:School\s*Diary|Circular|Announcement|Message)\b", " ", title, flags=re.I)
    title = re.sub(r"\b[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}\b", " ", title)
    title = re.sub(r"\b(?:Home\s*Work|Class\s*Work)\s*:?", " ", title, flags=re.I)
    title = re.sub(r"\b(?:Attachment|Attach|Download|Open|Click Here)\b", " ", title, flags=re.I)
    title = clean(title).strip(" -:,")

    if title and title.lower() != "attachment":
        if len(title) > 140:
            title = title[:140].rsplit(" ", 1)[0]
        return title

    # Last fallback is filename, never literal Attachment.
    try:
        fallback = base.title_from_pdf_url(attachment_url)
    except Exception:
        fallback = "Study Material"
    if clean(fallback).lower() == "attachment":
        fallback = "Study Material"
    return clean(fallback) or f"Study Material {number}".strip()


def direct_attachment_from_target(target: Dict[str, object], current_url: str) -> Optional[str]:
    values: List[str] = []

    for key in ("directStrings",):
        raw = target.get(key)
        if isinstance(raw, list):
            values.extend(str(x) for x in raw if x)

    for key in ("directUrl", "directPdf"):
        value = target.get(key)
        if value:
            values.append(str(value))

    # V2 stores the complete href in attrs/html too, but directStrings is preferred.
    for raw in values:
        candidate = resolve_url(raw, current_url)
        if candidate and is_study_attachment_url(candidate):
            return candidate

        for quoted in re.findall(
            r"['\"]([^'\"]+(?:\.pdf|\.jpe?g|\.png|\.webp|download[^'\"]*|attachment[^'\"]*|getfile[^'\"]*))['\"]",
            raw,
            flags=re.I,
        ):
            candidate = resolve_url(quoted, current_url)
            if candidate and is_study_attachment_url(candidate):
                return candidate

    return None


def click_attachment_and_capture_opened_url(
    driver,
    target: Dict[str, object],
    app_handle: str,
) -> Optional[str]:
    """Click Attachment, then copy the exact URL that Chrome opens."""
    before_url = driver.current_url
    before_tabs = set(driver.window_handles)
    fallback_direct = direct_attachment_from_target(target, before_url)

    r1.drain_performance_log(driver)

    path = str(target.get("path") or "")
    if not r2.real_click_control(driver, path):
        print("Attachment control click failed.")
        return fallback_direct

    deadline = time.time() + 8.0

    while time.time() < deadline:
        try:
            handles = set(driver.window_handles)
            new_tabs = list(handles - before_tabs)

            if new_tabs:
                # This is the exact user flow: Attachment -> new tab -> copy URL.
                driver.switch_to.window(new_tabs[-1])
                time.sleep(0.35)
                opened_url = clean(driver.current_url)
                print(f"Attachment opened in new tab: {opened_url}")

                if opened_url.startswith(("http://", "https://")) and is_study_attachment_url(opened_url):
                    try:
                        driver.close()
                    finally:
                        driver.switch_to.window(app_handle)
                    print(f"Copied opened attachment URL: {opened_url}")
                    return opened_url

                # If Chrome first opens a viewer/wrapper, inspect its page for the file.
                page_file = base.extract_pdf_from_current_page(driver)
                if page_file:
                    try:
                        driver.close()
                    finally:
                        driver.switch_to.window(app_handle)
                    print(f"Copied attachment URL from opened viewer: {page_file}")
                    return page_file

                driver.close()
                driver.switch_to.window(app_handle)

            # Some attachments navigate the same tab rather than target=_blank.
            driver.switch_to.window(app_handle)
            now_url = clean(driver.current_url)
            if now_url != before_url and is_study_attachment_url(now_url):
                print(f"Attachment opened in same tab: {now_url}")
                return now_url

            # Network fallback for download handlers.
            captured = r1.attachment_url_from_performance(driver)
            if captured and is_study_attachment_url(captured):
                print(f"Copied attachment URL from browser network: {captured}")
                return captured
        except WebDriverException:
            pass

        time.sleep(0.2)

    # EduSecure already exposes the same URL in the Attachment anchor href.
    if fallback_direct:
        print(f"New-tab URL was not readable; using exact Attachment href: {fallback_direct}")
        return fallback_direct

    return None


def repaired_extract_attachment(driver, app_handle: str) -> Optional[str]:
    targets = r2.find_attachment_targets_v2(driver)
    if not targets:
        targets = r1.find_attachment_targets(driver)

    # Only try controls that actually represent the Attachment link first.
    targets = sorted(
        targets,
        key=lambda item: (
            0 if "attachment" in clean(str(item.get("text") or item.get("labelText") or "")).lower() else 1,
            -int(item.get("score") or 0),
        ),
    )

    if not targets:
        # True PDFs can also be embedded without a visible Attachment control.
        direct = base.extract_pdf_from_current_page(driver)
        return direct if direct and is_study_attachment_url(direct) else None

    print(f"Attachment controls detected: {len(targets)}")

    for index, target in enumerate(targets, start=1):
        text = clean(str(target.get("text") or ""))
        print(f"Trying Attachment control {index}/{len(targets)}: {text[:120]!r}")

        url = click_attachment_and_capture_opened_url(driver, target, app_handle)
        if url:
            print(f"Real EduSecure attachment URL extracted: {url}")
            return url

    return None


# Patch the existing sync engine. Main flow, login, date filtering, duplicate
# handling and ingest POST stay unchanged.
base.make_driver = r1.make_driver_with_network_logs
base.is_pdf_url = is_study_attachment_url
base.get_pdf_url_from_attachment = click_attachment_and_capture_opened_url
base.extract_pdf_from_current_message_detail = repaired_extract_attachment
base.make_title = derive_message_title


if __name__ == "__main__":
    base.main()
