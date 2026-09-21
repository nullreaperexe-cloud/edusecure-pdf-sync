from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

import requests

import openrouter_title as ai_title
import title_cleaner as intelligence

FIREBASE_PROJECT_ID = intelligence.FIREBASE_PROJECT_ID
FIREBASE_API_KEY = intelligence.FIREBASE_API_KEY
ANNOUNCEMENTS_COLLECTION = "announcements"
BACKFILL_STATE_DOCUMENT = "automation_state_announcement_backfill_v3_ai_refresh"

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


def firestore_request(
    method: str,
    url: str,
    *,
    id_token: str = "",
    params: Any = None,
    json_body: Any = None,
    timeout: int = 30,
    attempts: int = 5,
):
    """Firestore request with bounded retry for rate limits/transient errors."""
    retryable = {429, 500, 502, 503, 504}
    last = None

    for attempt in range(attempts):
        response = requests.request(
            method,
            url,
            params=params,
            headers=firestore_headers(id_token),
            json=json_body,
            timeout=timeout,
        )
        last = response

        if response.status_code not in retryable:
            return response

        wait_seconds = min(12, 1.5 * (2 ** attempt))
        retry_after = response.headers.get("Retry-After")
        try:
            if retry_after:
                wait_seconds = max(wait_seconds, float(retry_after))
        except Exception:
            pass

        print(
            f"Firestore HTTP {response.status_code}; "
            f"retrying in {wait_seconds:.1f}s ({attempt + 1}/{attempts})"
        )
        time.sleep(wait_seconds)

    return last


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
        response = firestore_request(
            "GET",
            base,
            params=params,
            id_token=id_token,
            timeout=30,
        )
        if response.status_code == 404:
            return source_ids, latest_message_date
        response.raise_for_status()
        body = response.json()

        for raw in body.get("documents", []):
            fields = raw.get("fields") or {}
            source_id = clean(decode_value(fields.get("sourceMessageId") or {}))
            if (
                source_id
                and source_id != BACKFILL_STATE_DOCUMENT
                and not source_id.startswith("__announcement_backfill")
                and not source_id.startswith("automation_state_announcement_backfill")
            ):
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


def load_existing_document_map(id_token: str) -> Dict[str, str]:
    """Map sourceMessageId -> Firestore document resource name."""
    base = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}"
    )
    params: Dict[str, Any] = {"pageSize": 1000, "key": FIREBASE_API_KEY}
    records: Dict[str, str] = {}

    while True:
        response = requests.get(
            base,
            params=params,
            headers=firestore_headers(id_token),
            timeout=30,
        )
        if response.status_code == 404:
            return records
        response.raise_for_status()
        body = response.json()

        for raw in body.get("documents", []):
            fields = raw.get("fields") or {}
            source_id = clean(decode_value(fields.get("sourceMessageId") or {}))
            name = clean(raw.get("name"))
            if (
                source_id
                and name
                and source_id != BACKFILL_STATE_DOCUMENT
                and not source_id.startswith("__announcement_backfill")
                and not source_id.startswith("automation_state_announcement_backfill")
            ):
                records[source_id] = name

        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    return records


def backfill_completed(id_token: str) -> bool:
    url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}/{BACKFILL_STATE_DOCUMENT}"
    )
    response = firestore_request(
        "GET",
        url,
        params={"key": FIREBASE_API_KEY},
        id_token=id_token,
        timeout=25,
    )
    if response.status_code == 404:
        return False
    if not response.ok:
        return False
    fields = response.json().get("fields") or {}
    return decode_value(fields.get("completed") or {}) is True


def mark_backfill_completed(id_token: str, scanned: int, created: int) -> bool:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fields = {
        "title": {"stringValue": "Announcement Backfill State"},
        "description": {"stringValue": "Internal automation state"},
        "category": {"stringValue": "General"},
        "subject": {"stringValue": "General"},
        # Website sorts by createdAt, so make it the EduSecure arrival date,
        # never the later GitHub/backfill upload time.
        "createdAt": (
            {"timestampValue": message_ts}
            if message_date
            else {"timestampValue": now}
        ),
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

    collection_url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}"
    )
    create = firestore_request(
        "POST",
        collection_url,
        params={"key": FIREBASE_API_KEY, "documentId": BACKFILL_STATE_DOCUMENT},
        id_token=id_token,
        json_body={"fields": fields},
        timeout=30,
    )
    if create.ok:
        return True
    if create.status_code != 409:
        return False

    document_url = f"{collection_url}/{BACKFILL_STATE_DOCUMENT}"
    update = firestore_request(
        "PATCH",
        document_url,
        params={"key": FIREBASE_API_KEY},
        id_token=id_token,
        json_body={"fields": fields},
        timeout=30,
    )
    return update.ok

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



def prepare_announcement_ai_text(value: Any) -> str:
    """Strip EduSecure chrome before any AI classification/title request."""
    text = clean(value)
    if not text:
        return ""

    # Remove leading message-type/date chrome only. Real dates inside the message
    # can remain in description, but they must never be copied into the title.
    text = re.sub(
        r"^(?:Message|Circular|School\s+Diary)\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,)?\s+20\d{2}\s*",
        " ",
        text,
        flags=re.I,
    )

    for pattern in intelligence.SCHOOL_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)

    # Remove URLs from the classification prompt. Link presence is not a category.
    text = re.sub(r"https?://\S+", " ", text, flags=re.I)

    # EduSecure often appends navigation labels to the actual message text.
    # Strip them only when they occur as trailing UI chrome.
    text = re.sub(
        r"(?:\s+(?:Pay\s*Now|Class\s*Test|Circular|More|Attachment|Attachments)){1,8}\s*$",
        " ",
        text,
        flags=re.I,
    )

    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")
    return text



def build_announcement(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
) -> Optional[Dict[str, Any]]:
    primary = clean(message_text) or clean(detail_text)
    if not useful_announcement(primary):
        return None

    cleaned_primary = prepare_announcement_ai_text(primary)
    cleaned_detail = prepare_announcement_ai_text(detail_text)

    evidence = [cleaned_primary or primary]
    if (
        len(cleaned_primary) < 40
        and cleaned_detail
        and cleaned_detail != cleaned_primary
    ):
        evidence.append(cleaned_detail)

    # OpenRouter AI is the ONLY authority for announcement title + section.
    # If AI is unavailable/invalid, postpone instead of guessing a category.
    ai_meta = ai_title.generate_announcement_metadata(
        evidence,
        ALLOWED_CATEGORIES,
    )
    if not ai_meta:
        print("Announcement AI metadata unavailable -> postpone this message")
        return {"_retry": True}

    description = clean_description(primary)
    if not description:
        description = ai_meta["title"]

    return {
        "title": ai_meta["title"],
        "description": description,
        "category": ai_meta["category"],
        "subject": ai_meta["subject"],
        "priority": ai_meta["priority"],
        "aiModel": ai_meta.get("aiModel", ""),
        "originalMessage": primary,
        "sourceMessageId": stable_message_id(message_text, message_date),
    }
def _announcement_fields(
    item: Dict[str, Any],
    message_date: Optional[date],
) -> Dict[str, Any]:
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

    return {
        "title": {"stringValue": clean(item.get("title"))},
        "description": {"stringValue": clean(item.get("description"))},
        "category": {"stringValue": clean(item.get("category")) or "General"},
        "subject": {"stringValue": clean(item.get("subject")) or "General"},
        "messageDate": message_date_value,
        "eventDate": {"nullValue": None},
        "createdAt": (
            {"timestampValue": message_ts}
            if message_date
            else {"timestampValue": now}
        ),
        "priority": {"stringValue": clean(item.get("priority")) or "normal"},
        "published": {"booleanValue": True},
        "sourceMessageId": {"stringValue": clean(item.get("sourceMessageId"))},
        "hasAttachment": {"booleanValue": False},
        "attachmentUrl": {"stringValue": ""},
        "aiModel": {"stringValue": clean(item.get("aiModel"))},
        "originalMessage": {"stringValue": clean(item.get("originalMessage"))},
    }



def announcement_document_name(source_id: str) -> str:
    return (
        f"projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/"
        f"{ANNOUNCEMENTS_COLLECTION}/{source_id}"
    )


def claim_announcement(
    source_id: str,
    message_date: Optional[date],
    id_token: str,
) -> str:
    """Atomically reserve a new message before spending an OpenRouter call."""
    if not source_id:
        return "failed"

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if message_date:
        message_ts = datetime(
            message_date.year,
            message_date.month,
            message_date.day,
            tzinfo=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        message_date_value: Dict[str, Any] = {"timestampValue": message_ts}
        sort_ts = message_ts
    else:
        message_date_value = {"nullValue": None}
        sort_ts = now

    fields = {
        "title": {"stringValue": "Processing Announcement"},
        "description": {"stringValue": ""},
        "category": {"stringValue": "General"},
        "subject": {"stringValue": "General"},
        "messageDate": message_date_value,
        "eventDate": {"nullValue": None},
        "createdAt": {"timestampValue": sort_ts},
        "priority": {"stringValue": "normal"},
        "published": {"booleanValue": False},
        "sourceMessageId": {"stringValue": source_id},
        "hasAttachment": {"booleanValue": False},
        "attachmentUrl": {"stringValue": ""},
        "aiStatus": {"stringValue": "processing"},
    }

    collection_url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}"
    )
    response = firestore_request(
        "POST",
        collection_url,
        params={"key": FIREBASE_API_KEY, "documentId": source_id},
        id_token=id_token,
        json_body={"fields": fields},
        timeout=30,
    )

    if response.ok:
        return "claimed"
    if response.status_code == 409:
        return "existing"

    print(f"Announcement claim failed: HTTP {response.status_code}")
    return "failed"


def delete_announcement_claim(source_id: str, id_token: str) -> None:
    if not source_id:
        return
    url = (
        f"https://firestore.googleapis.com/v1/"
        f"{announcement_document_name(source_id)}"
    )
    try:
        firestore_request(
            "DELETE",
            url,
            params={"key": FIREBASE_API_KEY},
            id_token=id_token,
            timeout=25,
            attempts=3,
        )
    except Exception:
        pass



def upload_announcement(
    item: Dict[str, Any],
    message_date: Optional[date],
    id_token: str,
    existing_document_name: str = "",
) -> bool:
    source_id = clean(item.get("sourceMessageId"))
    if not source_id:
        return False

    fields = _announcement_fields(item, message_date)
    collection_url = (
        f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{ANNOUNCEMENTS_COLLECTION}"
    )

    if existing_document_name:
        response = firestore_request(
            "PATCH",
            f"https://firestore.googleapis.com/v1/{existing_document_name}",
            params={"key": FIREBASE_API_KEY},
            id_token=id_token,
            json_body={"fields": fields},
            timeout=30,
        )
        if response.ok:
            print(f"✅ Announcement refreshed: {clean(item.get('title'))}")
            return True
    else:
        response = firestore_request(
            "POST",
            collection_url,
            params={"key": FIREBASE_API_KEY, "documentId": source_id},
            id_token=id_token,
            json_body={"fields": fields},
            timeout=30,
        )
        if response.ok:
            print(
                "✅ Announcement uploaded: "
                f"{clean(item.get('title'))} [{clean(item.get('category'))}]"
            )
            return True

        # A concurrent live/backfill create of the same stable document ID is
        # a duplicate, not a failure.
        if response.status_code == 409:
            print("Concurrent duplicate announcement create -> already exists")
            return True

    print(f"❌ Announcement upload failed: HTTP {response.status_code}")
    print(response.text[:1000])
    return False

def refresh_existing_announcement(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
    id_token: str,
    document_name: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Regenerate one historical announcement in place; never duplicate it."""
    item = build_announcement(message_text, detail_text, message_date)
    if not item:
        return "ignored", None
    if item.get("_retry"):
        return "retry", None
    if upload_announcement(
        item,
        message_date,
        id_token,
        existing_document_name=document_name,
    ):
        return "refreshed", item
    return "failed", item


def process_no_attachment_message(
    message_text: str,
    detail_text: str,
    message_date: Optional[date],
    id_token: str,
    existing_source_ids: Set[str],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Process one live no-attachment message without listing the collection."""
    source_id = stable_message_id(message_text, message_date)

    if source_id in existing_source_ids:
        print("Duplicate announcement sourceMessageId -> skip")
        return "duplicate", None

    claim_status = claim_announcement(source_id, message_date, id_token)
    if claim_status == "existing":
        existing_source_ids.add(source_id)
        print("Announcement document already exists -> skip before AI call")
        return "duplicate", None
    if claim_status != "claimed":
        return "failed", None

    item = build_announcement(message_text, detail_text, message_date)
    if not item:
        print("Message has no useful announcement content -> ignore")
        delete_announcement_claim(source_id, id_token)
        return "ignored", None
    if item.get("_retry"):
        delete_announcement_claim(source_id, id_token)
        return "retry", None

    item["sourceMessageId"] = source_id
    if upload_announcement(
        item,
        message_date,
        id_token,
        existing_document_name=announcement_document_name(source_id),
    ):
        existing_source_ids.add(source_id)
        return "created", item

    delete_announcement_claim(source_id, id_token)
    return "failed", item
