from __future__ import annotations

import os
import re
import time
from typing import Any, Iterable

import requests

import title_cleaner as intelligence

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_FREE_MODEL = "openrouter/free"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _iter_evidence(evidence: Any) -> Iterable[str]:
    if evidence is None:
        return []
    if isinstance(evidence, (list, tuple, set)):
        return [_clean(x) for x in evidence if _clean(x)]
    value = _clean(evidence)
    return [value] if value else []



def _final_title_cleanup(value: Any, subject: Any = "") -> str:
    """Hard post-filter: AI output can never bypass title safety rules."""
    text = _clean(value)

    # Remove dates BEFORE the normal sanitizer changes punctuation/separators.
    months = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?"
    )
    text = re.sub(
        rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,)?\s+20\d{{2}}\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})(?:,)?\s+20\d{{2}}\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", " ", text)
    text = re.sub(r"\b\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}\b", " ", text)
    text = re.sub(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        " ",
        text,
        flags=re.I,
    )

    # Now apply the mature deterministic cleaner as the final safety gate.
    text = intelligence.sanitize_title(text, subject)
    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")
    return text or "Study Material"

def generate_title(evidence: Any, subject: Any = "", fallback_title: Any = "") -> str:
    """Return a short AI title using only OpenRouter's free router.

    Safe behavior:
    - OPENROUTER_API_KEY is read only from the environment.
    - Model is always openrouter/free.
    - No paid-model fallback exists.
    - Any API/rate-limit/format error falls back to the existing deterministic cleaner.
    """
    canonical_subject = intelligence.normalize_subject(subject)
    fallback = _final_title_cleanup(
        fallback_title or "Study Material",
        canonical_subject,
    )

    if not OPENROUTER_API_KEY:
        print("OpenRouter key not configured; using local title cleaner.")
        return fallback

    parts = list(_iter_evidence(evidence))
    if not parts:
        return fallback

    # Remove obvious school/app chrome before sending text to the model.
    # Subject detection elsewhere still uses the original unmodified evidence.
    compact = []
    for part in parts:
        cleaned = intelligence._strip_common_junk(part)
        if cleaned and cleaned not in compact:
            compact.append(cleaned)

    source_text = "\n".join(compact or parts)[:3500].strip()
    if not source_text:
        return fallback

    system_prompt = (
        "You write titles for a Class 8 study-material library. "
        "Return exactly ONE short title only. No quotes, markdown, label, or explanation. "
        "Use correct English grammar and be straight to the point. Preserve the real "
        "topic, chapter, exercise, worksheet, passage, correction, assignment, or notice. "
        "Never include any date, day/month/year, session or academic year, school name, "
        "school code, Dear Student/Students/Parent/Parents, greetings, PFA, Attachment, "
        "Pay Now, Download, Preview, Circular, Circular No., message-type labels, "
        "teacher signatures, or UI filler. Do not repeat the subject name when it is "
        "provided separately. Prefer 3-10 useful words. Never invent unsupported details."
    )
    user_prompt = (
        f"Subject: {canonical_subject or 'Unknown'}\n"
        f"Safe fallback title: {fallback}\n"
        f"EduSecure text:\n{source_text}\n\n"
        "Output only the final clean title."
    )

    payload = {
        "model": OPENROUTER_FREE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 60,
    }
    headers = {
        "Authorization": "Bearer " + OPENROUTER_API_KEY,
        "Content-Type": "application/json",
        "HTTP-Referer": "https://eightapdf-study-library.nullreaper-exe.chatgpt.site/",
        "X-Title": "8aPDF EduSecure Title Cleaner",
    }

    # Retry once for transient free-router capacity/rate errors. We never select
    # a paid model; if free service remains unavailable we immediately use fallback.
    for attempt in range(2):
        try:
            response = requests.post(
                OPENROUTER_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=35,
            )
        except requests.RequestException as exc:
            print(f"OpenRouter request failed ({type(exc).__name__}).")
            if attempt == 0:
                time.sleep(1.5)
                continue
            return fallback

        if response.ok:
            try:
                body = response.json()
                choice = (body.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                answer = _clean(message.get("content"))
                answer = re.sub(
                    r"^(?:final\s+)?title\s*[:\-]\s*",
                    "",
                    answer,
                    flags=re.I,
                )
                answer = answer.splitlines()[0] if answer else ""
                answer = answer.strip(" \t\r\n\"'*_#-:")
                result = _final_title_cleanup(answer, canonical_subject)

                # Do not let vague/model-chatter responses replace a good local title.
                if (
                    result
                    and result not in {"Study Material"}
                    and not re.search(
                        r"\b(?:I cannot|I can't|here is|here's|unable to|as an AI)\b",
                        result,
                        flags=re.I,
                    )
                ):
                    model_used = _clean(body.get("model")) or OPENROUTER_FREE_MODEL
                    print(f"AI title generated with free OpenRouter model: {model_used}")
                    return result[:120].rstrip(" -:|,.;")
            except Exception as exc:
                print(f"OpenRouter returned unusable title ({type(exc).__name__}).")
            return fallback

        if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
            print(
                f"OpenRouter free route HTTP {response.status_code}; retrying once..."
            )
            time.sleep(1.5)
            continue

        print(
            f"OpenRouter free title unavailable (HTTP {response.status_code}); "
            "using local title cleaner."
        )
        return fallback

    return fallback
