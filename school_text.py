"""Source-faithful EduSecure PDF description extraction.

Descriptions are never AI-written, title-cropped or silently paraphrased.
Only identifiable app chrome and calendar dates are removed. Keep the original
school instructions, punctuation, subject names and exercise numbers.
"""
from __future__ import annotations

import re
from typing import Any

MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
_DATE_PATTERNS = (
    # 2026-11-03 / 2026/11/03 (do not touch Exercise 6.1).
    r"(?<![\w.])20\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\w)",
    # 05-11-26 / 5/11/2026.
    r"(?<![\w.])(?:0?[1-9]|[12]\d|3[01])[-/.](?:0?[1-9]|1[0-2])[-/.](?:20\d{2}|\d{2})(?!\w)",
    # 3 Nov 2026 / 3rd November / Nov 3, 2026.
    rf"(?<![\w.])\d{{1,2}}(?:st|nd|rd|th)?(?:\s+of)?[\s\-]+(?:{MONTHS})\.?(?:[\s,\-]+20\d{{2}})?(?!\w)",
    rf"(?<![\w.])(?:{MONTHS})\.?[\s\-]+\d{{1,2}}(?:st|nd|rd|th)?(?:[\s,\-]+20\d{{2}})?(?!\w)",
)
_CHROME_TRAIL = re.compile(
    r"(?:\s+(?:Pay\s+Now|Class\s+Test|More|Attachments?)){1,8}\s*$",
    re.IGNORECASE,
)
_CHROME_LINE = re.compile(
    r"(?im)^\s*(?:Back|Close|Pay Now|Download(?: PDF)?|View Attachment|"
    r"Attachments?|Class Test|More)\s*$"
)


def _text(value: Any) -> str:
    result = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    result = re.sub(r"[\t ]+", " ", result)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def clean_school_description(value: Any) -> str:
    """Preserve teacher text verbatim apart from dates and page navigation.

    Never enforce the short title limit on source descriptions.
    """
    result = _text(value)
    if not result:
        return ""
    result = re.sub(
        r"(?i)^\s*(?:Message|School Diary|Circular|Announcement)\s+(?="
        r"(?:20\d{2}[-/.]|\d{1,2}[-/.]|"
        + MONTHS + r")\b)",
        "", result,
    )
    control = _CHROME_LINE.search(result)
    if control:
        result = result[:control.start()]
    result = _CHROME_TRAIL.sub("", result)
    for pattern in _DATE_PATTERNS:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE)
    # Remove leftover 'Date:' headings, but not lessons about historical dates.
    result = re.sub(r"(?i)(?<!\w)\b(?:date|dated)\s*[:\-]\s*(?=[,.;\s]|$)", "", result)
    result = re.sub(r" {2,}", " ", result)
    result = re.sub(r" *\n *", "\n", result)
    result = re.sub(r"\(\s*\)", "", result)
    result = re.sub(r"\s+([,.;!?])", r"\1", result)
    return result.strip(" \n\t-–—,;:")


def source_pdf_description(preview: Any, detail: Any = "") -> str:
    """Use full opened message only when it can be tied to this dashboard card.

    EduSecure can leave old detail DOM content mounted; never accidentally store
    a different student's or teacher's message as the PDF description.
    """
    short = clean_school_description(preview)
    long = clean_school_description(detail)
    if not short:
        return long
    if not long or len(long) <= len(short):
        return short

    # Match a substantial opening phrase; ignore UI whitespace differences.
    compact_short = re.sub(r"\s+", " ", short)
    compact_long = re.sub(r"\s+", " ", long)
    words = compact_short.split()
    if len(words) < 4:
        return short
    anchor = " ".join(words[:min(7, len(words))])
    match = re.search(re.escape(anchor), compact_long, flags=re.IGNORECASE)
    if not match:
        return short
    extracted = compact_long[match.start():].strip()
    # A complete source message must at least contain the dashboard preview.
    # If the preview was cut mid-word, compare the stable opening instead.
    if len(extracted) < len(compact_short):
        return short
    return extracted
