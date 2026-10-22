"""Bounded repair of EXISTING EduSecure PDF titles in 8aPDF Supabase.

Uses GitHub OIDC -> restricted Edge Function only. No Firebase password,
service-role key, publicly writable table, or PDF deletion is required.
"""
from __future__ import annotations
import os
import re
import sys

import openrouter_title
import title_cleaner as intelligence
import title_quality as quality
import supabase_gateway as gateway

def ai_candidate_needed(title: str, locally_cleaned: str) -> bool:
    return bool(
        len(locally_cleaned.split()) > 11
        or re.search(r"\b(?:find attached|attached (?:pdf|file)|kindly|please|dear students|"
                     r"complete it|were done|bring your|tomorrow|reminder:)\b", title, flags=re.I)
        or locally_cleaned in {"Study Material", "School Notice"}
    )

def choose_title(record: dict, allow_ai: bool) -> tuple[str, bool]:
    original = str(record.get("title") or "").strip()
    subject = intelligence.normalize_subject(record.get("subject"))
    local = quality.polish_title(original, subject)
    if not local or local in {"Study Material", "School Notice"}:
        return original, False
    used_ai = False
    result = local
    if allow_ai and openrouter_title.OPENROUTER_API_KEY and ai_candidate_needed(original, local):
        # Stored title/description are the ONLY historical evidence available.
        # Do not claim verification against PDF contents that were not extracted.
        evidence = [original]
        desc = str(record.get("description") or "").strip()
        if desc and desc != original:
            evidence.append(desc)
        proposed = openrouter_title.generate_title(evidence, subject, fallback_title=local)
        if proposed and quality.supported_by_evidence(proposed, evidence):
            result = proposed
            used_ai = True
    if result != original and not quality.supported_by_evidence(result, [original, record.get("description") or ""]):
        return original, used_ai
    return result, used_ai

def main() -> int:
    gateway.health()
    records = gateway.call("listMaterials").get("records")
    if not isinstance(records, list):
        raise RuntimeError("Supabase material inventory unavailable")
    limit = max(0, min(30, int(os.environ.get("TITLE_CLEANUP_MAX_AI_CALLS", "12"))))
    dry_run = os.environ.get("TITLE_CLEANUP_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    checked = 0
    corrected = 0
    skipped = 0
    ai_calls = 0
    failures = 0
    for row in records:
        if not isinstance(row, dict) or "edusecure" not in str(row.get("source") or "").lower():
            continue
        item_id = str(row.get("id") or "")
        if not item_id.startswith("edusecure-"):
            continue
        title_before = str(row.get("title") or "").strip()
        if not title_before:
            continue
        checked += 1
        local = quality.polish_title(title_before, row.get("subject"))
        need_ai = ai_candidate_needed(title_before, local)
        allow_ai = bool(need_ai and ai_calls < limit and openrouter_title.OPENROUTER_API_KEY)
        if allow_ai:
            ai_calls += 1
        title_after, _ = choose_title(row, allow_ai)
        if title_after == title_before:
            skipped += 1
            continue
        if dry_run:
            print(f"DRY-RUN: {title_before!r} -> {title_after!r}")
            corrected += 1
            continue
        try:
            result = gateway.call("updateMaterialTitle",
                id=item_id, expected_title=title_before, title=title_after)
            if result.get("updated"):
                corrected += 1
                print(f"Title corrected: {title_before[:85]!r} -> {title_after!r}")
            elif result.get("skipped"):
                skipped += 1
            else:
                raise RuntimeError("Supabase did not confirm title edit or safe skip")
        except Exception as exc:
            failures += 1
            print(f"Title correction failed for {item_id[:28]}: {type(exc).__name__}")
    print(f"Supabase EduSecure title cleanup: checked={checked} corrected={corrected} "
          f"unchanged_or_skipped={skipped} AI_attempts={ai_calls} failures={failures} "
          f"dry_run={dry_run}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
