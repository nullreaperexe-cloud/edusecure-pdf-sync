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

    subjects_base = (
        f"https://firestore.googleapis.com/v1/projects/{announcements.FIREBASE_PROJECT_ID}"
        f"/databases/(default)/documents/subjects"
    )
    subject_probe_id = "automation_permission_probe_subject"
    subject_fields = {
        "name": {"stringValue": "Automation Permission Probe"},
        "createdAt": {
            "timestampValue": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        },
    }

    subject_write = announcements.firestore_request(
        "POST",
        subjects_base,
        params={
            "key": announcements.FIREBASE_API_KEY,
            "documentId": subject_probe_id,
        },
        id_token=token,
        json_body={"fields": subject_fields},
        timeout=25,
        attempts=2,
    )
    if subject_write.status_code == 409:
        subject_write = announcements.firestore_request(
            "PATCH",
            f"{subjects_base}/{subject_probe_id}",
            params={"key": announcements.FIREBASE_API_KEY},
            id_token=token,
            json_body={"fields": subject_fields},
            timeout=25,
            attempts=2,
        )

    if subject_write.ok:
        print("Admin-rule probe on subjects: OK")
        announcements.firestore_request(
            "DELETE",
            f"{subjects_base}/{subject_probe_id}",
            params={"key": announcements.FIREBASE_API_KEY},
            id_token=token,
            timeout=25,
            attempts=2,
        )
    else:
        print(
            f"Admin-rule probe on subjects: HTTP {subject_write.status_code} "
            "(admin identity/rule mismatch likely)"
        )

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
