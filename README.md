# EduSecure → 8aPDF Supabase Sync

This repository syncs **new EduSecure school PDFs and teacher announcements** to the new, independent 8aPDF Supabase project:
- Project URL: https://xudhjwojgxblbsebkzso.supabase.co
- Tables: `public.study_materials` and `public.announcements`
- GitHub workflow: `.github/workflows/sync.yml`; entrypoint: `sync_repair.py`
- Schedule: approximately every five minutes (GitHub scheduling is best-effort).
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
- Workflow tests run before it attempts to log in to EduSecure.
- Active sync fails closed if the OIDC identity or live Supabase inventory cannot be verified. It does not use stale Firestore caches for duplicate decisions.
- A message is only marked uploaded after the Edge function confirms the insert or preexisting record.
- GitHub Actions serializes overlapping scheduled runs; operations are idempotent for safe retries.
- All diagnostics are recorded in `sync_report.json` and attached to the run.

The old Firestore cleanup functions remain in older migration/legacy helper code for backwards-compatible tests; **they are never dispatched from the scheduled Supabase sync**. Do not run those legacy cleanup helpers against the new database.

## Verify the automation

1. Open [EduSecure PDF Sync workflow](https://github.com/nullreaperexe-cloud/edusecure-pdf-sync/actions/workflows/sync.yml).
2. Use **Run workflow → main → Run workflow**, or inspect the latest scheduled/push run.
3. Confirm **Validate automation code** passes and the next step shows:
   `Supabase 8aPDF backend authorized through GitHub Actions OIDC.`
4. Look for both `Verified Supabase study_materials inventory` and `Verified Supabase announcement IDs`, followed by new PDF/announcement confirmations whenever eligible source messages exist.
5. Check [8aPDF Supabase](https://supabase.com/dashboard/project/xudhjwojgxblbsebkzso/editor). The website should read the two tables using its public publishable key with RLS-enforced read access.

A code commit or passing local unit tests **does not alone prove** a live EduSecure login or published records. An actual successful GitHub Actions run is required to confirm the end-to-end path.

## Known separate feature

Older school push notification wake-up depended on Firebase Authentication. It has been disabled in the main sync rather than allowing that retired dependency to break PDF/announcement publication. Push delivery requires a separate Supabase-compatible implementation and is not guaranteed by this migration.
