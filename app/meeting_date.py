"""Keep the writer's meeting date only when the source gives it as THIS meeting's date.

WHY. When the job's mom_meta carries no meeting_date, the title of the minutes takes the date the
writer put in header.meeting_date. The writer's prompt already says to fill it only with "the date of
THIS meeting", and on the cluster it still took one from

    "...I need an approval of the February 17 2022 meeting minutes..."

— the date of the PREVIOUS meeting, whose minutes were being approved — and printed it in the title of
the new minutes. A rule in the prompt did not hold, so this is a check in code. A wrong date on a signed
record reads as true; a blank one gets noticed and filled in.

THE TEST. Every place the source writes that date is looked at, and the date is kept when at least one
of them is introduced as this meeting's, judged by the few words just before it:

  keep — "held on", "convened on", "took place on", "dated", "Date:", "date of the meeting",
         "today is", "today's meeting", "we met on"...
  drop — the same words also point at another meeting ("last", "next", "previous", "upcoming"...), a
         nearer word points away from it ("minutes", "approval", "due", "by"...), or "minutes" follows
         the date.

A date the job itself sent in mom_meta is always kept. When unsure, the date is blanked, never guessed —
and a mom_meta.meeting_date, when the backend sends one, fills the title regardless.

Measured on the real sources: t2.txt "approval of the February 17 2022 meeting" → dropped; the LEP
transcript "today is January 28th" → kept, "our last meeting was September 22nd" → dropped; t3.doc
"Date: 16 September 2026" → kept; a prompt "...held on 15 September 2026" → kept.
"""
import re
from typing import Any, Dict, Iterator, Optional, Set, Tuple

Day = Tuple[int, int, Optional[int]]            # (month, day, year or None)

_MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_PATTERNS = (
    # February 17 2022 · Sept. 23rd, 2026 · January 28th
    re.compile(rf"\b{_MON}\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{{4}})\b)?", re.I),
    # 16 September 2026 · 23rd of September · 11 Sep 26
    re.compile(rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\b(?:,?\s+(?P<year>\d{{4}}|\d{{2}})\b)?",
               re.I),
    # 2026-09-16
    re.compile(r"\b(?P<year>\d{4})-(?P<mon>\d{1,2})-(?P<day>\d{1,2})\b"),
    # 16/09/2026 · 16.09.2026 — day first, as dates are written here; month first is tried as well
    re.compile(r"\b(?P<day>\d{1,2})[/.-](?P<mon>\d{1,2})[/.-](?P<year>\d{4})\b"),
)

# Words just before a date that make it this meeting's.
_THIS = re.compile(r"\b(held|convened|conducted|took place|we met|meeting dated|dated|meeting date|"
                   r"date of (the )?(meeting|conference)|today)\b|\bdate\s*[:\-–]", re.I)
# Words that make it another meeting's, wherever they stand in the window.
_OTHER_MEETING = re.compile(r"\b(last|next|previous|prior|earlier|upcoming|following|subsequent|future|"
                            r"adjourned|postponed|rescheduled)\b", re.I)
# Words that point away from this meeting when they stand NEARER the date than any word above.
_AWAY = re.compile(r"\b(minutes|approv\w*|due|deadline|by|until|till|before|after|since|expir\w*|"
                   r"effective|from|birthday|anniversary)\b", re.I)
# A date followed by "minutes" names the minutes of that meeting, not this one.
_THEN_MINUTES = re.compile(r"^\W*(meeting\s+)?minutes\b", re.I)

_WINDOW_WORDS = 10


def _day(mon: str, day: str, year: Optional[str]) -> Set[Day]:
    """The readings of one written date. Two for an all-number date that could be either way round."""
    y = int(year) if year else None
    if y is not None and y < 100:
        y += 2000
    if mon.isdigit():
        pairs = {(int(mon), int(day)), (int(day), int(mon))}
    else:
        pairs = {(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
                  .index(mon[:3].lower()) + 1, int(day))}
    return {(m, d, y) for m, d in pairs if 1 <= m <= 12 and 1 <= d <= 31}


def _dates(text: str) -> Iterator[Tuple[int, int, Set[Day]]]:
    """(start, end, readings) for every date written in `text`, in order of appearance."""
    seen = set()
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            readings = _day(m.group("mon"), m.group("day"), m.group("year"))
            if readings and m.start() not in seen:
                seen.add(m.start())
                yield m.start(), m.end(), readings


def _same(a: Set[Day], b: Set[Day]) -> bool:
    """Same month and day; the year must agree only when both sides give one."""
    return any(ma == mb and da == db and (ya is None or yb is None or ya == yb)
               for ma, da, ya in a for mb, db, yb in b)


def _introduced_as_this(source: str, start: int, end: int) -> bool:
    before = re.split(r"[.!?]|\n\s*\n", source[max(0, start - 200):start])[-1]
    before = " ".join(before.split()[-_WINDOW_WORDS:])
    if _THEN_MINUTES.match(source[end:end + 40]) or _OTHER_MEETING.search(before):
        return False
    this = [m.end() for m in _THIS.finditer(before)]
    if not this:
        return False
    away = [m.end() for m in _AWAY.finditer(before)]
    return not away or max(this) > max(away)


def check(mom: Dict[str, Any], source: str, stated: Any = None) -> str:
    """Blank mom["meeting_date"] unless the source gives it as this meeting's date, or the job itself
    stated it. Returns the date that was dropped, or "" when it was kept or there was none."""
    value = str(mom.get("meeting_date") or "").strip()
    if not value:
        return ""
    wanted = next((r for _, _, r in _dates(value)), set())
    if wanted:
        told = next((r for _, _, r in _dates(str(stated or ""))), set())
        if told and _same(wanted, told):
            return ""
        if any(_same(wanted, found) and _introduced_as_this(source, s, e) for s, e, found in _dates(source)):
            return ""
    mom["meeting_date"] = ""
    return value
