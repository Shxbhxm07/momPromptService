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

PLAIN LINES, NOT JSON (2026-09-22). On the cluster EVERY decision and figure landed under the last ITEM: they
went in one JSON call ("reply hit the 476-token limit and was cut off"), the cut reply could not be read, and
leftovers went to the last item. Now each list has its own call (points in batches, decisions, figures) and the
model answers one plain line per agenda item — "0: 3, 7, 12" — about half the tokens of the JSON, and a reply
cut short still gives every line before the cut (essence found the same, 2026-09-18). A decision or figure the
model leaves out goes to the item of the placed point it shares the most numbers and words with; only one that
matches nothing goes to the last item.
"""
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
# Reply budget: an index is ~2 tokens on a plain line ("12, "); twice that leaves room for a model that lists a
# line under two items (the repeat is dropped here), plus a line start per agenda item.
_TOKENS_PER_INDEX = 4
_TOKENS_PER_ITEM = 8
_REPLY_OVERHEAD = 100

_SYSTEM = """You are given a meeting's AGENDA and a numbered list of {what} already written from that meeting.
Say which agenda item each one belongs to.{shown}

Answer with ONE LINE PER AGENDA ITEM, in exactly this form and nothing else:
0: 3, 7, 12
1: 1, 2

The number before the colon is the agenda item's number from the AGENDA; after it come the numbers of the
{what} that belong to it.

Rules:
- Numbers only. Never write, reword, explain or invent any text.
- Put each number under at most ONE agenda item — the one it belongs to.
- Leave out a number that fits no agenda item; do not force it.
- Leave out an agenda item with nothing under it."""

_WS = re.compile(r"\s+")
# Decisions and figures are placed AFTER the points, with each item's points shown under it (up to SHOWN, cut
# to SHOWN_CHARS): the titles alone did not tell the model that "collect batteries from the depot" belonged to
# "Vehicles for Thursday's convoy" (a real run, 2026-09-22); with the points shown it did.
SHOWN, SHOWN_CHARS = 10, 110
_SHOWN = ("\nUnder each agenda item you see points already placed there: they show what that item covered in this "
          "meeting.")
# "0: 3, 7, 12" — also "Item 0 - 3, 7", "0) 3 7"; a range "3-7" is read as 3, 4, 5, 6, 7.
_LINE = re.compile(r"^\s*(?:agenda\s*item|agenda|item)?\s*#?\s*(\d+)\s*[:)\-–=]\s*(.*)$", re.I)
_RANGE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")


def _numbered(values: List[str], start: int = 0, limit: int = SNIPPET) -> str:
    """One per line, numbered from `start` — the indices the model answers with."""
    lines = []
    for i, v in enumerate(values, start):
        lines.append(f"{i}. " + _WS.sub(" ", str(v)).strip()[:limit])
    return "\n".join(lines) or "(none)"


def _parse(reply: str, n_agenda: int) -> Dict[int, List[int]]:
    """{agenda index: [line indices]} from the model's plain lines. Anything else in the reply is ignored."""
    out: Dict[int, List[int]] = {}
    for line in (reply or "").splitlines():
        m = _LINE.match(line.replace("*", ""))
        if not m or not 0 <= int(m.group(1)) < n_agenda:
            continue
        rest, nums = m.group(2), []
        for lo, hi in _RANGE.findall(rest):
            lo, hi = int(lo), int(hi)
            if lo <= hi <= lo + POINT_BATCH:
                nums += list(range(lo, hi + 1))
        nums += [int(x) for x in re.findall(r"\d+", _RANGE.sub(" ", rest))]
        out.setdefault(int(m.group(1)), []).extend(nums)
    return out


def _ask(agenda: List[str], what: str, values: List[str], first: int = 0,
         shown: Optional[Dict[int, List[str]]] = None) -> Dict[int, List[int]]:
    """One call: which agenda item each of `values` (numbered from `first`) belongs to. `shown`: points already
    placed under each item, listed beneath its title."""
    from mom import _get_writer
    if shown:
        lines = []
        for a, title in enumerate(agenda):
            lines.append(f"{a}. {_WS.sub(' ', title).strip()[:SNIPPET]}")
            lines += ["     - " + _WS.sub(" ", p).strip()[:SHOWN_CHARS] for p in shown.get(a, [])[:SHOWN]]
        head = "AGENDA (with points already placed under each item):\n" + "\n".join(lines)
    else:
        head = f"AGENDA:\n{_numbered(agenda)}"
    user = f"{head}\n\n{what}:\n{_numbered(values, first)}"
    budget = _REPLY_OVERHEAD + _TOKENS_PER_INDEX * len(values) + _TOKENS_PER_ITEM * len(agenda)
    reply = _get_writer().generate(_SYSTEM.format(what=what, shown=_SHOWN if shown else ""), user,
                                   max_new_tokens=budget, temperature=0.0)
    return _parse(reply, len(agenda))


# ── a decision or figure the model left out: the item of the point it shares the most with ─────────
_STOP = {"with", "that", "this", "from", "have", "been", "were", "will", "would", "should", "their", "there",
         "they", "them", "about", "which", "what", "when", "where", "into", "also", "than", "then", "each",
         "such", "only", "some", "more", "most", "other", "after", "before", "during", "being", "shall",
         "must", "could", "made", "make", "done", "said", "meeting", "discussed", "decided", "agreed", "noted"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]+", str(text).lower()) if len(w) >= 4 and w not in _STOP}


def _numbers(text: str) -> set:
    return set(re.findall(r"\d+(?:[.,:]\d+)*", str(text)))


def _nearest(text: str, bags: Dict[int, tuple]) -> Optional[int]:
    """The agenda item whose title and points, taken together (`bags`: item → (numbers, words)), share the most
    with `text`: a number counts 3 (a one-digit number 1), a word 1. None when the best is under 2 or shared
    with another item — nothing clearly in common is not a match."""
    nums, words = _numbers(text), _words(text)
    scores = {a: sum(3 if len(n) > 1 else 1 for n in nums & bn) + len(words & bw) for a, (bn, bw) in bags.items()}
    best = max(scores.values(), default=0)
    winners = [a for a, v in scores.items() if v == best]
    return winners[0] if best >= 2 and len(winners) == 1 else None


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

    # Two rounds. Points first, in batches of POINT_BATCH; then decisions and figures, each in calls of their own,
    # shown the points each item got — so no reply lists more than POINT_BATCH numbers.
    labels = {"points": "POINTS", "decisions": "DECISIONS", "figures": "FIGURES"}
    shown: Dict[int, List[str]] = {}

    def one(call):
        kind, first, lines = call
        try:
            return call, _ask(agenda, labels[kind], lines, first, shown if kind != "points" else None)
        except Exception as e:
            logger.warning(f"grouping call for {kind} failed ({type(e).__name__}: {e}) — "
                           f"{len(lines)} line(s) left for the fallback")
            return call, None

    used = {"points": set(), "decisions": set(), "figures": set()}
    used_p, used_d, used_f = used["points"], used["decisions"], used["figures"]
    found: Dict[int, Dict[str, List[int]]] = {}

    def take(replies):
        for (kind, first, lines), data in replies:
            for a, nums in (data or {}).items():
                g = found.setdefault(a, {"points": [], "decisions": [], "figures": []})
                g[kind] += _clean_indices(nums, first, first + len(lines), used[kind])

    calls = [("points", s, points[s:s + POINT_BATCH]) for s in range(0, len(points), POINT_BATCH)]
    with ThreadPoolExecutor(max_workers=max(1, LLM_CONCURRENCY)) as pool:
        replies = list(pool.map(one, calls))
    if all(data is None for _, data in replies):
        logger.info("grouping calls all failed — one item, as before")
        return None
    take(replies)

    order = [a for a in range(len(agenda)) if a in found and found[a]["points"]]
    if not order:
        logger.info("grouping produced no usable item — one item, as before")
        return None
    placed = len(used_p) / len(points)
    if placed < MIN_PLACED:
        logger.info(f"grouping placed only {placed:.0%} of the points — one item, as before")
        return None

    # A point the model left out goes with its nearest earlier neighbour's item (points follow the
    # meeting), or the next one's when it comes before any placed point.
    owner = {i: a for a in order for i in found[a]["points"]}
    placed_in_order = sorted(owner)
    for i in range(len(points)):
        if i in owner:
            continue
        before = [j for j in placed_in_order if j < i]
        after = [j for j in placed_in_order if j > i]
        a = owner[before[-1]] if before else owner[after[0]]
        found[a]["points"].append(i)

    # Round two: decisions and figures, with each item's points shown under its title.
    shown.update({a: [points[i] for i in sorted(found[a]["points"])] for a in order})
    more = [("decisions", s, said[s:s + POINT_BATCH]) for s in range(0, len(said), POINT_BATCH)]
    more += [("figures", s, list(figures[s:s + POINT_BATCH])) for s in range(0, len(figures), POINT_BATCH)]
    if more:
        with ThreadPoolExecutor(max_workers=max(1, LLM_CONCURRENCY)) as pool:
            take(list(pool.map(one, more)))
    calls += more
    order = [a for a in range(len(agenda)) if a in found and any(found[a].values())]

    # A decision or figure the model left out has no place in the meeting's order: it goes to the item whose
    # title and points share the most numbers and words with it — "42 families without water" beside "42
    # families in Block C have had no water" — and only one with no clear match to the last item.
    bags = {}
    for a in order:
        texts = [agenda[a]] + [points[i] for i in found[a]["points"]]
        bags[a] = (set().union(*map(_numbers, texts)), set().union(*map(_words, texts)))
    matched = {"decisions": 0, "figures": 0}
    for kind, texts, taken in (("decisions", said, used_d), ("figures", list(figures), used_f)):
        for i, text in enumerate(texts):
            if i in taken:
                continue
            a = _nearest(text, bags)
            if a is None or a not in found:
                a = order[-1]
            else:
                matched[kind] += 1
            found[a][kind].append(i)
    for a in order:
        found[a]["decisions"].sort()
        found[a]["figures"].sort()

    groups = [{"title": agenda[a], "points": sorted(found[a]["points"]),
               "decisions": found[a]["decisions"], "figures": found[a]["figures"]} for a in order]
    logger.info(f"agenda grouping: {len(groups)} item(s) in {len(calls)} call(s), {placed:.0%} of points "
                f"placed by the model, {len(points) - len(used_p)} placed beside their neighbours; "
                f"decisions {len(used_d)} by the model + {matched['decisions']} by matching + "
                f"{len(decisions) - len(used_d) - matched['decisions']} to the last item; figures "
                f"{len(used_f)} + {matched['figures']} + {len(figures) - len(used_f) - matched['figures']}")
    return groups
