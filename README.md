# EduSecure PDF Sync → 8aPDF Supabase

The active GitHub Actions worker runs `sync_repair.py` every ~5 minutes (GitHub schedules are best-effort). It reads EduSecure ParentApp attachments, verifies PDF links, cleans titles and subjects, skips existing exact PDF URLs, and publishes **new PDF metadata directly to the Supabase `study_materials` table** that the 8aPDF website now reads.

## Required GitHub Actions secrets

Go to **Repo → Settings → Secrets and variables → Actions → New repository secret** and add:

- `EDUSECURE_USERNAME` — EduSecure login.
- `EDUSECURE_PASSWORD` — EduSecure password.
- **`SUPABASE_SECRET_KEY` — private server-side Supabase secret key** (`sb_secret_...`) or legacy service-role key, from the *same* Supabase project used by 8aPDF.
- `FIREBASE_ADMIN_PASSWORD` — optional for PDFs, but still necessary for the **existing Firestore-based teacher announcements and push wake-up**. `FIREBASE_ADMIN_EMAIL` can override the existing email.
- `OPENROUTER_API_KEY` — optional; clean deterministic titles are used if AI is unavailable.

**Never use the public `sb_publishable_...` key as `SUPABASE_SECRET_KEY`.** Public keys are designed for untrusted browser reads under RLS. Never paste a server-side secret in chat, code, issues or logs. The worker refuses to run without the server-side secret instead of falling back to the wrong PDF database.

The workflow sets `MATERIAL_BACKEND=supabase` and `SUPABASE_URL=https://rzhtesfikvykdnmrqwjs.supabase.co`. The Python code also defaults to Supabase. `MATERIAL_BACKEND=firestore` remains available only as a **manual legacy recovery option**; do not enable it for normal 8aPDF publication.

## Expected Supabase table columns

The existing `public.study_materials` table must allow the automation to insert the following fields:

| Field | Recommended PostgreSQL type | Purpose |
|---|---|---|
| `title` | `text` | Checked PDF heading |
| `subject` | `text` | Normalized school subject |
| `description` | `text` | Safe short description |
| `pdf_url` | `text` | Original source PDF URL (exact case and query) |
| `source` | `text` | `EduSecure GitHub Sync` |
| `source_date` | `timestamptz` | School message date |
| `order` | `integer` | Existing newest-first ordering |
| `created_at` | `timestamptz` | Automated UTC insertion time |

The table should also have an `id` primary key with a working default (UUID or integer). If the table has additional required columns without defaults, reconcile its schema before running. The worker deliberately does **not** auto-migrate or delete any database schema or historical PDFs.

For best concurrency safety, apply a **unique constraint/index to `pdf_url` after removing any existing URL duplicates**. The worker checks live URLs before inserting, rechecks after uncertain network responses, and GitHub Actions serializes sync jobs, but a database unique index is the strongest protection against other concurrent writers.

## Public website access and security

The 8aPDF browser uses a **publishable** key to read PDFs. Its Supabase Row Level Security (RLS) must allow the intended public `SELECT` rows; a successful private GitHub write does not guarantee the website can read. **Do not create an anonymous/public INSERT, UPDATE or DELETE policy to make automation work.** Admin writes should only use the private worker key. Teacher announcements remain in Firestore until a separate Supabase announcements table/migration is configured.

Only PDF metadata and the school-provided URL are stored; the worker does not copy school documents into Supabase Storage. Historical Firestore PDFs are **not** migrated automatically. For existing website records to appear in the new Supabase library, they must already be in `study_materials` or be migrated separately.

## Reliability and testing

Run `python -m unittest discover -v` and `python -m py_compile runner.py sync_repair.py supabase_materials.py`. The workflow runs tests automatically before opening the browser.

- Entire Supabase catalogue is paginated and validated; the sync **fails closed** on read errors rather than creating duplicates from a partial snapshot.
- New PDFs are published using exact case-sensitive URLs and verified server responses.
- A timed-out POST is **not blindly replayed**; the worker rereads Supabase to determine whether it committed.
- Existing Firestore cleanup/mutation routines are bypassed for PDF records under the active Supabase backend.
- Firestore teacher announcement processing remains separate and optional for PDF publication; Firebase login errors no longer prevent Supabase PDF work.
- Error summaries and screenshots are saved to `sync_report.json` and GitHub Actions artifacts.
- The old browser-based website date check (`CHECK_WEBSITE_DATE=1`) is optional; the current maintenance homepage will not display PDF dates.

Push notifications still use an existing worker that expects Firebase identity; PDF publication **does not imply that Supabase-sourced PDF push notifications have been migrated**. Confirm notification delivery separately.

## Check a live run

1. Open [EduSecure PDF Sync Actions](https://github.com/nullreaperexe-cloud/edusecure-pdf-sync/actions/workflows/sync.yml).
2. After creating `SUPABASE_SECRET_KEY`, click **Run workflow → main → Run workflow**.
3. Inspect **Validate automation code** and **Run one EduSecure sync cycle**.
4. Confirm the summary includes `Supabase study_materials loaded:` and `Supabase PDF publication confirmed` when new PDFs exist.
5. Check [8aPDF materials JSON](https://the-8apdf.vercel.app/api/materials) and your Supabase Table Editor. PDFs must appear in **Supabase**, not the old Firestore collection.

If you see HTTP 401/403, check the Supabase secret and project; HTTP 400/404/422 usually indicates REST/table/schema mismatch. If no new PDFs are eligible, a green run may not insert anything.

**Important:** GitHub commits are not proof of a successful end-to-end sync. A real run with correctly configured secrets, table schema, and school login is required for confirmation.
