"""The issuing HQ's official minutes template → the standing details that go on every set of minutes.

WHAT A TEMPLATE CONTRIBUTES, AND WHAT IT DOES NOT. A JSSD minutes template (Vol I Part 2, Appendix AD)
holds three kinds of content, and only one of them is information:

  * LAYOUT fixed by the manual — headings, order, numbering, the Action/Info columns. docx_export.py
    already renders it and was checked against Appendix AD, so it is not taken from the template.
  * SPECIMEN placeholders — "Telephone number here", "Address line 1", "FIRING PRACTICE", dotted
    leaders. Meaningless, and dangerous if they reach the minutes.
  * The issuing HQ's STANDING details — the originator's address, the secretary's telephone, the file
    reference, the signature block and the distribution list. Appendix AD explanatory note 1 names
    exactly these as "standard tenets of service-writing". They are what differs between one unit's
    template and another's, so they are what this module extracts.

Meeting content (title, date, attendees, items, decisions) always comes from the prompt and the source
documents. Classification and precedence are never taken from a template: they belong to a meeting,
and the security classification is never guessed.

WHY THE TEMPLATE TEXT NEVER REACHES THE MINUTES WRITER'S SOURCE. The writer's grounding check drops a
key point whose words are not in the source. Put a template into the source and its specimen text
("4. Nagin Range …") becomes "grounded" — it would pass the check and appear in real minutes. So the
template is read on its own path, here, and only validated standing details leave it.

WHY A MODEL EXTRACTS RATHER THAN FIXED RULES, AND WHY ITS ANSWER IS NOT TRUSTED. Measured on
2026-09-15 with one unit template saved as DOCX and as PDF: the model returned the same 11 correct
values from both, including the distribution table that PDF text extraction flattens into a single
run-on line — which positional rules cannot split. But the same model, told explicitly to skip
placeholders, returned "Telephone number here" and "Addressee 1" from a blank specimen, and returned a
company's Bengaluru street address as the "HQ address" from a non-JSSD document. Hence three guards
around one model call, none optional:

  1. a GATE — the document must read as a JSSD minutes template before anything is extracted;
  2. GROUNDING — every value must appear word for word in the template, so nothing can be invented;
  3. a SPECIMEN FILTER — the manual's placeholder phrases and filler are dropped in code.

A template problem of any kind never fails the job: the minutes are rendered without its details and
the reason is logged and recorded.
"""
import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("template")

# ── the gate ─────────────────────────────────────────────────────────────────────────────────────
# Phrases the manual mandates in minutes (Appendix AD, Ch 6 paras 16.x). A real template carries most
# of them; a company profile or a letter carries none.
_ANCHORS = {
    "attendees":   r"following\s+were\s+present",
    "distribution": r"\bdistribution\b",
    "closing":     r"agreement\s+with\s+the\s+minutes",
    "introduction": r"\bintroduction\b",
    "secretary":   r"\bsecretary\b",
    "copy_no":     r"\bcopy\s+no\b",
    "item":        r"\bitem\s+[ivxlc]+\b",
    "title":       r"minutes\s+of\s+(the\s+)?(meeting|conference)|title\s+of\s+the\s+meeting",
    "action_info": r"\baction\b[\s|]+\binfo\b",
    "decision":    r"\bdecision\s*\.",
}
MIN_ANCHORS = 3


def jssd_anchors(text: str) -> List[str]:
    t = text.lower()
    return [name for name, pat in _ANCHORS.items() if re.search(pat, t)]


# ── the specimen filter ──────────────────────────────────────────────────────────────────────────
# Placeholder wording from Appendix AD (English, pages 328-330) and the filler it uses. Matched against
# a normalised value; a hit means "this is instruction, not information".
_SPECIMEN = re.compile(
    r"telephone\s+number\s+here|address\s+line|file\s+number\s+comes\s+here|number\s+of\s+pages|"
    r"\bxx\s*/\s*yy\b|rank\s+and\s+name|\baddressee\s*\d+\b|place\s+here|security\s+classification|"
    r"\bun\s*decorated\b|appointment\s+of|comes\s+here|\bnumber\s+here\b|\bnagin\s+range\b|"
    r"\bfiring\s+practice\b", re.I)
_FILLER = re.compile(r"\.{4,}|…{2,}|_{3,}|-{4,}")
_ONLY_PUNCT = re.compile(r"^[\s.…,;:\-–—_()'\"/|]*$")
_WHOLE_WORD_PLACEHOLDERS = {"appointment", "date", "dt", "dt date", "precedence", "copy no", "rank", "name",
                            "remarks", "no of copies", "distribution", "addressee", "signature"}


def is_specimen(value: str) -> bool:
    v = re.sub(r"\s+", " ", (value or "")).strip()
    if not v or _ONLY_PUNCT.match(v):
        return True
    return bool(_SPECIMEN.search(v) or _FILLER.search(v) or v.lower().strip(".:") in _WHOLE_WORD_PLACEHOLDERS)


# ── grounding ────────────────────────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    s = (s or "").replace("–", "-").replace("—", "-").replace("’", "'").replace("‘", "'")
    s = s.replace("|", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def grounded(value: str, template_norm: str) -> bool:
    v = _norm(value)
    return bool(v) and v in template_norm


# ── the model call ───────────────────────────────────────────────────────────────────────────────
PROMPT_VERSION = "standing-details-v1"   # part of the cache key: a changed prompt must not reuse old answers

_SYSTEM = """You read a Minutes of Meeting TEMPLATE in the Indian Armed Forces JSSD format and extract ONLY the
issuing headquarters' STANDING details — the parts that stay the same from one meeting to the next.

Extract:
- telephone: the secretary's telephone number
- address: the originator's address lines, in order
- file_ref: the file reference number
- secretary: the name and rank in the signature block
- distribution: the rows of the distribution list

Rules:
- Copy values EXACTLY as they appear in the template. Never invent, complete or correct a value.
- Templates contain instructional placeholders such as "Telephone number here", "Address line 1",
  "File number comes here", "Copy No xx/yy", "Rank and name of ...", "Addressee 1", dotted lines, and
  "(UN Decorated)". These are NOT values: return an empty string or empty list for them.
- Do NOT extract anything about a particular meeting: no title, date, time, venue, attendees, agenda
  items, discussion, decisions or actions.
- Do NOT extract the security classification or precedence.
- If a detail is absent, return an empty string or an empty list."""

_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["telephone", "address", "file_ref", "secretary", "distribution"],
    "properties": {
        "telephone": {"type": "string"},
        "address": {"type": "array", "items": {"type": "string"}},
        "file_ref": {"type": "string"},
        "secretary": {"type": "object", "additionalProperties": False, "required": ["name", "rank"],
                      "properties": {"name": {"type": "string"}, "rank": {"type": "string"}}},
        "distribution": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["addressee", "copies", "copy_no", "remarks"],
            "properties": {k: {"type": "string"} for k in ("addressee", "copies", "copy_no", "remarks")}}},
    },
}

# A template is a few pages; its standing details sit at the TOP (superscription) and the BOTTOM
# (signature and distribution). If one is unusually long, keep both ends rather than the first N chars.
MAX_TEMPLATE_CHARS = 20000
_HEAD, _TAIL = 12000, 8000
# The measured non-JSSD document ran a 900-token reply into the limit; a genuine 30-row distribution
# list needs ~800. 1500 covers real templates with room, and a cut-off reply fails parsing safely.
MAX_REPLY_TOKENS = 1500


def _clip(text: str) -> str:
    if len(text) <= MAX_TEMPLATE_CHARS:
        return text
    return text[:_HEAD] + "\n…\n" + text[-_TAIL:]


def _ask_model(text: str) -> Dict[str, Any]:
    """One call. Raises on a transport error; returns {} for a reply that is not usable JSON."""
    from mom import _get_writer
    reply = _get_writer().generate(
        _SYSTEM, "TEMPLATE:\n" + _clip(text), max_new_tokens=MAX_REPLY_TOKENS, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "standing_details", "schema": _SCHEMA, "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


# ── the file reference, from the layout ──────────────────────────────────────────────────────────
# A service file reference is slash-separated with no spaces ("A/12345/DOT/MoM", "SPEC/9901/Trg/MoM")
# and stands immediately before "dt" on its line. The specimen "File number comes here dt Date" has no
# slash, so it never matches.
_FILE_REF_BEFORE_DT = re.compile(r"(?<!\S)([A-Za-z0-9()&.\-]+(?:/[A-Za-z0-9()&.\-]+)+)[ \t|]+dt\b", re.I)


def _file_ref_from_layout(template_text: str) -> str:
    """The reference before "dt", searched only in the superscription — the text above the attendee
    list — so a reference quoted further down the minutes cannot be mistaken for the file's own."""
    head = re.split(r"following\s+were\s+present", template_text, maxsplit=1, flags=re.I)[0]
    m = _FILE_REF_BEFORE_DT.search(head)
    return m.group(1) if m else ""


# ── validation ───────────────────────────────────────────────────────────────────────────────────
_TEL_PREFIX = re.compile(r"^(tele(phone)?|tel|ph(one)?)\s*(no\.?)?\s*[:.\-]?\s*", re.I)
_LIMITS = {"telephone": 40, "address_line": 120, "file_ref": 80, "name": 60, "rank": 40, "cell": 120}
MAX_ADDRESS_LINES, MAX_DISTRIBUTION_ROWS = 6, 60


def validate(raw: Dict[str, Any], template_text: str) -> Tuple[Dict[str, Any], List[str]]:
    """Keep only values that are real (not specimen), present word for word, and of sane length.

    Returns (details, dropped) — `dropped` says what was refused and why, for the log.
    """
    tn = _norm(template_text)
    dropped: List[str] = []
    out: Dict[str, Any] = {}

    def keep(label: str, value: Any, limit: int) -> str:
        v = re.sub(r"\s+", " ", str(value or "")).strip()
        if not v:
            return ""
        if is_specimen(v):
            dropped.append(f"{label}: specimen {v!r}"); return ""
        if len(v) > limit:
            dropped.append(f"{label}: too long"); return ""
        if not grounded(v, tn):
            dropped.append(f"{label}: not in template {v!r}"); return ""
        return v

    # The specimen check runs on the value AS GIVEN, before the "Tele:" prefix is stripped: stripping
    # first turns "Telephone number here" into "number here", which no longer reads as a placeholder
    # and IS present in the template — so it passed both guards. Caught by a test on the real reply.
    tel_raw = str(raw.get("telephone") or "").strip()
    if tel_raw and is_specimen(tel_raw):
        dropped.append(f"telephone: specimen {tel_raw!r}")
    elif (t := keep("telephone", _TEL_PREFIX.sub("", tel_raw).strip(), _LIMITS["telephone"])):
        out["telephone"] = t

    lines = [keep("address", ln, _LIMITS["address_line"]) for ln in (raw.get("address") or [])[:MAX_ADDRESS_LINES]
             if isinstance(ln, str)]
    if (lines := [ln for ln in lines if ln]):
        out["address"] = lines

    ref = re.sub(r"\s+dt\b.*$", "", str(raw.get("file_ref") or ""), flags=re.I).strip()
    if (r := keep("file_ref", ref, _LIMITS["file_ref"])):
        out["file_ref"] = r
    elif not ref and (found := _file_ref_from_layout(template_text)):
        # The model returned no reference. On the cluster, Llama 3.3 70B on watsonx did exactly that
        # for a template the same model via OpenRouter read correctly — most likely because the line
        # also carries the "dt Date" placeholder. The manual gives a structural anchor instead: "the
        # file reference and the date are in line with each other" (Ch 6 para 16.2), so the reference
        # is what stands right before "dt". Same guards as a model value.
        if (r := keep("file_ref", found, _LIMITS["file_ref"])):
            out["file_ref"] = r
            logger.info(f"file_ref {r!r} taken from the '<reference> dt' line (the model returned none)")

    sec = raw.get("secretary") if isinstance(raw.get("secretary"), dict) else {}
    name = keep("secretary.name", str(sec.get("name") or "").strip("() "), _LIMITS["name"])
    # A rank without a real name is the specimen signature block ("(UN Decorated) / Lt Cdr"): the
    # rank alone would print a rank above an empty name, so both go.
    if name:
        rank = keep("secretary.rank", sec.get("rank"), _LIMITS["rank"])
        out["secretary"] = {"name": name, **({"rank": rank} if rank else {})}
    elif sec.get("rank"):
        dropped.append("secretary.rank: no real name beside it")

    rows = []
    for d in (raw.get("distribution") or [])[:MAX_DISTRIBUTION_ROWS]:
        if not isinstance(d, dict):
            continue
        addressee = keep("distribution.addressee", d.get("addressee"), _LIMITS["cell"])
        if not addressee:
            continue
        row = {"addressee": addressee}
        for k in ("copies", "copy_no", "remarks"):
            v = str(d.get(k) or "").strip()
            # Short cells ("One", "1", "NA") are too common to prove anything by grounding alone, so
            # they are kept only when the row's addressee was itself real and present.
            if v and not is_specimen(v) and len(v) <= 40:
                row[k] = v
        rows.append(row)
    # The AD specimen's only "real-looking" row is "File / One / 9 / NA". If every other row was a
    # placeholder, that File row is specimen too.
    if any(r["addressee"].lower() != "file" for r in rows):
        out["distribution"] = rows
    elif rows:
        dropped.append("distribution: only the specimen File row survived")
    return out, dropped


# ── merging into the job's details ───────────────────────────────────────────────────────────────
STANDING_FIELDS = ("telephone", "address", "file_ref", "secretary", "distribution")


def merge(job_meta: Dict[str, Any], template: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The job's own mom_meta wins, field by field; the template fills only what the job left empty.

    Returns (meta for the renderer, the fields that came from the template).
    """
    meta = dict(job_meta or {})
    used = []
    for k in STANDING_FIELDS:
        if k not in template:
            continue
        mine = meta.get(k)
        empty = (not mine) or (k == "secretary" and isinstance(mine, dict) and not str(mine.get("name") or "").strip())
        if empty:
            meta[k] = template[k]
            used.append(k)
    return meta, used


# ── orchestration ────────────────────────────────────────────────────────────────────────────────
@dataclass
class TemplateResult:
    name: str = ""
    status: str = "none"          # none | used | not_jssd | unreadable | no_details | failed
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    dropped: List[str] = field(default_factory=list)


_cache: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 64


def load(job, store) -> TemplateResult:
    """Read the job's template and return its validated standing details. Never raises."""
    if not job.template_url:
        return TemplateResult()
    name = job.template_name or job.template_url.rsplit("/", 1)[-1]
    res = TemplateResult(name=name)
    tag = f"[JOB {job.conversation_id}] [TEMPLATE {name}]"
    try:
        raw = store.download(job.template_url)
    except Exception as e:
        res.status, res.reason = "unreadable", f"could not download: {e}"
        logger.warning(f"{tag} {res.reason}"); return res
    try:
        import document_checker
        from documents import extract_text_blocks
        document_checker.check(raw, name)          # type and size, as for any attachment
        text = "\n".join(b for b in extract_text_blocks(raw, name).blocks if b.strip())
    except Exception as e:
        res.status, res.reason = "unreadable", f"{type(e).__name__}: {e}"
        logger.warning(f"{tag} not readable — {res.reason}"); return res

    anchors = jssd_anchors(text)
    if len(anchors) < MIN_ANCHORS:
        res.status = "not_jssd"
        res.reason = f"does not read as a JSSD minutes template ({len(anchors)} of {MIN_ANCHORS} required markers: {anchors})"
        logger.warning(f"{tag} ignored — {res.reason}"); return res

    key = hashlib.sha256(raw + PROMPT_VERSION.encode()).hexdigest()
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        res.details, res.dropped = cached
        logger.info(f"{tag} standing details from cache")
    else:
        try:
            data = _ask_model(text)
        except Exception as e:
            res.status, res.reason = "failed", f"extraction call failed: {type(e).__name__}: {e}"
            logger.warning(f"{tag} {res.reason}"); return res   # transient: not cached, the next job retries
        res.details, res.dropped = validate(data, text)
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (res.details, res.dropped)

    for d in res.dropped:
        logger.info(f"{tag} refused {d}")
    if res.details:
        res.status = "used"
        logger.info(f"{tag} standing details: {sorted(res.details)} (markers: {anchors})")
    else:
        res.status, res.reason = "no_details", "no standing details survived validation (a blank specimen?)"
        logger.info(f"{tag} {res.reason}")
    return res
