from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

import requests

import openrouter_title as ai_title
import title_cleaner as intelligence

FIREBASE_PROJECT_ID = intelligence.FIREBASE_PROJECT_ID
FIREBASE_API_KEY = intelligence.FIREBASE_API_KEY
ANNOUNCEMENTS_COLLECTION = "announcements"
BACKFILL_STATE_DOCUMENT = "__announcement_backfill_v1__"

ALLOWED_CATEGORIES = (
    "Tests",
    "Exams",
    "Homework",
    "Assignments",
    "Projects",
    "Important",
    "Events",
    "Holidays",
    "Timetable",
    "Results",
    "Activities",
    "Competitions",
    "General",
)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def firestore_headers(id_token: str = "") -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    return headers


def decode_value(value: Dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in (
        "stringValue",
        "timestampValue",
        "integerValue",
        "doubleValue",
        "booleanValue",
        "nullValue",
    ):
        if key in value:
            return value[key]
    return None


def stable_message_id(message_text: Any, message_date: Optional[date] = None) -> str:
    """Stable id shared by historical backfill and future 5-minute sync."""
    day = message_date.isoformat() if isinstance(message_date, date) else ""
    normalized = clean(message_text).lower()
    normalized = re.sub(r"\s+", " ", normalized)
    digest = hashlib.sha256(f"{day}|{normalized}".encode("utf-8")).hexdigest()
    return f"edusecure-{digest[:32]}"


def load_existing_state(id_token: str) -> Tuple[Set[str], Optional[date]]:
    base = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}"
    )
    params: Dict[str, Any] = {"pageSize": 1000, "key": FIREBASE_API_KEY}
    source_ids: Set[str] = set()
    latest_message_date: Optional[date] = None

    while True:
        response = requests.get(
            base,
            params=params,
            headers=firestore_headers(id_token),
            timeout=30,
        )
        if response.status_code == 404:
            return source_ids, latest_message_date
        response.raise_for_status()
        body = response.json()

        for raw in body.get("documents", []):
            fields = raw.get("fields") or {}
            source_id = clean(decode_value(fields.get("sourceMessageId") or {}))
            if source_id and not source_id.startswith("__announcement_backfill"):
                source_ids.add(source_id)

            raw_date = clean(decode_value(fields.get("messageDate") or {}))
            if raw_date:
                try:
                    parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
                except Exception:
                    parsed = None
                if parsed and (latest_message_date is None or parsed > latest_message_date):
                    latest_message_date = parsed

        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    return source_ids, latest_message_date


def list_existing_source_ids(id_token: str) -> Set[str]:
    source_ids, _latest = load_existing_state(id_token)
    return source_ids


def backfill_completed(id_token: str) -> bool:
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}/{BACKFILL_STATE_DOCUMENT}"
    )
    response = requests.get(
        url,
        params={"key": FIREBASE_API_KEY},
        headers=firestore_headers(id_token),
        timeout=25,
    )
    if response.status_code == 404:
        return False
    if not response.ok:
        return False
    fields = response.json().get("fields") or {}
    return decode_value(fields.get("completed") or {}) is True


def mark_backfill_completed(id_token: str, scanned: int, created: int) -> bool:
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}/{BACKFILL_STATE_DOCUMENT}"
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "fields": {
            "title": {"stringValue": "Announcement Backfill State"},
            "description": {"stringValue": "Internal automation state"},
            "category": {"stringValue": "General"},
            "subject": {"stringValue": "General"},
            "createdAt": {"timestampValue": now},
            "priority": {"stringValue": "normal"},
            "published": {"booleanValue": False},
            "sourceMessageId": {"stringValue": BACKFILL_STATE_DOCUMENT},
            "hasAttachment": {"booleanValue": False},
            "attachmentUrl": {"stringValue": ""},
            "completed": {"booleanValue": True},
            "scanned": {"integerValue": str(scanned)},
            "created": {"integerValue": str(created)},
            "completedAt": {"timestampValue": now},
        }
    }
    response = requests.patch(
        url,
        params={"key": FIREBASE_API_KEY},
        headers=firestore_headers(id_token),
        json=payload,
        timeout=30,
    )
    return response.ok


def classify_category(text: Any) -> str:
    raw = clean(text).lower()

    rules = (
        ("Tests", r"\b(?:class\s+test|unit\s+test|test|quiz)\b"),
        ("Exams", r"\b(?:exam|examination|mid[- ]?term|term[- ]?exam|assessment)\b"),
        ("Homework", r"\b(?:home\s*work|homework|complete\s+(?:the\s+)?worksheet|do\s+exercise)\b"),
        ("Assignments", r"\b(?:assignment|submission|submit\s+by)\b"),
        ("Projects", r"\b(?:project|project\s+work|model\s+making)\b"),
        ("Holidays", r"\b(?:holiday|school\s+closed|school\s+closure|closed\s+tomorrow)\b"),
        ("Timetable", r"\b(?:time\s*table|timetable|schedule\s+change|period\s+change)\b"),
        ("Results", r"\b(?:result|marks|report\s+card)\b"),
        ("Competitions", r"\b(?:competition|inter[- ]?school|olympiad|contest)\b"),
        ("Events", r"\b(?:event|annual\s+function|exhibition|celebration|ptm|parent[- ]teacher)\b"),
        ("Activities", r"\b(?:activity|practice|bring\s+(?:a|an|the)?\s*|uniform|drawing\s+sheet|craft)\b"),
        ("Important", r"\b(?:important|urgent|deadline|last\s+date|mandatory|must\s+bring)\b"),
    )

    for category, pattern in rules:
        if re.search(pattern, raw, flags=re.I):
            return category
    return "General"


def classify_priority(text: Any) -> str:
    raw = clean(text)
    if re.search(r"\b(?:emergency|urgent|school\s+closed\s+tomorrow)\b", raw, flags=re.I):
        return "urgent"
    if re.search(
        r"\b(?:important|exam|examination|deadline|last\s+date|mandatory|result|timetable)\b",
        raw,
        flags=re.I,
    ):
        return "important"
    return "normal"


def useful_announcement(text: Any) -> bool:
    raw = clean(text)
    if len(raw) < 12:
        return False

    stripped = raw
    for pattern in intelligence.SCHOOL_PATTERNS:
        stripped = re.sub(pattern, " ", stripped, flags=re.I)
    for pattern in intelligence.GREETING_PATTERNS:
        stripped = re.sub(pattern, " ", stripped, flags=re.I)
    for pattern in intelligence.UI_JUNK_PATTERNS:
        stripped = re.sub(pattern, " ", stripped, flags=re.I)

    stripped = re.sub(r"\s+", " ", stripped).strip(" -:|,.;")
    if len(stripped) < 8:
        return False

    useless = {
        "good morning",
        "good afternoon",
        "good evening",
        "thank you",
        "thanks",
        "dear parents",
        "dear students",
    }
    return stripped.lower() not in useless


def clean_description(text: Any) -> str:
    """Keep useful dates/instructions while removing obvious app/school chrome."""
    value = clean(text)
    if not value:
        return ""

    for pattern in intelligence.SCHOOL_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.I)
    for pattern in intelligence.UI_JUNK_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.I)

    value = re.sub(r"\bDear\s+(?:Parents?|Students?|All)\b[:,\s-]*", " ", value, flags=re.I)
    value = re.sub(r"\bGood\s+(?:Morning|Afternoon|Evening)\b[:,\s-]*", " ", value, flags=re.I)
    value = re.sub(r"\b(?:Regards|Warm\s+Regards|Best\s+Regards)\b.*$", " ", value, flags=re.I)
    value = re.sub(r"\bPay\s*Now\b", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -:|,.;")

    if len(value) > 420:
        shortened = value[:420]
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        value = shortened.rstrip(" -:|,.;") + "…"
    return value


def build_announcement(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
) -> Optional[Dict[str, Any]]:
    evidence = [message_text, detail_text]
    combined = clean(detail_text) or clean(message_text)
    if not useful_announcement(combined):
        return None

    subject = intelligence.detect_subject(evidence)
    if subject in {"Circular", "General", "School Diary", "Message", "Announcement", "Notice"}:
        subject = "General"

    fallback = intelligence.sanitize_title(combined, subject)
    title = ai_title.generate_title(
        evidence,
        subject=subject,
        fallback_title=fallback,
    )

    description = clean_description(combined)
    if not description:
        description = title

    return {
        "title": title,
        "description": description,
        "category": classify_category(combined),
        "subject": subject or "General",
        "priority": classify_priority(combined),
        "sourceMessageId": stable_message_id(message_text, message_date),
    }


def upload_announcement(
    item: Dict[str, Any],
    message_date: Optional[date],
    id_token: str,
) -> bool:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if message_date:
        message_ts = datetime(
            message_date.year,
            message_date.month,
            message_date.day,
            tzinfo=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        message_date_value: Dict[str, Any] = {"timestampValue": message_ts}
    else:
        message_date_value = {"nullValue": None}

    fields = {
        "title": {"stringValue": clean(item.get("title"))},
        "description": {"stringValue": clean(item.get("description"))},
        "category": {"stringValue": clean(item.get("category")) or "General"},
        "subject": {"stringValue": clean(item.get("subject")) or "General"},
        "messageDate": message_date_value,
        "eventDate": {"nullValue": None},
        "createdAt": {"timestampValue": now},
        "priority": {"stringValue": clean(item.get("priority")) or "normal"},
        "published": {"booleanValue": True},
        "sourceMessageId": {"stringValue": clean(item.get("sourceMessageId"))},
        "hasAttachment": {"booleanValue": False},
        "attachmentUrl": {"stringValue": ""},
    }

    source_id = clean(item.get("sourceMessageId"))
    if not source_id:
        return False

    # Stable sourceMessageId is also the Firestore document ID. This makes
    # live sync + historical backfill idempotent even if they race.
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}/{source_id}"
    )
    response = requests.patch(
        url,
        params={"key": FIREBASE_API_KEY},
        headers=firestore_headers(id_token),
        json={"fields": fields},
        timeout=30,
    )

    if response.ok:
        print(
            "✅ Announcement uploaded: "
            f"{clean(item.get('title'))} [{clean(item.get('category'))}]"
        )
        return True

    print(f"❌ Announcement upload failed: HTTP {response.status_code}")
    print(response.text[:1000])
    return False


def process_no_attachment_message(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
    id_token: str,
    existing_source_ids: Set[str],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Process exactly one no-attachment EduSecure message.

    Returns status: created / duplicate / ignored / failed
    """
    source_id = stable_message_id(message_text, message_date)
    if source_id in existing_source_ids:
        print("Duplicate announcement sourceMessageId -> skip")
        return "duplicate", None

    item = build_announcement(message_text, detail_text, message_date)
    if not item:
        print("Message has no useful announcement content -> ignore")
        return "ignored", None

    if upload_announcement(item, message_date, id_token):
        existing_source_ids.add(source_id)
        return "created", item
    return "failed", item
