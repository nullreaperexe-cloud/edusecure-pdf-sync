from __future__ import annotations

import os
import re
from typing import Any, Dict

import requests

FIREBASE_PROJECT_ID = "academyvault-5d1eb"
FIREBASE_API_KEY = "AIzaSyATxKki6gkNWic_CnoGbZnOZjAUj1lbKGI"
FIREBASE_ADMIN_EMAIL = os.environ.get("FIREBASE_ADMIN_EMAIL") or "nullreaper.exe@gmail.com"
FIREBASE_ADMIN_PASSWORD = os.environ.get("FIREBASE_ADMIN_PASSWORD", "")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_subject(value: Any) -> str:
    subject = clean(value)
    if subject.lower() in {"ai", "artificial intelligence"}:
        return "Computer"
    return subject


SCHOOL_PATTERNS = (
    r"\bManav\s+Mangal\s+SMART\s+SCHOOL(?:\s*-?\s*88)?\b",
    r"\bManav\s+Mangal\s+Smart\s+School(?:\s*-?\s*88)?\b",
    r"\bSMART\s+SCHOOL(?:\s*-?\s*88)?\b",
)

UI_JUNK_PATTERNS = (
    r"\bPay\s*Now\b",
    r"\bPDF\s*Material\b",
    r"\bPreview\b",
    r"\bDownload(?:\s*PDF)?\b",
    r"\bAttachment(?:s)?\b",
    r"\bClick\s+Here\b",
    r"\bOpen\b",
    r"\bView\b",
    r"\bSchool\s*Diary\b",
)

GREETING_PATTERNS = (
    r"\bDear\s+Parents?\b",
    r"\bDear\s+Students?\b",
    r"\bDear\s+All\b",
    r"\bGood\s+Morning\b",
    r"\bGood\s+Afternoon\b",
    r"\bGood\s+Evening\b",
    r"\bKindly\b",
    r"\bPlease\b",
    r"\bPFA\s+of\b",
    r"\bPFA\b",
    r"\bThanks\b",
    r"\bThank\s+You\b",
)


def _remove_subject_from_title(text: str, subject: str) -> str:
    subject = normalize_subject(subject)
    if not subject or subject.lower() in {"circular", "general", "life skills"}:
        return text

    aliases = {subject}
    if subject == "Mathematics":
        aliases.update({"Math", "Maths"})
    elif subject == "Computer":
        aliases.update({"Computer Science", "ICT", "AI", "Artificial Intelligence"})
    elif subject == "Social Science":
        aliases.update({"SST", "Social Studies"})

    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        text = re.sub(rf"^\s*(?:Subject\s*[:\-]\s*)?{escaped}\s*[:\-–—|]+\s*", "", text, flags=re.I)
        text = re.sub(rf"^\s*{escaped}\s+", "", text, flags=re.I)
        text = re.sub(rf"\bSubject\s*[:\-]\s*{escaped}\b", "", text, flags=re.I)
    return text


def sanitize_title(value: Any, subject: Any = "") -> str:
    text = clean(value)
    if not text:
        return "Study Material"

    # School branding must never appear in a study-material title.
    for pattern in SCHOOL_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)

    # Remove school/internal code when it is used as circular metadata.
    text = re.sub(r"\bMMSS\s*-?\s*88\b", " ", text, flags=re.I)
    text = re.sub(r"\bMMSS88\b", " ", text, flags=re.I)

    # Remove UI/app chrome and greetings, not educational content.
    for pattern in UI_JUNK_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)
    for pattern in GREETING_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)

    # Homework/Classwork are labels. Keep the text that follows them.
    text = re.sub(r"\bHome\s*Work\s*[:\-]\s*", " ", text, flags=re.I)
    text = re.sub(r"\bClass\s*Work\s*[:\-]\s*", " ", text, flags=re.I)

    # Avoid repeating the subject inside the title when it already has its own field.
    text = _remove_subject_from_title(text, clean(subject))

    # Common EduSecure wording cleanup while preserving the actual topic.
    text = re.sub(r"\bCircular\s+Circular\b", "Circular", text, flags=re.I)
    text = re.sub(r"\bof\s+Circular\s+(No\.?\s*\d+)", r"Circular \1", text, flags=re.I)
    text = re.sub(r"\bCircular\s+of\s+Circular\b", "Circular", text, flags=re.I)
    text = re.sub(r"\bnote\s+the\b", "", text, flags=re.I)

    # Normalize punctuation/separators created by removals.
    text = re.sub(r"\s*[,;|]+\s*", " - ", text)
    text = re.sub(r"\s*[-–—]{2,}\s*", " - ", text)
    text = re.sub(r"(?:\s+-\s+){2,}", " - ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")

    # Small readability fixes for common school messages.
    text = re.sub(r"\bQ\s*no\.?\s*(\d+)\b", r"Q No. \1", text, flags=re.I)
    text = re.sub(r"\bExercise\s+(\d+(?:\.\d+)?)\b", r"Exercise \1", text, flags=re.I)
    text = re.sub(r"\bcorrection\s+in\b", "Correction in", text, count=1, flags=re.I)

    if not text:
        return "Study Material"
    return text[:160].rstrip(" -:|,.;")


def firebase_sign_in() -> str:
    if not FIREBASE_ADMIN_PASSWORD:
        raise RuntimeError("Missing FIREBASE_ADMIN_PASSWORD")
    response = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
        json={
            "email": FIREBASE_ADMIN_EMAIL,
            "password": FIREBASE_ADMIN_PASSWORD,
            "returnSecureToken": True,
        },
        timeout=25,
    )
    response.raise_for_status()
    token = response.json().get("idToken", "")
    if not token:
        raise RuntimeError("Firebase sign-in returned no idToken")
    return token


def headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def decode_value(value: Dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "timestampValue", "integerValue", "doubleValue", "booleanValue"):
        if key in value:
            return value[key]
    return None


def list_materials(token: str) -> list[Dict[str, Any]]:
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/study_materials"
    params: Dict[str, Any] = {"pageSize": 1000, "key": FIREBASE_API_KEY}
    out: list[Dict[str, Any]] = []
    while True:
        response = requests.get(url, params=params, headers=headers(token), timeout=30)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        for raw in body.get("documents", []):
            fields = {k: decode_value(v) for k, v in (raw.get("fields") or {}).items()}
            fields["_name"] = raw.get("name", "")
            out.append(fields)
        next_token = body.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
    return out


def patch_title_description(document_name: str, title: str, description: str, token: str) -> bool:
    response = requests.patch(
        f"https://firestore.googleapis.com/v1/{document_name}",
        params=[
            ("key", FIREBASE_API_KEY),
            ("updateMask.fieldPaths", "title"),
            ("updateMask.fieldPaths", "description"),
        ],
        headers=headers(token),
        json={
            "fields": {
                "title": {"stringValue": title},
                "description": {"stringValue": description},
            }
        },
        timeout=25,
    )
    return response.ok


def main() -> int:
    token = firebase_sign_in()
    materials = list_materials(token)
    checked = 0
    fixed = 0

    for item in materials:
        source = clean(item.get("source")).lower()
        if "edusecure" not in source:
            continue

        checked += 1
        old_title = clean(item.get("title"))
        old_description = clean(item.get("description"))
        subject = clean(item.get("subject"))
        new_title = sanitize_title(old_title, subject)
        new_description = sanitize_title(old_description or old_title, subject)

        # Current automation mirrors title into description, so keep both clean and consistent.
        if not new_description or new_description == "Study Material":
            new_description = new_title

        if new_title == old_title and new_description == old_description:
            continue

        if patch_title_description(clean(item.get("_name")), new_title, new_description, token):
            fixed += 1
            print(f"✅ Cleaned title: {old_title[:90]} -> {new_title[:90]}")
        else:
            print(f"❌ Could not patch: {old_title[:100]}")

    print(f"EduSecure title cleanup complete: checked={checked}, fixed={fixed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
