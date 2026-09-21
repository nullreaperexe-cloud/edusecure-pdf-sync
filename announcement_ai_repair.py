from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

import requests

import announcement_processor as announcements
import openrouter_title as ai_title
import runner


def clean(value: Any) -> str:
    return announcements.clean(value)


def list_existing_announcements(id_token: str) -> List[Dict[str, Any]]:
    base = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{announcements.ANNOUNCEMENTS_COLLECTION}"
    )
    params: Dict[str, Any] = {"pageSize": 200, "key": announcements.FIREBASE_API_KEY}
    docs: List[Dict[str, Any]] = []

    while True:
        response = announcements.firestore_request(
            "GET",
            base,
            params=params,
            id_token=id_token,
            timeout=30,
        )
        if response.status_code == 404:
            return docs
        response.raise_for_status()
        body = response.json()

        for raw in body.get("documents", []):
            fields = raw.get("fields") or {}
            source_id = clean(announcements.decode_value(fields.get("sourceMessageId") or {}))
            if (
                source_id.startswith("__announcement_backfill")
                or source_id.startswith("automation_state_announcement_backfill")
            ):
                continue

            item = {
                "_name": raw.get("name", ""),
                "title": clean(announcements.decode_value(fields.get("title") or {})),
                "description": clean(announcements.decode_value(fields.get("description") or {})),
                "category": clean(announcements.decode_value(fields.get("category") or {})),
                "subject": clean(announcements.decode_value(fields.get("subject") or {})),
                "priority": clean(announcements.decode_value(fields.get("priority") or {})),
                "originalMessage": clean(announcements.decode_value(fields.get("originalMessage") or {})),
                "sourceMessageId": source_id,
            }
            docs.append(item)

        token = body.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token

    return docs


def patch_ai_fields(document_name: str, ai_meta: Dict[str, str], id_token: str) -> bool:
    if not document_name:
        return False

    fields = {
        "title": {"stringValue": clean(ai_meta.get("title"))},
        "category": {"stringValue": clean(ai_meta.get("category"))},
        "subject": {"stringValue": clean(ai_meta.get("subject")) or "General"},
        "priority": {"stringValue": clean(ai_meta.get("priority")) or "normal"},
        "aiModel": {"stringValue": clean(ai_meta.get("aiModel"))},
        "aiProcessedAt": {
            "timestampValue": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        },
    }

    params = [
        ("key", announcements.FIREBASE_API_KEY),
        ("updateMask.fieldPaths", "title"),
        ("updateMask.fieldPaths", "category"),
        ("updateMask.fieldPaths", "subject"),
        ("updateMask.fieldPaths", "priority"),
        ("updateMask.fieldPaths", "aiModel"),
        ("updateMask.fieldPaths", "aiProcessedAt"),
    ]
    response = announcements.firestore_request(
        "PATCH",
        f"https://firestore.googleapis.com/v1/{document_name}",
        params=params,
        id_token=id_token,
        json_body={"fields": fields},
        timeout=30,
    )
    if response.ok:
        return True
    print(f"Patch failed HTTP {response.status_code}: {response.text[:500]}")
    return False


def main() -> int:
    id_token = runner.firebase_sign_in()
    if not id_token:
        print("Firebase admin sign-in failed.")
        return 2

    docs = list_existing_announcements(id_token)
    print(f"Existing announcements loaded for AI repair: {len(docs)}")

    checked = 0
    corrected = 0
    unchanged = 0
    retries = 0
    failures = 0

    for doc in docs:
        checked += 1
        evidence = []
        for value in (doc.get("originalMessage"), doc.get("description"), doc.get("title")):
            value = clean(value)
            if value and value not in evidence:
                evidence.append(value)

        if not evidence:
            unchanged += 1
            continue

        ai_meta = ai_title.generate_announcement_metadata(
            evidence,
            announcements.ALLOWED_CATEGORIES,
        )
        if not ai_meta:
            retries += 1
            print(f"[{checked}] AI unavailable -> left unchanged for retry")
            continue

        before = (
            clean(doc.get("title")),
            clean(doc.get("category")),
            clean(doc.get("subject")),
            clean(doc.get("priority")),
        )
        after = (
            clean(ai_meta.get("title")),
            clean(ai_meta.get("category")),
            clean(ai_meta.get("subject")),
            clean(ai_meta.get("priority")),
        )

        if before == after:
            unchanged += 1
            print(f"[{checked}] Already correct: {before[0]} [{before[1]}]")
            continue

        if patch_ai_fields(doc.get("_name", ""), ai_meta, id_token):
            corrected += 1
            print(
                f"[{checked}] Corrected: {before[0]} [{before[1]}] -> "
                f"{after[0]} [{after[1]}]"
            )
        else:
            failures += 1

    print("\n=== AI ANNOUNCEMENT REPAIR COMPLETE ===")
    print(f"Checked: {checked}")
    print(f"Corrected: {corrected}")
    print(f"Unchanged: {unchanged}")
    print(f"AI retries pending: {retries}")
    print(f"Failures: {failures}")

    return 1 if failures or retries else 0


if __name__ == "__main__":
    raise SystemExit(main())
