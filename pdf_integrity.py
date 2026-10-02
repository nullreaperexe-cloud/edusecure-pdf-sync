"""Conservative PDF content/title verification for EduSecure uploads.

No remote PDF text is sent to AI. A missing/unreadable PDF never authorizes an
unrelated AI title: the original EduSecure message remains the fallback.
"""
from __future__ import annotations

import io
import re
from typing import Any, Optional, Tuple

import requests
from pypdf import PdfReader


MAX_PDF_BYTES = 12 * 1024 * 1024
STOP_WORDS = {
    "class", "school", "study", "material", "materials", "pdf", "the",
    "and", "for", "with", "from", "this", "that", "about", "your",
    "guidelines", "notice", "circular", "students", "student",
    "chapter", "question", "questions", "homework",
}


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def significant_words(value: Any) -> set[str]:
    return {
        word for word in re.findall(r"[a-z]{3,}", compact(value).lower())
        if word not in STOP_WORDS
    }


def fetch_pdf_text(pdf_url: str) -> Tuple[str, str]:
    """Return (extracted text, status). Unavailable PDFs are never guessed."""
    try:
        response = requests.get(
            pdf_url,
            timeout=(7, 18),
            headers={"User-Agent": "8aPDF-content-verifier/1.0"},
            stream=True,
        )
        response.raise_for_status()
        size = int(response.headers.get("Content-Length") or 0)
        if size > MAX_PDF_BYTES:
            return "", "too-large"
        chunks = []
        total = 0
        for part in response.iter_content(chunk_size=65536):
            if not part:
                continue
            total += len(part)
            if total > MAX_PDF_BYTES:
                return "", "too-large"
            chunks.append(part)
        data = b"".join(chunks)
        if not data.startswith(b"%PDF"):
            return "", "not-a-pdf"
        pdf = PdfReader(io.BytesIO(data), strict=False)
        texts = []
        for page in pdf.pages[:3]:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(texts)[:14000].strip()
        return text, ("readable" if len(compact(text)) >= 45 else "image-only")
    except Exception as exc:
        print(f"PDF text verification unavailable ({type(exc).__name__}); using message evidence")
        return "", "unavailable"


def evidence_title(pdf_text: str, message_text: str) -> str:
    """Only infer titles from unambiguous, document-supported patterns."""
    text = compact(pdf_text)
    low = text.lower()
    if re.search(r"\bchess\b", low) and re.search(r"\b(tournament|fide|prize)\b", low):
        return "Interschool Chess Tournament Achievement"
    if re.search(r"\bshining manavite\b", low):
        return "Shining Manavite Student Achievement"
    # A circular without extractable text can safely carry the message's stated topic.
    msg = compact(message_text).lower()
    if not text and "shining manavite" in msg:
        return "Shining Manavite Student Achievement"
    return ""


def select_verified_title(
    message_text: str,
    proposed_title: str,
    fallback_title: str,
    pdf_text: str,
) -> Tuple[str, str]:
    """Reject invented topics rather than publishing a confidently wrong title."""
    proposed = compact(proposed_title)
    fallback = compact(fallback_title) or "Study Material"
    verified = evidence_title(pdf_text, message_text)
    if verified:
        if "chess" in verified.lower() and "chess" in proposed.lower():
            return proposed, "pdf-and-ai-agree"
        if "shining manavite" in verified.lower() and "shining manavite" in proposed.lower():
            return proposed, "pdf-and-ai-agree"
        return verified, "document-topic-override"

    # Prefer actual PDF content when its text layer is usable. Otherwise the
    # EduSecure message is the only evidence; never assume an AI guess is true.
    evidence = pdf_text if len(compact(pdf_text)) >= 45 else message_text
    proposed_words = significant_words(proposed)
    evidence_words = significant_words(evidence)
    overlap = proposed_words & evidence_words
    required = 2 if len(proposed_words) >= 3 else 1
    if len(overlap) < required:
        return fallback, "unverified-ai-title"

    # A class/grade explicitly written in the document outranks the generic
    # Class 8 context used in earlier OpenRouter prompts.
    explicit_grade = re.search(r"\b(?:class|grade)\s*(?:vi|vii|viii|ix|6|7|8|9)\b", evidence, re.I)
    if explicit_grade and re.search(r"\bclass\s*(?:8|viii)\b", proposed, re.I):
        grade = explicit_grade.group(0).lower().replace("grade", "").replace("class", "").strip()
        if grade not in {"viii", "8"}:
            return fallback, "document-grade-mismatch"

    return proposed or fallback, "verified-evidence-overlap"
