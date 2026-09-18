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

NO REPLY GROWS WITH THE MEETING. The first version asked for the whole meeting in one call; on a
3-hour meeting (t4, 765 points in one ITEM) the model listed far more than the cap, the reply hit its
800-token limit, and nothing was shortened — 57 pages. Now each long ITEM gets its own call, at most
BATCH points per call with a proportional share of the cap, and the model returns its picks RANKED,
most important first: code keeps the top of the list, so even an over-long answer is usable.

A PLAIN LINE, NOT JSON. Asked for {"keep": [...]} under a strict JSON schema, the model host returned
just "{"; under plain JSON mode, only whitespace until the token limit — the trap the writer's window
pass documents. Asked for one line of numbers ("107, 110, 117, 130") it answered in about a second,
with exactly the cap, every time (t4, 2026-09-18). Numbers are read from the line; the cap, the range
check and the rest are still enforced here.

The dropped points are removed from `key_points` for real and the ITEM groups renumbered, so the stored
state, the search record and a later edit all see the same minutes the Word file shows.
"""
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
# Points per call; a longer ITEM is split and each part gets its share of the cap.
BATCH = 80
# Reply budget: enough for one line listing every point shown (~4 tokens a number), should the model
# ignore the cap. Only the top of the list is used, and the cap's worth always fits.
_TOKENS_PER_INDEX = 4
_REPLY_OVERHEAD = 60
_NUMBER = re.compile(r"\d+")

_SYSTEM = """You are given one ITEM from the minutes of a meeting that were already written and checked: its
numbered POINTS of discussion, the DECISIONS it reached, and the FIGURES printed under it.

Official minutes record only the essence of the discussion that led to each decision, not everything that
was said. List the points worth keeping, MOST IMPORTANT FIRST, at most {cap}.

Answer with ONE LINE: the numbers of the points to keep, most important first, separated by commas —
for example: 112, 105, 130. Nothing else: never write, reword or invent any text.

Prefer points that:
- explain why a decision was taken, or what it depends on;
- carry a specific fact: a figure, amount, date, name, quantity or result;
- record a problem raised, a disagreement, or a reservation.

Leave out points that:
- repeat another point or a decision;
- only restate a number already listed under FIGURES — the figures are printed anyway;
- only say that something was discussed, mentioned, presented or reviewed, without saying what;
- are greetings, introductions, thanks, procedure or small talk.

Only use point numbers from the list. Keep at least one."""

_WS = re.compile(r"\s+")


def _short(text: Any) -> str:
    return _WS.sub(" ", str(text)).strip()[:SNIPPET]


def _prompt(item: Dict[str, Any], batch: List[int], points: List[str], decisions: List[Any],
            figures: List[str]) -> str:
    lines = [f"ITEM – {_short(item['title'])}", "POINTS:"]
    lines += [f"{i}. {_short(points[i])}" for i in batch]
    said = [decisions[j] for j in item.get("decisions") or [] if 0 <= j < len(decisions)]
    if said:
        lines.append("DECISIONS:")
        lines += [f"- {_short(d[0] if isinstance(d, (list, tuple)) else d)}" for d in said]
    quoted = [figures[j] for j in item.get("figures") or [] if 0 <= j < len(figures)]
    if quoted:
        lines.append("FIGURES:")
        lines += [f"- {_short(f)}" for f in quoted]
    return "\n".join(lines)


def _ask(item: Dict[str, Any], batch: List[int], points: List[str], decisions: List[Any],
         figures: List[str], cap: int) -> List[int]:
    """The model's picks for one batch of one ITEM, ranked, as it sent them (checked by the caller)."""
    from mom import _get_writer
    reply = _get_writer().generate(
        _SYSTEM.format(cap=cap), _prompt(item, batch, points, decisions, figures),
        max_new_tokens=_REPLY_OVERHEAD + _TOKENS_PER_INDEX * len(batch), temperature=0.0)
    return [int(x) for x in _NUMBER.findall(reply or "")]


def _shares(sizes: List[int], limit: int) -> List[int]:
    """The cap split across one ITEM's batches in proportion to their size, adding up to exactly
    `limit` (largest remainder). A small last batch can get 0 and is then not asked at all — rounding
    each batch up to 1 had let a 170-point ITEM keep 5 against a cap of 4."""
    total = sum(sizes)
    exact = [limit * n / total for n in sizes]
    shares = [int(x) for x in exact]
    for i in sorted(range(len(sizes)), key=lambda i: exact[i] - shares[i], reverse=True)[:limit - sum(shares)]:
        shares[i] += 1
    return shares


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

    from concurrent.futures import ThreadPoolExecutor
    from llama.config import LLM_CONCURRENCY

    # One call per BATCH points of each long ITEM, each with its share of the cap.
    calls = []
    for k in long:
        own = items[k]["points"]
        batches = [own[s:s + BATCH] for s in range(0, len(own), BATCH)]
        calls += [(k, b, n) for b, n in zip(batches, _shares([len(b) for b in batches], limit)) if n]

    def one(call):
        k, batch, share = call
        try:
            return call, _ask(items[k], batch, points, decisions, figures, share)
        except Exception as e:
            logger.warning(f"essence call for item {k} failed ({type(e).__name__}: {e}) — "
                           f"its {len(batch)} point(s) kept")
            return call, None

    with ThreadPoolExecutor(max_workers=max(1, LLM_CONCURRENCY)) as pool:
        replies = list(pool.map(one, calls))

    chosen: Dict[int, List[int]] = {}
    answered = set()
    for (k, batch, share), picks in replies:
        allowed, keep = set(batch), []
        for i in picks or []:
            if isinstance(i, int) and not isinstance(i, bool) and i in allowed and i not in keep:
                keep.append(i)
        if keep:
            answered.add(k)
            chosen.setdefault(k, []).extend(keep[:share])     # the model's top picks, as it ranked them
        else:
            chosen.setdefault(k, []).extend(batch)            # nothing usable for this part: kept whole
    for k in chosen:
        own = items[k]["points"]
        chosen[k] = sorted(chosen[k], key=own.index)          # printed in the ITEM's own order

    if len(answered) < MIN_ANSWERED * len(long):
        logger.info(f"essence answered {len(answered)} of {len(long)} long item(s) — "
                    "minutes kept at full length")
        return None

    new_points: List[str] = []
    kept_by_item: List[List[int]] = []
    for k, it in enumerate(items):
        kept = chosen.get(k, it["points"])
        if k in long and k not in answered:
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
                f"({len(items)} item(s), at most {limit} each, {len(calls)} call(s))")
    return before, len(new_points)
