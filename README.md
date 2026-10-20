# EduSecure → 8aPDF Supabase Sync

This repository continuously syncs **new EduSecure school PDFs and teacher announcements** to the new, independent 8aPDF Supabase project.
The GitHub workflow launches a **five-hour worker window** running `worker_loop.py`; while active, the worker starts a fresh `sync_repair.py` check **every five minutes** (or immediately after a slower cycle finishes, without running concurrent browsers). Each new process gets its own GitHub OIDC identity.

A finalizer uses GitHub's `workflow_dispatch` with the built-in `GITHUB_TOKEN` (`actions: write`) to queue the next five-hour worker. A separate five-minute scheduled watchdog provides backup, and workflow concurrency prevents overlapping workers. The GitHub-hosted 6-hour job cap is respected with a 5h45m job timeout.

**Limits:** GitHub scheduled runs can be delayed or skipped, and an account can exhaust its included Actions minutes. Continuous five-hour jobs consume approximately 720 runner-hours/month if kept running around the clock; check plan quotas and billing before relying on uninterrupted service. A successful full runtime/handoff must be confirmed in Actions logs, not assumed merely because code was committed.

This repository syncs **new EduSecure school PDFs and teacher announcements** to the new, independent 8aPDF Supabase project:
- Project URL: https://xudhjwojgxblbsebkzso.supabase.co
- Tables: `public.study_materials` and `public.announcements`
- GitHub workflow: `.github/workflows/sync.yml`; entrypoint: `sync_repair.py`
- Scheduler: a persistent worker checks every five minutes, with independent five-minute scheduled watchdog and explicit five-hour handoff.
- **Firebase/Firestore credentials are NOT used by the active sync.**

## Secretless GitHub → Supabase authentication

The GitHub workflow grants `id-token: write` and requests a short-lived GitHub Actions OIDC token for audience `8apdf-supabase-sync`. It sends the signed token over HTTPS to the deployed Supabase Edge Function `edusecure-sync` (with Supabase's `verify_jwt` gateway setting disabled **because the function independently validates the GitHub JWT signature**).

The Edge Function verifies issuer, audience, signature (GitHub JWKS), repository, branch, owner, event and expiration **before** accessing the database with its server-side Supabase credentials. It never shares those credentials with GitHub or the public browser. All writes are gated on this authorization; **never** make public/anon insert policies to fix authorization.

The active sync requires only these GitHub secrets:
- `EDUSECURE_USERNAME`: actual EduSecure school login.
- `EDUSECURE_PASSWORD`: EduSecure school password.
- `OPENROUTER_API_KEY` (optional): AI-generated title refinement; deterministic safe fallback if unavailable.

**No `SUPABASE_SECRET_KEY`, `FIREBASE_ADMIN_PASSWORD` or Firebase user account is necessary for this sync.** Do not paste any private keys into GitHub source files or logs.

## PDF and announcement behavior

New PDF metadata is published through the verified Edge function to `study_materials`, preserving the original source PDF URL, sanitized title, subject and school message date. Exact canonical URL matching prevents duplicate PDF records (case-sensitive path/query); the table additionally has a unique `pdf_url` index. Existing rows are never deleted or automatically overwritten by the live sync.

Non-PDF school messages, including messages with non-PDF attachments, are published to `announcements` through the same authenticated function. Announcement IDs are deterministic from the original message and date; records already published are not duplicated. The original normalized announcement data is retained in the `raw` JSON column for website compatibility.

**Historical Firestore data is not automatically copied by this scheduled job.** Any missing historical documents require a separate source export. PDFs are kept at their original school-hosted URLs, not copied into Supabase Storage.

## Reliability

- `python -m unittest discover -v` — run all offline regression tests.
- `python -m py_compile runner.py sync_repair.py supabase_gateway.py supabase_materials.py supabase_announcements.py` — check syntax.
- Workflow tests run before it attempts to log in to EduSecure. Each individual cycle has an 18-minute safety timeout; it cannot run concurrently with another cycle.
- Active sync fails closed if the OIDC identity or live Supabase inventory cannot be verified. It does not use stale Firestore caches for duplicate decisions.
- A message is only marked uploaded after the Edge function confirms the insert or preexisting record.
- GitHub Actions serializes overlapping scheduled runs; operations are idempotent for safe retries.
- All diagnostics are recorded in `sync_report.json` and attached to the run.

The old Firestore cleanup functions remain in older migration/legacy helper code for backwards-compatible tests; **they are never dispatched from the scheduled Supabase sync**. Do not run those legacy cleanup helpers against the new database.

## Verify the automation

1. Open [EduSecure PDF Sync workflow](https://github.com/nullreaperexe-cloud/edusecure-pdf-sync/actions/workflows/sync.yml).
2. Use **Run workflow → main → Run workflow**, or inspect the latest scheduled/push run.
3. Confirm **Validate automation code** passes, **Keep EduSecure worker alive for 5 hours** starts, and **Dispatch next five-hour worker** queues its successor at completion. The authentication preflight shows:
   `Supabase 8aPDF backend authorized through GitHub Actions OIDC.`
4. Look for both `Verified Supabase study_materials inventory` and `Verified Supabase announcement IDs`, followed by new PDF/announcement confirmations whenever eligible source messages exist.
5. Check [8aPDF Supabase](https://supabase.com/dashboard/project/xudhjwojgxblbsebkzso/editor). The website should read the two tables using its public publishable key with RLS-enforced read access.

A code commit or passing local unit tests **does not alone prove** a live EduSecure login or published records. An actual successful GitHub Actions run is required to confirm the end-to-end path.

## Known separate feature

Older school push notification wake-up depended on Firebase Authentication. It has been disabled in the main sync rather than allowing that retired dependency to break PDF/announcement publication. Push delivery requires a separate Supabase-compatible implementation and is not guaranteed by this migration.


## Google Drive disaster-recovery backup

The same five-hour EduSecure worker now invokes **drive_backup.py** after a
normal sync cycle, no more than once per hour (configurable). Backup failure,
missing credentials, or a slow Google Drive request cannot block Supabase sync.

### What gets saved

- Full current Supabase **study_materials** and **announcements** inventories,
  including older records, raw JSON, and admin-panel edits.
- Private **8aPDF_Backup/materials.json** and
  **8aPDF_Backup/announcements.json**, with a dated snapshot on changes in
  **daily_snapshots/**.
- Actual PDF files in a private **PDFs/** subfolder, keyed by SHA-256 of the
  original PDF URL. Up to 10 PDF attempts per backup pass (configurable).
  Inaccessible, oversized, or non-PDF source URLs are reported as missing.
- **backup-manifest.json**, plus **drive_backup_report.json** in the GitHub
  Actions run diagnostics with counts, pending files and failures.

This is **backup only**. It does not turn Google Drive into a live database,
public CDN, website failover endpoint or music store. Supabase remains primary.

### Activate backup with ZERO PC commands (browser-only)

The recommended route uses Google OAuth 2.0 Playground and just ONE GitHub
Actions secret. A downloaded OAuth client JSON has no refresh token and
cannot authorize unattended uploads by itself.

1. In Google Cloud Console select the backup project. Open **Google Auth
   Platform → Clients → Create client**; use **Web application** and add
   this Authorized redirect URI: https://developers.google.com/oauthplayground
   Create client and keep its web client ID and client secret.
2. Under **Google Auth Platform → Audience**, add your Drive account as a
   test user (if Testing). **Data access** should include
   https://www.googleapis.com/auth/drive.file . Testing refresh tokens can
   expire after seven days; use In production when permitted.
3. Open https://developers.google.com/oauthplayground and click ⚙.
   Set OAuth flow=Server-side, Access type=Offline, Force prompt=Consent
   Screen; enable **Use your own OAuth credentials** and paste your NEW
   WEB application ID and secret. Close settings.
4. In Step 1 paste scope https://www.googleapis.com/auth/drive.file .
   Click **Authorize APIs**, choose the Drive owner account, approve.
   In Step 2 click **Exchange authorization code for tokens**.
   Copy the REFRESH token (not the short-lived access token). Do not use
   the Playground default client: its refresh tokens expire in 24 hours.
5. Go to GitHub repository Settings → Secrets and variables → Actions →
   New repository secret. Name: **GDRIVE_BACKUP_OAUTH_JSON**. Value is a
   JSON object with three keys: client_id, client_secret, refresh_token.
   Fill the values in GitHub only; never paste credentials in chat.
   Example with placeholders:

       {
         "client_id": "WEB_APPLICATION_CLIENT_ID",
         "client_secret": "WEB_APPLICATION_CLIENT_SECRET",
         "refresh_token": "GOOGLE_OAUTH_PLAYGROUND_REFRESH_TOKEN"
       }

6. Run GitHub Actions → EduSecure PDF Sync → Run workflow (main). Inspect
   Google Drive backup status in the run summary. Successful backups are
   stored in a PRIVATE 8aPDF_Backup folder under that Google account.

Existing three-secret configuration and local desktop OAuth helper still
work. The combined JSON takes precedence. Google Cloud's downloaded
web/installed client JSON or a service-account JSON alone is NOT sufficient
for writing into the owner's personal Google Drive quota.

Keep credentials and backed-up school data private. These backups do not
change Supabase, music files or frontend failover.
Optional worker variables: GDRIVE_BACKUP_INTERVAL_SECONDS (3600),
GDRIVE_BACKUP_TIMEOUT_SECONDS (240), GDRIVE_BACKUP_MAX_PDFS_PER_RUN (10),
GDRIVE_BACKUP_MAX_PDF_MIB (25), GDRIVE_BACKUP_ALLOWED_PDF_HOSTS
(comma-separated allowlisted exact HTTPS hostnames). URLs outside this
allowlist are intentionally skipped for safety. Files can only be restored
manually until a separate website fallback/restore service is built.

Never add OAuth tokens, school credentials, or private backup files to the
public GitHub repository or public URLs. Run offline regression tests with
**python -m unittest discover -v**.
