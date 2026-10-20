"""ONE-TIME LOCAL Google OAuth setup for the private 8aPDF Drive backup.

Run ONLY on your own PC with a downloaded Desktop OAuth client JSON:
  python -m pip install google-auth-oauthlib
  python scripts/setup_drive_oauth.py /path/to/client_secret.json

The helper never sends credentials to ChatGPT or GitHub. Paste the resulting
values YOURSELF into GitHub Actions repository secrets. Never commit the JSON
client secret file, a refresh token, or terminal output containing secrets.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

SCOPE = ["https://www.googleapis.com/auth/drive.file"]


def main():
    if os.getenv("GITHUB_ACTIONS") == "true":
        raise SystemExit("Do not run OAuth consent setup on a GitHub Actions worker")
    p = argparse.ArgumentParser(description="Authorize private 8aPDF Google Drive backup")
    p.add_argument("client_json", help="Downloaded Desktop OAuth client JSON file (local only)")
    args = p.parse_args()
    path = Path(args.client_json).expanduser().resolve(strict=True)
    source = json.loads(path.read_text(encoding="utf-8"))
    client = source.get("installed")
    if not isinstance(client, dict):
        raise SystemExit("Expected a Google OAuth Desktop app client JSON")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit("Install locally: python -m pip install google-auth-oauthlib") from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPE)
    creds = flow.run_local_server(
        host="localhost", port=0, open_browser=True,
        access_type="offline", prompt="consent",
        authorization_prompt_message="Open this URL in your browser: {url}",
    )
    if not creds.refresh_token:
        raise SystemExit("No refresh token returned. Revoke old consent, then retry.")
    print("\nPRIVATE: Add these as GitHub Actions repository secrets. DO NOT SHARE:\n")
    print("GDRIVE_BACKUP_CLIENT_ID=" + str(client.get("client_id", "")))
    print("GDRIVE_BACKUP_CLIENT_SECRET=" + str(client.get("client_secret", "")))
    print("GDRIVE_BACKUP_REFRESH_TOKEN=" + creds.refresh_token)
    print("\nAfter setting secrets, close this terminal. Never commit or screenshot them.")
    print("Avoid leaving an External Google OAuth app in Testing for long-term access.")


if __name__ == "__main__":
    main()
