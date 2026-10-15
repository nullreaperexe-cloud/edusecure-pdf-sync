# EduSecure PDF Sync

This GitHub Actions workflow checks the EduSecure ParentApp every hour and sends newly found PDF metadata to the 8aPDF Study Library ingest endpoint. Duplicate links are ignored by the website.

## Required repository secrets

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

- `EDUSECURE_USERNAME`
- `EDUSECURE_PASSWORD`
- `AUTOMATION_TOKEN` (the token configured for the 8aPDF website)

The workflow is also available from **Actions → EduSecure PDF Sync → Run workflow** for a manual test.

Only metadata and the source PDF URL are sent. The workflow does not store passwords in the repository.

## Reliability and background notifications

Run `python sync_repair.py` for the active bounded worker. Run `python -m unittest discover -v` before deploying. The workflow retains its five-minute handoff and hourly watchdog; failed/deferred cycles now report nonzero status and still hand off after successful validation.

`reliable_http.py` retries transport failures and transient responses with bounded jitter and Retry-After support. PDF writes use deterministic IDs, preserve case-sensitive URLs, and never suppress a distinct PDF solely because its title matches. Incremental cache writes preserve the authoritative refresh timestamp. Diagnostics are saved atomically in `sync_report.json` and included in the Actions summary.

`push_bridge.py` wakes the existing 8aPDF Supabase worker after confirmed publication using the current Firebase administrator ID token. No new GitHub secret is required with the existing default administrator. If overriding `FIREBASE_ADMIN_EMAIL`, configure the matching `FIREBASE_SYNC_EMAIL` on the backend. `SCHOOL_PUSH_ENDPOINT` and `WEBSITE_URL` are optional environment overrides. Set `CHECK_WEBSITE_DATE=1` only for an explicit visual audit; Firestore is the normal source of truth.

Exit codes: `0` complete, `1` partial/interrupted failure, `2` authentication/configuration/read failure, `75` deferred quota or announcement processing. The outer workflow records timeouts explicitly. Scheduled backend polling remains the notification fallback if the sync wake-up fails. See `The-8aPDF/docs/cross-repo-audit.md` in the connected website repository for the full implementation, SQL, deployment order and verification evidence.
