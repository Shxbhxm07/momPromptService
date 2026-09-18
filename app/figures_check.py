"""Keep the writer's own example numbers, and rupees nobody mentioned, out of the minutes.

WHY. A real cluster job on a New Zealand council meeting (t4, 2026-09-18) printed these figures:

    $51,840 (Rs 51,840).        $32,000.        7,30,340 (Rs 7,30,340).

None of the three numbers is anywhere in the transcript. They are the EXAMPLES in the writer's own figures
prompt ("Rs 51,840", "$32,000", "7,30,340"), copied into the answer. The "(Rs ...)" brackets and the lakh
grouping came from a rule that told the writer money is always in Indian notation. The prompts are
fixed — the currency rule now says keep the source's currency, and the figures prompt says its examples
are format only — but a prompt rule is a request, so this is the check:

  1. A line carrying a number that appears in the writer's PROMPTS but NOT in the source is dropped. The
     numbers are read from the prompts themselves, so a future example is covered too. Years and round
     powers of ten are left out (a real meeting says "2026" and "a thousand" all the time), and numbers
     spoken in words count as present: "thirty-two thousand" in the source keeps a "$32,000" line.
  2. When the source never mentions rupees, lakh or crore, a "(Rs ...)" bracket is removed and lakh-style
     grouping becomes international: 7,30,340 → 730,340 — the same number, written as the source writes.

Neither can touch a real figure: rule 1 only fires on a number the source does not contain, and rule 2
only rewrites notation, never a value.
"""
import re
from typing import Any, Dict, List, Set, Tuple

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_RUPEE = re.compile(r"₹|\b(rupees?|rs\.?|inr|lakhs?|lacs?|crores?)(?=\W|$)", re.I)
_RUPEE_BRACKET = re.compile(r"\s*\(\s*(?:Rs\.?|₹|INR)\s*[\d,]+(?:\.\d+)?(?:\s*(?:lakhs?|crores?))?\s*\)", re.I)
_LAKH_GROUPING = re.compile(r"\b\d{1,2}(?:,\d{2})+,\d{3}\b")

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_SCALES = {"thousand": 10**3, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "million": 10**6,
           "crore": 10**7, "crores": 10**7, "billion": 10**9}


def _digits(text: str) -> Set[str]:
    """Every number written in digits, commas removed: '$51,840' and '51840' are the same."""
    return {m.replace(",", "").rstrip(".") for m in _NUMBER.findall(text or "")}


def _spoken(text: str) -> Set[str]:
    """Numbers spoken in words, or digits with a scale word: 'sixty-two thousand', '1.2 million'."""
    found: Set[str] = set()
    total, current, in_number = 0.0, 0.0, False

    def flush():
        nonlocal total, current, in_number
        if in_number:
            value = total + current
            found.add(str(int(value)) if value == int(value) else str(value))
        total, current, in_number = 0.0, 0.0, False

    prev = ""
    for tok in re.findall(r"[a-z]+|\d+(?:\.\d+)?", (text or "").lower().replace(",", "").replace("-", " ")):
        if prev == "a" and not in_number and (tok == "hundred" or tok in _SCALES):
            current, in_number = 1.0, True                       # "a hundred", "a million"
        prev = tok
        if tok in _UNITS or tok in _TENS:
            current += _UNITS.get(tok, 0) + _TENS.get(tok, 0)
            in_number = True
        elif tok[0].isdigit():
            flush()
            current, in_number = float(tok), True
        elif tok == "hundred" and in_number:
            current = (current or 1) * 100
        elif tok in _SCALES and in_number:
            total += (current or 1) * _SCALES[tok]
            current = 0.0
        elif tok == "a" and not in_number:
            continue
        elif tok == "and" and in_number:
            continue
        else:
            flush()
    flush()
    return found


def _prompt_examples() -> Set[str]:
    """The distinctive numbers in the writer's prompts: 4+ digits, not a year, not a round power of ten."""
    import llama.prompts as prompts
    text = "\n".join(v for v in vars(prompts).values() if isinstance(v, str))
    out = set()
    for n in _digits(text):
        whole = n.split(".")[0]
        if len(whole) < 4 or (len(whole) == 4 and 1900 <= int(whole) <= 2100) or re.fullmatch(r"10*", whole):
            continue
        out.add(n)
    return out


def check(mom: Dict[str, Any], source: str) -> Tuple[List[str], int]:
    """Drop lines carrying a prompt example the source never states, and undo rupee notation the source
    never used, in place. Returns (lines dropped, lines rewritten)."""
    present = _digits(source) | _spoken(source)
    leaked = _prompt_examples() - present
    no_rupees = not _RUPEE.search(source or "")

    def leaks(line: str) -> bool:
        return bool(_digits(line) & leaked)

    def renotate(line: str) -> str:
        if not no_rupees:
            return line
        line = _RUPEE_BRACKET.sub("", line)
        return _LAKH_GROUPING.sub(lambda m: f"{int(m.group(0).replace(',', '')):,}", line)

    dropped: List[str] = []
    rewritten = 0
    for key in ("key_points", "decisions", "key_figures"):
        kept = []
        for line in mom.get(key) or []:
            line = str(line)
            if leaks(line):
                dropped.append(line)
                continue
            new = renotate(line)
            rewritten += new != line
            kept.append(new)
        mom[key] = kept
    actions = []
    for ai in mom.get("action_items") or []:
        if not isinstance(ai, dict):
            actions.append(ai)
            continue
        task = str(ai.get("task") or "")
        if leaks(task):
            dropped.append(task)
            continue
        new = renotate(task)
        if new != task:
            ai = dict(ai, task=new)
            rewritten += 1
        actions.append(ai)
    mom["action_items"] = actions
    if isinstance(mom.get("summary"), str):
        new = renotate(mom["summary"])
        rewritten += new != mom["summary"]
        mom["summary"] = new
    return dropped, rewritten
