from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import announcement_processor as announcements
import runner

PROBE_ID = "automation_permission_probe"


def main() -> int:
    print(
        "Firebase admin email source: "
        + ("GitHub secret" if os.environ.get("FIREBASE_ADMIN_EMAIL", "").strip() else "code fallback")
    )

    token = runner.firebase_sign_in()
    if not token:
        print("PERMISSION CHECK FAILED: Firebase authentication did not return an ID token.")
        return 2

    base = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/{announcements.ANNOUNCEMENTS_COLLECTION}"
    )
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fields = {
        "title": {"stringValue": "Automation Permission Probe"},
        "description": {"stringValue": "Temporary hidden probe; safe to delete."},
        "category": {"stringValue": "General"},
        "subject": {"stringValue": "General"},
        "createdAt": {"timestampValue": now},
        "priority": {"stringValue": "normal"},
        "published": {"booleanValue": False},
        "sourceMessageId": {"stringValue": PROBE_ID},
        "hasAttachment": {"booleanValue": False},
        "attachmentUrl": {"stringValue": ""},
    }

    create = announcements.firestore_request(
        "POST",
        base,
        params={"key": announcements.FIREBASE_API_KEY, "documentId": PROBE_ID},
        id_token=token,
        json_body={"fields": fields},
        timeout=25,
        attempts=2,
    )

    if create.status_code == 409:
        print("Probe already exists; testing update permission instead.")
        create = announcements.firestore_request(
            "PATCH",
            f"{base}/{PROBE_ID}",
            params={"key": announcements.FIREBASE_API_KEY},
            id_token=token,
            json_body={"fields": fields},
            timeout=25,
            attempts=2,
        )

    if not create.ok:
        print(f"PERMISSION CHECK FAILED: announcement write returned HTTP {create.status_code}.")
        print(create.text[:600])
        return 3

    print("Announcement write permission: OK")

    delete = announcements.firestore_request(
        "DELETE",
        f"{base}/{PROBE_ID}",
        params={"key": announcements.FIREBASE_API_KEY},
        id_token=token,
        timeout=25,
        attempts=2,
    )

    if not (delete.ok or delete.status_code == 404):
        print(f"PERMISSION CHECK WARNING: cleanup delete returned HTTP {delete.status_code}.")
        return 4

    print("Probe cleanup: OK")
    print("FIREBASE ANNOUNCEMENT WRITE CHECK PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
