"""Wake the durable school-push worker after confirmed Firestore publication.

Uses the existing Firebase admin ID token. No VAPID/private key enters GitHub
or the browser. A failed wake-up is reported; database Cron remains the fallback.
"""
import os
import requests
from reliable_http import request

ENDPOINT = os.environ.get("SCHOOL_PUSH_ENDPOINT", "https://uumqvvsfqwpwtwerdylv.supabase.co/functions/v1/school-push")


def notify_published(id_token: str) -> dict:
    if not id_token:
        return {"ok": False, "error": "Firebase authentication is required to wake the push worker"}
    try:
        response = request("POST", ENDPOINT, headers={"Content-Type": "application/json", "Authorization": "Bearer " + id_token}, json={"action": "sync"}, timeout=(7, 85), attempts=2)
        try:
            response.raise_for_status()
            result = response.json()
        finally:
            response.close()
        return {"ok": result.get("ok", False), "busy": result.get("busy", False), "sent": result.get("sent", 0)}
    except (requests.RequestException, ValueError) as exc:
        # Never print credentials, endpoint tokens, or authorization headers.
        return {"ok": False, "error": "Push wake-up failed: " + type(exc).__name__}
