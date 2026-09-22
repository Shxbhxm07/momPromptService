"""No size limits (the user's rule, 2026-09-22: "I do not want any limit"). A 290-page document was refused at
300,000 characters; scanned pages past 200 were skipped; decisions, actions and figures read the whole text in
one call, which a document longer than the model can read would fail; a very large MoM could not be re-prompted."""
import copy
import os

import agenda_items as ag
import config
import document_checker
import minutes_edit as me
import mom as mom_module
from llama.core import llm_manager as lm

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok  ", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


print("the defaults are 'no limit'")
check("MAX_SOURCE_CHARS 0", config.MAX_SOURCE_CHARS == 0, config.MAX_SOURCE_CHARS)
check("MAX_DOC_MB 0", config.MAX_DOC_MB == 0, config.MAX_DOC_MB)
check("OCR_MAX_PAGES 0", config.OCR_MAX_PAGES == 0, config.OCR_MAX_PAGES)
try:
    document_checker.check(b"%PDF-1.7\n" + b"0" * (60 * 1048576), "big.pdf")
    check("a 60 MB file is accepted", True)
except Exception as e:
    check("a 60 MB file is accepted", False, e)
document_checker.MAX_DOC_MB = 50
try:
    document_checker.check(b"%PDF-1.7\n" + b"0" * (60 * 1048576), "big.pdf")
    check("MAX_DOC_MB=50 still brings the limit back", False)
except document_checker.UnreadableDocument:
    check("MAX_DOC_MB=50 still brings the limit back", True)
document_checker.MAX_DOC_MB = 0

print("every scanned page is read")
import documents
SCAN = "/scans/scanned-transcript-05min.pdf"
scan = open(SCAN, "rb").read() if os.path.exists(SCAN) else None
if scan:
    documents.OCR_MAX_PAGES = 0
    full = documents.extract_text_blocks(scan, "scan.pdf")
    documents.OCR_MAX_PAGES = 1
    cut = documents.extract_text_blocks(scan, "scan.pdf")
    documents.OCR_MAX_PAGES = 0
    n_full, n_cut = sum(len(b) for b in full.blocks), sum(len(b) for b in cut.blocks)
    check("OCR_MAX_PAGES=0 reads both pages of the scan", n_full > n_cut * 1.6, (n_full, n_cut))
else:
    print(f"  (skipped: no scan at {SCAN})")

print("a long transcript is read in parts, never refused")
calls = []


class Fake:
    def generate(self, system, user, max_tokens, temperature, **kw):
        calls.append((system, user))
        if "part 2 of" in user:
            return "None explicitly stated."
        return f"- found in call {len(calls)}"


text = "".join(f"Line {i}: the committee discussed item {i} at some length and agreed to revisit it.\n"
               for i in range(9000))                      # ~780,000 characters
route = {"context_limit": 65536}
out = lm.LLMManager._read_whole(Fake(), "SYSTEM", text, "List all decisions:", 1500, 0.0, route, "DECISIONS")
parts = [u for _, u in calls]
check("split into parts", len(parts) > 3, len(parts))
check("every part fits the model (65,536 tokens at 3 chars/token)",
      all(len(u) // 3 + 1500 + len("SYSTEM") < 65536 for u in parts), max(len(u) for u in parts))
check("each part says which part it is", all("TRANSCRIPT (part " in u for u in parts))
check("a part with nothing found is left out of the joined reply", "None explicitly" not in out and out.count("- found") == len(parts) - 1, out)
body = [u.split(":\n", 1)[1].rsplit("\n\nList all decisions:", 1)[0] for u in parts]
check("no line of the transcript is lost", all(f"Line {i}:" in "".join(body) for i in range(0, 9000, 37)))
check("each part opens with the end of the one before", all(b[:200] in body[k - 1] for k, b in enumerate(body) if k))
calls.clear()
lm.LLMManager._read_whole(Fake(), "SYSTEM", text[:50000], "List all decisions:", 1500, 0.0, route, "DECISIONS")
check("a short transcript is still ONE call, as before",
      len(calls) == 1 and calls[0][1] == f"TRANSCRIPT:\n{text[:50000]}\n\nList all decisions:", len(calls))
hindi = "बैठक में पानी की आपूर्ति पर चर्चा हुई। " * 20000
calls.clear()
lm.LLMManager._read_whole(Fake(), "SYSTEM", hindi, "List all decisions:", 1500, 0.0, route, "DECISIONS")
check("Hindi is cut into smaller parts (the tokenizer splits it finer)",
      all(len(u) < 65536 * 1.5 for _, u in calls) and len(calls) > 1, [len(u) for _, u in calls])
check("the merge of a long meeting reads far more than 16,000 characters",
      lm.LLMManager._SYNTHESIS_INPUT_CHARS > 100000 if lm.MODEL_CONTEXT_LIMIT >= 65536 else
      lm.LLMManager._SYNTHESIS_INPUT_CHARS >= 16000, lm.LLMManager._SYNTHESIS_INPUT_CHARS)

print("re-prompting a very large MoM")
big = {"title": "Council meeting", "summary": "", "agenda": ["Budget", "Roads"],
       "attendees": [{"name": "Maj Rohit Negi", "role": "Chairman"}, {"name": "Capt Arjun Bhatia", "role": ""}],
       "key_points": [f"Point {i}: the council heard a report on road number {i} and its repair schedule." for i in range(2000)]
       + ["The website redesign will cost 40,000 and go live in March."] + [f"Later point {i} on drainage." for i in range(500)],
       "decisions": [f"Decision {i} on road {i}." for i in range(150)], "action_items": [], "key_figures": []}
full = me._shown(big, {})
fitted = me._fitted(big, {}, "delete the point about the website redesign", 60000)
check("the full view would not fit", len(full) > 150000, len(full))
check("the view shown fits", len(fitted) <= 60000, len(fitted))
check("the line the instruction names is shown, under its real number",
      "2000. The website redesign will cost 40,000" in fitted)
check("attendees and agenda are always whole", "0. Maj Rohit Negi [role: Chairman]" in fitted and "1. Roads" in fitted)
check("the model is told lines are hidden", "more lines not shown" in fitted)
small = copy.deepcopy(big)
small["key_points"] = small["key_points"][:20]
small["decisions"] = small["decisions"][:5]
check("a normal MoM is shown exactly as before", me._fitted(small, {}, "anything", 60000) == me._shown(small, {}))
many = me._fitted(big, {}, "change the point about road repair", 60000)
check("a word on every line (road): still fits, first matches shown", len(many) <= 60000 and "0. Point 0:" in many, len(many))
seen = []
mom_module._get_writer = lambda: type("W", (), {"generate": lambda self, s, u, **k: seen.append(u) or '{"changes": []}'})()
me._ask(big, {}, "delete the point about the website redesign")
check("the edit call itself fits the model", len(seen[0]) <= me._room(), (len(seen[0]), me._room()))

print("ITEMs: no 12-item cap")
agenda = [f"Agenda subject {k}" for k in range(20)]
points = [f"About subject {k}, part {j}" for k in range(20) for j in range(3)]


class Group:
    def generate(self, system, user, **kw):
        if "\nPOINTS:\n" in user:
            return "\n".join(f"{k}: {3 * k}, {3 * k + 1}, {3 * k + 2}" for k in range(20))
        return ""


mom_module._get_writer = lambda: Group()
g = ag.group({"agenda": agenda}, points, [], [])
check("20 agenda entries → 20 ITEMs", g is not None and len(g) == 20, len(g or []))

print(f"\n{ok} passed, {fail} failed")
