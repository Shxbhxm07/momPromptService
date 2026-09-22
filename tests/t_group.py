"""ITEM grouping: plain-line replies, separate calls, and the matching fallback for decisions and figures."""
import agenda_items as ag
import mom as mom_module

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok  ", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


print("reading the model's lines")
p = ag._parse("0: 3, 7, 12\n1: 1, 2", 3)
check("plain lines", p == {0: [3, 7, 12], 1: [1, 2]}, p)
p = ag._parse("Here you go:\nItem 0 - 3, 7\n**1:** 4\nagenda item 2: 5-8\n9: 1, 2\nnothing else", 3)
check("variants, a range, junk and an unknown item ignored", p == {0: [3, 7], 1: [4], 2: [5, 6, 7, 8]}, p)
p = ag._parse("0: 1, 2, 3\n1: 4, 5\n2: 6, 1", 3)
check("a reply cut off mid-line keeps what came before", p[0] == [1, 2, 3] and p[1] == [4, 5], p)
p = ag._parse("0: none\n1: -\n2:", 3)
check("empty items", all(not v for v in p.values()), p)

AGENDA = ["Water supply in Block C", "Dussehra mela arrangements", "Convoy of 12 vehicles"]
POINTS = ["42 families in Block C have had no water since 1900 hrs yesterday.",
          "The main line near the JCO mess burst during digging.",
          "The mela will be held on 12 October near the parade ground.",
          "A first aid post will be needed near the main stage.",
          "The convoy of 12 vehicles leaves on 28 September.",
          "Two vehicles still need a clutch plate."]
DECISIONS = [("Bowsers will go to Block C twice a day until the line is restored.", ""),
             ("A first aid post with an ambulance will be set up near the main stage for the mela.", "SMO"),
             ("All 12 vehicles will be lined up and checked by 1600 hrs on 27 September.", "Capt Arjun Bhatia")]
FIGURES = ["42 families without water", "12 vehicles in the convoy", "1600 hrs vehicle check"]
MOM = {"agenda": AGENDA}


class Writer:
    def __init__(self, replies):
        self.replies, self.seen = replies, []

    def generate(self, system, user, max_new_tokens, temperature, **kw):
        self.seen.append((system, user, max_new_tokens, kw))
        for key, reply in self.replies.items():
            if f"\n{key}:\n" in user:
                return reply
        return ""


def run(replies):
    w = Writer(replies)
    mom_module._get_writer = lambda: w
    return ag.group(MOM, POINTS, DECISIONS, FIGURES), w


print("the model places everything")
g, w = run({"POINTS": "0: 0, 1\n1: 2, 3\n2: 4, 5", "DECISIONS": "0: 0\n1: 1\n2: 2", "FIGURES": "0: 0\n2: 1, 2"})
check("three items", [x["title"] for x in g] == AGENDA, g)
check("decisions spread over the items", [x["decisions"] for x in g] == [[0], [1], [2]], g)
check("figures spread over the items", [x["figures"] for x in g] == [[0], [], [1, 2]], g)
check("three calls: points, decisions, figures", len(w.seen) == 3, len(w.seen))
check("no JSON asked for", all("response_format" not in str(kw) for *_, kw in w.seen))
check("each reply budget is small", all(t <= ag._REPLY_OVERHEAD + 4 * 80 + 8 * 12 for _, _, t, _ in w.seen),
      [t for _, _, t, _ in w.seen])

print("the model gives nothing for decisions and figures (the cluster's cut-off reply)")
g, w = run({"POINTS": "0: 0, 1\n1: 2, 3\n2: 4, 5", "DECISIONS": "", "FIGURES": "garbled {"})
check("decisions go beside the points they match, not all to the last item",
      [x["decisions"] for x in g] == [[0], [1], [2]], [x["decisions"] for x in g])
check("figures too (42 → water, 12 vehicles and 1600 hrs → convoy)",
      [x["figures"] for x in g] == [[0], [], [1, 2]], [x["figures"] for x in g])

print("round two: decisions and figures see each item's points")
g, w = run({"POINTS": "0: 0, 1\n1: 2, 3\n2: 4, 5", "DECISIONS": "0: 0\n1: 1\n2: 2", "FIGURES": "0: 0\n2: 1, 2"})
dec_call = [u for _, u, _, _ in w.seen if "\nDECISIONS:\n" in u][0]
check("the decisions call lists the points under each item",
      "(with points already placed under each item)" in dec_call and "- Two vehicles still need a clutch plate." in dec_call
      and dec_call.index("Two vehicles") > dec_call.index("2. Convoy"), dec_call[:400])
check("  and is told what they are", any("points already placed there" in sy for sy, u, _, _ in w.seen if "DECISIONS" in u))
check("the points call is not", "with points already placed" not in [u for _, u, _, _ in w.seen if "\nPOINTS:\n" in u][0])
g, w = run({"POINTS": "0: 0", "DECISIONS": "0: 0, 1, 2"})
check("points grouping failed → no second round (no wasted calls)", g is None and len(w.seen) == 1, len(w.seen))
g, w = run({"POINTS": "0: 0, 1, 2, 3\n2: 4, 5", "DECISIONS": "0: 0\n1: 1\n2: 2"})
check("an agenda item with no points but a decision still prints", [x["title"] for x in g] == AGENDA
      and g[1]["points"] == [] and g[1]["decisions"] == [1], g)

print("a decision that matches nothing")
odd = DECISIONS + [("Tea will be served at the next session.", "")]
w = Writer({"POINTS": "0: 0, 1\n1: 2, 3\n2: 4, 5", "DECISIONS": "0: 0\n1: 1\n2: 2"})
mom_module._get_writer = lambda: w
g = ag.group(MOM, POINTS, odd, FIGURES)
check("goes to the last item, as before", 3 in g[-1]["decisions"], g)
check("  and nothing is lost", sorted(i for x in g for i in x["decisions"]) == [0, 1, 2, 3], g)
check("every figure printed once", sorted(i for x in g for i in x["figures"]) == [0, 1, 2], g)

print("guards kept")
g, w = run({"POINTS": "0: 0", "DECISIONS": "0: 0, 1, 2"})
check("under half the points placed → one item, as before", g is None, g)
g, w = run({"POINTS": "0: 0, 1, 1, 0\n1: 2, 3, 0\n2: 4, 5, 99", "DECISIONS": "0: 0, 0\n1: 0, 1\n2: 2, 7"})
check("repeats and out-of-range numbers dropped", [x["points"] for x in g] == [[0, 1], [2, 3], [4, 5]]
      and [x["decisions"] for x in g] == [[0], [1], [2]], g)
mom_module._get_writer = lambda: (_ for _ in ()).throw(RuntimeError("model down"))
check("the model down → one item, as before", ag.group(MOM, POINTS, DECISIONS, FIGURES) is None)

print("a long meeting: every call's reply stays small")
many = [f"Point {i} about the water line" for i in range(200)]
w = Writer({"POINTS": "\n".join(f"{a}: " + ", ".join(str(i) for i in range(a * 67, min(200, a * 67 + 67))) for a in range(3))})
mom_module._get_writer = lambda: w
ag.group(MOM, many, DECISIONS, FIGURES)
check("200 points → 3 point calls + decisions + figures", len(w.seen) == 5, len(w.seen))
check("  numbered on from each batch's start", "80. Point 80" in w.seen[1][1] and "160. Point 160" in w.seen[2][1])

print(f"\n{ok} passed, {fail} failed")
