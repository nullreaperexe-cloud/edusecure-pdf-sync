#!/usr/bin/env python3
"""Sync PDF links from EduSecure ParentApp into the 8aPDF Firebase ingest endpoint.

All credentials are read from environment variables. No passwords or tokens belong
in this file.
"""
import json
import os
import re
import sys
from urllib.parse import urljoin

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
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "25"))


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def login(session):
    username = os.environ["EDUSECURE_USERNAME"]
    password = os.environ["EDUSECURE_PASSWORD"]
    response = session.get(LOGIN_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("EduSecure login form was not found")

    fields = {}
    for inp in form.select("input[name]"):
        typ = (inp.get("type") or "text").lower()
        if typ in {"hidden", "submit"}:
            fields[inp["name"]] = inp.get("value", "")
    password_input = form.select_one('input[type="password"]')
    if not password_input or not password_input.get("name"):
        raise RuntimeError("EduSecure password field was not found")
    fields[password_input["name"]] = password

    text_inputs = [
        x for x in form.select('input[type="text"], input:not([type])')
        if x.get("name") and x.get("name") != password_input.get("name")
    ]
    if not text_inputs:
        raise RuntimeError("EduSecure username field was not found")
    fields[text_inputs[0]["name"]] = username

    submit = form.select_one('input[type="submit"], button[type="submit"]')
    if submit and submit.get("name"):
        fields[submit["name"]] = submit.get("value", submit.get_text(" ", strip=True))
    result = session.post(response.url, data=fields, timeout=30, allow_redirects=True)
    result.raise_for_status()
    if "login.aspx" in result.url.lower() or 'type="password"' in result.text.lower():
        raise RuntimeError("EduSecure login failed; check the GitHub Secrets")


def extract_items(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for anchor in soup.select("a[href], a[onclick]"):
        href = anchor.get("href", "")
        onclick = anchor.get("onclick", "")
        raw = href + " " + onclick
        matches = re.findall(r"""(?:https?://|\.\./|/)[^\s"'<>]+?\.pdf(?:\?[^\s"'<>]*)?""", raw, re.I)
        for match in matches:
            link = urljoin(page_url, match.rstrip(");,"))
            title = clean(anchor.get_text(" ", strip=True))
            if not title:
                title = clean(soup.title.get_text(" ", strip=True) if soup.title else "Study PDF")
            found.setdefault(link, {
                "title": title[:250] or "Study PDF",
                "subject": "School Material",
                "description": title[:500] or "School study material",
                "pdf_url": link,
            })
    return found


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": "8aPDF-EduSecure-Sync/1.0"})
    login(session)
    items = {}
    for page in PAGES:
        try:
            response = session.get(page, timeout=30)
            response.raise_for_status()
            items.update(extract_items(response.text, response.url))
        except requests.RequestException as exc:
            print(f"Warning: could not scan {page}: {exc}", file=sys.stderr)

    payload = list(items.values())[:MAX_ITEMS]
    if not payload:
        print("No PDF links found.")
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
