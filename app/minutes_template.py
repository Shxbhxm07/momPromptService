"""Fill a minutes TEMPLATE — a Word file whose values are named slots (docxtpl), like the service-letter
template the user gave as the reference (2026-09-20).

WHY A TEMPLATE. The layout lives in the Word file, so it can be changed in Word — wording, fonts, spacing,
fixed lines — without a rebuild; the service only supplies values. The default, templates/jssd_minutes.docx,
is JSSD Appendix AD, built by tools/make_minutes_template.py from docx_export's own building blocks and held
to the manual by tools/check_jssd_layout.py. A job may attach its own template with slots instead (see
template_details: status "fillable"); then that one is filled.

EVERY SLOT HAS A VALUE OR "xxx...xxx" — the user's rule: "everything is customisable; if the particular value
is present then use that value from the transcript, otherwise xxx...xxx". That includes a slot the service
does not know (a client's typo or a new field): it prints the marker instead of vanishing silently.

Slots (templates/README.md has them with examples):
  header        classification, pages_shown, page_count, page_count_word, page_count_text, draft
  superscription telephone, precedence, copy_no, address[], file_reference, date_of_issue
  title         title (the whole centre heading), meeting_title, venue, meeting_time, meeting_date
  body          present_number, attendees[{number, name, appointment, label}],
                introduction[{number, text, subs[{number, text}]}],
                items[{roman, title, classification, entries[{number, text, decision, action, info,
                       figures[{number, text}]}]}],
                closing_number, amendments_by
  signature     secretary_name, secretary_rank
  distribution  distribution[{addressee, copies, copy_no, remarks}]
  raw, for templates laid out differently: purpose, summary, agenda[], key_points[], key_figures[],
                decisions[], action_items[{task, owner, due}], chairman
"""
import io
import logging
import os
import re
from typing import Any, Dict, List, Optional

import jinja2

import docx_export as dx
from docx_export import MISSING, _clean, _date, _grade, _lines, _roman, _stop, _time

logger = logging.getLogger("template")

DEFAULT_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "jssd_minutes.docx")


class _Missing(jinja2.ChainableUndefined):
    """A slot the context does not carry prints "xxx...xxx", loops over nothing and is false in {% if %}."""

    def __str__(self) -> str:
        return MISSING

    def __html__(self) -> str:
        return MISSING

    def __iter__(self):
        return iter(())

    def __bool__(self) -> bool:
        return False


def _listing(text: str):
    from docxtpl import Listing
    return Listing(text)


def _or_missing(value: Any) -> str:
    return _clean(value) or MISSING


def _title_lines(mom: Dict[str, Any], meta: Dict[str, Any]) -> List[str]:
    """The centre heading: place, time and date are mandatory (AD note 2) — a line breaks between phrases
    so each reads on its own (Part 1 App E note 11). Same wording as docx_export._title."""
    venue = _clean(meta.get("venue")) or _clean(mom.get("venue")) or MISSING
    when = _time(meta.get("meeting_time") or mom.get("meeting_time")) or MISSING
    day = _date(meta.get("meeting_date") or mom.get("meeting_date")) or MISSING
    phrases = [f"MINUTES OF THE MEETING HELD AT {venue}", f"AT {when} ON {day}",
               f"TO DISCUSS {_clean(mom.get('title')) or MISSING}"]
    lines: List[str] = []
    for phrase in phrases:
        if lines and len(lines[-1]) + 1 + len(phrase) <= 52:
            lines[-1] += " " + phrase
        else:
            lines.append(phrase)
    return [line.upper().rstrip(".").replace(MISSING.upper(), MISSING) for line in lines]


def _attendees(mom: Dict[str, Any], number: int) -> List[Dict[str, str]]:
    """Chairman first, the others in the order given, the secretary last (AD note 3; Ch 6 para 16.4)."""
    rows = []
    for a in mom.get("attendees") or []:
        name, role = (_clean(a.get("name")), _clean(a.get("role"))) if isinstance(a, dict) else (_clean(a), "")
        if not name:
            continue
        parts = [x.strip() for x in role.split(",") if x.strip()]
        label = ("Chairman" if any(dx._CHAIR.fullmatch(x) for x in parts)
                 else "Secretary" if any(dx._SECRETARY.fullmatch(x) for x in parts) else "")
        appointment = ", ".join(x for x in parts if not (dx._CHAIR.fullmatch(x) or dx._SECRETARY.fullmatch(x)))
        rows.append((name.strip("[]").replace("_", " "), appointment or MISSING, label))
    rows.sort(key=lambda r: {"Chairman": 0, "Secretary": 2}.get(r[2], 1))
    if not rows:
        rows = [(MISSING, MISSING, "")]
    return [{"number": f"{number}.{i}.", "name": n, "appointment": a, "label": lab}
            for i, (n, a, lab) in enumerate(rows, 1)]


def context(mom: Dict[str, Any], meta: Optional[Dict[str, Any]] = None, pages: int = 1) -> Dict[str, Any]:
    """Every slot the default template uses, and the raw values a differently laid-out one may want."""
    meta = meta if isinstance(meta, dict) else {}
    grade = _grade(meta.get("classification"))
    given = bool(_clean(meta.get("classification")))

    tele = _clean(meta.get("telephone"))
    tele = re.sub(r"^tele(phone)?\s*(no\.?)?\s*[:.-]?\s*", "", tele, flags=re.I) if tele else ""
    copy_no = _clean(meta.get("copy_no"))
    sec = meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {}

    n = 1                                              # paragraphs 1, 2, 3 … straight through (AD)
    present_number = n
    attendees = _attendees(mom, present_number)

    purpose = _clean(mom.get("purpose"))
    agenda = [a for a in (_clean(x) for x in mom.get("agenda") or []) if a]
    summary = [s for s in (_clean(x) for x in re.split(r"\n\s*\n", str(mom.get("summary") or ""))) if s]
    blocks = [(purpose or MISSING, []), ("The following agenda was taken up:-", agenda or [MISSING])] \
        + [(s, []) for s in summary]
    introduction = []
    for text, subs in blocks:
        n += 1
        introduction.append({"number": n, "text": _stop(text),
                             "subs": [{"number": f"{n}.{j}.", "text": _stop(s)} for j, s in enumerate(subs, 1)]})

    item_grade = grade or ("UNCLASSIFIED" if given else MISSING)
    items = []
    for k, item in enumerate(dx._items(mom, item_grade), 1):
        entries = []
        for p in item["points"]:
            n += 1
            entries.append({"number": n, "text": _stop(p), "decision": False, "action": "", "info": "", "figures": []})
        if item["figures"]:
            n += 1
            entries.append({"number": n, "text": "The following figures were quoted:-", "decision": False,
                            "action": "", "info": "",
                            "figures": [{"number": f"{n}.{j}.", "text": _stop(f)}
                                        for j, f in enumerate(item["figures"], 1)]})
        for d, owner in item["decisions"]:
            n += 1
            # Action and Info are endorsed against each decision (AD note 6): whoever is not named, the marker.
            entries.append({"number": n, "text": _stop(d), "decision": True,
                            "action": owner or MISSING, "info": MISSING, "figures": []})
        items.append({"roman": _roman(k), "title": item["title"].upper(), "classification": item["grade"],
                      "entries": entries})
    n += 1
    closing_number = n

    distribution = []
    for d in meta.get("distribution") or []:
        if isinstance(d, dict) and _clean(d.get("addressee")):
            name = _clean(d.get("addressee"))
            distribution.append({"addressee": name,
                                 "copies": _clean(d.get("copies")) or ("One" if name.lower() == "file" else MISSING),
                                 "copy_no": _clean(d.get("copy_no")), "remarks": _clean(d.get("remarks"))})
        elif isinstance(d, str) and _clean(d):
            distribution.append({"addressee": _clean(d), "copies": "One" if _clean(d).lower() == "file" else MISSING,
                                 "copy_no": "", "remarks": ""})
    if not distribution:
        distribution.append({"addressee": MISSING, "copies": MISSING, "copy_no": "", "remarks": ""})
    if not any(r["addressee"].lower() == "file" for r in distribution):
        distribution.append({"addressee": "File", "copies": "One", "copy_no": "", "remarks": ""})

    actions = [a for a in (mom.get("action_items") or []) if isinstance(a, dict) and _clean(a.get("task"))]
    chairman = next((a["name"] for a in attendees if a["label"] == "Chairman"), MISSING)
    return {
        "draft": bool(meta.get("draft")),
        "classification": grade, "pages_shown": grade in dx._PAGES_SHOWN,
        "page_count": str(pages), "page_count_word": dx._WORDS[min(pages, 9)].capitalize(),
        "page_count_text": dx._pages_text(pages),
        "telephone": tele or MISSING,
        "precedence": _clean(meta.get("precedence")).upper(),
        "copy_no": (copy_no if copy_no.lower().startswith("copy") else f"Copy No {copy_no}") if copy_no else "",
        "address": _lines(meta.get("address")) or [MISSING],
        "file_reference": _or_missing(meta.get("file_ref")),
        "date_of_issue": _date(meta.get("issue_date")) or MISSING,
        # One paragraph, a line break between phrases — docxtpl's Listing escapes the text and turns "\n"
        # into a Word line break, as docx_export's add_break() did.
        "title": _listing("\n".join(_title_lines(mom, meta))),
        "meeting_title": _or_missing(mom.get("title")),
        "venue": _clean(meta.get("venue")) or _or_missing(mom.get("venue")),
        "meeting_time": _time(meta.get("meeting_time") or mom.get("meeting_time")) or MISSING,
        "meeting_date": _date(meta.get("meeting_date") or mom.get("meeting_date")) or MISSING,
        "present_number": present_number, "attendees": attendees,
        "introduction": introduction, "items": items,
        "closing_number": closing_number,
        "amendments_by": _date(meta.get("amendments_by")) or MISSING,
        "secretary_name": _clean(sec.get("name") or meta.get("secretary_name")) or MISSING,
        "secretary_rank": _clean(sec.get("rank") or meta.get("secretary_rank")) or MISSING,
        "distribution": distribution,
        "purpose": purpose or MISSING,
        "summary": " ".join(summary) or MISSING,
        "agenda": agenda or [MISSING],
        "key_points": [p for p in (_clean(x) for x in mom.get("key_points") or []) if p] or [MISSING],
        "key_figures": [p for p in (_clean(x) for x in mom.get("key_figures") or []) if p] or [MISSING],
        "decisions": [p for p in (_clean(x) for x in mom.get("decisions") or []) if p] or [MISSING],
        "action_items": [{"task": _clean(a.get("task")), "owner": _or_missing(a.get("assigned_to")),
                          "due": _date(a.get("due")) or _or_missing(a.get("due"))} for a in actions]
                        or [{"task": MISSING, "owner": MISSING, "due": MISSING}],
        "chairman": chairman,
    }


def is_fillable(data: bytes) -> bool:
    """Does this Word file carry template slots ({{ … }} or {% … %}) in its text?"""
    try:
        import docx
        d = docx.Document(io.BytesIO(data))
    except Exception:
        return False
    parts = [d.element.body] + [p._element for s in d.sections
                                for p in (s.header, s.footer, s.first_page_header, s.first_page_footer)]
    text = " ".join("".join(t.text or "" for t in part.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                    for part in parts)
    return bool(re.search(r"\{\{.*?\}\}|\{%.*?%\}", text))


def _fill(template: bytes, ctx: Dict[str, Any], grade: str, copy_no: str, draft: bool) -> Any:
    from docxtpl import DocxTemplate
    tpl = DocxTemplate(io.BytesIO(template))
    env = jinja2.Environment(undefined=_Missing, autoescape=True)
    tpl.render(ctx, jinja_env=env, autoescape=True)
    doc = tpl.docx
    if draft:                                          # Part 1 para 73: drafts in one-and-a-half spacing
        doc.styles["Normal"].paragraph_format.line_spacing = dx.DRAFT_LINE_SPACING
    sec = doc.sections[0]
    headers = [sec.first_page_header, sec.header]
    own_mark = any(el.tag.endswith("}textpath") for h in headers for el in h._element.iter())
    if grade and not own_mark:                         # the classification across every page (Part 1 para 76)
        mark = grade
        if copy_no and grade in dx._COPY_IN_WATERMARK:
            mark += "  " + (copy_no if copy_no.lower().startswith("copy") else f"Copy No {copy_no}")
        for i, h in enumerate(headers, 1):
            if h.paragraphs:
                dx._watermark(h, mark, i)
    return doc


def render(mom: Dict[str, Any], meta: Optional[Dict[str, Any]] = None, template: Optional[bytes] = None) -> bytes:
    """The minutes as a Word file, filled from `template` (a job's own, with slots) or the default."""
    meta = meta if isinstance(meta, dict) else {}
    if template is None:
        with open(DEFAULT_TEMPLATE, "rb") as f:
            template = f.read()
    grade, copy_no, draft = _grade(meta.get("classification")), _clean(meta.get("copy_no")), bool(meta.get("draft"))

    doc = _fill(template, context(mom, meta), grade, copy_no, draft)
    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    if grade in dx._PAGES_SHOWN:
        # Page 1 of CONFIDENTIAL and higher states the number of pages. It is a Word field, recalculated
        # when Word opens the file; the number stored with it is what other viewers show, so it is the
        # count LibreOffice lays out, or an estimate — as docx_export does.
        pages = dx._laid_out_pages(data) or dx._estimate_pages(doc)
        if pages != 1:
            doc = _fill(template, context(mom, meta, pages), grade, copy_no, draft)
            buf = io.BytesIO()
            doc.save(buf)
            data = buf.getvalue()
    return data
