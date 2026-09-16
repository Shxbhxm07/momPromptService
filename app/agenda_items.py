"""Sort the verified points, decisions and figures under the meeting's agenda items.

WHY. Appendix AD records the discussion as ITEM I, ITEM II, ITEM III — one block per agenda entry,
each ending in its own Decision with the owner in the Action column (Ch 6 paras 16.7, 16.14). The
writer returns flat lists with no link to the agenda, so `docx_export._items()` could only ever build
a single ITEM holding everything. That is the last structural difference from the manual's layout;
the renderer already draws several items correctly once it is given the grouping.

WHAT THIS DOES NOT DO. It never writes a sentence. The model is shown numbered lists and answers with
INDEX NUMBERS only — "point 5 belongs to agenda item 2" — so every line in the minutes is still text
that came out of the writer's own grounding checks. The schema allows integers and nothing else, and
any index that is out of range, repeated, or missing is handled here rather than trusted.

FAILURE IS ALWAYS SAFE. No agenda, a refused call, unusable JSON, or a grouping that leaves most of
the meeting unplaced — each falls back to the single ITEM the service produced before, which is
correct JSSD, just coarser. A wrong grouping is worse than a coarse one, so the bar to accept is
`MIN_PLACED`.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agenda")

# Below this share of placed points the grouping is treated as a failure: a model that places three
# points out of seventy has not understood the meeting, and a near-empty ITEM II reads worse than one
# honest ITEM I.
MIN_PLACED = 0.5
MAX_ITEMS = 12
# Points are shown to the model shortened: it needs enough to recognise the subject, not the whole
# sentence, and a 70-point meeting must still fit well inside the context.
SNIPPET = 140
MAX_REPLY_TOKENS = 1200

_SYSTEM = """You are given a meeting's AGENDA and numbered lists of POINTS, DECISIONS and FIGURES already written
from that meeting. Sort them under the agenda items they belong to.

Answer with INDEX NUMBERS ONLY. Never write, reword, translate or invent any text.

Rules:
- Every agenda item you return must use its index from the AGENDA list.
- Put each point, decision and figure under at most ONE agenda item — the one it belongs to.
- If something fits no agenda item, leave it out; do not force it.
- Keep the agenda's order.
- An agenda item with nothing under it may be left out."""

_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["agenda", "points", "decisions", "figures"],
        "properties": {
            "agenda": {"type": "integer"},
            "points": {"type": "array", "items": {"type": "integer"}},
            "decisions": {"type": "array", "items": {"type": "integer"}},
            "figures": {"type": "array", "items": {"type": "integer"}},
        }}}},
}


_WS = re.compile(r"\s+")


def _numbered(values: List[str], limit: int = SNIPPET) -> str:
    """One per line, numbered from 0 — the indices the model answers with."""
    lines = []
    for i, v in enumerate(values):
        lines.append(f"{i}. " + _WS.sub(" ", str(v)).strip()[:limit])
    return "\n".join(lines)


def _ask(agenda: List[str], points: List[str], decisions: List[str], figures: List[str]) -> Dict[str, Any]:
    from mom import _get_writer
    user = (f"AGENDA:\n{_numbered(agenda)}\n\nPOINTS:\n{_numbered(points)}\n\n"
            f"DECISIONS:\n{_numbered(decisions)}\n\nFIGURES:\n{_numbered(figures)}")
    reply = _get_writer().generate(
        _SYSTEM, user, max_new_tokens=MAX_REPLY_TOKENS, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "agenda_grouping", "schema": _SCHEMA, "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


def _clean_indices(raw: Any, count: int, taken: set) -> List[int]:
    """In-range, first-come indices only — a repeat anywhere in the reply is dropped, not duplicated."""
    out = []
    for i in raw if isinstance(raw, list) else []:
        if isinstance(i, bool) or not isinstance(i, int):
            continue
        if 0 <= i < count and i not in taken:
            taken.add(i)
            out.append(i)
    return out


def group(mom: Dict[str, Any], points: List[str], decisions: List[Any], figures: List[str]) -> Optional[List[Dict[str, Any]]]:
    """Agenda items as index groups: [{title, points[i], decisions[i], figures[i]}], or None.

    `points`, `decisions` and `figures` are the renderer's own flattened lists, so the indices point
    at exactly the strings it will print — no text crosses back from the model.
    """
    agenda = [re.sub(r"\s+", " ", str(a)).strip() for a in (mom.get("agenda") or []) if str(a).strip()]
    if len(agenda) < 2 or not points:
        return None                      # one agenda entry is one ITEM: today's behaviour already
    try:
        data = _ask(agenda[:MAX_ITEMS], points, [d[0] if isinstance(d, (list, tuple)) else str(d) for d in decisions], figures)
    except Exception as e:
        logger.warning(f"grouping call failed ({type(e).__name__}: {e}) — one item, as before")
        return None

    used_p, used_d, used_f = set(), set(), set()
    groups: List[Dict[str, Any]] = []
    for it in (data.get("items") or [])[:MAX_ITEMS]:
        if not isinstance(it, dict) or not isinstance(it.get("agenda"), int):
            continue
        a = it["agenda"]
        if not (0 <= a < len(agenda)):
            continue
        g = {"title": agenda[a],
             "points": _clean_indices(it.get("points"), len(points), used_p),
             "decisions": _clean_indices(it.get("decisions"), len(decisions), used_d),
             "figures": _clean_indices(it.get("figures"), len(figures), used_f)}
        if g["points"] or g["decisions"] or g["figures"]:
            groups.append(g)

    if not groups:
        logger.info("grouping produced no usable item — one item, as before")
        return None
    placed = len(used_p) / len(points)
    if placed < MIN_PLACED:
        logger.info(f"grouping placed only {placed:.0%} of the points — one item, as before")
        return None

    # Whatever the model left out still belongs in the minutes: it goes to the last item, in its
    # original order, rather than being dropped or given an invented heading.
    tail = groups[-1]
    tail["points"] += [i for i in range(len(points)) if i not in used_p]
    tail["decisions"] += [i for i in range(len(decisions)) if i not in used_d]
    tail["figures"] += [i for i in range(len(figures)) if i not in used_f]
    logger.info(f"agenda grouping: {len(groups)} item(s), {placed:.0%} of points placed by the model, "
                f"{len(points) - len(used_p)} left to the last item")
    return groups
