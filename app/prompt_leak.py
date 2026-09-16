"""Drop the lines that are the user's own instruction, not something the meeting said.

WHY. `compose_source` labels the prompt "USER'S REQUEST FOR THESE MINUTES", and the writer still
sometimes minutes it. A real run with the prompt "Prepare the minutes of this meeting from the attached
transcript. Record each motion, who moved and seconded it..." printed, in the official minutes:

    7.  Decision.  Prepare the minutes of the meeting.

which no one at that meeting decided. It is the request itself, read as business.

THE TEST IS DELIBERATELY NARROW, because dropping a real decision is far worse than leaving a stray one.
All four must hold:

  1. the line opens with an instruction verb aimed at us — "prepare", "summarise", "list"…;
  2. every content word in it also occurs in the prompt;
  3. it is short (a genuine minute carries detail the request cannot);
  4. nobody owns it and nothing is due — a task a meeting really assigned has an owner or a date.

So "A motion to approve the minutes was made and seconded." stays (opens with a noun), and
"Prepare the annual training calendar by 30 Sep 26", assigned to SO (Trg), stays (owner and date),
even when the prompt happens to use those words.
"""
import re
from typing import Any, Dict, List

# Words carried by both a request and a minute; matching on them would prove nothing.
_STOP = {
    "a", "an", "the", "this", "that", "these", "those", "of", "for", "to", "in", "on", "at", "by",
    "from", "with", "and", "or", "but", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "their", "our", "your", "his", "her", "them", "they", "we", "you", "i",
    "all", "each", "every", "any", "some", "which", "who", "whom", "whose", "what", "how",
    "please", "kindly", "also", "then", "than", "into", "about", "over", "under", "up", "down",
}

# A line that starts this way is addressed to whoever is writing the minutes.
_INSTRUCTION = {
    "prepare", "write", "draft", "make", "create", "generate", "produce", "compile", "build",
    "summarise", "summarize", "list", "record", "note", "capture", "include", "cover", "focus",
    "give", "provide", "extract", "highlight", "mention", "add", "show", "keep", "format", "use",
}

MAX_WORDS = 15          # beyond this the line carries detail no request could have supplied
_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> List[str]:
    return _WORD.findall(str(text or "").lower())


def _content(words: List[str]) -> List[str]:
    return [w for w in words if w not in _STOP]


def _is_the_request(text: str, prompt_words: set) -> bool:
    words = _words(text)
    if not words or len(words) > MAX_WORDS:
        return False
    if words[0] not in _INSTRUCTION:
        return False
    body = _content(words)
    return len(body) >= 2 and all(w in prompt_words for w in body)


def strip(mom: Dict[str, Any], prompt: str) -> List[str]:
    """Remove the prompt's own sentences from the minutes, in place. Returns what was dropped."""
    if not prompt or not isinstance(mom, dict):
        return []
    prompt_words = set(_content(_words(prompt)))
    if not prompt_words:
        return []
    dropped: List[str] = []

    for key in ("key_points", "decisions"):
        kept = []
        for line in mom.get(key) or []:
            if isinstance(line, str) and _is_the_request(line, prompt_words):
                dropped.append(f"{key}: {line}")
            else:
                kept.append(line)
        if key in mom:
            mom[key] = kept

    kept_actions = []
    for ai in mom.get("action_items") or []:
        owned = isinstance(ai, dict) and (str(ai.get("assigned_to") or "").strip()
                                          or str(ai.get("due") or "").strip())
        if isinstance(ai, dict) and not owned and _is_the_request(ai.get("task"), prompt_words):
            dropped.append(f"action_items: {ai.get('task')}")
        else:
            kept_actions.append(ai)
    if "action_items" in mom:
        mom["action_items"] = kept_actions
    return dropped
