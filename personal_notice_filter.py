"""Protect private ParentApp notices from the shared 8aPDF library.

Class-wide homework, exams, circulars and general parent reminders remain public.
A student identifier is stored as a digest instead of publishing their name in
this public repository. Additional names can be configured via the private
EDUSECURE_PRIVATE_STUDENT_NAMES GitHub Actions secret.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import unicodedata
from typing import Any

# Digests of the intended student's first-name token and full-name variants.
# Never add names or other identifiable personal information to this file.
_PROTECTED_HASHES = frozenset({
    "924abc170fd6fc74a8518132092d68ecd35b72dd56372c231ecf81b007870bf7",
    "765b15e9a784ded0721efe8d6c78c2e6149c632a9b0e660f67e4af0a8904af51",
})

# Attendance notifications addressed to one child are private even if the
# card has omitted or truncated the child's name.
_PRIVATE_ATTENDANCE = (
    re.compile(r"\battendance\b.{0,180}\byour\s+child\b.{0,140}\b(?:absent|present|late|on\s+leave)\b"),
    re.compile(r"\byour\s+child\b.{0,100}\b(?:is|was|has\s+been|marked)\b.{0,100}\b(?:absent|present|late)\b"),
)


def _normalize(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    value = value.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _protected_hashes() -> set[str]:
    protected = set(_PROTECTED_HASHES)
    # Comma/semicolon/newline-separated names, read from Actions secrets only.
    for name in re.split(r"[,;\n|]+", os.getenv("EDUSECURE_PRIVATE_STUDENT_NAMES", "")):
        normalized = _normalize(name)
        if normalized:
            protected.add(_hash(normalized))
            words = normalized.split()
            if words and len(words[0]) >= 4:
                protected.add(_hash(words[0]))
    for digest in re.split(r"[,;\n| ]+", os.getenv("EDUSECURE_PRIVATE_STUDENT_HASHES", "")):
        if re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            protected.add(digest.lower())
    return protected


def personal_notice_reason(*parts: Any) -> str:
    """Return the reason for excluding a personal notice, else empty string.

    Examine ORIGINAL preview/detail text as well as final title/description.
    Do not rely on Groq's generated title to decide whether a notice is private.
    """
    text = _normalize(" ".join(
        json.dumps(p, ensure_ascii=False, default=str) if isinstance(p, (dict, list)) else str(p or "")
        for p in parts
    ))
    if not text:
        return ""
    protected = _protected_hashes()
    tokens = text.split()
    if any(len(t) >= 4 and _hash(t) in protected for t in tokens):
        return "protected_student_name"
    if any(_hash(" ".join(tokens[i:i+2])) in protected for i in range(len(tokens)-1)):
        return "protected_student_name"
    if any(pattern.search(text) for pattern in _PRIVATE_ATTENDANCE):
        return "individual_attendance"
    return ""


def is_personal_notice(*parts: Any) -> bool:
    return bool(personal_notice_reason(*parts))
