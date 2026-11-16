"""Content-only EduSecure PDF metadata. No AI calls, no scraped-title fallback.

The scraped school message is audit-only. PDF text is untrusted data: lines
that try to direct an LLM or rename the document are never followed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

PROMPT_VERSION = "pdf-content-deterministic-v1"
NEUTRAL_TITLE = "School Material"
MAX_DESCRIPTION_CHARS = 800
MAX_TITLE_CHARS = 160

SUBJECTS = (
    ("Mathematics", r"mathematics|maths?|गणित|ਗਣਿਤ"),
    ("Science", r"science|विज्ञान|ਵਿਗਿਆਨ"),
    ("Social Science", r"social\s+science|social\s+studies|s\.?s\.?t\.?"),
    ("English", r"english|अंग्रेज़ी|ਅੰਗਰੇਜ਼ੀ"),
    ("Hindi", r"hindi|हिंदी|हिन्दी"),
    ("Punjabi", r"punjabi|ਪੰਜਾਬੀ"),
    ("Computer", r"computer\s*(?:science|applications?|studies)?|informatics"),
    ("Art", r"art|drawing|चित्रकला|ਚਿੱਤਰਕਲਾ"),
)
BLOCK_META = re.compile(
    r"^(?:name|student\s*name|roll\s*(?:no\.?|number)|school|class|grade|section|"
    r"admission\s*no\.?|page\s*\d+|date|subject)\s*[:\-]?\s*",
    re.IGNORECASE,
)
INSTRUCTION_INJECTION = re.compile(
    r"(?i)\b(?:ignore\s+(?:all\s+)?(?:previous|above)\s+instructions|"
    r"you\s+are\s+(?:an?\s+)?(?:ai|assistant|chatgpt)|"
    r"system\s*(?:message|prompt)|developer\s*(?:message|prompt)|"
    r"override\s+(?:the\s+)?(?:title|subject|instructions)|"
    r"(?:set|change|rename|output)\s+(?:the\s+)?(?:pdf\s+)?(?:title|subject)\s+to)\b"
)
HEADINGS = re.compile(
    r"(?i)(?:\b(?:chapter|ch\.?|exercise|ex\.?|unit|lesson|worksheet|"
    r"question\s*paper|question\s*answers?|test\s*paper|class\s*test|"
    r"revision|assignment|homework|circular|notice|practice|"
    r"guidelines|questions?|project|notes?)\b|"
    r"पाठ|अध्याय|अभ्यास|प्रश्न|कार्यपत्रक|ਕੰਮ|ਪਾਠ|ਅਭਿਆਸ|ਪ੍ਰਸ਼ਨ)"
)
BARE_GENERIC = re.compile(
    r"(?i)^(?:school|class\s*[ivx0-9]+|grade\s*[ivx0-9]+|date|"
    r"worksheet|homework|assignment|notes?|practice|circular|test|questions?|"
    r"subject\s*:\s*.+)$"
)


def compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\u00a0", " ")).strip()


def lines_from_pdf(pdf_text: str) -> list[str]:
    lines: list[str] = []
    for value in str(pdf_text or "").splitlines()[:140]:
        line = compact(value)
        if not line or INSTRUCTION_INJECTION.search(line):
            continue
        if len(line) > 300:
            continue
        # A line copied from the PDF is evidence; NEVER enhance it from portal text.
        if line not in lines:
            lines.append(line)
    return lines


def explicit_subject(lines: list[str]) -> str:
    for line in lines[:25]:
        declared = re.match(r"(?i)^subject\s*[:\-]\s*(.{2,55})$", line)
        candidate = (declared.group(1) if declared else line).strip()
        for subject, pattern in SUBJECTS:
            if re.fullmatch(pattern, candidate, re.IGNORECASE):
                return subject
    return "General"


def document_heading(lines: list[str]) -> str:
    scored: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines[:45]):
        if len(line) > MAX_TITLE_CHARS or len(line) < 6:
            continue
        if BLOCK_META.match(line) or BARE_GENERIC.fullmatch(line):
            continue
        if re.fullmatch(r"[\d\W_]+", line) or re.search(r"(?i)\b(?:page\s+\d+\s+of\s+\d+|www\.|http[s]?:)", line):
            continue
        priority = 0
        if HEADINGS.search(line):
            priority += 4
        if ":" in line or "–" in line or " - " in line:
            priority += 1
        if re.search(r"\b\d+(?:\.\d+)*\b", line):
            priority += 1
        if priority < 4:
            continue
        scored.append((-priority, index, line))
    scored.sort()
    return scored[0][2] if scored else ""


def content_description(lines: list[str], heading: str) -> str:
    parts: list[str] = []
    total = 0
    if heading:
        parts.append(heading)
        total = len(heading)
    for line in lines[:32]:
        if line == heading or BLOCK_META.match(line) or BARE_GENERIC.fullmatch(line):
            continue
        if not parts and len(line) > 220:
            continue
        if total + len(line) + 2 > MAX_DESCRIPTION_CHARS:
            break
        parts.append(line)
        total += len(line) + 2
        if len(parts) >= 6:
            break
    return "\n".join(parts)


def metadata_from_pdf(pdf_text: str, read_status: str, *,
                      scraped_title: str = "", scraped_description: str = "") -> dict[str, Any]:
    status = str(read_status or "unavailable")
    readable = status == "readable" and bool(compact(pdf_text))
    lines = lines_from_pdf(pdf_text) if readable else []
    heading = document_heading(lines)
    subject = explicit_subject(lines)
    finished = readable and bool(heading)
    review = (
        "complete" if finished else
        "pending_content_read" if status in {"unavailable", "download-failed"} else
        "needs_review"
    )
    now = datetime.now(timezone.utc).isoformat() if readable or status in {"image-only", "not-a-pdf", "corrupt"} else None
    return {
        "title": heading if finished else NEUTRAL_TITLE,
        "subject": subject if finished else "General",
        "description": content_description(lines, heading) if finished else "",
        "raw": {
            "source_title_hint": str(scraped_title or "")[:300],
            "source_description_hint": str(scraped_description or "")[:1000],
            "evidence_source": "pdf_content" if finished else "unavailable",
            "prompt_version": PROMPT_VERSION,
            "review_status": review,
            "content_read_at": now,
            "pdf_read_status": status,
        },
    }
