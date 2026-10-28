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
    # PDF-specific teacher-message templates observed in live 8aPDF records.
    # Extract documented topics without inventing chapter content.
    worksheet_key = re.match(
        r"^find\s+(?:the\s+)?attached\s+answer\s+key\s+of\s+worksheet\s+on\s+"
        r"(.+?)(?:\.\s*reminder\s*:.*)?$", title, flags=re.I,
    )
    if worksheet_key:
        title = worksheet_key.group(1).strip(" .") + " Worksheet Answer Key"
    title = re.sub(
        r"^question\s+answers\s+of\s+chapter\s+(\d+(?:\.\d+)*)\s+were\s+done\b.*$",
        r"Chapter \1 Question Answers", title, flags=re.I,
    )
    worksheet_practice = re.match(
        r"^practice\s+(?:the\s+)?worksheet\s+of\s+(?:(?:Mathematics|Maths)\s+)?"
        r"(?:chapter\s+)?(.+?)\s+in\s+(?:a\s+)?separate\s+notebook\b.*$",
        title, flags=re.I,
    )
    if worksheet_practice:
        title = worksheet_practice.group(1).strip(" .") + " Worksheet"
    bring_books = re.match(
        r"^bring\s+(.+?)\s+tomorrow\.\s+also\s*-\s*find\s+attached\s+the\s+pdf\b",
        title, flags=re.I,
    )
    if bring_books:
        title = bring_books.group(1).strip(" .")
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
    # Never translate Hindi/Punjabi into English or erase named lesson words.
    # Require strong native-script token overlap; grammar cannot change the topic.
    for script in (r"[\u0900-\u097f]+", r"[\u0a00-\u0a7f]+"):
        original_words = set(re.findall(script, source))
        if original_words:
            rewritten_words = set(re.findall(script, cand))
            if not rewritten_words or len(original_words & rewritten_words) < max(
                1, (len(rewritten_words) * 3 + 3) // 4
            ):
                return False
            if len(rewritten_words - original_words) > 1:
                return False
            # On longer Hindi/Punjabi messages a short AI paraphrase can drop
            # the actual poem/lesson/person name while retaining generic words.
            # Fail closed instead of silently deleting that information.
            if len(original_words) > 4 and len(original_words & rewritten_words) * 5 < len(original_words) * 3:
                return False
    if re.search(r"[\u0900-\u097f\u0a00-\u0a7f]", cand):
        return True
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


MAX_TITLE_LENGTH = 40
_INCOMPLETE_ENDINGS = frozenset({
    "and", "or", "of", "for", "to", "from", "in", "on", "with", "the",
    "a", "an", "also", "if", "but", "about", "regarding", "your", "our",
    "my", "their", "at", "by", "into", "as", "this", "that",
})


def fit_title(value: Any, subject: Any = "", *, source_text: Any = "",
              fallback: str = "School Update") -> str:
    """Meaningful, readable school title of at most 40 Unicode characters.

    Not a fixed 40-character slice: remove app fragments, prefer complete
    phrases, preserve identifiers, and trim at word boundaries as a last resort.
    Full original prose stays in description/originalMessage.
    """
    title = polish_title(value, subject)
    original = title
    source = str(source_text or "")
    if not title or title in {"Study Material", "School Notice"}:
        return fallback

    # The EduSecure preview often appends part of the next Class Work sentence.
    title = re.split(r"\.\s+(?=[A-Z])", title, maxsplit=1)[0].strip(" -–—:|,.;")
    title = re.sub(r"\s+in\s+your\s+own$", "", title, flags=re.I)
    title = re.sub(r"\s+(?:tomorrow\s+)?and\s+come\b.*$", "", title, flags=re.I)
    title = re.sub(
        r"^(?:we\s+are\s+)?(?:delighted|pleased|happy)\s+to\s+announce\s+(?:the\s+)?",
        "", title, flags=re.I,
    )
    title = re.sub(r"^(?:kindly|please)\s+", "", title, flags=re.I)
    title = re.sub(
        r"^(?:English|Computer|Maths?|Mathematics|Science|Hindi|Punjabi|French|Social Science)\s*[-:–]\s*",
        "", title, flags=re.I,
    )

    # A misleading preview like "we are delighted to announce the results of"
    # has no topic. Recover the named result from the *full* message if present.
    if re.fullmatch(r"(?:the\s+)?results?\s*(?:of|for)?", title, flags=re.I):
        match = re.search(
            r"\bresults?\s+(?:of|for)\s+(.+?)(?:\.\s|[;:\n]|$)",
            source, flags=re.I,
        )
        if match:
            topic = re.sub(r"\s+", " ", match.group(1)).strip(" -–—:|,.;")
            candidate = topic + " Results"
            if 8 <= len(candidate) <= MAX_TITLE_LENGTH:
                title = candidate
        if re.fullmatch(r"(?:the\s+)?results?\s*(?:of|for)?", title, flags=re.I):
            title = "School Results"

    if len(title) > MAX_TITLE_LENGTH:
        # Keep the useful named lesson/book and real exercise identifiers.
        title = re.sub(r"\bWorksheet Answer Key\b", "Answer Key", title, flags=re.I)
        match = re.search(r"\bBBC\s+Compacta\s+Module\s+\d+\b", title, flags=re.I)
        if match and re.match(r"^Bring\b", title, flags=re.I):
            title = "Bring " + match.group(0)
        title = re.sub(
            r"\bWrite\s*/\s*print\s+programs?\s+of\s+chapter\s+(\d+(?:\.\d+)*)\b",
            r"Write/Print Chapter \1 Programs", title, flags=re.I,
        )
    if len(title) > MAX_TITLE_LENGTH:
        title = re.sub(r"\bChapter\b", "Ch.", title, flags=re.I)
    if len(title) > MAX_TITLE_LENGTH:
        title = re.sub(r"\s+and\s+", " & ", title, flags=re.I)

    # Last resort: drop whole words, never cut a word or leave "of/and/to".
    words = title.split()
    while words and len(" ".join(words)) > MAX_TITLE_LENGTH:
        words.pop()
    while words and re.sub(r"[^\w]+", "", words[-1]).lower() in _INCOMPLETE_ENDINGS:
        words.pop()
    if words and re.fullmatch(r"module|chapter|exercise|unit|no\.?", words[-1], flags=re.I):
        # Do not strand an academic identifier without its accompanying number.
        if re.search(r"\b" + re.escape(words[-1]) + r"\s*\d+", original, flags=re.I):
            words.pop()

    result = " ".join(words).strip(" -–—:|,.;")
    if len(result) < 8 or not re.search(r"\w", result):
        return fallback
    if result[:1].islower():
        result = result[0].upper() + result[1:]
    return result
