# EduSecure PDF Sync

This GitHub Actions workflow checks the EduSecure ParentApp every hour and sends newly found PDF metadata to the 8aPDF Study Library ingest endpoint. Duplicate links are ignored by the website.

## Required repository secrets

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

- `EDUSECURE_USERNAME`
- `EDUSECURE_PASSWORD`
- `FIREBASE_ADMIN_PASSWORD` (required for authenticated Firestore writes)
- `FIREBASE_ADMIN_EMAIL` (set if not using the current default admin)
- `OPENROUTER_API_KEY` (optional; deterministic title fallback works without it)

Legacy `AUTOMATION_TOKEN` is only needed by old ingest-based scripts; the active `sync_repair.py` worker writes to Firestore using Firebase authentication.

The workflow is also available from **Actions → EduSecure PDF Sync → Run workflow** for a manual test.

Only metadata and the source PDF URL are sent. The workflow does not store passwords in the repository.

## Reliability and background notifications

Run `python sync_repair.py` for the active bounded worker. Run `python -m unittest discover -v` before deploying. The GitHub Actions workflow uses an independent five-minute schedule (UTC minutes 2, 7, 12, etc.) plus manual runs and code-change triggers. Each run is bounded. It no longer self-dispatches or sleeps merely to maintain cadence, so one failed validation/sync no longer kills the future schedule.

`reliable_http.py` retries transport failures and transient responses with bounded jitter and Retry-After support. Attachment detection includes offscreen anchors, embedded PDF URLs, and a portal-button fallback; distinct PDFs are not discarded just because their message text matches. PDF writes use deterministic IDs, preserve case-sensitive URLs, and never suppress a distinct PDF solely because its title matches. Incremental cache writes preserve the authoritative refresh timestamp. Diagnostics are saved atomically in `sync_report.json` and included in the Actions summary.

`push_bridge.py` wakes the existing 8aPDF Supabase worker after confirmed publication using the current Firebase administrator ID token. No new GitHub secret is required with the existing default administrator. If overriding `FIREBASE_ADMIN_EMAIL`, configure the matching `FIREBASE_SYNC_EMAIL` on the backend. `SCHOOL_PUSH_ENDPOINT` and `WEBSITE_URL` are optional environment overrides. Set `CHECK_WEBSITE_DATE=1` only for an explicit visual audit; Firestore is the normal source of truth.

Exit codes: `0` complete, `1` partial/interrupted failure, `2` authentication/configuration/read failure, `75` deferred quota or announcement processing. The outer workflow records timeouts explicitly. Scheduled backend polling remains the notification fallback if the sync wake-up fails. See `The-8aPDF/docs/cross-repo-audit.md` in the connected website repository for the full implementation, SQL, deployment order and verification evidence.


## Check a live run

1. Open [EduSecure PDF Sync Actions](https://github.com/nullreaperexe-cloud/edusecure-pdf-sync/actions/workflows/sync.yml).
2. Click **Run workflow → main → Run workflow** to request a run right away. The code-change trigger also starts a run after a relevant commit.
3. Open the newest run. Confirm **Validate automation code** is green, then inspect **Run one EduSecure sync cycle**.
4. Scroll down to the **cycle status summary** and, if needed, download the **edusecure-sync-diagnostics** artifact for `sync_report.json` and screenshots.
5. If authentication fails, check GitHub Actions secrets for `EDUSECURE_USERNAME`, `EDUSECURE_PASSWORD`, and `FIREBASE_ADMIN_PASSWORD`; do not paste credentials in tickets or chat.

**Note:** GitHub cron runs are best effort and may be delayed by GitHub. A code repair does not prove that the school's live login, Firestore permissions, or third-party endpoints are healthy. A green live sync cycle and correctly published Firestore record are required for end-to-end confirmation.
