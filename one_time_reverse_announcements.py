from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import announcement_processor as announcements
import openrouter_title as ai_title
import runner


STATE_DOC_ID = "automation_state_reverse_existing_announcements_v1"


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def decode(value: Dict[str, Any]) -> Any:
    return announcements.decode_value(value or {})


def parse_ts(raw: Any) -> Optional[datetime]:
    value = clean(raw)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_existing_title(value: Any) -> str:
    text = clean(value)

    # Keep prior title-date requirement enforced on already-uploaded cards too.
    text = ai_title._strip_title_dates(text)

    # EduSecure UI labels must never appear in a visible title.
    text = re.sub(r"\bClass\s*Test\s*More\b", " ", text, flags=re.I)
    text = re.sub(r"\bTest\s*More\b", " ", text, flags=re.I)
    text = re.sub(r"\bClass\s*Test\b", " ", text, flags=re.I)
    text = re.sub(r"\bMore\b", " ", text, flags=re.I)

    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")
    return text or "School Update"


def state_completed(id_token: str) -> bool:
    url = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{announcements.ANNOUNCEMENTS_COLLECTION}/{STATE_DOC_ID}"
    )
    response = announcements.firestore_request(
        "GET",
        url,
        params={"key": announcements.FIREBASE_API_KEY},
        id_token=id_token,
        timeout=25,
        attempts=2,
    )
    if response.status_code == 404:
        return False
    if not response.ok:
        return False
    fields = response.json().get("fields") or {}
    return decode(fields.get("completed") or {}) is True


def save_state(id_token: str, count: int, old_top: str, old_bottom: str) -> bool:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    base = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{announcements.ANNOUNCEMENTS_COLLECTION}"
    )
    fields = {
        "title": {"stringValue": "One-time Announcement Order Reversal"},
        "description": {"stringValue": "Internal automation state"},
        "category": {"stringValue": "General"},
        "subject": {"stringValue": "General"},
        "createdAt": {"timestampValue": now},
        "priority": {"stringValue": "normal"},
        "published": {"booleanValue": False},
        "sourceMessageId": {"stringValue": STATE_DOC_ID},
        "hasAttachment": {"booleanValue": False},
        "attachmentUrl": {"stringValue": ""},
        "completed": {"booleanValue": True},
        "reversedCount": {"integerValue": str(count)},
        "oldTopTitle": {"stringValue": old_top},
        "oldBottomTitle": {"stringValue": old_bottom},
        "completedAt": {"timestampValue": now},
    }

    response = announcements.firestore_request(
        "POST",
        base,
        params={"key": announcements.FIREBASE_API_KEY, "documentId": STATE_DOC_ID},
        id_token=id_token,
        json_body={"fields": fields},
        timeout=30,
        attempts=2,
    )
    if response.ok:
        return True
    if response.status_code == 409:
        response = announcements.firestore_request(
            "PATCH",
            f"{base}/{STATE_DOC_ID}",
            params={"key": announcements.FIREBASE_API_KEY},
            id_token=id_token,
            json_body={"fields": fields},
            timeout=30,
            attempts=2,
        )
        return response.ok
    return False


def list_published(id_token: str) -> List[Dict[str, Any]]:
    base = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{announcements.ANNOUNCEMENTS_COLLECTION}"
    )
    params: Dict[str, Any] = {"pageSize": 200, "key": announcements.FIREBASE_API_KEY}
    rows: List[Dict[str, Any]] = []

    while True:
        response = announcements.firestore_request(
            "GET",
            base,
            params=params,
            id_token=id_token,
            timeout=30,
            attempts=3,
        )
        response.raise_for_status()
        body = response.json()

        for raw in body.get("documents", []):
            name = clean(raw.get("name"))
            if not name:
                continue
            doc_id = name.rsplit("/", 1)[-1]

            if (
                doc_id == STATE_DOC_ID
                or doc_id.startswith("automation_state_")
                or doc_id.startswith("__announcement_backfill")
            ):
                continue

            fields = raw.get("fields") or {}
            if decode(fields.get("published") or {}) is not True:
                continue

            created = parse_ts(decode(fields.get("createdAt") or {}))
            if created is None:
                created = parse_ts(decode(fields.get("messageDate") or {}))
            if created is None:
                created = parse_ts(raw.get("createTime"))
            if created is None:
                created = datetime(1970, 1, 1, tzinfo=timezone.utc)

            rows.append(
                {
                    "name": name,
                    "doc_id": doc_id,
                    "title": clean(decode(fields.get("title") or {})),
                    "created": created,
                }
            )

        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    # This mirrors the website query: createdAt DESC, with document id as stable tie-break.
    rows.sort(
        key=lambda row: (row["created"], row["doc_id"]),
        reverse=True,
    )
    return rows


def patch_row(
    row: Dict[str, Any],
    new_created_at: datetime,
    id_token: str,
) -> bool:
    title = clean_existing_title(row["title"])
    timestamp = new_created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    params = [
        ("key", announcements.FIREBASE_API_KEY),
        ("updateMask.fieldPaths", "title"),
        ("updateMask.fieldPaths", "createdAt"),
        ("updateMask.fieldPaths", "oneTimeReverseV1"),
    ]
    response = announcements.firestore_request(
        "PATCH",
        f"https://firestore.googleapis.com/v1/{row['name']}",
        params=params,
        id_token=id_token,
        json_body={
            "fields": {
                "title": {"stringValue": title},
                "createdAt": {"timestampValue": timestamp},
                "oneTimeReverseV1": {"booleanValue": True},
            }
        },
        timeout=30,
        attempts=3,
    )
    if not response.ok:
        print(
            f"Failed to reverse {row['doc_id']}: HTTP {response.status_code}"
        )
        return False
    return True


def main() -> int:
    id_token = runner.firebase_sign_in()
    if not id_token:
        print("Firebase admin sign-in failed.")
        return 2

    if state_completed(id_token):
        print("One-time announcement reversal already completed ✅")
        return 0

    rows = list_published(id_token)
    if len(rows) < 2:
        print(f"Only {len(rows)} published announcement(s); nothing to reverse.")
        return 0

    old_top = rows[0]["title"]
    old_bottom = rows[-1]["title"]
    base = min(row["created"] for row in rows)

    print(f"Published announcements to reverse: {len(rows)}")
    print(f"OLD TOP: {old_top}")
    print(f"OLD BOTTOM: {old_bottom}")

    failures = 0

    # Current top gets earliest sort timestamp; current bottom gets latest.
    # Therefore Firestore createdAt DESC becomes the exact reverse order.
    for index, row in enumerate(rows):
        new_created_at = base + timedelta(microseconds=index)
        if patch_row(row, new_created_at, id_token):
            print(
                f"[{index + 1}/{len(rows)}] reversed + title-cleaned: "
                f"{clean_existing_title(row['title'])}"
            )
        else:
            failures += 1

    if failures:
        print(f"One-time reversal incomplete. Failures: {failures}")
        return 1

    if not save_state(id_token, len(rows), old_top, old_bottom):
        print("Reversal succeeded but completion state could not be saved.")
        return 1

    print("ONE-TIME REVERSAL COMPLETE ✅")
    print(f"NEW TOP should be old bottom: {old_bottom}")
    print(f"NEW BOTTOM should be old top: {old_top}")
    print("messageDate was NOT changed.")
    print("Future announcements will keep normal EduSecure date ordering.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
