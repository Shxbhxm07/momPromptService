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
  3. CLASSIFICATION AND PRECEDENCE are never editable from chat text. A misread "remove the secret
     part" must not declassify a document; those come from `mom_meta` only.
  4. NOTHING ELSE MOVES — untouched lines are copied exactly, and the ITEM grouping is remapped from
     the old minutes to the new rather than worked out again, so items do not reshuffle.

EVERY VERSION IS KEPT: each edit writes a new .docx under its own hash and pushes the previous state
onto a history, so "undo the last change" is a change like any other.
"""
import copy
import difflib
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Tuple

import minutes_state
from docx_export import build_mom_docx, flatten_items
from kafka_contract import KafkaJob, build_ack, summary_object_key
from setup import Clients

logger = logging.getLogger("edit")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MAX_CHANGES = 40
MAX_REPLY_TOKENS = 2000
SNIPPET = 160                 # how much of each line the model is shown; enough to recognise it
MIN_QUOTE = 8                 # a shorter quote proves nothing about which line was meant

# Header fields a chat instruction may change. Classification, precedence and copy number are NOT
# here, by guard 3; neither is the distribution list, which comes from the HQ's template.
EDITABLE_META = {"venue", "meeting_date", "meeting_time", "file_ref", "amendments_by", "telephone",
                 "secretary_name", "secretary_rank"}
LISTS = ("key_points", "decisions", "action_items", "agenda", "attendees", "key_figures")

_SYSTEM = """You are given MINUTES already written from a meeting, and one INSTRUCTION from the user asking to change them.

Answer with a list of CHANGES. Never rewrite the minutes, never repeat unchanged lines.

Every change names what it touches by its index in the list shown, and quotes the first words of that line so the change can be checked.

Operations:
- set_meta   : change a header field. field = venue | meeting_date | meeting_time | file_ref | amendments_by | telephone | secretary_name | secretary_rank
- set_title  : change the meeting's title
- delete     : remove one line.              list + index + quote
- replace    : reword one line.              list + index + quote + value
- add        : add one new line at the end.  list + value (for action_items also owner and due)
- set_owner  : set who owns an action and when it is due. list = action_items, index + quote + owner + due
- set_role   : set an attendee's role.       list = attendees, index + quote + value (e.g. Chairman, Secretary)
- undo       : undo the previous change to these minutes
- cannot     : the instruction cannot be done as a change; say why in value

Rules:
- Use ONLY words the user gave you or that are already in the minutes. Never invent a name, a date or a number.
- The user cannot change the security classification here; answer "cannot" if asked.
- If the instruction asks for something that needs the original meeting document read again (for example "focus more on the budget"), answer "cannot".
- Leave unused fields as "" and unused indexes as -1."""

_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["changes"],
    "properties": {"changes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["op", "list", "index", "quote", "field", "value", "owner", "due"],
        "properties": {
            "op": {"type": "string", "enum": ["set_meta", "set_title", "delete", "replace", "add",
                                              "set_owner", "set_role", "undo", "cannot"]},
            "list": {"type": "string", "enum": list(LISTS) + [""]},
            "index": {"type": "integer"},
            "quote": {"type": "string"},
            "field": {"type": "string"},
            "value": {"type": "string"},
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


def _shown(mom: Dict[str, Any], meta: Dict[str, Any]) -> str:
    sec = meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {}
    header = [f"title: {_flat(mom.get('title'))}"]
    header += [f"{f}: {_flat(meta.get(f))}" for f in
               ("venue", "meeting_date", "meeting_time", "file_ref", "amendments_by", "telephone")]
    header += [f"secretary_name: {_flat(sec.get('name'))}", f"secretary_rank: {_flat(sec.get('rank'))}"]
    parts = ["HEADER:\n" + "\n".join(header)]
    for name in LISTS:
        parts.append(f"{name.upper()}:\n{_numbered(mom.get(name) or [])}")
    return "\n\n".join(parts)


def _ask(mom: Dict[str, Any], meta: Dict[str, Any], instruction: str) -> Dict[str, Any]:
    from mom import _get_writer
    reply = _get_writer().generate(
        _SYSTEM, f"MINUTES:\n{_shown(mom, meta)}\n\nINSTRUCTION:\n{instruction}",
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


# ── guard 2: no invented detail ──────────────────────────────────────────────────────────────────
def _normalise(text: str) -> str:
    text = _flat(text).lower()
    for long, short in _MONTHS.items():
        text = text.replace(long, short)
    return text


def _unsupported(new_text: str, allowed: str) -> List[str]:
    """Names and numbers in `new_text` that appear nowhere in `allowed`. Empty means it checks out."""
    hay = _normalise(allowed)
    bad = []
    for token in _CAP.findall(_flat(new_text)) + _NUM.findall(_flat(new_text)):
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


def apply(mom: Dict[str, Any], meta: Dict[str, Any], changes: List[Dict[str, Any]],
          instruction: str) -> Tuple[List[str], List[str], bool]:
    """Change `mom` and `meta` in place. Returns (what was done, what was refused, undo asked)."""
    done: List[str] = []
    refused: List[str] = []
    allowed = _allowed_text(mom, meta, instruction)
    undo = False
    # Deletions are collected and applied at the end: removing as we go would shift every later index.
    to_delete: Dict[str, set] = {name: set() for name in LISTS}

    for ch in changes[:MAX_CHANGES]:
        if not isinstance(ch, dict):
            continue
        op, name = _flat(ch.get("op")), _flat(ch.get("list"))
        idx = ch.get("index") if isinstance(ch.get("index"), int) else -1
        value, quote = _flat(ch.get("value")), _flat(ch.get("quote"))
        owner, due, field = _flat(ch.get("owner")), _flat(ch.get("due")), _flat(ch.get("field"))

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
            bad = _unsupported(value, allowed)
            if bad:
                refused.append(f"{field}: {value!r} mentions {', '.join(bad)}, which you did not give")
                continue
            if field.startswith("secretary_"):
                sec = dict(meta.get("secretary") if isinstance(meta.get("secretary"), dict) else {})
                sec[field.split("_", 1)[1]] = value
                meta["secretary"] = sec
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
            bad = _unsupported(" ".join([value, owner, due]), allowed)
            if bad:
                refused.append(f"new {name[:-1]}: mentions {', '.join(bad)}, which you did not give")
                continue
            if name == "action_items":
                items.append({"task": value, "assigned_to": owner, "assigned_by": "", "due": due})
            elif name == "attendees":
                items.append({"name": value, "role": owner})
            else:
                items.append(value)
            done.append(f"added to {name}: {value[:60]}")
            continue

        if not 0 <= idx < len(items):
            refused.append(f"{op}: there is no {name} {idx}")
            continue
        line = _line_of(items[idx])
        if not _quote_matches(quote, line):
            refused.append(f"{op} {name} {idx}: the quoted text does not match that line")
            logger.info(f"quote {quote[:40]!r} does not match {line[:60]!r} — change skipped")
            continue
        if op == "delete":
            to_delete[name].add(idx)
            done.append(f"removed from {name}: {line[:60]}")
        elif op in ("replace", "set_role", "set_owner"):
            bad = _unsupported(" ".join([value, owner, due]), allowed)
            if bad:
                refused.append(f"{op} {name} {idx}: mentions {', '.join(bad)}, which you did not give")
                continue
            if op == "set_role":
                entry = items[idx] if isinstance(items[idx], dict) else {"name": _flat(items[idx])}
                items[idx] = {**entry, "role": value or owner}
                done.append(f"{_flat(items[idx].get('name'))} → {value or owner}")
            elif op == "set_owner" and isinstance(items[idx], dict):
                items[idx] = {**items[idx], "assigned_to": owner or items[idx].get("assigned_to", ""),
                              "due": due or items[idx].get("due", "")}
                done.append(f"owner of {name} {idx} → {owner or '(unchanged)'}"
                            + (f", due {due}" if due else ""))
            elif op == "replace" and value:
                if isinstance(items[idx], dict):
                    inner = "task" if "task" in items[idx] else "name"
                    items[idx] = {**items[idx], inner: value}
                else:
                    items[idx] = value
                done.append(f"reworded {name} {idx}")
            else:
                refused.append(f"{op} {name} {idx}: nothing to change")

    for name, drop in to_delete.items():
        if drop:
            mom[name] = [v for i, v in enumerate(mom.get(name) or []) if i not in drop]
    return done, refused, undo


def process(job: KafkaJob, c: Clients) -> dict:
    """An edit job → an acknowledgement, exactly like a normal job. Never raises."""
    t0 = time.time()
    tag = f"[JOB {job.conversation_id}]"
    try:
        instruction = job.prompt.strip()
        if not instruction:
            raise ValueError("An edit needs a prompt saying what to change.")
        state = minutes_state.load(job, c.store)
        if not state:
            raise ValueError(f"No minutes to edit for conversation {job.conversation_id!r}. "
                             "Create them first, then send the change.")

        mom = copy.deepcopy(state["mom"])
        meta = copy.deepcopy(state.get("meta") or {})
        before = copy.deepcopy(mom)
        history = list(state.get("history") or [])

        data = _ask(mom, meta, instruction)
        changes = data.get("changes") if isinstance(data.get("changes"), list) else []
        logger.info(f"{tag} edit: {len(changes)} change(s) proposed for {instruction[:70]!r}")
        done, refused, undo = apply(mom, meta, changes, instruction)

        if undo:
            if not history:
                raise ValueError("There is nothing to undo: these are the first minutes.")
            previous = history.pop()
            mom = copy.deepcopy(previous["mom"])
            meta = copy.deepcopy(previous.get("meta") or {})
            done = ["undid the previous change"]         # exclusive: nothing else is applied with it
        elif not done:
            reason = refused[0] if refused else "nothing in the minutes matched that instruction"
            raise ValueError(f"No change was made: {reason}.")
        else:
            groups = state["mom"].get("item_groups")
            if isinstance(groups, list) and groups:
                mom["item_groups"] = _regroup(groups, before, mom)

        docx_bytes = build_mom_docx(mom, meta)
        bucket, key = c.store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest()),
                                     docx_bytes, DOCX_MIME)
        history.append(minutes_state.snapshot(state))
        minutes_state.save(job, c.store, mom=mom, meta=meta, template=state.get("template") or {},
                           object_key=key, bucket=bucket, instruction=instruction, changes=done,
                           history=history)
        c.index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key,
                          template=state.get("template") or {})
        c.chunks.index_mom(job, mom, summary_bucket=bucket, summary_object_key=key)

        for line in done:
            logger.info(f"{tag}   ✓ {line}")
        for line in refused:
            logger.info(f"{tag}   ✗ {line}")
        logger.info(f"{tag} edited in {time.time()-t0:.0f}s — {bucket}/{key}")
        said = "; ".join(done)
        if refused:
            said += f". Not done: {refused[0]}"
        return build_ack(job, success=True, bucket=bucket, object_key=key, description=said)
    except Exception as e:
        logger.error(f"{tag} edit failed after {time.time()-t0:.0f}s: {e}")
        # ValueError is only ever raised here with a sentence meant for the user, so it is passed on
        # as it stands. Any other exception keeps its class name, which is what makes a MinIO or model
        # failure diagnosable from the ack alone.
        return build_ack(job, success=False,
                         description=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")

