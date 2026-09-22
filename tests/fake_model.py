"""A stand-in for the model for timing and equivalence tests: the SAME question always gets the SAME answer, in the
shape each step expects, after a delay that grows with the question (a 227k-character read takes longest) plus a
fixed per-question jitter, so parallel calls finish out of order the way real ones do. No network, no key."""
import json
import re
import threading
import time
import zlib

from llama import prompts as P

CALLS = []
_lock = threading.Lock()
IN_FLIGHT = [0, 0]           # now, most seen
# Like the real service's gate: at most this many answers being worked on at once (FAKE_GATE, default: no gate).
import os
GATE = threading.BoundedSemaphore(int(os.getenv("FAKE_GATE", "1000")))


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 40 <= len(s.strip()) <= 300]


def _body(user):
    """The transcript part of a request: after the last line of box-drawing or a TRANSCRIPT…: heading."""
    for mark in ("─" * 20, "TRANSCRIPT"):
        if mark in user:
            user = user.split(mark, 1)[1]
    return user


def _is(system, prompt):
    return system.startswith(prompt[:300])


def answer(system, user):
    s = _sentences(_body(user))
    if _is(system, P.MEETING_ANALYSIS_PROMPT_JSON) or _is(system, P.SYNTHESIS_PROMPT_JSON):
        if _is(system, P.SYNTHESIS_PROMPT_JSON):           # merge: every partial's lists, in order
            merged = {"title": "", "attendees": [], "agenda": [], "summary": "", "key_points": [], "decisions": [],
                      "action_items": [], "key_figures": []}
            from llama.localization.mom_i18n import _extract_json, _repair_json
            for part in user.split("=== PARTIAL JSON ==="):
                if "{" not in part:
                    continue
                d = _extract_json(part[part.index("{"):]) or _repair_json(part[part.index("{"):]) or {}
                d = d if isinstance(d, dict) else {}
                merged["title"] = merged["title"] or d.get("title", "")
                merged["summary"] = merged["summary"] or d.get("summary", "")
                for k in ("attendees", "agenda", "key_points", "decisions", "action_items"):
                    merged[k] += d.get(k) or []
            return json.dumps(merged)
        return json.dumps({
            "title": "Weekly review", "summary": " ".join(s[:2])[:300],
            "attendees": [{"name": n, "role": ""} for n in sorted(set(re.findall(r"\b(?:Col|Maj|Capt|Mr|Ms) [A-Z][a-z]+", user)))[:4]],
            "agenda": [x[:50] for x in s[:3]], "key_points": s[:4],
            "decisions": [x for x in s if "agree" in x.lower() or "decid" in x.lower()][:3],
            "action_items": [{"task": x, "assigned_to": "", "due": ""} for x in s if " will " in x][:3],
            "key_figures": []})
    if _is(system, P.WINDOW_EXTRACTION_PROMPT):
        pts = [{"text": x, "quote": x[:70], "type": "decision" if "agree" in x.lower() else "key_point",
                "owner": "", "due": ""} for x in s[:4]]
        return json.dumps({"points": pts})
    for prompt, pick in ((P.DECISIONS_EXTRACTION_PROMPT, "agree"), (P.ACTION_ITEMS_EXTRACTION_PROMPT, " will "),
                         (P.FIGURES_EXTRACTION_PROMPT, "0"), (P.KEY_POINTS_EXTRACTION_PROMPT, "the")):
        if _is(system, prompt):
            found = [x for x in s if pick in x.lower()][:6]
            return "\n".join(f"- {x}" for x in found) or "None explicitly stated."
    if _is(system, P.SUMMARY_FROM_POINTS_PROMPT):
        return "The meeting covered " + " ".join(s[:1])[:200]
    return "{}"          # merges and anything else: no change


def generate(system, user, *args, **kw):
    with _lock:
        IN_FLIGHT[0] += 1
        IN_FLIGHT[1] = max(IN_FLIGHT[1], IN_FLIGHT[0])
        CALLS.append(system[:40])
    try:
        with GATE:
            time.sleep(0.03 + len(user) / 300000 + (zlib.crc32(user.encode()) % 40) / 1000)
        return answer(system, user)
    finally:
        with _lock:
            IN_FLIGHT[0] -= 1
