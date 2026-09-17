from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional, Tuple

import requests

FIREBASE_PROJECT_ID = "academyvault-5d1eb"
FIREBASE_API_KEY = "AIzaSyATxKki6gkNWic_CnoGbZnOZjAUj1lbKGI"
FIREBASE_ADMIN_EMAIL = os.environ.get("FIREBASE_ADMIN_EMAIL") or "nullreaper.exe@gmail.com"
FIREBASE_ADMIN_PASSWORD = os.environ.get("FIREBASE_ADMIN_PASSWORD", "")

ACADEMIC_SUBJECTS = {
    "Mathematics",
    "Science",
    "English",
    "Hindi",
    "Punjabi",
    "French",
    "Sanskrit",
    "Computer",
    "Social Science",
    "Life Skills",
    "GK",
    "EVS",
    "Moral Science",
    "Physical Education",
    "Art",
    "Music",
    "IoT",
}

SUBJECT_ALIASES = {
    "math": "Mathematics",
    "maths": "Mathematics",
    "mathematics": "Mathematics",
    "science": "Science",
    "physics": "Science",
    "chemistry": "Science",
    "biology": "Science",
    "english": "English",
    "hindi": "Hindi",
    "punjabi": "Punjabi",
    "french": "French",
    "sanskrit": "Sanskrit",
    "computer": "Computer",
    "computer science": "Computer",
    "ict": "Computer",
    "artificial intelligence": "Computer",
    "ai": "Computer",
    "social science": "Social Science",
    "social studies": "Social Science",
    "sst": "Social Science",
    "history": "Social Science",
    "geography": "Social Science",
    "civics": "Social Science",
    "political science": "Social Science",
    "life skills": "Life Skills",
    "life skill": "Life Skills",
    "general knowledge": "GK",
    "gk": "GK",
    "evs": "EVS",
    "environmental studies": "EVS",
    "moral science": "Moral Science",
    "value education": "Moral Science",
    "physical education": "Physical Education",
    "pe": "Physical Education",
    "art": "Art",
    "music": "Music",
    "iot": "IoT",
    "internet of things": "IoT",
}

EXPLICIT_SUBJECT_RE = re.compile(
    r"\b(Artificial\s+Intelligence|Internet\s+of\s+Things|Computer\s+Science|"
    r"Political\s+Science|Social\s+Science|Social\s+Studies|Environmental\s+Studies|"
    r"General\s+Knowledge|Moral\s+Science|Value\s+Education|Physical\s+Education|Life\s+Skills?|"
    r"Mathematics|Maths|Math|Science|Physics|Chemistry|Biology|English|Hindi|Punjabi|"
    r"French|Sanskrit|Computer|ICT|AI|IoT|SST|History|Geography|Civics|GK|EVS|Art|Music|PE)\b",
    re.I,
)

SCHOOL_PATTERNS = (
    r"\bManav\s+Mangal\s+SMART\s+SCHOOL(?:\s*-?\s*88)?\b",
    r"\bManav\s+Mangal\s+Smart\s+School(?:\s*-?\s*88)?\b",
    r"\bSMART\s+SCHOOL(?:\s*-?\s*88)?\b",
    r"\bMMSS\s*-?\s*88\b",
    r"\bMMSS88\b",
)

UI_JUNK_PATTERNS = (
    r"\bPay\s*Now\b",
    r"\bPDF\s*Material\b",
    r"\bPreview\b",
    r"\bDownload(?:\s*PDF)?\b",
    r"\bAttachment(?:s)?\b",
    r"\bClick\s+Here\b",
    r"\bOpen\b",
    r"\bView\b",
    r"\bSchool\s*Diary\b",
)

GREETING_PATTERNS = (
    r"\bDear\s+Parents?\b",
    r"\bDear\s+Students?\b",
    r"\bDear\s+All\b",
    r"\bGood\s+Morning\b",
    r"\bGood\s+Afternoon\b",
    r"\bGood\s+Evening\b",
    r"\bKindly\b",
    r"\bPlease\b",
    r"\bThanks\b",
    r"\bThank\s+You\b",
)

ADMIN_WORD_RE = re.compile(
    r"\b(circular|notice|announcement|holiday|fee|fees|transport|bus|event|timetable|"
    r"schedule|parent meeting|ptm|school closure|closed tomorrow|uniform)\b",
    re.I,
)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_subject(value: Any) -> str:
    raw = clean(value)
    if not raw:
        return ""
    return SUBJECT_ALIASES.get(raw.lower(), raw)


def _iter_text(evidence: Any) -> Iterable[str]:
    if evidence is None:
        return []
    if isinstance(evidence, (list, tuple, set)):
        return [clean(x) for x in evidence if clean(x)]
    if isinstance(evidence, dict):
        return [clean(v) for v in evidence.values() if clean(v)]
    value = clean(evidence)
    return [value] if value else []


def _canonical_subject(value: Any) -> Optional[str]:
    subject = normalize_subject(value)
    return subject if subject in ACADEMIC_SUBJECTS else None


def classify_subject(
    evidence: Any,
    current_subject: Any = "",
    explicit_subject: Any = "",
) -> Tuple[str, int, Dict[str, int]]:
    """Confidence-based academic classifier.

    Academic evidence always outranks administrative labels. Existing valid
    academic subjects are preserved when new evidence is weak/ambiguous.
    """
    parts = list(_iter_text(evidence))
    raw = " \n ".join(parts)
    current = normalize_subject(current_subject)
    explicit = normalize_subject(explicit_subject)
    scores: Dict[str, int] = {}

    def add(subject: str, points: int) -> None:
        subject = normalize_subject(subject)
        if subject in ACADEMIC_SUBJECTS:
            scores[subject] = scores.get(subject, 0) + points

    if explicit in ACADEMIC_SUBJECTS:
        add(explicit, 100)

    for match in re.finditer(r"(?:^|\b)(?:Subject|Sub)\s*[:\-]\s*([^|\n]{1,60})", raw, flags=re.I):
        named = EXPLICIT_SUBJECT_RE.search(match.group(1))
        if named:
            canonical = _canonical_subject(named.group(1))
            if canonical:
                add(canonical, 100)

    for named in EXPLICIT_SUBJECT_RE.finditer(raw):
        canonical = _canonical_subject(named.group(1))
        if canonical:
            add(canonical, 80)

    if re.search(r"[\u0A00-\u0A7F]", raw):
        add("Punjabi", 90)
    sanskrit_hits = len(re.findall(
        r"(?:संस्कृत(?:म्)?|श्लोक(?:ः|म्)?|सुभाषित|धातुरूप|शब्दरूप|सन्धि|समास|संस्कृत\s*व्याकरण)",
        raw,
        flags=re.I,
    ))
    if sanskrit_hits:
        add("Sanskrit", 95 + min(15, sanskrit_hits * 5))
    elif re.search(r"[\u0900-\u097F]", raw):
        add("Hindi", 85)

    keyword_rules = {
        "Mathematics": [
            r"\balgebra\b", r"\bgeometry\b", r"\barithmetic\b", r"\bfractions?\b",
            r"\bdecimals?\b", r"\bintegers?\b", r"\brational\s+numbers?\b",
            r"\blinear\s+equations?\b", r"\bequations?\b", r"\bmensuration\b",
            r"\bpercentages?\b", r"\bratio\b", r"\bproportion\b", r"\bexponents?\b",
            r"\bpowers?\b", r"\bsquare\b", r"\bcube\b", r"\barea\b", r"\bperimeter\b",
            r"\bvolume\b", r"\bgraphs?\b", r"\bcoordinate\s+geometry\b",
            r"\bfactorisation\b", r"\bfactorization\b", r"\bquadrilateral\b",
            r"\btriangle\b", r"\bpolygon\b", r"\bdata\s+handling\b", r"\bprobability\b",
            r"\bdirect\s+and\s+inverse\s+proportion\b", r"\bcomparing\s+quantities\b",
        ],
        "Science": [
            r"\bforce\b", r"\bfriction\b", r"\bcells?\b", r"\bmicroorganisms?\b",
            r"\bcombustion\b", r"\bphotosynthesis\b", r"\bmetals?\b", r"\bnon[- ]?metals?\b",
            r"\breproduction\b", r"\bchemical\s+reactions?\b", r"\belectricity\b",
            r"\bcurrent\b", r"\blight\b", r"\bsound\b", r"\bcrop\s+production\b",
            r"\bcoal\b", r"\bpetroleum\b", r"\bconservation\b", r"\bpollution\b",
        ],
        "English": [
            r"\benglish\s+grammar\b", r"\benglish\s+literature\b", r"\bcomprehension\b",
            r"\breading\s+comprehension\b", r"\bprose\b", r"\bpoem\b", r"\bpoetry\b",
            r"\bcreative\s+writing\b", r"\bletter\s+writing\b", r"\bnotice\s+writing\b",
            r"\barticle\s+writing\b", r"\bdiary\s+entry\b", r"\bspeech\s+writing\b",
            r"\btenses?\b", r"\bdeterminers?\b", r"\bvoice\b", r"\bvocabulary\b", r"\bidioms?\b",
        ],
        "French": [
            r"\bfrench\b", r"\bfrançais(?:e)?\b", r"\bgrammaire\b", r"\bvocabulaire\b",
            r"\bconjugaison\b", r"\bcompréhension\b", r"\bfrench\s+unseen\s+passage\b",
            r"\bunseen\s+passage\s+in\s+french\b",
        ],
        "Computer": [
            r"\bcomputer\b", r"\bcomputer\s+science\b", r"\bict\b", r"\bcoding\b",
            r"\bprogramming\b", r"\bpython\b", r"\bhtml\b", r"\bcss\b", r"\bjavascript\b",
            r"\bdatabase\b", r"\bnetworking\b", r"\bcybersecurity\b", r"\bspreadsheet\b",
            r"\bexcel\b", r"\bpowerpoint\b", r"\bartificial\s+intelligence\b", r"\bai\b",
            r"\bmachine\s+learning\b", r"\bcomputer\s+vision\b", r"\bnlp\b",
            r"\bnatural\s+language\s+processing\b", r"\bchatbots?\b",
        ],
        "Social Science": [
            r"\bsst\b", r"\bsocial\s+science\b", r"\bsocial\s+studies\b", r"\bhistory\b",
            r"\bgeography\b", r"\bcivics\b", r"\bpolitical\s+science\b", r"\bconstitution\b",
            r"\bparliament\b", r"\bjudiciary\b", r"\bdemocracy\b", r"\bresources?\b",
            r"\bagriculture\b", r"\bindustries\b", r"\bmap\s+work\b",
        ],
        "Life Skills": [
            r"\blife\s+skills?\b", r"\bsel\b", r"\bsocial\s+emotional\s+learning\b",
            r"\bself[- ]management\b", r"\bempathy\b", r"\bdecision\s+making\b",
            r"\binterpersonal\s+skills?\b", r"\bcommunication\s+skills?\b",
        ],
    }

    for subject, patterns in keyword_rules.items():
        for pattern in patterns:
            hits = len(re.findall(pattern, raw, flags=re.I))
            if hits:
                add(subject, 15 * min(hits, 3))

    exercise_hits = len(re.findall(r"\bexercise\s*(?:no\.?\s*)?\d+(?:\.\d+)+\b", raw, flags=re.I))
    if exercise_hits:
        add("Mathematics", 45 + min(20, (exercise_hits - 1) * 10))
    if exercise_hits and re.search(r"\bq(?:uestion)?\s*no\.?\s*\d+\b", raw, flags=re.I):
        add("Mathematics", 25)

    if re.search(r"\bfrench\s+unseen\s+passage\b|\bunseen\s+passage\b.*\bfrench\b", raw, flags=re.I):
        add("French", 40)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_subject = ranked[0][0] if ranked else ""
    best_score = ranked[0][1] if ranked else 0
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    if current in ACADEMIC_SUBJECTS:
        if not best_subject:
            return current, 0, scores
        if best_subject == current:
            return current, best_score, scores
        if best_score < 60 or best_score < second_score + 15:
            return current, best_score, scores

    if best_subject and best_score >= 30 and best_score >= second_score + 5:
        return best_subject, best_score, scores

    admin_score = 0
    admin_score += 20 * len(ADMIN_WORD_RE.findall(raw))
    if re.search(r"\b(?:school|parent|parents|student|students)\b", raw, flags=re.I):
        admin_score += 10

    if admin_score >= 20:
        return "Circular", admin_score, scores

    if current and current not in {"General", "School Diary", "Message", "Announcement", "Notice"}:
        return current, 0, scores
    return "General", 0, scores


def detect_subject(evidence: Any, current_subject: Any = "", explicit_subject: Any = "") -> str:
    return classify_subject(evidence, current_subject=current_subject, explicit_subject=explicit_subject)[0]


def _remove_subject_from_title(text: str, subject: str) -> str:
    subject = normalize_subject(subject)
    if not subject or subject in {"Circular", "General", "Life Skills"}:
        return text

    aliases = {subject}
    if subject == "Mathematics":
        aliases.update({"Math", "Maths"})
    elif subject == "Social Science":
        aliases.update({"SST", "Social Studies"})

    for alias in sorted(aliases, key=len, reverse=True):
        escaped = re.escape(alias)
        text = re.sub(rf"^\s*(?:Subject\s*[:\-]\s*)?{escaped}\s*[:\-–—|]+\s*", "", text, flags=re.I)
        text = re.sub(rf"^\s*{escaped}\s+", "", text, flags=re.I)
        text = re.sub(rf"\bSubject\s*[:\-]\s*{escaped}\b", "", text, flags=re.I)
    return text


def _strip_common_junk(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""

    for pattern in SCHOOL_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)

    text = re.sub(r"\bPFA\s+(?:of\s+)?Circular(?:\s+No\.?\s*[:#-]?\s*\d+)?\b", " ", text, flags=re.I)
    text = re.sub(r"\bCircular\s+No\.?\s*[:#-]?\s*\d+\b", " ", text, flags=re.I)
    text = re.sub(r"\bCircular\b", " ", text, flags=re.I)
    text = re.sub(r"\bPFA\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:academic\s+)?session\s+20\d{2}\s*[-/]\s*(?:20)?\d{2}\b", " ", text, flags=re.I)
    text = re.sub(r"\b20\d{2}\s*[-/]\s*(?:20)?\d{2}\b", " ", text)
    text = re.sub(r"\bShining\s+Manavite\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:Message|Announcement)\s*[:\-]?\b", " ", text, flags=re.I)

    for pattern in UI_JUNK_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)
    for pattern in GREETING_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.I)

    text = re.sub(r"\bHome\s*Work\s*[:\-]\s*", " ", text, flags=re.I)
    text = re.sub(r"\bClass\s*Work\s*[:\-]\s*", " ", text, flags=re.I)
    text = re.sub(r"\bnote\s+the\b", " ", text, flags=re.I)
    text = re.sub(r"\b(?:Regards|Warm\s+Regards|Best\s+Regards)\b.*$", " ", text, flags=re.I)

    text = re.sub(r"\s*[,;|]+\s*", " - ", text)
    text = re.sub(r"\s*[-–—]{2,}\s*", " - ", text)
    text = re.sub(r"(?:\s+-\s+){2,}", " - ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")
    return text


def sanitize_title(value: Any, subject: Any = "") -> str:
    original = clean(value)
    text = _strip_common_junk(original)
    text = _remove_subject_from_title(text, clean(subject))

    text = re.sub(r"\bQ\s*no\.?\s*(\d+)\b", r"Q No. \1", text, flags=re.I)
    text = re.sub(r"\bQuestion\s*no\.?\s*(\d+)\b", r"Question No. \1", text, flags=re.I)
    text = re.sub(r"\bExercise\s*(\d+(?:\.\d+)*)\b", r"Exercise \1", text, flags=re.I)
    text = re.sub(r"\bcorrection\s+in\b", "Correction in", text, count=1, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -:|,.;")

    if text:
        return text[:160].rstrip(" -:|,.;")

    if ADMIN_WORD_RE.search(original):
        return "School Notice"
    return "Study Material"


def sanitize_description(value: Any, subject: Any = "", fallback_title: Any = "") -> str:
    text = sanitize_title(value, subject)
    if text in {"Study Material", "School Notice"} and clean(fallback_title):
        fallback = sanitize_title(fallback_title, subject)
        if fallback not in {"Study Material"}:
            return fallback
    return text


def semantic_duplicate_key(source_date: Any, title: Any, subject: Any) -> str:
    raw_date = clean(source_date)
    day = ""
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", raw_date)
    if m:
        day = "-".join(m.groups())
    elif isinstance(source_date, (date, datetime)):
        day = source_date.date().isoformat() if isinstance(source_date, datetime) else source_date.isoformat()

    canonical_subject = normalize_subject(subject)
    clean_title = sanitize_title(title, canonical_subject).lower()
    clean_title = re.sub(r"[^\w\u0900-\u097F\u0A00-\u0A7F]+", " ", clean_title, flags=re.UNICODE)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    return "|".join((day, canonical_subject.lower(), clean_title)) if day and clean_title else ""


def finalize_material_fields(item: Dict[str, Any]) -> Dict[str, str]:
    """Final safety gate used immediately before Firestore upload."""
    original_evidence = item.get("_evidence") or [
        item.get("original_title"),
        item.get("title"),
        item.get("description"),
        item.get("homework"),
        item.get("classwork"),
        item.get("message"),
        item.get("category"),
    ]
    current_subject = normalize_subject(item.get("subject"))
    subject = detect_subject(original_evidence, current_subject=current_subject)
    if subject == "General" and current_subject in ACADEMIC_SUBJECTS:
        subject = current_subject
    if not subject or subject == "School Diary":
        subject = "Circular" if ADMIN_WORD_RE.search(" ".join(_iter_text(original_evidence))) else "General"

    title = sanitize_title(item.get("title") or item.get("original_title") or item.get("description"), subject)
    description = sanitize_description(item.get("description") or item.get("title"), subject, fallback_title=title)
    return {
        "title": title,
        "subject": normalize_subject(subject),
        "description": description or title,
        "url": clean(item.get("url") or item.get("pdf_url")),
    }


def firebase_sign_in() -> str:
    if not FIREBASE_ADMIN_PASSWORD:
        raise RuntimeError("Missing FIREBASE_ADMIN_PASSWORD")
    response = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
        json={
            "email": FIREBASE_ADMIN_EMAIL,
            "password": FIREBASE_ADMIN_PASSWORD,
            "returnSecureToken": True,
        },
        timeout=25,
    )
    response.raise_for_status()
    token = response.json().get("idToken", "")
    if not token:
        raise RuntimeError("Firebase sign-in returned no idToken")
    return token


def headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def decode_value(value: Dict[str, Any]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "timestampValue", "integerValue", "doubleValue", "booleanValue"):
        if key in value:
            return value[key]
    return None


def list_materials(token: str) -> list[Dict[str, Any]]:
    url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents/study_materials"
    params: Dict[str, Any] = {"pageSize": 1000, "key": FIREBASE_API_KEY}
    out: list[Dict[str, Any]] = []
    while True:
        response = requests.get(url, params=params, headers=headers(token), timeout=30)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        body = response.json()
        for raw in body.get("documents", []):
            fields = {k: decode_value(v) for k, v in (raw.get("fields") or {}).items()}
            fields["_name"] = raw.get("name", "")
            out.append(fields)
        next_token = body.get("nextPageToken")
        if not next_token:
            break
        params["pageToken"] = next_token
    return out


def patch_material_fields(
    document_name: str,
    token: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    subject: Optional[str] = None,
) -> bool:
    fields: Dict[str, Dict[str, str]] = {}
    masks = []
    if title is not None:
        fields["title"] = {"stringValue": title}
        masks.append(("updateMask.fieldPaths", "title"))
    if description is not None:
        fields["description"] = {"stringValue": description}
        masks.append(("updateMask.fieldPaths", "description"))
    if subject is not None:
        fields["subject"] = {"stringValue": subject}
        masks.append(("updateMask.fieldPaths", "subject"))
    if not fields:
        return True

    response = requests.patch(
        f"https://firestore.googleapis.com/v1/{document_name}",
        params=[("key", FIREBASE_API_KEY), *masks],
        headers=headers(token),
        json={"fields": fields},
        timeout=25,
    )
    return response.ok


def main() -> int:
    token = firebase_sign_in()
    materials = list_materials(token)
    checked = 0
    fixed = 0
    subject_fixed = 0

    for item in materials:
        source = clean(item.get("source")).lower()
        if "edusecure" not in source:
            continue

        checked += 1
        old_title = clean(item.get("title"))
        old_description = clean(item.get("description"))
        old_subject = normalize_subject(item.get("subject"))
        evidence = [old_title, old_description]
        new_subject = detect_subject(evidence, current_subject=old_subject)
        if new_subject == "General" and old_subject in ACADEMIC_SUBJECTS:
            new_subject = old_subject
        if new_subject == "General" and old_subject.lower() == "circular" and ADMIN_WORD_RE.search(" ".join(evidence)):
            new_subject = "Circular"

        new_title = sanitize_title(old_title, new_subject)
        new_description = sanitize_description(old_description or old_title, new_subject, fallback_title=new_title)

        changes: Dict[str, str] = {}
        if new_title != old_title:
            changes["title"] = new_title
        if new_description != old_description:
            changes["description"] = new_description
        if new_subject and new_subject != old_subject:
            changes["subject"] = new_subject

        if not changes:
            continue

        if patch_material_fields(
            clean(item.get("_name")),
            token,
            title=changes.get("title"),
            description=changes.get("description"),
            subject=changes.get("subject"),
        ):
            fixed += 1
            if "subject" in changes:
                subject_fixed += 1
            date_text = clean(item.get("source_date"))
            print(
                "✅ CLEANED "
                f"date={date_text or '(unknown)'} | "
                f"title: {old_title!r} -> {new_title!r} | "
                f"subject: {old_subject or '(blank)'} -> {new_subject or '(blank)'}"
            )
        else:
            print(f"❌ Could not patch: {old_title[:100]}")

    print(
        "EduSecure intelligence cleanup complete: "
        f"checked={checked}, records_fixed={fixed}, subjects_fixed={subject_fixed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
