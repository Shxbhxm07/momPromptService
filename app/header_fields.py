"""The minutes' header details — file reference, telephone, venue, secretary … — from every place that can give
them, in the user's order of priority (2026-10-14):

    1. what the JOB sets: its `questionnaire` (the backend's form), then `mom_meta`
    2. what the user's PROMPT says
    3. what the attached DOCUMENTS say
   (then, as before: what the user gave in earlier messages of the conversation, and the template's standing details)

THE QUESTIONNAIRE has the correspondence service's shape — "questionnaire": [{"question": "file reference number",
"answer": "AMCID12323"}] — and a plain {"file reference number": "AMCID12323"} is read too. A question is matched to
a header field by its wording (ALIASES; a typo such as "file refernce number" still matches). Every answer is ALSO a
template slot named after its question ("file reference number" → {{ file_reference_number }}), so a client's own
fill-in template can ask for any field its form has, not only the ones Appendix AD prints.

THE PROMPT AND THE DOCUMENTS are read by one short model call each, beside the minutes writer, and the answers are
not trusted: a value is kept only when it is written in that text (nothing invented), and a classification only as
the one grade word the prompt names. From the documents only the issuing office's details are taken — telephone,
address, file reference, secretary, distribution: a source document's own date, copy number, precedence or
classification belong to that document, not to these minutes. The documents' venue, date and time are the minutes
writer's already (it reads them whole, and meeting_date checks its date).
"""
import difflib
import json
import logging
import re
import unicodedata
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("header")

# Every header field the minutes print, as the renderer's `meta` names it. "title" is the meeting's title, which
# lives in the minutes themselves (mom["title"]) rather than in the header.
FIELDS = ("venue", "meeting_date", "meeting_time", "telephone", "address", "file_ref", "issue_date", "precedence",
          "copy_no", "classification", "amendments_by", "secretary_name", "secretary_rank", "distribution")
DOCUMENT_FIELDS = ("telephone", "address", "file_ref", "secretary_name", "secretary_rank", "distribution")

# How a form may ask for each field. Matched on the question's words, lower case, punctuation ignored.
ALIASES = {
    "file_ref": ["file reference number", "file reference no", "file reference", "file ref no", "file ref",
                 "file number", "file no", "reference number", "reference no", "ref no", "letter number",
                 "letter no", "फ़ाइल संख्या", "फ़ाइल नंबर", "फ़ाइल सं", "संदर्भ संख्या", "पत्र संख्या"],
    "telephone": ["telephone number", "telephone no", "telephone", "tele no", "tele", "tel no", "tel", "phone number",
                  "phone no", "phone", "ph no", "ph", "contact number", "contact no", "mobile number", "mobile no",
                  "mobile", "mob no", "दूरभाष", "दूरभाष संख्या", "फ़ोन", "फ़ोन नंबर", "मोबाइल नंबर"],
    "issue_date": ["date of issue", "issue date", "dated", "letter date", "date of letter"],
    "venue": ["venue", "meeting venue", "venue of meeting", "venue of the meeting", "place", "place of meeting",
              "meeting place", "location", "held at", "meeting held at", "स्थान", "बैठक का स्थान"],
    "meeting_date": ["meeting date", "date of meeting", "date of the meeting", "date", "held on", "meeting held on",
                     "दिनांक", "तारीख़", "तिथि", "बैठक की तारीख़", "बैठक की तिथि", "बैठक का दिनांक"],
    "meeting_time": ["meeting time", "time of meeting", "time of the meeting", "time", "start time",
                     "meeting start time", "समय", "बैठक का समय"],
    "address": ["address", "originator address", "office address", "unit address", "hq address", "address of hq",
                "address of the hq", "address of office", "headquarters address", "पता", "कार्यालय का पता"],
    "classification": ["classification", "security classification", "security grading"],
    "precedence": ["precedence"],
    "copy_no": ["copy number", "copy no"],
    "amendments_by": ["amendments by", "amendments date", "date for amendments", "last date for amendments"],
    "secretary_name": ["secretary", "secretary name", "name of secretary", "name of the secretary", "सचिव",
                       "सचिव का नाम"],
    "secretary_rank": ["secretary rank", "rank of secretary", "rank of the secretary"],
    "distribution": ["distribution", "distribution list", "वितरण", "वितरण सूची"],
    "title": ["title", "meeting title", "title of meeting", "title of the meeting", "subject", "meeting subject",
              "subject of meeting", "subject of the meeting", "topic", "विषय", "बैठक का विषय"],
}
_CLOSE = 0.88                 # how alike a question must be to an alias: one wrong letter in "file reference number"


_NUKTA = "़"


def _words(text: Any) -> List[str]:
    """The words of a question, lower case, in any script. A Devanagari word keeps its vowel signs: they are marks,
    not letters, so a plain \\w+ would split "फ़ाइल" in two."""
    text = unicodedata.normalize("NFC", str(text or "").lower()).replace("।", " ").replace("॥", " ")
    return re.findall(r"[^\W_][\wऀ-ॿ]*", text)


def _phrase(question: str) -> str:
    """The question to match: its words without a bracketed hint ("Date (DD/MM/YYYY)" asks for the date), and
    without the nukta, so फ़ाइल and फाइल are the same word."""
    return " ".join(_words(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", str(question or "")).replace(_NUKTA, "")))


_BY_ALIAS = {_phrase(alias): f for f, aliases in ALIASES.items() for alias in aliases}


def slot_name(question: str) -> str:
    """The template slot an answer fills: "File Reference No." → "file_reference_no", "फ़ाइल संख्या" → "फ़ाइल_संख्या"."""
    return "_".join(_words(question))


def field_of(question: str) -> str:
    """The header field a question asks for, or "" when it is none of them."""
    phrase = _phrase(question)
    if not phrase:
        return ""
    if phrase in _BY_ALIAS:
        return _BY_ALIAS[phrase]
    close = difflib.get_close_matches(phrase, list(_BY_ALIAS), n=1, cutoff=_CLOSE)
    return _BY_ALIAS[close[0]] if close else ""


# ── putting a value where the renderer reads it ─────────────────────────────────────────────────────────────
def _split(value: str) -> List[str]:
    """Lines separated by ";" or new lines, or — when there is only one — by commas."""
    parts = [p.strip() for p in re.split(r"\s*[;\n]\s*", value) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in parts[0].split(",") if p.strip()]
    return parts


def put(meta: Dict[str, Any], field: str, value: Any) -> None:
    """Set one header field in `meta`, in the shape the renderer reads (the same shapes a re-prompt writes)."""
    value = value if isinstance(value, (list, dict)) else str(value if value is not None else "").strip()
    if not value:
        return
    if field in ("secretary_name", "secretary_rank"):
        sec = dict(meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {})
        sec[field.split("_", 1)[1]] = value
        meta["secretary"] = sec
    elif field == "address":
        meta["address"] = value if isinstance(value, list) else _split(value)
    elif field == "distribution":
        rows = []
        for entry in (value if isinstance(value, list) else _split(value)):
            bits = [b.strip() for b in str(entry).split("|")] + ["", ""]
            if bits[0]:
                rows.append({"addressee": bits[0], "copies": bits[1], "remarks": bits[2]})
        if rows:
            meta["distribution"] = rows
    elif field == "precedence":
        meta["precedence"] = str(value).upper()
    else:
        meta[field] = value


def _empty(meta: Dict[str, Any], field: str) -> bool:
    if field in ("secretary_name", "secretary_rank"):
        sec = meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {}
        return not str(sec.get(field.split("_", 1)[1]) or meta.get(field) or "").strip()
    v = meta.get(field)
    return not v or (isinstance(v, str) and not v.strip())


# ── 1. the job's own values ─────────────────────────────────────────────────────────────────────────────────
def from_questionnaire(questionnaire: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(header values by field — "title" included —, every answer by its question as asked)."""
    fields: Dict[str, str] = {}
    for question, answer in (questionnaire or {}).items():
        field = field_of(question)
        if field:
            fields[field] = answer
    return fields, dict(questionnaire or {})


def slots(answers: Dict[str, Any]) -> Dict[str, str]:
    """Every answer by its slot name; a later question with the same slot name ("Venue", "venue") wins. A Hindi slot
    is there with its nukta and without it, so {{ फ़ाइल_संख्या }} and {{ फाइल_संख्या }} both work."""
    out: Dict[str, str] = {}
    for question, answer in (answers or {}).items():
        slot = slot_name(question)
        if slot:
            out[slot] = answer
            out[slot.replace(_NUKTA, "")] = answer
    return out


def inputs(job) -> Tuple[Dict[str, Any], str]:
    """The header the job itself sets — its questionnaire over its mom_meta — and the title it sets ("" if none).

    Every answer also goes in meta["fields"], by its question as asked, for the template (see minutes_template)."""
    meta = json.loads(json.dumps(job.mom_meta or {}, default=str))      # a deep copy the job cannot see changed
    fields, answers = from_questionnaire(job.questionnaire)
    title = fields.pop("title", "")
    for field, value in fields.items():
        put(meta, field, value)
    if answers:
        meta["fields"] = {**(meta.get("fields") if isinstance(meta.get("fields"), dict) else {}), **answers}
    unmatched = [q for q in job.questionnaire if not field_of(q)]
    if job.questionnaire:
        logger.info(f"[JOB {job.conversation_id}] questionnaire: {len(job.questionnaire)} answer(s)"
                    + (f", header fields {sorted(set(fields) | ({'title'} if title else set()))}" if fields or title else "")
                    + (f"; template slots only: {unmatched}" if unmatched else ""))
    return meta, title


# ── 2. and 3. the prompt and the documents, read by the model and checked ───────────────────────────────────
_DESCRIBE = {
    "venue": "where THIS meeting was held",
    "meeting_date": "the date THIS meeting was held — never the date of an earlier or a next meeting",
    "meeting_time": "the time THIS meeting was held",
    "telephone": "the telephone number of the issuing office or the secretary",
    "address": 'the issuing office\'s address, its lines separated by " ; "',
    "file_ref": "the file reference number of the minutes",
    "issue_date": 'the date of issue, written after "dt" beside the file reference',
    "precedence": "the precedence, e.g. IMMEDIATE, PRIORITY, ROUTINE",
    "copy_no": "the copy number",
    "classification": "the security classification: RESTRICTED, CONFIDENTIAL, SECRET, TOP SECRET or UNCLASSIFIED",
    "amendments_by": "the last date for amendments to the minutes",
    "secretary_name": "the name of the secretary who signs the minutes",
    "secretary_rank": "the rank of that secretary",
    "distribution": 'who receives copies of the minutes, separated by " ; "',
}

_SYSTEM = """You read a {what} and pick out details for the header of Minutes of Meeting — only the ones the text states.

Fields:
{fields}

Rules:
- Copy each value EXACTLY as the text writes it. Never invent, complete, correct or reformat a value.
- A detail the text does not state is "".
- A placeholder or an example is not a value ("Telephone number here", "xx/yy", "File number comes here", dotted lines)."""

_WHAT = {"prompt": "user's REQUEST for minutes of a meeting", "documents": "SOURCE DOCUMENT of a meeting"}
MAX_TEXT_CHARS = 20000
_HEAD, _TAIL = 12000, 8000    # a header sits at the top of a document, the signature and distribution at the end
MAX_VALUE_CHARS = 300
MAX_REPLY_TOKENS = 800
_MONTHS = {"january": "jan", "february": "feb", "march": "mar", "april": "apr", "june": "jun", "july": "jul",
           "august": "aug", "september": "sep", "october": "oct", "november": "nov", "december": "dec"}


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[:_HEAD] + "\n…\n" + text[-_TAIL:]


def _norm(text: str) -> str:
    """Words and numbers only, lower case, months short, ordinals plain: "12th September" → "12 sep"."""
    words = [_MONTHS.get(w, w) for w in _words(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(text or ""), flags=re.I))]
    return " ".join(words)


def written_in(value: str, text: str) -> bool:
    """Is `value` written in `text` — the same words in the same order, punctuation and case aside?"""
    v = _norm(value)
    return bool(v) and f" {v} " in f" {_norm(text)} "


def _ask_model(kind: str, text: str, wanted: List[str]) -> Dict[str, Any]:
    """One call. Raises on a transport error; {} for a reply that is not usable JSON."""
    from mom import _get_writer
    system = _SYSTEM.format(what=_WHAT[kind], fields="\n".join(f"- {f}: {_DESCRIBE[f]}" for f in wanted))
    schema = {"type": "object", "additionalProperties": False, "required": list(wanted),
              "properties": {f: {"type": "string"} for f in wanted}}
    reply = _get_writer().generate(
        system, "TEXT:\n" + _clip(text), max_new_tokens=MAX_REPLY_TOKENS, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "header_details", "schema": schema, "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


def _checked(kind: str, data: Dict[str, Any], text: str, wanted: List[str], tag: str) -> Dict[str, Any]:
    """The model's values that are really in `text`, in the renderer's shapes. What is refused is logged."""
    from template_details import is_placeholder
    meta: Dict[str, Any] = {}
    for field in wanted:
        value = re.sub(r"[ \t]+", " ", str(data.get(field) or "")).strip()
        if not value:
            continue
        if field == "telephone":
            value = re.sub(r"^(tele(phone)?|tel|ph(one)?)\s*(no\.?)?\s*[:.\-]?\s*", "", value, flags=re.I)
        why = ""
        if len(value) > MAX_VALUE_CHARS:
            why = "too long"
        elif is_placeholder(value):
            why = "a placeholder"
        elif field == "classification":
            # Only the ONE grade the text names: "top secret" is never read as SECRET, and nothing is inferred.
            from minutes_edit import _grade_of, _grades_in
            grade = _grade_of(value)
            if not grade or _grades_in(text) != [grade]:
                why = f"the text names {', '.join(_grades_in(text)) or 'no grade'}"
            value = grade
        elif not all(written_in(part, text) for part in (_split(value) if field in ("address", "distribution")
                                                         else [value])):
            why = "not written in it"
        if why:
            logger.info(f"{tag} {field} {value!r} from the {kind} refused: {why}")
            continue
        put(meta, field, value)
    return meta


def read(kind: str, text: str, have: Dict[str, Any], tag: str) -> Dict[str, Any]:
    """The header details `text` (the prompt, or the documents) states, for the fields `have` does not already
    hold. Never raises: a failed call costs these details, never the job."""
    fields = FIELDS if kind == "prompt" else DOCUMENT_FIELDS
    wanted = [f for f in fields if _empty(have, f)]
    if not (text or "").strip() or not wanted:
        return {}
    try:
        found = _checked(kind, _ask_model(kind, text, wanted), text, wanted, tag)
    except Exception as e:
        logger.warning(f"{tag} header details from the {kind} not read ({type(e).__name__}: {e})")
        return {}
    if found:
        logger.info(f"{tag} header details from the {kind}: {sorted(found)}")
    return found


# ── the merge ───────────────────────────────────────────────────────────────────────────────────────────────
def merge(top: Dict[str, Any], layers: List[Tuple[str, Dict[str, Any]]]) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """`top` (the job's own values) filled from each layer in turn, highest priority first: a layer gives only
    what every layer above it left empty. Returns (the header, the fields each layer gave).

    The secretary is one person: a lower layer's name and rank are taken only when no layer above named one, so a
    name from the prompt is never printed over a rank that belonged to someone in a document."""
    meta = json.loads(json.dumps(top or {}, default=str))
    used: Dict[str, List[str]] = {}
    for name, layer in layers:
        layer = layer if isinstance(layer, dict) else {}
        for key, value in layer.items():
            if not value or key in ("secretary_name", "secretary_rank"):
                continue
            if key == "secretary":
                mine = dict(meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {})
                if str(mine.get("name") or meta.get("secretary_name") or "").strip() or not isinstance(value, dict):
                    continue
                took = {k: v for k, v in value.items() if v and not str(mine.get(k) or "").strip()}
                if took:
                    meta["secretary"] = {**mine, **took}
                    used.setdefault(name, []).append("secretary")
            elif key == "fields" and isinstance(value, dict):
                mine = meta.get("fields") if isinstance(meta.get("fields"), dict) else {}
                meta["fields"] = {**value, **mine}
            elif _empty(meta, key):
                meta[key] = value
                used.setdefault(name, []).append(key)
    return meta, used
