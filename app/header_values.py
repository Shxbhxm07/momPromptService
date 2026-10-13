"""EVERY header value of the minutes, from every place the user can give one, in ONE dictionary.

    the screen's fields and buttons (mom_meta, metaData, grading)  ─┐
    the user's own prompt ("Tele: 0194-2450101", "Venue: …")        ├─▶ collect() ─▶ one dict ─▶ the template
    what earlier messages of this conversation already set          │               (minutes_template / docx_export)
    the HQ's official template (template_details)                  ─┘

WHO WINS (2026-10-13, the user's rule): the SCREEN first — a field a person filled in or a button they
pressed is exact, while a prompt is prose. Then the prompt, then what earlier messages of the same
conversation set, and last the template's standing details. A value nobody gave is not guessed: the
slot prints MISSING ("....xxxx....xxxx....") and any later message can still fill it (minutes_edit).

THE PROMPT IS READ BY CODE, NOT BY THE MODEL. Only LABELLED values are taken — "Tele: 0194-2450101",
"Venue - Conference Room B", "File ref: 1234/56/Trg dt 12 Oct 26" — so nothing is inferred from prose and
nothing can be invented; the label decides the field, which is why "Venue: IAF Head Quarters" can never
land in the meeting time. What is taken is also REMOVED from the text the minutes are written from, so
"Tele: 0194-2450101" is a header detail, not something said at the meeting.
"""
import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("header")

# The header fields the minutes print. Same set the re-prompt may fill (minutes_edit.EDITABLE_META),
# in the shape the renderer takes: `address` a list of lines, `secretary` {name, rank}, `distribution`
# rows of {addressee, copies, remarks}; the rest plain text.
FIELDS = ("classification", "precedence", "copy_no", "telephone", "address", "file_ref", "issue_date",
          "venue", "meeting_date", "meeting_time", "secretary", "amendments_by", "distribution")

# The grades, longest first so "top secret" is not also read as "secret"; the abbreviations are Part 1's.
GRADES = [("TOP SECRET", r"top\s+secret"), ("CONFIDENTIAL", r"confidential|confd"),
          ("RESTRICTED", r"restricted|restd"), ("UNCLASSIFIED", r"unclassified|unclas"),
          ("SECRET", r"secret")]

# What the user may write before a value. The first match on a line decides the field, so the longer
# labels come first ("meeting date" before "date", "secretary rank" before "secretary").
_LABELS: List[Tuple[str, str]] = [
    ("classification", r"classification|security\s+grading|grading"),
    ("precedence", r"precedence"),
    ("copy_no", r"copy\s*(?:no|number)"),
    ("telephone", r"tele(?:phone)?|phone|tel|mobile|contact\s*(?:no|number)"),
    ("address", r"address"),
    ("file_ref", r"file\s*(?:ref(?:erence)?|no|number)|ref(?:erence)?"),
    ("issue_date", r"date\s+of\s+issue|issue\s+date|dt"),
    ("venue", r"venue|place|location|held\s+at"),
    ("meeting_date", r"meeting\s+date|date\s+of\s+(?:the\s+)?meeting|date"),
    ("meeting_time", r"meeting\s+time|time"),
    ("secretary_rank", r"secretary(?:'s)?\s+rank"),
    ("secretary", r"secretary|minuting\s+officer"),
    ("amendments_by", r"amendments?\s*(?:by|date|due)?"),
    ("distribution", r"distribution(?:\s+list)?|copy\s+to|circulat(?:e|ion)\s+to"),
]
_LABEL_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(p for _, p in _LABELS) + r")\s*[:：\-–=]\s*", re.I)
_WS = re.compile(r"\s+")
_VALUE_MAX = 300           # a label's value is a detail, not a paragraph


def _flat(v: Any) -> str:
    return _WS.sub(" ", str(v if v is not None else "")).strip()


def grades_in(text: str) -> List[str]:
    """Every grade the text names, longest first, each span counted once ("top secret" is not "secret")."""
    found, rest = [], (text or "").lower()
    for grade, pattern in GRADES:
        rx = re.compile(rf"\b(?:{pattern})\b")
        if rx.search(rest):
            found.append(grade)
            rest = rx.sub(" ", rest)
    return found


def grade_of(value: str) -> str:
    """The ONE grade `value` names, or "" when it names none or several."""
    found = grades_in(value)
    return found[0] if len(found) == 1 else ""


def _field_of(label: str) -> str:
    low = label.lower()
    for name, pattern in _LABELS:
        if re.fullmatch(pattern, low, re.I):
            return name
    return ""


def _rows(value: str) -> List[Dict[str, str]]:
    """A distribution list: "addressee | copies | remarks", rows separated by ";" or a line break."""
    rows = []
    for entry in re.split(r"\s*[;\n]\s*", value):
        bits = [_flat(b) for b in entry.split("|")] + ["", ""]
        if bits[0]:
            rows.append({"addressee": bits[0], "copies": bits[1], "remarks": bits[2]})
    return rows


def _address(value: str) -> List[str]:
    parts = [x for x in (_flat(p) for p in re.split(r"\s*[;\n]\s*", value)) if x]
    if len(parts) == 1:                      # "HQ 7 Inf Bde, C/O 56 APO, PIN 900111"
        parts = [x for x in (_flat(p) for p in parts[0].split(",")) if x]
    return parts


def from_prompt(prompt: str) -> Tuple[Dict[str, Any], str]:
    """(the header values the user LABELLED in their prompt, the prompt without those labelled parts).

    Each value runs from its label to the next label or the end of the prompt, so a multi-line address
    and a one-line "Tele: 123, Venue: Room B" both read correctly."""
    text = prompt or ""
    marks = list(_LABEL_RE.finditer(text))
    if not marks:
        return {}, text
    found: Dict[str, Any] = {}
    cut: List[Tuple[int, int]] = []
    for i, m in enumerate(marks):
        field = _field_of(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        raw = text[m.end():end].rstrip()
        if not field or not raw.strip() or len(raw) > _VALUE_MAX:
            continue
        value = _flat(raw.rstrip(",;"))
        if field == "address":
            found["address"] = _address(raw)
        elif field == "distribution":
            found["distribution"] = _rows(raw)
        elif field == "secretary":
            found.setdefault("secretary", {})["name"] = value
        elif field == "secretary_rank":
            found.setdefault("secretary", {})["rank"] = value
        elif field == "classification":
            grade = grade_of(value)
            if not grade:
                continue                      # "classification: as applicable" names no grade — nothing is set
            found["classification"] = grade
        elif field == "precedence":
            found["precedence"] = value.upper()
        else:
            found[field] = value
        cut.append((m.start(), end))
    # The header details are not things said at the meeting: they leave the text the minutes are written from.
    rest = text
    for start, end in reversed(cut):
        rest = rest[:start] + rest[end:]
    return found, _WS.sub(" ", rest).strip() if cut else text


def from_screen(job: Any) -> Dict[str, Any]:
    """What the IMIR screen's own fields and buttons sent: `mom_meta`, any header values inside `metaData`
    (a JSON object), and `grading` when that button names a classification."""
    ui: Dict[str, Any] = {k: v for k, v in (getattr(job, "mom_meta", None) or {}).items() if v not in (None, "")}
    echo = getattr(job, "echo", None) or {}
    extra = echo.get("metaData")
    if isinstance(extra, str) and extra.strip().startswith("{"):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = None
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k in FIELDS and v not in (None, "", [], {}) and k not in ui:
                ui[k] = v
    grading = _flat(echo.get("grading"))
    if grading and "classification" not in ui and grade_of(grading):
        ui["classification"] = grade_of(grading)
    return ui


def collect(job: Any, template: Dict[str, Any], earlier: Dict[str, Any] = None) -> Tuple[Dict[str, Any], Dict[str, str], str]:
    """(the one header dictionary, where each value came from, the prompt without its header lines).

    Order: the screen, then the prompt, then earlier messages of this conversation, then the template.
    A field nobody gave is absent, and the renderer prints MISSING for it."""
    screen = from_screen(job)
    spoken, rest = from_prompt(getattr(job, "prompt", "") or "")
    layers = (("the screen", screen), ("the prompt", spoken),
              ("an earlier message", dict(earlier or {})), ("the template", dict(template or {})))
    meta: Dict[str, Any] = {}
    source: Dict[str, str] = {}
    for where, values in layers:
        for field, value in (values or {}).items():
            if value in (None, "", [], {}):
                continue
            if field == "secretary" and isinstance(value, dict):      # name and rank fill separately
                sec = dict(meta.get("secretary") or {})
                for part in ("name", "rank"):
                    if _flat(value.get(part)) and not _flat(sec.get(part)):
                        sec[part] = _flat(value[part])
                        source[f"secretary_{part}"] = where
                if sec:
                    meta["secretary"] = sec
                continue
            if field in meta:
                continue
            meta[field] = value
            source[field] = where
    told = {w: [f for f, s in source.items() if s == w] for _, w in ((0, x[0]) for x in layers)}
    logger.info("header values: " + "; ".join(f"{w}: {', '.join(sorted(fs))}" for w, fs in told.items() if fs)
                + f"; not given: {len([f for f in FIELDS if f not in meta])} of {len(FIELDS)}")
    return meta, source, rest
