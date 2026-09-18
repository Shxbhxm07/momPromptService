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

NO REPLY GROWS WITH THE MEETING. A 3-hour council meeting (t4, 2026-09-18) produced 765 points, and one
call had to list every one of them: the reply hit its 1,200-token limit, came back cut off, and the
whole grouping was lost — 57 pages in a single ITEM. So points go to the model POINT_BATCH at a time,
decisions and figures in one small call of their own, and every call's reply budget is sized to what it
may list. A batch that fails costs only its own points, and those are placed next to their neighbours:
the writer's points come out in meeting order, so the item of the nearest earlier point is the best
guess — far better than piling them all into the last item.
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
# Points per call. A reply then lists at most this many indices, whatever the meeting's length.
POINT_BATCH = 80
# Reply budget: about 4 tokens per index the model may list, plus the JSON around them.
_TOKENS_PER_INDEX = 4
_REPLY_OVERHEAD = 300

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


def _numbered(values: List[str], start: int = 0, limit: int = SNIPPET) -> str:
    """One per line, numbered from `start` — the indices the model answers with."""
    lines = []
    for i, v in enumerate(values, start):
        lines.append(f"{i}. " + _WS.sub(" ", str(v)).strip()[:limit])
    return "\n".join(lines) or "(none)"


def _ask(agenda: List[str], points: List[str], decisions: List[str], figures: List[str],
         first_point: int = 0) -> Dict[str, Any]:
    from mom import _get_writer
    user = (f"AGENDA:\n{_numbered(agenda)}\n\nPOINTS:\n{_numbered(points, first_point)}\n\n"
            f"DECISIONS:\n{_numbered(decisions)}\n\nFIGURES:\n{_numbered(figures)}")
    budget = _REPLY_OVERHEAD + _TOKENS_PER_INDEX * (len(points) + len(decisions) + len(figures) + len(agenda))
    reply = _get_writer().generate(
        _SYSTEM, user, max_new_tokens=budget, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "agenda_grouping", "schema": _SCHEMA, "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


def _clean_indices(raw: Any, lo: int, hi: int, taken: set) -> List[int]:
    """Indices in [lo, hi), first-come only — a repeat anywhere in the reply is dropped, not duplicated."""
    out = []
    for i in raw if isinstance(raw, list) else []:
        if isinstance(i, bool) or not isinstance(i, int):
            continue
        if lo <= i < hi and i not in taken:
            taken.add(i)
            out.append(i)
    return out


def group(mom: Dict[str, Any], points: List[str], decisions: List[Any], figures: List[str]) -> Optional[List[Dict[str, Any]]]:
    """Agenda items as index groups: [{title, points[i], decisions[i], figures[i]}], or None.

    `points`, `decisions` and `figures` are the renderer's own flattened lists, so the indices point
    at exactly the strings it will print — no text crosses back from the model.
    """
    from concurrent.futures import ThreadPoolExecutor
    from llama.config import LLM_CONCURRENCY

    agenda = [re.sub(r"\s+", " ", str(a)).strip() for a in (mom.get("agenda") or []) if str(a).strip()]
    if len(agenda) < 2 or not points:
        return None                      # one agenda entry is one ITEM: today's behaviour already
    agenda = agenda[:MAX_ITEMS]
    said = [d[0] if isinstance(d, (list, tuple)) else str(d) for d in decisions]

    # (first point index, points, decisions, figures) per call: decisions and figures once, on their own.
    calls = [(0, [], said, list(figures))] if said or figures else []
    calls += [(s, points[s:s + POINT_BATCH], [], []) for s in range(0, len(points), POINT_BATCH)]

    def one(call):
        first, pts, dec, fig = call
        try:
            return call, _ask(agenda, pts, dec, fig, first)
        except Exception as e:
            logger.warning(f"grouping call failed ({type(e).__name__}: {e}) — "
                           f"{len(pts) or len(dec) + len(fig)} line(s) left for the fallback")
            return call, None

    with ThreadPoolExecutor(max_workers=max(1, LLM_CONCURRENCY)) as pool:
        replies = list(pool.map(one, calls))
    if all(data is None for _, data in replies):
        logger.info("grouping calls all failed — one item, as before")
        return None

    used_p, used_d, used_f = set(), set(), set()
    found: Dict[int, Dict[str, List[int]]] = {}
    for (first, pts, dec, fig), data in replies:
        for it in (data or {}).get("items") or []:
            if not isinstance(it, dict) or isinstance(it.get("agenda"), bool) or not isinstance(it.get("agenda"), int):
                continue
            a = it["agenda"]
            if not (0 <= a < len(agenda)):
                continue
            g = found.setdefault(a, {"points": [], "decisions": [], "figures": []})
            g["points"] += _clean_indices(it.get("points"), first, first + len(pts), used_p)
            if dec:
                g["decisions"] += _clean_indices(it.get("decisions"), 0, len(decisions), used_d)
            if fig:
                g["figures"] += _clean_indices(it.get("figures"), 0, len(figures), used_f)

    order = [a for a in range(len(agenda)) if a in found and any(found[a].values())]
    if not order:
        logger.info("grouping produced no usable item — one item, as before")
        return None
    placed = len(used_p) / len(points)
    if placed < MIN_PLACED:
        logger.info(f"grouping placed only {placed:.0%} of the points — one item, as before")
        return None

    # A point the model left out goes with its nearest earlier neighbour's item (points follow the
    # meeting), or the next one's when it comes before any placed point. Decisions and figures carry no
    # such order, so theirs go to the last item, as before.
    owner = {i: a for a in order for i in found[a]["points"]}
    placed_in_order = sorted(owner)
    for i in range(len(points)):
        if i in owner:
            continue
        before = [j for j in placed_in_order if j < i]
        after = [j for j in placed_in_order if j > i]
        a = owner[before[-1]] if before else owner[after[0]]
        found[a]["points"].append(i)
    tail = found[order[-1]]
    tail["decisions"] += [i for i in range(len(decisions)) if i not in used_d]
    tail["figures"] += [i for i in range(len(figures)) if i not in used_f]

    groups = [{"title": agenda[a], "points": sorted(found[a]["points"]),
               "decisions": found[a]["decisions"], "figures": found[a]["figures"]} for a in order]
    logger.info(f"agenda grouping: {len(groups)} item(s) in {len(calls)} call(s), {placed:.0%} of points "
                f"placed by the model, {len(points) - len(used_p)} placed beside their neighbours")
    return groups
