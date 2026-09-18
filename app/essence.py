"""Keep only the essence of the discussion: a few points per ITEM, chosen by index.

WHY. The minutes came out far too long — 12 pages and 134 numbered paragraphs for a 15-minute meeting,
78 key points of which the writer itself flagged 55 as carrying no specific detail. The manual is
explicit (Ch 6 para 10): "A good minute should be brief ... A minute is not a verbatim record and should
not attempt to reproduce, however summarily, what every speaker said. Only the essence leading to the
conclusion should be recorded." Appendix AD draws one or two discussion paragraphs per ITEM before its
Decision, and para 16.14 asks for "the decision together with a brief of the discussion".

WHAT THIS DOES NOT DO. It never writes a sentence. The model is shown each ITEM's numbered points, the
decisions that ITEM reached and the figures printed under it, and answers with INDEX NUMBERS only — which
points to keep. Every line left
in the minutes is still text that passed the writer's own grounding checks. Decisions, actions and
figures are never touched here: only discussion points are dropped.

WHY NOT THE ALTERNATIVES. Turning off the writer's window pass (MOM_WINDOW_KEY_POINTS) would shorten the
minutes too, but that pass IS the quote check that keeps invented content out. Dropping every point the
writer marks "without a specific detail" is blunt: that flag is about wording, not importance. Asking the
writer for fewer points changes extraction, which is where the grounding lives.

FAILURE IS ALWAYS SAFE. A refused call, unusable JSON, or answers for fewer than half the ITEMs that
needed shortening leave the minutes exactly as the writer produced them. An ITEM the model skips, or
answers with nothing valid, keeps all its points. Indices from another ITEM, repeats, and anything past
the cap are dropped in code, not trusted.

The dropped points are removed from `key_points` for real and the ITEM groups renumbered, so the stored
state, the search record and a later edit all see the same minutes the Word file shows.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from config import ESSENCE_POINTS_PER_ITEM

logger = logging.getLogger("essence")

# With no agenda grouping everything sits in one ITEM, so it may keep a few ITEMs' worth.
SINGLE_ITEM_FACTOR = 3
# Below this share of ITEMs answered, the reply is not trusted at all.
MIN_ANSWERED = 0.5
# Points are shown shortened: enough to judge whether a point carries a fact, not the whole sentence.
SNIPPET = 220
MAX_REPLY_TOKENS = 800

_SYSTEM = """You are given the minutes of a meeting that were already written and checked: for each ITEM, its
numbered POINTS of discussion, the DECISIONS that item reached, and the FIGURES printed under it.

Official minutes record only the essence of the discussion that led to each decision, not everything that
was said. For each ITEM, choose AT MOST {cap} points to keep.

Answer with INDEX NUMBERS ONLY. Never write, reword, translate or invent any text.

Prefer points that:
- explain why a decision was taken, or what it depends on;
- carry a specific fact: a figure, amount, date, name, quantity or result;
- record a problem raised, a disagreement, or a reservation.

Leave out points that:
- repeat another point or a decision;
- only restate a number already listed under FIGURES — the figures are printed anyway;
- only say that something was discussed, mentioned, presented or reviewed, without saying what;
- are greetings, introductions, thanks, procedure or small talk.

Only choose points listed under that same ITEM. Keep at least one point for every ITEM."""

_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["item", "keep"],
        "properties": {"item": {"type": "integer"},
                       "keep": {"type": "array", "items": {"type": "integer"}}}}}},
}

_WS = re.compile(r"\s+")


def _short(text: Any) -> str:
    return _WS.sub(" ", str(text)).strip()[:SNIPPET]


def _prompt(items: List[Dict[str, Any]], points: List[str], decisions: List[Any], figures: List[str]) -> str:
    blocks = []
    for k, it in enumerate(items):
        lines = [f"ITEM {k} – {_short(it['title'])}", "POINTS:"]
        lines += [f"{i}. {_short(points[i])}" for i in it["points"]]
        said = [decisions[j] for j in it.get("decisions") or [] if 0 <= j < len(decisions)]
        if said:
            lines.append("DECISIONS:")
            lines += [f"- {_short(d[0] if isinstance(d, (list, tuple)) else d)}" for d in said]
        quoted = [figures[j] for j in it.get("figures") or [] if 0 <= j < len(figures)]
        if quoted:
            lines.append("FIGURES:")
            lines += [f"- {_short(f)}" for f in quoted]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _ask(items: List[Dict[str, Any]], points: List[str], decisions: List[Any], figures: List[str],
         cap: int) -> Dict[str, Any]:
    from mom import _get_writer
    reply = _get_writer().generate(
        _SYSTEM.format(cap=cap), _prompt(items, points, decisions, figures),
        max_new_tokens=MAX_REPLY_TOKENS, temperature=0.0,
        extra={"response_format": {"type": "json_schema",
                                   "json_schema": {"name": "essence", "schema": _SCHEMA, "strict": True}}})
    try:
        data = json.loads(reply)
    except (TypeError, ValueError):
        from llama.localization.mom_i18n import _extract_json, _repair_json
        data = _extract_json(_repair_json(reply or "")) if reply else None
    return data if isinstance(data, dict) else {}


def select(mom: Dict[str, Any], points: List[str], decisions: List[Any], figures: List[str],
           groups: Optional[List[Dict[str, Any]]]) -> Optional[Tuple[int, int]]:
    """Shorten mom["key_points"] to the essence and renumber mom["item_groups"] to match.

    `points`, `decisions` and `figures` are the renderer's flattened lists (docx_export.flatten_items)
    and `groups` the agenda grouping over them, or None for a single ITEM. Returns (points before,
    points after), or None when nothing was changed.
    """
    cap = ESSENCE_POINTS_PER_ITEM
    if cap <= 0 or not points:
        return None
    if groups:
        items, limit = groups, cap
    else:
        items = [{"title": mom.get("title") or "Discussion",
                  "points": list(range(len(points))), "decisions": list(range(len(decisions))),
                  "figures": list(range(len(figures)))}]
        limit = cap * SINGLE_ITEM_FACTOR
    long = [k for k, it in enumerate(items) if len(it["points"]) > limit]
    if not long:
        return None

    try:
        data = _ask(items, points, decisions, figures, limit)
    except Exception as e:
        logger.warning(f"essence call failed ({type(e).__name__}: {e}) — minutes kept at full length")
        return None

    chosen: Dict[int, List[int]] = {}
    for entry in data.get("items") or []:
        if not isinstance(entry, dict) or isinstance(entry.get("item"), bool) or not isinstance(entry.get("item"), int):
            continue
        k = entry["item"]
        if k not in long or k in chosen:
            continue
        own, keep = items[k]["points"], []
        for i in entry.get("keep") if isinstance(entry.get("keep"), list) else []:
            if isinstance(i, int) and not isinstance(i, bool) and i in own and i not in keep:
                keep.append(i)
        if keep:
            chosen[k] = sorted(keep, key=own.index)[:limit]      # printed in the ITEM's own order

    if len(chosen) < MIN_ANSWERED * len(long):
        logger.info(f"essence reply answered {len(chosen)} of {len(long)} long item(s) — "
                    "minutes kept at full length")
        return None

    new_points: List[str] = []
    kept_by_item: List[List[int]] = []
    for k, it in enumerate(items):
        kept = chosen.get(k, it["points"])
        if k in long and k not in chosen:
            logger.info(f"essence: no usable answer for item {k} — its {len(kept)} points kept")
        kept_by_item.append(list(range(len(new_points), len(new_points) + len(kept))))
        new_points += [points[i] for i in kept]

    before = len(points)
    mom["key_points"] = new_points
    if groups:
        for g, kept in zip(groups, kept_by_item):
            g["points"] = kept
        mom["item_groups"] = groups
    logger.info(f"essence: kept {len(new_points)} of {before} points "
                f"({len(items)} item(s), at most {limit} each)")
    return before, len(new_points)
