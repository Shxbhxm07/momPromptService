"""A second prompt CHANGES the minutes already written, instead of writing them again.

    turn 1  template + document + prompt  ─▶  minutes, and minutes_state beside them
    turn 2  "change the venue to Conference Room B and remove the point about the website"
            ─▶ the saved minutes + one model call ─▶ a list of CHANGES ─▶ a new .docx

WHY NOT SIMPLY RUN THE JOB AGAIN. Measured on the cluster, the same job run twice gave 5 ITEMs and
then 4, 132 paragraphs and then 126, 15 decisions and then 13. Asking to change the venue would hand
back a different document. Here, a line nobody mentioned is copied through untouched.

WHY NOT LET THE MODEL REWRITE THE MINUTES. It quietly drops and reworks lines it was not asked about,
and nothing can check that. So it may answer ONLY with changes, each naming what it targets by INDEX
and quoting the line it means. Code applies them.

FOUR GUARDS, none optional:
  1. WRONG LINE — every change to an existing line must quote that line; a quote that does not match
     the line at that index is dropped, so "point 41" can never hit point 42.
  2. INVENTED DETAIL — names and numbers in new text must already appear in the user's instruction,
     the minutes, or the header. The model cannot supply a due date nobody gave.
  3. CLASSIFICATION is never CHANGED from chat text: a misread "remove the secret part" must not
     declassify a document. One the job never gave (printed "xxx...xxx") may be FILLED — only with the
     one grade word the user wrote ("mark it restricted"), never with one the model inferred.
  4. NOTHING ELSE MOVES — untouched lines are copied exactly, and the ITEM grouping is remapped from
     the old minutes to the new rather than worked out again, so items do not reshuffle.

EVERY VERSION IS KEPT: each edit writes a new .docx under its own hash folder, named "MoM-<file name>"
like the first version, and pushes the previous state onto a history, so "undo the last change" is a
change like any other.
"""
import copy
import difflib
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import minutes_state
from docx_export import build_mom_docx, flatten_items
from kafka_contract import KafkaJob, build_ack, summary_file_name, summary_object_key
from setup import Clients

logger = logging.getLogger("edit")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MAX_CHANGES = 40
MAX_REPLY_TOKENS = 2000
SNIPPET = 160                 # how much of each line the model is shown; enough to recognise it
MIN_QUOTE = 8                 # a shorter quote proves nothing about which line was meant
ANSWER_CHARS = 1500           # the longest answer to a question sent back in the ack's description

# Every header detail the minutes print — each shows "xxx...xxx" until someone provides it, and the user may
# provide any of them in a later prompt, in any wording (the user's rule, 2026-09-20). Classification only
# under guard 3.
EDITABLE_META = {"venue", "meeting_date", "meeting_time", "telephone", "address", "file_ref", "issue_date",
                 "precedence", "copy_no", "classification", "amendments_by", "secretary_name", "secretary_rank",
                 "distribution"}
MISSING = "xxx...xxx"
# The grades, longest first so "top secret" is not also read as "secret"; the abbreviations are Part 1's.
_GRADE_WORDS = [("TOP SECRET", r"top\s+secret"), ("CONFIDENTIAL", r"confidential|confd"),
                ("RESTRICTED", r"restricted|restd"), ("UNCLASSIFIED", r"unclassified|unclas"),
                ("SECRET", r"secret")]
LISTS = ("key_points", "decisions", "action_items", "agenda", "attendees", "key_figures")

_SYSTEM = """You are given MINUTES already written from a meeting, and one INSTRUCTION from the user asking to change them.

Answer with a list of CHANGES. Never rewrite the minutes, never repeat unchanged lines.

Every change names what it touches by its index in the list shown (the number before the line; the first line is 0), and quotes that line so the change can be checked: copy its first words EXACTLY as shown, at least 8 characters — for an attendee, the name as shown (e.g. "Col. Ariz Khan").

Operations:
- set_meta   : fill or change a header field. field = venue | meeting_date | meeting_time | telephone | address | file_ref | issue_date | precedence | copy_no | classification | amendments_by | secretary_name | secretary_rank | distribution
               address      : its lines separated by " ; "  (e.g. "HQ 7 Inf Bde ; C/O 56 APO ; PIN 900111")
               distribution : the COMPLETE list after the change, existing rows included, separated by " ; ",
                              each "addressee | copies | remarks" — copies and remarks only if the user gave them
               issue_date   : the date after "dt" beside the file reference
- set_title  : change the meeting's title
- delete     : remove one line.              list + index + quote
- replace    : reword one line.              list + index + quote + value (for an attendee: value = the new name)
- add        : add one new line at the end.  list + value
               attendees    : value = the person's name, role = their role
               action_items : value = the task, owner and due when given
- set_owner  : set who owns an action and when it is due. list = action_items, index + quote + owner + due
- set_role   : set an attendee's role.       list = attendees, index + quote + role (e.g. Chairman, Secretary)
- undo       : undo the LAST change to these minutes (may come with other changes: they apply after it)
- answer     : the INSTRUCTION is a question, or asks to explain the minutes ("explain the agenda", "what was
               decided about the convoy?", "who attended?"). Nothing is changed. Put the answer in value: plain,
               short sentences, using ONLY what the MINUTES above say. If they do not say, answer that they do not.
- cannot     : the instruction asks for a change that cannot be made to these minutes; say why in value

Rules:
- Use ONLY words the user gave you or that are already in the minutes. Never invent a name, a date or a number.
- value, role, owner and due each hold only their own text. The [role: …], [owner: …] and [due: …] after a line
  show how the minutes are laid out: never copy them into value. Adding "Teena as an intern in AI" is
  value "Teena", role "intern in AI".
- A header field showing xxx...xxx was never provided: fill it when the user gives it, in the user's words.
- classification: only when the HEADER shows xxx...xxx for it, and only the grade the user wrote (RESTRICTED,
  CONFIDENTIAL, SECRET, TOP SECRET or UNCLASSIFIED). A classification already set cannot be changed here;
  answer "cannot" if asked.
- If the instruction asks for something that needs the original meeting document read again (for example "focus more on the budget"), answer "cannot".
- A question is never "cannot": answer it with "answer".
- EARLIER REQUESTS, when shown, are this user's previous messages and what each changed. Use them to understand
  "him", "that", "the change you made": the person or line they point to. To revert an earlier change that is not
  the last one, set the line back to the old value shown there.
- Leave unused fields as "" and unused indexes as -1."""

_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["changes"],
    "properties": {"changes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["op", "list", "index", "quote", "field", "value", "role", "owner", "due"],
        "properties": {
            "op": {"type": "string", "enum": ["set_meta", "set_title", "delete", "replace", "add",
                                              "set_owner", "set_role", "undo", "answer", "cannot"]},
            "list": {"type": "string", "enum": list(LISTS) + [""]},
            "index": {"type": "integer"},
            "quote": {"type": "string"},
            "field": {"type": "string"},
            "value": {"type": "string"},
            "role": {"type": "string"},
            "owner": {"type": "string"},
            "due": {"type": "string"},
        }}}},
}

_WS = re.compile(r"\s+")
_MONTHS = {"january": "jan", "february": "feb", "march": "mar", "april": "apr", "june": "jun",
           "july": "jul", "august": "aug", "september": "sep", "october": "oct",
           "november": "nov", "december": "dec"}
_CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z'’\-]{1,})")
_NUM = re.compile(r"\d+")


def _flat(v: Any) -> str:
    return _WS.sub(" ", str(v if v is not None else "")).strip()


def _line_of(item: Any) -> str:
    """One entry of a list as the model sees it: attendees and actions are objects, the rest strings."""
    if isinstance(item, dict):
        if "task" in item:
            bits = [_flat(item.get("task"))]
            if _flat(item.get("assigned_to")):
                bits.append(f"[owner: {_flat(item.get('assigned_to'))}]")
            if _flat(item.get("due")):
                bits.append(f"[due: {_flat(item.get('due'))}]")
            return " ".join(bits)
        return f"{_flat(item.get('name'))} [role: {_flat(item.get('role')) or '—'}]"
    return _flat(item)


def _numbered(items: List[Any]) -> str:
    return "\n".join(f"{i}. {_line_of(v)[:SNIPPET]}" for i, v in enumerate(items)) or "(none)"


def _rows(meta: Dict[str, Any]) -> str:
    rows = []
    for d in meta.get("distribution") or []:
        if isinstance(d, dict) and _flat(d.get("addressee")):
            rows.append(" | ".join(_flat(d.get(k)) for k in ("addressee", "copies", "remarks")).rstrip(" |"))
        elif isinstance(d, str) and _flat(d):
            rows.append(_flat(d))
    return " ; ".join(rows)


def _shown(mom: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """The minutes as the model sees them: every header field, xxx...xxx where it was never provided."""
    sec = meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {}
    address = meta.get("address")
    values = {
        "venue": meta.get("venue") or mom.get("venue"), "meeting_date": meta.get("meeting_date") or mom.get("meeting_date"),
        "meeting_time": meta.get("meeting_time") or mom.get("meeting_time"), "telephone": meta.get("telephone"),
        "address": " ; ".join(_flat(a) for a in address) if isinstance(address, list) else address,
        "file_ref": meta.get("file_ref"), "issue_date": meta.get("issue_date"), "precedence": meta.get("precedence"),
        "copy_no": meta.get("copy_no"), "classification": meta.get("classification"),
        "amendments_by": meta.get("amendments_by"), "secretary_name": sec.get("name"),
        "secretary_rank": sec.get("rank"), "distribution": _rows(meta)}
    header = [f"title: {_flat(mom.get('title'))}"]
    header += [f"{f}: {_flat(v) or MISSING}" for f, v in values.items()]
    parts = ["HEADER:\n" + "\n".join(header)]
    for name in LISTS:
        parts.append(f"{name.upper()}:\n{_numbered(mom.get(name) or [])}")
    return "\n\n".join(parts)


def _earlier(state: Dict[str, Any], keep: int = 5) -> str:
    """The user's previous requests on these minutes and what each did, oldest first — without them "update
    his position" reached the model alone, and it changed the wrong person (seen 2026-09-20)."""
    steps = [h for h in (state.get("history") or []) if isinstance(h, dict)] + [state]
    lines = [f'- "{_flat(h.get("instruction"))[:160]}" → {"; ".join(h.get("changes") or []) or "nothing changed"}'
             for h in steps if _flat(h.get("instruction"))]
    return "\n".join(lines[-keep:])


# ── "his", "her", "their": the person changed last ───────────────────────────────────────────────
# Seen on the cluster, 2026-09-21: "Update Ariz Khan name to Shubham Pandey", then "add his position as an AI
# engineer" changed TEENA's role — the model was shown the earlier requests and still guessed. A pronoun with no
# name means the person the last change was about; code decides that, the same for every pronoun (a name says
# nothing about how a person is referred to).
_PRONOUN = re.compile(r"\b(he|him|his|she|her|hers|they|them|their|theirs)\b", re.I)
_TITLES = {"col", "colonel", "maj", "major", "capt", "captain", "lt", "lieutenant", "gen", "general", "brig",
           "brigadier", "sub", "subedar", "hav", "havildar", "naik", "sep", "sepoy", "cdr", "cmde", "mr", "mrs",
           "ms", "miss", "dr", "shri", "smt", "sri", "sir", "the", "and", "for"}


def _name_words(name: str) -> set:
    return {w for w in re.findall(r"[a-z]+", _flat(name).lower()) if len(w) >= 3 and w not in _TITLES}


def _names_someone(instruction: str, attendees: List[Any]) -> bool:
    """Does the instruction name anyone on the attendee list (a first name or a surname is enough)?"""
    said = set(re.findall(r"[a-z]+", instruction.lower()))
    return any(_name_words(a.get("name")) & said for a in attendees if isinstance(a, dict))


def _last_person(state: Dict[str, Any], look_back: int = 5) -> int:
    """The index, in the current attendee list, of the ONE attendee the last change to the list added, renamed
    or gave a role — an undo of one of those included; -1 when it touched none or several (a delete, an undo of
    an add, minutes written again).
    Changes that left the list alone (a telephone number, a deleted point) are stepped over."""
    versions = [state] + [h for h in reversed(state.get("history") or []) if isinstance(h, dict)]
    for newer, older in list(zip(versions, versions[1:]))[:look_back]:
        now = (newer.get("mom") or {}).get("attendees") or []
        was = (older.get("mom") or {}).get("attendees") or []
        if now == was:
            continue
        changed = [i for i, a in enumerate(now) if a not in was]
        return changed[0] if len(changed) == 1 else -1
    return -1


def _about_person(ch: Any) -> bool:
    return isinstance(ch, dict) and (
        (ch.get("list") == "attendees" and ch.get("op") in ("set_role", "replace", "delete"))
        or (ch.get("op") == "set_owner" and bool(_flat(ch.get("owner")))))


def _pronoun_target(state: Dict[str, Any], instruction: str) -> Tuple[str, int]:
    """(the pronoun, the index of the person it means) when the instruction says "his"/"her"/"their" and names
    nobody on the attendee list; ("", -1) when there is nothing to resolve; (pronoun, -1) when it cannot be."""
    m = _PRONOUN.search(instruction)
    attendees = (state.get("mom") or {}).get("attendees") or []
    if not m or _names_someone(instruction, attendees):
        return "", -1
    return m.group(0), _last_person(state)


def _resolve_pronoun(changes: List[Any], mom: Dict[str, Any], pronoun: str, last: int, tag: str) -> List[Any]:
    """Every change about a person made about the person changed last. Raises ValueError when the
    instruction says "his" and there is no one person it can mean — asked, never guessed."""
    if not pronoun or not any(_about_person(ch) for ch in changes):
        return changes
    attendees = mom.get("attendees") or []
    if not 0 <= last < len(attendees) or not isinstance(attendees[last], dict):
        raise ValueError(f"No change was made: I could not tell who {pronoun!r} means. Please write the "
                         "person's name.")
    person = attendees[last]
    name = _flat(person.get("name"))
    out = []
    for ch in changes:
        if _about_person(ch) and ch.get("list") == "attendees":
            if ch.get("index") != last or not _quote_matches(_flat(ch.get("quote")), _line_of(person)):
                logger.info(f"{tag} {pronoun!r} is {name} (changed last), not the line the model chose — "
                            f"{ch.get('op')} goes to {name}")
            ch = dict(ch, index=last, quote=_line_of(person))
        elif _about_person(ch) and _flat(ch.get("owner")) != name:
            logger.info(f"{tag} {pronoun!r} is {name} (changed last) — owner {_flat(ch.get('owner'))!r} → {name}")
            ch = dict(ch, owner=name)
        out.append(ch)
    return out


def _ask(mom: Dict[str, Any], meta: Dict[str, Any], instruction: str, earlier: str = "",
         refused: str = "") -> Dict[str, Any]:
    """The model's changes for `instruction`. `refused`: its previous answer and why the checks refused it,
    for the one second try (process)."""
    from mom import _get_writer
    before = f"EARLIER REQUESTS (oldest first):\n{earlier}\n\n" if earlier else ""
    after = (f"\n\nYOUR PREVIOUS ANSWER WAS REFUSED BY THE CHECKS:\n{refused}\nAnswer again with the complete, "
             "corrected list of changes for the INSTRUCTION, following every rule. If it cannot be done within "
             "the rules, answer cannot.") if refused else ""
    reply = _get_writer().generate(
        _SYSTEM, f"MINUTES:\n{_shown(mom, meta)}\n\n{before}INSTRUCTION:\n{instruction}{after}",
        max_new_tokens=MAX_REPLY_TOKENS, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "minute_changes", "schema": _SCHEMA,
                                                   "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


# ── guard 3: a classification is filled only from the user's own word, and never changed ─────────────
def _grades_in(text: str) -> List[str]:
    """Every grade the text names, longest first, each span counted once ("top secret" is not "secret")."""
    found, rest = [], (text or "").lower()
    for grade, pattern in _GRADE_WORDS:
        rx = re.compile(rf"\b(?:{pattern})\b")
        if rx.search(rest):
            found.append(grade)
            rest = rx.sub(" ", rest)
    return found


def _grade_of(value: str) -> str:
    grades = _grades_in(value)
    return grades[0] if len(grades) == 1 else ""


def _classification_refused(meta: Dict[str, Any], value: str, instruction: str) -> str:
    """Why this classification may not be set, or "" when it may."""
    if _flat(meta.get("classification")):
        return (f"the classification is {_flat(meta.get('classification'))!r}, set with the job — it cannot be "
                "changed from a prompt")
    grade, said = _grade_of(value), _grades_in(instruction)
    if not grade:
        return "classification: give one of RESTRICTED, CONFIDENTIAL, SECRET, TOP SECRET or UNCLASSIFIED"
    if said != [grade]:
        return (f"classification: set only a grade you wrote yourself — the instruction names "
                f"{', '.join(said) or 'none'}")
    return ""


# ── guard 2: no invented detail ──────────────────────────────────────────────────────────────────
def _normalise(text: str) -> str:
    text = _flat(text).lower()
    for long, short in _MONTHS.items():
        text = text.replace(long, short)
    return text


def _unsupported(new_text: str, allowed: str, every_word: bool = False) -> List[str]:
    """Names and numbers in `new_text` that appear nowhere in `allowed`. Empty means it checks out.

    A sentence's first word is capitalised anyway, so it is skipped — except for a header value
    (`every_word`), where every word is a name, a place or a rank: an invented one-word venue must not pass.
    """
    hay = _normalise(allowed)
    bad = []
    caps = re.findall(r"\b[A-Z][A-Za-z'’\-]{1,}", _flat(new_text)) if every_word else _CAP.findall(_flat(new_text))
    for token in caps + _NUM.findall(_flat(new_text)):
        if _normalise(token) not in hay:
            bad.append(token)
    return bad


def _allowed_text(mom: Dict[str, Any], meta: Dict[str, Any], instruction: str) -> str:
    bits = [instruction, _flat(mom.get("title")), _flat(mom.get("summary"))]
    for name in LISTS:
        bits += [_line_of(v) for v in (mom.get(name) or [])]
    for v in meta.values():
        bits.append(_flat(v) if not isinstance(v, (list, dict)) else _flat(str(v)))
    return " ".join(bits)


# ── guard 4: the ITEM grouping follows the lines it grouped ──────────────────────────────────────
def _index_map(old: List[str], new: List[str]) -> Dict[int, int]:
    """old index → new index, for lines that survived the edit. Deleted lines are simply absent."""
    out: Dict[int, int] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out[i1 + k] = j1 + k
        elif tag == "replace":                      # a reworded line keeps its place in the item
            for k in range(min(i2 - i1, j2 - j1)):
                out[i1 + k] = j1 + k
    return out


def _regroup(groups: List[Dict[str, Any]], before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The same ITEMs, pointing at the edited lines. Anything added lands in the last item."""
    op, of, od = flatten_items(before)
    np, nf, nd = flatten_items(after)
    maps = {"points": _index_map(op, np), "figures": _index_map(of, nf),
            "decisions": _index_map([d[0] for d in od], [d[0] for d in nd])}
    sizes = {"points": len(np), "figures": len(nf), "decisions": len(nd)}
    out, seen = [], {k: set() for k in maps}
    for g in groups:
        moved = {"title": g.get("title")}
        for key, m in maps.items():
            kept = [m[i] for i in (g.get(key) or []) if i in m]
            moved[key] = kept
            seen[key].update(kept)
        out.append(moved)
    if not out:
        return []
    for key in maps:                                # new lines join the last item, as leftovers do
        out[-1][key] = out[-1][key] + [i for i in range(sizes[key]) if i not in seen[key]]
    return [g for g in out if g["points"] or g["decisions"] or g["figures"]]


# ── applying the changes ─────────────────────────────────────────────────────────────────────────
def _quote_matches(quote: str, line: str) -> bool:
    q, l = _normalise(quote), _normalise(line)
    return len(q) >= MIN_QUOTE and (q in l or l.startswith(q[:MIN_QUOTE]))


def _plain(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", _normalise(text))) + " "


def _said(value: str, *sources: str) -> bool:
    """Is `value`, as a whole phrase, in one of `sources`? For a short label — a role, an owner, a name —
    each word being somewhere in the minutes proves nothing: "Garrison Commander" passed that way, built from
    "Assistant Garrison Engineer" and "Station Commander" (caught by test, 2026-09-20)."""
    v = _plain(value)
    return v.strip() == "" or any(v in _plain(src) for src in sources)


def _answer_checked(answers: List[str], mom: Dict[str, Any], meta: Dict[str, Any], instruction: str,
                    tag: str) -> str:
    """The model's answer to a question about the minutes, or a sentence saying it could not be given.

    It goes to the chat, not into the document — still, a name or a number the minutes do not hold is not
    passed on (the same test as guard 2): an answer with an invented figure is worse than none."""
    text = " ".join(answers)
    if not text:
        return "I could not find an answer to that in these minutes."
    bad = _unsupported(text, _allowed_text(mom, meta, instruction))
    if bad:
        logger.info(f"{tag} answer not given: it mentions {', '.join(bad[:5])}, which the minutes do not")
        return "I could not answer that from these minutes."
    return text


_LABEL = re.compile(r"\s*\[(role|owner|due):\s*([^\]]*)\]", re.I)
_NONE = {"", "—", "-", "none", "n/a"}


def _labels(ch: Dict[str, Any]) -> Dict[str, Any]:
    """The change with any "[role: …]", "[owner: …]", "[due: …]" moved out of its value into its own field.

    Those labels are how `_line_of` SHOWS a line to the model, and it copies them back: "Teena [role: Intern
    in AI]" was refused as a name the user never gave, and "Shubham Pandey [role: Team Leader of AI]" as a
    rename the user never wrote (cluster, 2026-09-19/21). A label is ours, not an invented detail; what it
    carries is still checked like any other value. A field the model did fill wins over a label."""
    value = _flat(ch.get("value"))
    found = _LABEL.findall(value)
    if not found:
        return ch
    out = dict(ch, value=_flat(_LABEL.sub(" ", value)))
    for key, text in found:
        key, text = key.lower(), _flat(text)
        if text.lower() not in _NONE and not _flat(out.get(key)):
            out[key] = text
    return out


def _find_line(items: List[Any], idx: int, quote: str) -> Tuple[int, str]:
    """(index, "") of the line a change is about, or (-1, why not).

    The line at `idx` when the quote matches it. Otherwise the ONE line of the list the quote matches: the
    model's number was one line off twice running on 2026-09-20 ("Col. Ariz Khan" quoted, line 5 — Gupta's —
    given), and the quote is the part that says what the user meant. Several lines, none, or a quote too
    short to tell apart are refused: a change never lands on a line nobody named (guard 1)."""
    if 0 <= idx < len(items) and _quote_matches(quote, _line_of(items[idx])):
        return idx, ""
    if len(_normalise(quote)) < MIN_QUOTE:
        return -1, f"the quote {quote!r} is too short to tell which line is meant"
    hits = [i for i, v in enumerate(items) if _quote_matches(quote, _line_of(v))]
    if len(hits) == 1:
        logger.info(f"quote {quote[:40]!r} names line {hits[0]}, not {idx} — the quoted line is changed")
        return hits[0], ""
    return -1, "the quoted text matches no line" if not hits else "the quoted text matches several lines"


def apply(mom: Dict[str, Any], meta: Dict[str, Any], changes: List[Dict[str, Any]],
          instruction: str, earlier: str = "") -> Tuple[List[str], List[str], bool]:
    """Change `mom` and `meta` in place. Returns (what was done, what was refused, undo asked).

    `earlier` is the user's previous requests and what they changed (with old values), so a revert to a
    value no longer in the minutes counts as the user's own words, not an invention."""
    done: List[str] = []
    refused: List[str] = []
    allowed = _allowed_text(mom, meta, instruction) + " " + earlier
    undo = False
    # Deletions are collected and applied at the end: removing as we go would shift every later index.
    to_delete: Dict[str, set] = {name: set() for name in LISTS}

    for ch in changes[:MAX_CHANGES]:
        if not isinstance(ch, dict):
            continue
        ch = _labels(ch)
        op, name = _flat(ch.get("op")), _flat(ch.get("list"))
        idx = ch.get("index") if isinstance(ch.get("index"), int) else -1
        value, quote = _flat(ch.get("value")), _flat(ch.get("quote"))
        owner, due, field = _flat(ch.get("owner")), _flat(ch.get("due")), _flat(ch.get("field"))
        role = _flat(ch.get("role"))

        if op == "undo":
            undo = True
            continue
        if op == "cannot":
            refused.append(value or "the instruction cannot be done as a change to these minutes")
            continue
        if op == "set_meta":
            if field not in EDITABLE_META:
                refused.append(f"{field or 'that header field'} cannot be changed from a prompt")
                continue
            bad = _unsupported(value, allowed, every_word=True)
            if bad:
                refused.append(f"{field}: {value!r} mentions {', '.join(bad)}, which you did not give")
                continue
            if field == "classification":
                why = _classification_refused(meta, value, instruction)
                if why:
                    refused.append(why)
                    continue
                value = _grade_of(value)
                meta["classification"] = value
            elif field.startswith("secretary_"):
                sec = dict(meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {})
                sec[field.split("_", 1)[1]] = value
                meta["secretary"] = sec
            elif field == "address":
                parts = [x for x in (_flat(v) for v in re.split(r"\s*[;\n]\s*", value)) if x]
                if len(parts) == 1:                     # "HQ 7 Inf Bde, C/O 56 APO, PIN 900111"
                    parts = [x for x in (_flat(v) for v in parts[0].split(",")) if x]
                meta["address"] = parts
            elif field == "distribution":
                rows = []
                for entry in re.split(r"\s*[;\n]\s*", value):
                    bits = [_flat(b) for b in entry.split("|")] + ["", ""]
                    if bits[0]:
                        rows.append({"addressee": bits[0], "copies": bits[1], "remarks": bits[2]})
                if not rows:
                    refused.append("distribution: no addressee given")
                    continue
                meta["distribution"] = rows
            elif field == "precedence":
                meta["precedence"] = value.upper()
            else:
                meta[field] = value
            done.append(f"{field} → {value}")
            continue
        if op == "set_title":
            bad = _unsupported(value, allowed)
            if bad:
                refused.append(f"the new title mentions {', '.join(bad)}, which you did not give")
                continue
            mom["title"] = value
            done.append(f"title → {value}")
            continue

        items = mom.get(name)
        if name not in LISTS or not isinstance(items, list):
            refused.append(f"{op}: {name or 'that list'} is not part of the minutes")
            continue
        if op == "add":
            if not value:
                continue
            if name == "attendees":                 # the role of a new attendee: `role` (older answers: `owner`)
                role, owner = role or owner, ""
            bad = _unsupported(" ".join([value, role, owner, due]), allowed)
            if bad:
                refused.append(f"new {name[:-1]}: mentions {', '.join(bad)}, which you did not give")
                continue
            people = " ; ".join(_flat(a.get("name")) for a in (mom.get("attendees") or []) if isinstance(a, dict))
            if name == "attendees" and not _said(value, instruction, earlier):
                refused.append(f"new attendee {value!r}: not a name you gave")
                continue
            if name == "attendees" and role and not _said(role, instruction, earlier):
                refused.append(f"new attendee {value}: the role {role!r} is not what you wrote")
                continue
            if owner and not _said(owner, instruction, earlier, people):
                refused.append(f"{owner!r}: not a name you gave or one at the meeting")
                continue
            if name == "action_items":
                items.append({"task": value, "assigned_to": owner, "assigned_by": "", "due": due})
            elif name == "attendees":
                items.append({"name": value, "role": role})
            else:
                items.append(value)
            done.append(f"added to {name}: {value[:60]}" + (f" ({role})" if name == "attendees" and role else ""))
            continue

        idx, why = _find_line(items, idx, quote)
        if idx < 0:
            refused.append(f"{op} {name}: {why}")
            logger.info(f"{op} {name}: {why} — change skipped")
            continue
        line = _line_of(items[idx])
        if op == "delete":
            to_delete[name].add(idx)
            done.append(f"removed from {name}: {line[:60]}")
        elif op in ("replace", "set_role", "set_owner"):
            bad = _unsupported(" ".join([value, role, owner, due]), allowed)
            if bad:
                refused.append(f"{op} {name} {idx}: mentions {', '.join(bad)}, which you did not give")
                continue
            if op == "set_role":                    # the new role: `role` (older answers: `value` or `owner`)
                role = role or value or owner
            elif name == "attendees" and isinstance(items[idx], dict) and _plain(role) == _plain(items[idx].get("role")):
                role = ""                           # the role the line already has, carried along: no change
            # A role, an owner or a person's name must be the user's own phrase (or an earlier value being
            # put back); an owner may also be anyone already on the attendee list. Name and role are checked
            # each on its own: together they are never a phrase the user wrote.
            people = " ; ".join(_flat(a.get("name")) for a in (mom.get("attendees") or []) if isinstance(a, dict))
            name_given = value if (op == "replace" and name == "attendees") else ""
            if name_given and not _said(name_given, instruction, earlier):
                refused.append(f"{op} {name}: {name_given!r} is not what you wrote")
                continue
            if name == "attendees" and role and not _said(role, instruction, earlier):
                refused.append(f"{op} {name}: the role {role!r} is not what you wrote")
                continue
            if op == "set_owner" and owner and not _said(owner, instruction, earlier, people):
                refused.append(f"set_owner: {owner!r} is not a name you gave or one at the meeting")
                continue
            if op == "set_role" or (op == "replace" and name == "attendees" and role and not value):
                entry = items[idx] if isinstance(items[idx], dict) else {"name": _flat(items[idx])}
                was = _flat(entry.get("role")) or "none"
                items[idx] = {**entry, "role": role}
                done.append(f"{_flat(items[idx].get('name'))}: role {was} → {role}")
            elif op == "set_owner" and isinstance(items[idx], dict):
                was = _flat(items[idx].get("assigned_to")) or "none"
                items[idx] = {**items[idx], "assigned_to": owner or items[idx].get("assigned_to", ""),
                              "due": due or items[idx].get("due", "")}
                done.append(f"owner of '{_flat(items[idx].get('task'))[:50]}': {was} → {owner or '(unchanged)'}"
                            + (f", due {due}" if due else ""))
            elif op == "replace" and value:
                if isinstance(items[idx], dict):
                    inner = "task" if "task" in items[idx] else "name"
                    was = _flat(items[idx].get(inner))
                    items[idx] = {**items[idx], inner: value}
                    if inner == "name" and role:
                        items[idx]["role"] = role
                else:
                    was = _flat(items[idx])
                    items[idx] = value
                done.append(f"{name}: '{was[:60]}' → '{value[:60]}'" + (f" ({role})" if name == "attendees" and role else ""))
            else:
                refused.append(f"{op} {name} {idx}: nothing to change")

    for name, drop in to_delete.items():
        if drop:
            mom[name] = [v for i, v in enumerate(mom.get(name) or []) if i not in drop]
    return done, refused, undo


# The name the first version was stored under: "{hash}/MoM-notes.docx" → "MoM-notes".
_STORED_NAME = re.compile(r"(MoM(?:-.+)?)\.docx")


def _file_name(state: Dict[str, Any], job: KafkaJob, mom: Dict[str, Any]) -> str:
    """An edited version keeps the name the minutes already have.

    An edit carries no document, so the name cannot be worked out from the job again — it is read back
    from where the last version was stored. Minutes stored before the rename (as "{hash}.docx") have
    none, so they get a name the usual way, from a `fileName` the edit may carry or the title.
    """
    stored = _STORED_NAME.fullmatch(str(state.get("summary_object_key") or "").rsplit("/", 1)[-1])
    return stored.group(1) if stored else summary_file_name(job, mom.get("title") or "")


def _fillable_template(state: Dict[str, Any], c: Clients, tag: str) -> Optional[bytes]:
    """The job's fill-in template, fetched again, so the edited minutes keep the layout they had. The
    default template when there was none, or it can no longer be read (logged, never fatal)."""
    url = ((state.get("template") or {}).get("url") or "").strip()
    if not url:
        return None
    try:
        import minutes_template
        raw = c.store.download(url)
        if minutes_template.is_fillable(raw):
            return raw
        logger.warning(f"{tag} the template at {url!r} no longer has slots — the default layout is used")
    except Exception as e:
        logger.warning(f"{tag} the template at {url!r} could not be read ({type(e).__name__}) — the default "
                       "layout is used")
    return None


def _same_minutes(job: KafkaJob, state: Optional[Dict[str, Any]]) -> bool:
    """Is this message about the minutes in `state` — the same document(s), or none?"""
    if not state:
        return False
    mine, known = set(minutes_state.sources_of(job)), state.get("sources")
    return not mine or (known is not None and mine <= set(known))


def previous_state(job: KafkaJob, c: Clients) -> Optional[Dict[str, Any]]:
    """The saved minutes a follow-up that is written AGAIN from the same document ("focus more on the budget")
    replaces, or None for a new meeting (a different document). Never raises.

    Two things carry over from it: the header details the user gave in earlier messages — the telephone or
    address must not fall back to xxx...xxx — and the history, with the replaced version on top, so "undo"
    brings back the minutes and every change the user made to them. Until 2026-09-21 the rewrite started a
    fresh history, and a question answered "cannot" cost the user all their edits with no way back."""
    try:
        state = minutes_state.load(job, c.store) if job.conversation_id else None
        return state if _same_minutes(job, state) else None
    except Exception:
        return None


def header_so_far(job: KafkaJob, c: Clients) -> Dict[str, Any]:
    """The header details of `previous_state`, or {}. Never raises."""
    state = previous_state(job, c)
    return copy.deepcopy(state.get("meta") or {}) if state else {}


def follow_up(job: KafkaJob, c: Clients) -> Optional[dict]:
    """A message that did not say it is an edit, but is one: this conversation already has minutes, and the
    message carries the same document(s) or none. Tried as an edit; None when it is not one.

    The IMIR frontend sends every follow-up as a new job, re-attaching the file (seen 2026-09-20: "add tele
    9654396200" re-read the PDF, wrote the minutes again, and the telephone stayed xxx...xxx). A DIFFERENT
    document is a new meeting, and so are minutes too old to know what they were written from (saved before
    `sources` existed) when the message brings a file. Never raises.
    """
    try:
        if not job.prompt.strip() or not job.conversation_id:
            return None
        state = minutes_state.load(job, c.store)
        if not _same_minutes(job, state):
            return None
        mine = minutes_state.sources_of(job)
        logger.info(f"[JOB {job.conversation_id}] a follow-up on the minutes this conversation already has "
                    f"({'same document' if mine else 'no document'}) — tried as a change first")
        return process(job, c, follow=True, state=state)
    except Exception as e:
        logger.warning(f"[JOB {job.conversation_id}] follow-up check failed ({type(e).__name__}: {e}) — "
                       "the minutes are written again")
        return None


def process(job: KafkaJob, c: Clients, follow: bool = False,
            state: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    """An edit job → an acknowledgement, exactly like a normal job. Never raises.

    `follow`: the job did not say it is an edit (see follow_up). Then an instruction the model cannot turn
    into a change — "focus more on the budget", a question — returns None, and the caller writes the minutes
    again. A change the guards refuse is still answered with the reason, never rewritten around the guard."""
    t0 = time.time()
    tag = f"[JOB {job.conversation_id}]"
    try:
        instruction = job.prompt.strip()
        if not instruction:
            raise ValueError("An edit needs a prompt saying what to change.")
        state = state or minutes_state.load(job, c.store)
        if not state:
            raise ValueError(f"No minutes to edit for conversation {job.conversation_id!r}. "
                             "Create them first, then send the change.")

        mom = copy.deepcopy(state["mom"])
        meta = copy.deepcopy(state.get("meta") or {})
        before = copy.deepcopy(mom)
        history = list(state.get("history") or [])

        earlier = _earlier(state)
        pronoun, last = _pronoun_target(state, instruction)
        if pronoun and last >= 0:
            earlier += (f'\n- ("{pronoun}" in the INSTRUCTION means {_flat(mom["attendees"][last].get("name"))}, '
                        f"the person changed last: ATTENDEES line {last})")
        data = _ask(mom, meta, instruction, earlier)
        changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        logger.info(f"{tag} edit: {len(changes)} change(s) proposed for {instruction[:70]!r}")
        # A QUESTION IS ANSWERED, NOTHING CHANGES. "can you explain me the agenda of this meeting" used to come
        # back "cannot", and a follow-up that cannot be done is written again from the document: the user's
        # added and renamed attendees were gone, and so was the history undo reads (cluster, 2026-09-21).
        asked = any(isinstance(ch, dict) and ch.get("op") == "answer" for ch in changes)
        answers = [_flat(ch.get("value")) for ch in changes
                   if isinstance(ch, dict) and ch.get("op") == "answer" and _flat(ch.get("value"))]
        changes = [ch for ch in changes if not (isinstance(ch, dict) and ch.get("op") == "answer")]
        answer = _answer_checked(answers, mom, meta, instruction, tag) if asked else ""
        if asked and all(isinstance(ch, dict) and ch.get("op") == "cannot" for ch in changes):
            logger.info(f"{tag} answered a question in {time.time()-t0:.0f}s — the minutes are unchanged")
            return build_ack(job, success=True, bucket=state.get("summary_bucket") or "",
                             object_key=state.get("summary_object_key") or "", description=answer,
                             limit=ANSWER_CHARS)
        # UNDO FIRST, THEN THE REST. "revert the change to Gupta and update Ariz Khan's position" used to do
        # only the undo — the rest was silently dropped. The other changes are now applied to the restored
        # version, each checked against it (a quote that no longer matches is refused and said so).
        undo = any(isinstance(ch, dict) and ch.get("op") == "undo" for ch in changes)
        if undo:
            if not history:
                raise ValueError("There is nothing to undo: these are the first minutes.")
            previous = history.pop()
            mom = copy.deepcopy(previous["mom"])
            meta = copy.deepcopy(previous.get("meta") or {})
            before = copy.deepcopy(mom)
            changes = [ch for ch in changes if not (isinstance(ch, dict) and ch.get("op") == "undo")]
        else:
            changes = _resolve_pronoun(changes, mom, pronoun, last, tag)
        base_meta = copy.deepcopy(meta)
        done, refused, _ = apply(mom, meta, changes, instruction, earlier)
        if follow and not done and not undo and all(
                isinstance(ch, dict) and ch.get("op") == "cannot" for ch in changes):
            logger.info(f"{tag} not a change to the minutes ({refused[0] if refused else 'no change proposed'}) — "
                        "writing them again from the document")
            return None

        # ONE SECOND TRY when a check refused a change: the model is shown its answer and the reasons, the way
        # validation libraries re-ask (Instructor's "reask"), and its new answer passes the SAME checks — the
        # guards are never loosened, the model only gets to put the user's words in the right fields. One
        # extra call (~2 s), only after a refusal. The better of the two answers is kept.
        cannots = sum(1 for ch in changes if isinstance(ch, dict) and ch.get("op") == "cannot")
        if len(refused) > cannots:
            shown = [{k: v for k, v in ch.items() if v not in ("", -1, None)} for ch in changes if isinstance(ch, dict)]
            why = f"{json.dumps(shown, ensure_ascii=False)}\nRefused:\n" + "\n".join(f"- {r}" for r in refused)
            again = _ask(before, base_meta, instruction, earlier, refused=why)
            retry = [ch for ch in (again.get("changes") if isinstance(again.get("changes"), list) else [])
                     if isinstance(ch, dict) and ch.get("op") not in ("undo", "answer")]   # undo already applied
            if not undo:
                retry = _resolve_pronoun(retry, before, pronoun, last, tag)
            mom2, meta2 = copy.deepcopy(before), copy.deepcopy(base_meta)
            done2, refused2, _ = apply(mom2, meta2, retry, instruction, earlier)
            better = len(done2) > len(done)
            logger.info(f"{tag} second try after {len(refused) - cannots} refusal(s): {len(done2)} change(s) made, "
                        f"{len(refused2)} refused — {'used' if better else 'first answer kept'}")
            if better:
                mom, meta, done, refused = mom2, meta2, done2, refused2

        if not done and not undo:
            reason = refused[0] if refused else "nothing in the minutes matched that instruction"
            raise ValueError(f"No change was made: {reason}.")
        base = previous["mom"] if undo else state["mom"]
        if done:
            groups = base.get("item_groups")
            if isinstance(groups, list) and groups:
                mom["item_groups"] = _regroup(groups, before, mom)
        if undo:
            done = ["undid the previous change"] + done

        # THE STATE IS SAVED LAST — it is what the next prompt edits, so it may change only once everything
        # that can fail has succeeded. Saved first (as until 2026-09-20), an Elasticsearch outage after it
        # left the edit applied behind a FAILED ack, and the user's natural retry applied it AGAIN: "delete
        # point 5" twice deletes two different points. Now a failure anywhere leaves the saved minutes as
        # they were — the new .docx sits unused under its own hash — and sending the change again is safe.
        # Same order as a new job (minutes.process): file, search record, state; the chunk copy, which
        # never raises, after.
        docx_bytes = build_mom_docx(mom, meta, template=_fillable_template(state, c, tag))
        bucket, key = c.store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest(),
                                                        name=_file_name(state, job, mom)),
                                     docx_bytes, DOCX_MIME)
        c.index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key,
                          template=state.get("template") or {})
        history.append(minutes_state.snapshot(state))
        if not minutes_state.save(job, c.store, mom=mom, meta=meta, template=state.get("template") or {},
                                  object_key=key, bucket=bucket, instruction=instruction, changes=done,
                                  history=history, sources=state.get("sources")):
            # For a new job a lost state only costs the NEXT edit; for an edit it IS the edit.
            raise ValueError("The change could not be saved to file storage, so it was not made. "
                             "Send it again.")
        c.chunks.index_mom(job, mom, summary_bucket=bucket, summary_object_key=key)

        for line in done:
            logger.info(f"{tag}   ✓ {line}")
        for line in refused:
            logger.info(f"{tag}   ✗ {line}")
        logger.info(f"{tag} edited in {time.time()-t0:.0f}s — {bucket}/{key}")
        said = "; ".join(done)
        if refused:
            said += f". Not done: {refused[0]}"
        if answer:
            said += f". {answer}"
        return build_ack(job, success=True, bucket=bucket, object_key=key, description=said,
                         limit=ANSWER_CHARS if answer else None)
    except Exception as e:
        logger.error(f"{tag} edit failed after {time.time()-t0:.0f}s: {e}")
        # ValueError is only ever raised here with a sentence meant for the user, so it is passed on
        # as it stands. Any other exception keeps its class name, which is what makes a MinIO or model
        # failure diagnosable from the ack alone.
        return build_ack(job, success=False,
                         description=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")

