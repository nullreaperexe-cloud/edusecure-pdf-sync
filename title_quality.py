"""Conservative PDF title grammar and source-fidelity checks.

Never invent educational subjects, chapter numbers, dates or assignment details.
Rules apply to English titles; Hindi/Punjabi text is left in its original script.
"""
from __future__ import annotations
import re
from typing import Any
import title_cleaner as intelligence

FILLER = {
    "please", "kindly", "the", "of", "to", "for", "in", "on", "at", "a", "an",
    "and", "with", "pdf", "attached", "attachment", "find", "refer", "students",
    "student", "your", "you", "this", "that", "have", "has", "been", "is",
    "are", "it", "dear", "file", "document", "download", "shared",
}
STOP_ENDINGS = r"(?:and|or|of|for|to|from|in|on|with|the|a|an|also|if|but)"

def polish_title(value: Any, subject: Any = "") -> str:
    """Shorten obvious message boilerplate without replacing real subject matter."""
    title = intelligence.sanitize_title(value, subject)
    if not title or title in {"Study Material", "School Notice"}:
        return title
    if re.search(r"[\u0900-\u097f\u0a00-\u0a7f]", title):
        return title
    title = re.sub(
        r"^(?:please\s+)?(?:find|refer\s+to|see|check)\s+(?:the\s+)?"
        r"(?:attached\s+)?(?:pdf|document|file|attachment)\s*"
        r"(?:of|on|for|about|regarding|below)?\s*",
        "", title, flags=re.I,
    )
    title = re.sub(r"\s*[\(\[]\s*(?:pdf\s+)?attached\s*[\)\]]", "", title, flags=re.I)
    title = re.sub(r"^(?:please\s+)?(?:find|see)\s+attached\s+(?:the\s+)?", "", title, flags=re.I)
    title = re.sub(r"^(?:pdf|attachment)\s*(?:of|for|on|:|-)\s*", "", title, flags=re.I)
    title = re.sub(r"^notes\s+of\s+chapter\s+(\d+(?:\.\d+)*)\b", r"Chapter \1 Notes", title, flags=re.I)
    title = re.sub(r"\s*(?:\bpdf\b)\s*(?:is\s+)?attached\.?$", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" \t-–—:|,.;")
    # The source UI often truncates the last sentence mid-clause.
    title = re.sub(r"\s+" + STOP_ENDINGS + r"$", "", title, flags=re.I).strip(" -:|,.;")
    if not title:
        return intelligence.sanitize_title(value, subject)
    # Keep identifiers (Q3, 7.2, AI, IoT, PDFs) and existing mixed case intact.
    if title[:1].islower():
        title = title[0].upper() + title[1:]
    return title[:120].rstrip(" -:|,.;")

def supported_by_evidence(candidate: str, evidence: Any) -> bool:
    """Reject hallucinations; grammar rewrites must retain real source vocabulary."""
    source = " ".join(str(s) for s in evidence) if isinstance(evidence, (tuple, list)) else str(evidence or "")
    cand = str(candidate or "").strip()
    if not cand or not source or len(cand) > 120:
        return False
    if re.search(r"[\u0900-\u097f\u0a00-\u0a7f]", cand):
        return bool(re.search(r"[\u0900-\u097f\u0a00-\u0a7f]", source))
    source_nums = set(re.findall(r"\b\d+(?:\.\d+)*\b", source))
    candidate_nums = set(re.findall(r"\b\d+(?:\.\d+)*\b", cand))
    if not candidate_nums.issubset(source_nums):
        return False
    def words(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in FILLER}
    sw, cw = words(source), words(cand)
    if not cw:
        return False
    if not sw:
        return True
    return len(sw & cw) >= min(2, len(cw)) and len(sw & cw) / len(cw) >= 0.5
