#!/usr/bin/env python3
"""Incrementally sync newly posted EduSecure PDFs to 8aPDF."""
import json
import os
import re
import sys
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = os.getenv("EDUSECURE_BASE_URL", "https://edusecure.org/ManavMangal88/ParentApp/").rstrip("/") + "/"
LOGIN_URL = urljoin(BASE_URL, "login.aspx")
PAGES = (
    urljoin(BASE_URL, "Dashboard.aspx"),
    urljoin(BASE_URL, "Announcement.aspx?Type=Announcement"),
    urljoin(BASE_URL, "Announcement.aspx?Type=Circular"),
)
INGEST_URL = os.getenv(
    "INGEST_URL",
    "https://eightapdf-study-library.nullreaper-exe.chatgpt.site/automation/ingest",
)
# Safety default: the existing library is current through 30 August 2026.
# Change SYNC_AFTER only when the library's latest date is intentionally advanced.
SYNC_AFTER = date.fromisoformat(os.getenv("SYNC_AFTER", "2026-08-30"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "25"))
DATE_PATTERNS = (
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%d/%b/%Y", "%d/%B/%Y", "%b %d, %Y", "%B %d, %Y",
)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(value):
    value = clean(value).replace(",", "")
    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(value.replace(",", ""), pattern.replace(",", "")).date()
        except ValueError:
            continue
    return None


def find_posted_date(container):
    text = clean(container.get_text(" ", strip=True))
    candidates = re.findall(
        r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|"
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
        text,
        re.I,
    )
    for candidate in candidates:
        parsed = parse_date(candidate)
        if parsed:
            return parsed
    return None


def login(session):
    response = session.get(LOGIN_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("EduSecure login form was not found")
    password_input = form.select_one('input[type="password"]')
    if not password_input or not password_input.get("name"):
        raise RuntimeError("EduSecure password field was not found")
    fields = {}
    for inp in form.select("input[name]"):
        typ = (inp.get("type") or "text").lower()
        if typ in {"hidden", "submit"}:
            fields[inp["name"]] = inp.get("value", "")
    fields[password_input["name"]] = os.environ["EDUSECURE_PASSWORD"]
    username_inputs = [
        x for x in form.select('input[type="text"], input:not([type])')
        if x.get("name") and x.get("name") != password_input.get("name")
    ]
    if not username_inputs:
        raise RuntimeError("EduSecure username field was not found")
    fields[username_inputs[0]["name"]] = os.environ["EDUSECURE_USERNAME"]
    submit = form.select_one('input[type="submit"], button[type="submit"]')
    if submit and submit.get("name"):
        fields[submit["name"]] = submit.get("value", submit.get_text(" ", strip=True))
    result = session.post(response.url, data=fields, timeout=30, allow_redirects=True)
    result.raise_for_status()
    if "login.aspx" in result.url.lower() or 'type="password"' in result.text.lower():
        raise RuntimeError("EduSecure login failed; check the GitHub Secrets")


def title_from_container(anchor, container, fallback_subject):
    if container.name == "tr":
        cells = []
        for cell in container.select("th, td"):
            text = clean(cell.get_text(" ", strip=True))
            if text and not re.search(r"attachment|download|\.pdf\b", text, re.I):
                cells.append(text)
        if cells:
            return clean(" — ".join(cells))[:250]
    text = clean(container.get_text(" ", strip=True))
    text = re.sub(r"\battachment\b|\bdownload\b", " ", text, flags=re.I)
    text = re.sub(r"\S+\.pdf(?:\?\S*)?", " ", text, flags=re.I)
    text = clean(text)
    return (text or fallback_subject or "Study PDF")[:250]


def extract_items(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    query = parse_qs(urlparse(page_url).query)
    fallback_subject = (query.get("Type", ["School Material"])[0] or "School Material").title()
    found = {}
    for anchor in soup.select("a[href], a[onclick]"):
        raw = (anchor.get("href", "") or "") + " " + (anchor.get("onclick", "") or "")
        matches = re.findall(
            r"""(?:https?://|\.\./|/)[^\s"'<>]+?\.pdf(?:\?[^\s"'<>]*)?""",
            raw,
            re.I,
        )
        if not matches:
            continue
        container = anchor.find_parent("tr") or anchor.find_parent("li") or anchor.parent
        posted = find_posted_date(container)
        # Never import undated/old rows: this prevents a first run from filling the site.
        if not posted or posted <= SYNC_AFTER:
            continue
        title = title_from_container(anchor, container, fallback_subject)
        for match in matches:
            link = urljoin(page_url, match.rstrip(");,"))
            found.setdefault(link, {
                "title": title,
                "subject": fallback_subject,
                "description": title,
                "pdf_url": link,
            })
    return found


def discover_detail_links(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href", "")
        if re.search(r"Announcement\\.aspx\\?[^\\s"']*Id=\\d+", href, re.I):
            links.add(urljoin(page_url, href))
    return links


def scan_page(session, page, items):
    response = session.get(page, timeout=30)
    response.raise_for_status()
    items.update(extract_items(response.text, response.url))
    # EduSecure exposes attachments only after opening each announcement detail page.
    for detail_url in list(discover_detail_links(response.text, response.url))[:75]:
        try:
            detail = session.get(detail_url, timeout=30)
            detail.raise_for_status()
            items.update(extract_items(detail.text, detail.url))
        except requests.RequestException as exc:
            print(f"Warning: could not scan detail {detail_url}: {exc}", file=sys.stderr)


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "8aPDF-EduSecure-Sync/1.1"})
    login(session)
    items = {}
    for page in PAGES:
        try:
            scan_page(session, page, items)
        except requests.RequestException as exc:
            print(f"Warning: could not scan {page}: {exc}", file=sys.stderr)

    payload = list(items.values())[:MAX_ITEMS]
    if not payload:
        print(f"No new PDF links found after {SYNC_AFTER.isoformat()}.")
        return

    token = os.environ["AUTOMATION_TOKEN"]
    response = requests.post(
        INGEST_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
