# EduSecure PDF Sync

This GitHub Actions workflow checks the EduSecure ParentApp every hour and sends newly found PDF metadata to the 8aPDF Study Library ingest endpoint. Duplicate links are ignored by the website.

## Required repository secrets

Add these under **Settings → Secrets and variables → Actions → New repository secret**:

- `EDUSECURE_USERNAME`
- `EDUSECURE_PASSWORD`
- `AUTOMATION_TOKEN` (the token configured for the 8aPDF website)

The workflow is also available from **Actions → EduSecure PDF Sync → Run workflow** for a manual test.

Only metadata and the source PDF URL are sent. The workflow does not store passwords in the repository.
