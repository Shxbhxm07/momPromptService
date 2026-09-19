"""Tidy the finished minutes before they are printed: nothing said twice, and owners by their full name.
Code only — no model call, and no word the writer did not produce. NOTHING IS REMOVED UNLESS ANOTHER PRINTED
LINE STILL STATES IT: every number and name of the original minutes is still in the tidied ones.

Seen on a real 15-minute meeting (2026-09-19), which printed as eight pages:
  * 7 of its 24 "Decision." lines were repeats. The writer returns decisions and action items as two lists,
    each de-duplicated on its own but never against the other, and the renderer prints both as
    "Decision." lines — so "Graded PT will be mandatory for one week after leave" stood beside "Make one
    week of graded PT a standing order after leave. | Sameer".
  * Owners came as the model heard them: "Rohit", "Karan", "Sameer" beside "Maj Rohit Negi".
  * 44 figures were listed, most of them already in a point or decision ("BPET pass percentage — 86"
    under "BPET pass percentage of 86 is not good enough"), some no figure at all ("Companies to be
    lectured — all companies").

merge_repeats() and full_owner_names() run before the ITEM grouping, so its indices are built on the
final lists; trim_figures() runs after essence, once the points that will print are known.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("tidy")

# Words that say nothing about WHAT was decided, so two lines sharing only these are not the same line.
_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "of", "for", "to", "in", "on", "at", "by", "from",
    "with", "and", "or", "but", "as", "is", "are", "was", "were", "be", "been", "being", "will", "shall",
    "would", "should", "can", "may", "must", "it", "its", "their", "our", "his", "her", "them", "they",
    "we", "all", "each", "every", "any", "some", "which", "who", "also", "then", "than", "into", "about",
    "decided", "decision", "agreed", "meeting", "there", "has", "have", "had", "not", "no", "so", "do",
}
_RANKS = {"gen", "lt", "col", "maj", "capt", "brig", "sub", "nb", "hav", "nk", "sep", "sgt", "cdr", "cmdr",
          "mr", "mrs", "ms", "dr", "shri", "smt", "sahab", "sir", "ma'am", "maam"}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Two lines are the same decision when at least this share of the shorter one's content words is in the
# other, and at least MIN_SHARED words in all. Measured on the real repeats above: 0.60-1.00; the nearest
# pair that was NOT a repeat (the ORS line against the medical-cover action) shared 0.29.
SAME_LINE = 0.6
MIN_SHARED = 3
# NOTHING IS CUT THAT IS NOT ALSO PRINTED ELSEWHERE (the user's rule, 2026-09-19: "my accuracy levels should
# not be dropped"). A first version also capped figures at 5 per ITEM; on the real job that cut 18 figures no
# other line carried — "120 grenades", "3 heat injury cases" — so the cap is gone and will not come back.
# Label words too generic to show that two lines state the same fact.
_GENERIC = {"date", "number", "total", "count", "duration", "figure", "amount", "day", "week", "month", "year",
            "time", "value", "quantity", "detail"}


def _stem(w: str) -> str:
    """Crude, on purpose: enough that "serviced", "service" and "services" meet."""
    for end in ("ing", "ed", "es", "s"):
        if len(w) > len(end) + 3 and w.endswith(end):
            w = w[: -len(end)]
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def _numbers(text: str) -> Set[str]:
    return {n.replace(",", "") for n in _NUM.findall(text or "")}


def _words(text: str, drop: Set[str] = frozenset()) -> Set[str]:
    """Content words, stemmed; numbers included, since "5 October" and "12 October" are not the same."""
    ws = re.findall(r"[a-z]+|\d[\d,.]*", (text or "").lower())
    return {_stem(w.replace(",", "")) for w in ws if w not in _STOP and w not in drop and len(w) > 1}


def _names(text: str) -> Set[str]:
    """Capitalised words that are not the first word of the line — people, places, courses."""
    return set(re.findall(r"(?<=\s)[A-Z][A-Za-z]+", " " + re.sub(r"^\W*\w+", "", text or "")))


def _covers(kept: str, dropped: str, ignore: Set[str] = frozenset()) -> bool:
    """Does `kept` still say everything `dropped` says? Every number and name, and every content word but
    one — the one being a verb form the stemmer cannot join ("sent" / "send", "failed" / "failures").
    "Graded PT will be mandatory … before any test" is NOT covered by "Make one week of graded PT a
    standing order after leave": "before any test" would be lost, so both lines stay."""
    return (_numbers(dropped) <= _numbers(kept)
            and {n.lower() for n in _names(dropped)} <= {w.lower() for w in re.findall(r"[A-Za-z]+", kept)}
            and len(_words(dropped, ignore) - _words(kept, ignore)) <= 1)


def _name_parts(mom: Dict[str, Any]) -> Set[str]:
    """Every word of every attendee's name — an action often opens "Rohit to …", which says who, not what."""
    parts = set()
    for a in mom.get("attendees") or []:
        if isinstance(a, dict):
            parts |= {w for w in re.findall(r"[a-z]+", str(a.get("name") or "").lower())}
    return parts | _RANKS


def merge_repeats(mom: Dict[str, Any]) -> List[str]:
    """Drop a decision that an action item already records, keeping the owner and the fuller wording.

    The action keeps its owner and due date; its text becomes whichever of the two says more — and only if
    that text still carries every number and name of the other; otherwise both stay. Numbers must
    not disagree: two lines that both carry numbers and share none are different decisions however alike
    their words ("retest on 5 October" vs "retest on 12 October"). Returns the decisions merged away.
    """
    decisions = [d for d in (mom.get("decisions") or []) if isinstance(d, str) and d.strip()]
    actions = [a for a in (mom.get("action_items") or []) if isinstance(a, dict) and str(a.get("task") or "").strip()]
    if not decisions or not actions:
        return []
    names = _name_parts(mom)
    a_words = [_words(a["task"], names) for a in actions]
    kept, merged = [], []
    for d in decisions:
        dw = _words(d, names)
        best, best_score = None, 0.0
        for j, aw in enumerate(a_words):
            shared = dw & aw
            if len(shared) < MIN_SHARED or not dw or not aw:
                continue
            dn, an = _numbers(d), _numbers(actions[j]["task"])
            if dn and an and not dn & an:
                continue
            score = len(shared) / min(len(dw), len(aw))
            if score >= SAME_LINE and score > best_score:
                best, best_score = j, score
        if best is None:
            kept.append(d)
            continue
        a = actions[best]
        fuller, other = (d, a["task"]) if len(dw) > len(a_words[best]) else (a["task"], d)
        if not _covers(fuller, other, names):     # each carries something the other lacks: keep both
            kept.append(d)
            continue
        if fuller is d:                           # the decision says more: its words, the action's owner
            a["task"] = d
            a_words[best] = dw
        merged.append(d)
    if merged:
        mom["decisions"] = kept
        logger.info(f"merged {len(merged)} decision(s) into the action item that already records them")
    return merged


def full_owner_names(mom: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Write each owner as the attendee list has them — "Rohit" becomes "Maj Rohit Negi".

    Only when exactly ONE attendee matches every word the model gave; "Sub Sahab" or a name nobody at the
    meeting has is left as written. A task that opens with its owner's name ("Karan to speak to the repair
    agency") loses it, since the Action column now carries the owner. Returns (was, now) pairs.
    """
    people = [str(a.get("name") or "").strip() for a in (mom.get("attendees") or []) if isinstance(a, dict)]
    people = [p for p in people if p]
    if not people:
        return []
    word_sets = [set(re.findall(r"[a-z]+", p.lower())) - _RANKS for p in people]
    changed = []

    def resolve(part: str) -> str:
        part = part.strip()
        if not part or part in people:
            return part
        ws = set(re.findall(r"[a-z]+", part.lower())) - _RANKS
        hits = [p for p, s in zip(people, word_sets) if ws and ws <= s]
        return hits[0] if len(hits) == 1 else part

    for a in mom.get("action_items") or []:
        if not isinstance(a, dict):
            continue
        owner = str(a.get("assigned_to") or "")
        parts = re.split(r"\s*(?:,|&|/|\band\b)\s*", owner)
        new = ", ".join(p for p in (resolve(x) for x in parts) if p)
        if owner.strip() and new != owner.strip():
            changed.append((owner, new))
            a["assigned_to"] = new
        task = str(a.get("task") or "")
        m = re.match(r"\s*([A-Z][a-z]+(?: [A-Z][a-z]+){0,3}) to (\w)", task)
        if m and new and resolve(m.group(1)) in new.split(", ") and resolve(m.group(1)) != m.group(1):
            a["task"] = m.group(2).upper() + task[m.end(2):]
    if changed:
        logger.info(f"owners written in full: {', '.join(f'{w} -> {n}' for w, n in changed[:6])}")
    return changed


def trim_figures(mom: Dict[str, Any], points: List[str], decisions: List[Any], figures: List[str],
                 groups: Optional[List[Dict[str, Any]]]) -> Optional[Tuple[int, int]]:
    """Drop the figures a printed point or decision already states, and renumber mom["item_groups"].

    `points`, `decisions` and `figures` are the renderer's flattened lists AFTER essence, and `groups` the
    ITEM grouping over them (or None). A figure goes ONLY when ONE printed point or decision holds ALL of
    it — every number and every specific word of its label ("BPET pass percentage — 86" under "BPET pass
    percentage of 86 is not good enough"). One shared word is not enough: "Young Officers course vacancies
    — 2" was once dropped for "Section Commanders course to start on 2 November", a different fact. A
    figure with no number is dropped only when one line holds every one of its words. When in doubt, it
    stays: a figure said twice costs a line; a fact lost costs the minutes.
    Returns (figures before, after), or None when nothing changed.
    """
    if not figures:
        return None
    said = [str(d[0] if isinstance(d, (list, tuple)) else d) for d in decisions] + list(points)
    said_nums = [_numbers(s) for s in said]
    said_words = [_words(s) for s in said]

    def said_elsewhere(f: str) -> bool:
        low = f.lower()
        nums = _numbers(f)
        if nums:
            label = _words(re.split(r"\s[—–-]\s|:", f, 1)[0]) - {_stem(n) for n in nums} - _GENERIC
            return bool(label) and any(nums <= sn and label <= sw for sn, sw in zip(said_nums, said_words))
        if any(m in low for m in _MONTHS) or any(d in low for d in _DAYS):
            return False                          # a date in words: kept
        fw = _words(f) - _GENERIC
        return bool(fw) and any(fw <= sw for sw in said_words)

    items = groups if groups else [{"figures": list(range(len(figures)))}]
    keep_by_item = [[i for i in (it.get("figures") or []) if not said_elsewhere(figures[i])] for it in items]
    kept_all = sorted({i for ks in keep_by_item for i in ks})
    if len(kept_all) == len(figures):
        return None
    new_index = {old: new for new, old in enumerate(kept_all)}
    mom["key_figures"] = [figures[i] for i in kept_all]
    if groups:
        for g, ks in zip(groups, keep_by_item):
            g["figures"] = [new_index[i] for i in ks]
        mom["item_groups"] = groups
    logger.info(f"figures: kept {len(kept_all)} of {len(figures)} — the rest are already stated in a printed "
                f"point or decision")
    return len(figures), len(kept_all)
