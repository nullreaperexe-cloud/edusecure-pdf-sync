"""One-time conservative audit of existing EduSecure PDF titles.

Never deletes records or invents replacements. Automatically repairs only
unambiguous document-supported achievement titles (e.g. attached chess PDF).
Potential other mismatches are reported for a later targeted review.
"""
from __future__ import annotations

import os
from time import monotonic

import pdf_integrity as integrity
import title_cleaner as firestore


def main() -> int:
    token = firestore.firebase_sign_in()
    if not token:
        print("Firebase auth failed; no records changed.")
        return 2
    materials = firestore.list_materials(token)
    maximum_seconds = int(os.getenv("PDF_INTEGRITY_MAX_SECONDS", "2100"))
    deadline = monotonic() + maximum_seconds
    checked = corrected = unavailable = suspect = failed = 0

    print(f"Inspecting {len(materials)} existing study-material records")
    for doc in materials:
        if "edusecure" not in firestore.clean(doc.get("source")).lower():
            continue
        url = firestore.clean(doc.get("pdf_url"))
        if not url.lower().split("?", 1)[0].endswith(".pdf"):
            continue
        if monotonic() > deadline:
            print("Time budget exhausted before finishing the collection.")
            return 1

        checked += 1
        title = firestore.clean(doc.get("title"))
        pdf_text, status = integrity.fetch_pdf_text(url)
        if status != "readable":
            unavailable += 1
            continue
        supported_title = integrity.evidence_title(pdf_text, "")
        if not supported_title:
            # Do not overwrite arbitrary school titles without enough proof.
            _, reason = integrity.select_verified_title(
                "", title, title, pdf_text,
            )
            if reason == "unverified-ai-title":
                suspect += 1
                print(f"REVIEW ONLY: possible mismatch: {title!r} ({url.split('/')[-1][:72]})")
            continue
        replacement, reason = integrity.select_verified_title(
            "", title, title, pdf_text,
        )
        if reason != "document-topic-override" or replacement == title:
            continue
        description = firestore.clean(doc.get("description"))
        should_fix_description = not description or description == title
        subject = firestore.clean(doc.get("subject"))
        # Student achievement circular is not a school academic assignment.
        new_subject = None
        if supported_title == "Interschool Chess Tournament Achievement" and subject in {
            "Social Science", "Mathematics", "Science", "English", "Computer", "Hindi",
        }:
            new_subject = "Circular"

        print(f"Verified repair: {title!r} -> {replacement!r}")
        success = firestore.patch_material_fields(
            firestore.clean(doc.get("_name")), token,
            title=replacement,
            description=replacement if should_fix_description else None,
            subject=new_subject,
        )
        if success:
            corrected += 1
        else:
            failed += 1
            print("Firestore patch failed for verified PDF title")

    print(
        f"PDF AUDIT: examined={checked}, fixed={corrected}, "
        f"unreadable={unavailable}, review_only={suspect}, failed={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
