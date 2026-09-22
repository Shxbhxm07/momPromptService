"""Problem 1 (cluster, 2026-09-21): a question wrote the minutes again and lost every edit.
Replayed: add Teena, rename Ariz Khan, ask about the agenda, then a real rewrite and undo."""
import copy
import json

import agenda_items
import minutes
import minutes_edit as me
import minutes_state
from kafka_contract import parse_job

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok  ", name)
    else:
        fail += 1
        print("  FAIL", name, extra)


def ch(**kw):
    base = {"op": "", "list": "", "index": -1, "quote": "", "field": "", "value": "", "role": "", "owner": "", "due": ""}
    base.update(kw)
    return base


class Store:
    def __init__(self):
        self.files, self.puts = {}, []

    def download(self, url):
        for key in (url, url.split("/", 1)[-1]):
            if key in self.files:
                return self.files[key]
        raise RuntimeError("NoSuchKey")

    def upload(self, key, data, mime):
        self.files[key] = data
        self.puts.append(key)
        return "mom", key


class Index:
    def index_mom(self, *a, **k):
        pass


TRANSCRIPT = ("Maj Rohit Negi opened the meeting. Water supply in Block C: 42 families without water since 1900 hrs "
              "yesterday; the line will be repaired by the evening of 24 September. Dussehra mela: a first aid post "
              "near the main stage. Convoy: all 12 vehicles to be checked by 1600 hrs.")
WRITES = []


class LLM:
    def generate(self, source, meta):
        WRITES.append(source)
        return {"content": {
            "header": {"title": "Weekly maintenance review"},
            "summary": "The meeting addressed the water line repair in Block C. It also covered the mela and the convoy.",
            "attendees": [{"name": "Maj Rohit Negi", "role": "Station Commander"}, {"name": "Ariz Khan", "role": ""}],
            "agenda": ["Water supply in Block C", "Dussehra mela", "Convoy"],
            "key_points": ["42 families in Block C have had no water since 1900 hrs yesterday."],
            "decisions": ["The line will be repaired by the evening of 24 September."],
            "action_items": [{"task": "Check all 12 vehicles", "assigned_to": "", "due": "1600 hrs"}],
            "key_figures": []}}


class C:
    def __init__(self):
        self.store, self.index, self.chunks, self.llm = Store(), Index(), Index(), LLM()


minutes._read_documents = lambda job, c: [("scanned-transcript-05min.pdf", TRANSCRIPT)]
minutes.build_mom_docx = lambda mom, meta, template=None: json.dumps(mom, sort_keys=True).encode()
me.build_mom_docx = minutes.build_mom_docx
agenda_items.group = lambda *a, **k: None
minutes.agenda_items.group = agenda_items.group

ANSWERS = []
CALLS = []


def fake_ask(mom, meta, instr, earlier="", refused=""):
    CALLS.append(instr)
    return {"changes": ANSWERS.pop(0)}


me._ask = fake_ask
c = C()
BASE = {"tenant_id": "t", "conversationId": "conv-1", "file_urls": ["mom/conv-1/05941299-5117-4bb6-b95f-782e412d99d3"],
        "document_names": ["scanned-transcript-05min.pdf"], "userId": "u1", "path": "AsItIs"}


def send(prompt, answers=None):
    if answers is not None:
        ANSWERS[:] = [answers]
    return minutes.process(parse_job({**BASE, "prompt": prompt}), c)


def state():
    return minutes_state.load(parse_job(BASE), c.store)


print("the session of 2026-09-21, replayed")
ack = send("Standard minutes for the weekly maintenance review")
check("first MoM written", ack["message"] == "SUCCESS" and len(WRITES) == 1, ack.get("description"))
ack = send("add Teena as an intern in AI", [ch(op="add", list="attendees", value="Teena", role="intern in AI")])
check("Teena added", ack["message"] == "SUCCESS" and state()["mom"]["attendees"][-1]["name"] == "Teena", ack["description"])
ack = send("Update Ariz Khan name to Shubham Pandey",
           [ch(op="replace", list="attendees", index=1, quote="Ariz Khan", value="Shubham Pandey")])
edited = state()
check("renamed", [a["name"] for a in edited["mom"]["attendees"]] == ["Maj Rohit Negi", "Shubham Pandey", "Teena"],
      edited["mom"]["attendees"])
file_before, puts_before = edited["summary_object_key"], len(c.store.puts)

print("a question is answered; nothing changes")
answer = ("The agenda had three items: Water supply in Block C, the Dussehra mela and the convoy of 12 vehicles.")
ack = send("can you explain me the agenda of this meetig", [ch(op="answer", value=answer)])
check("SUCCESS, the answer is the reply", ack["message"] == "SUCCESS" and ack["description"] == answer, ack["description"])
check("the same file comes back", ack.get("summaryObjectKey") == file_before, ack.get("summaryObjectKey"))
check("no new file, no new state", len(c.store.puts) == puts_before, c.store.puts[puts_before:])
check("the minutes were NOT written again", len(WRITES) == 1, len(WRITES))
check("Teena and Shubham Pandey still there", state()["mom"]["attendees"] == edited["mom"]["attendees"])
check("the backend's own fields come back", ack.get("userId") == "u1" and ack.get("path") == "AsItIs")

ack = send("how many families were without water?", [ch(op="answer", value="42 families."), ch(op="cannot", value="x")])
check("answer + cannot: still an answer, no rewrite", ack["description"] == "42 families." and len(WRITES) == 1,
      (ack["description"], len(WRITES)))
ack = send("how many vehicles?", [ch(op="answer", value="There are 15 vehicles.")])
check("an invented number is not passed on", ack["description"] == "I could not answer that from these minutes."
      and ack["message"] == "SUCCESS" and len(c.store.puts) == puts_before, ack["description"])
ack = send("who is Col. Verma?", [ch(op="answer", value="Col. Verma is the Garrison Engineer.")])
check("an invented name is not passed on", ack["description"] == "I could not answer that from these minutes.",
      ack["description"])
long = "The minutes say the water line in Block C will be repaired by the evening of 24 September. " * 25
ack = send("explain the water problem", [ch(op="answer", value=long)])
check("a long answer is kept up to 1500 characters", 1400 < len(ack["description"]) <= 1500, len(ack["description"]))
ack = send("add Hav Mohan Lal and tell me who attended",
           [ch(op="add", list="attendees", value="Hav Mohan Lal"),
            ch(op="answer", value="Maj Rohit Negi, Shubham Pandey and Teena attended.")])
check("a change with a question: change made, answer added",
      ack["message"] == "SUCCESS" and "Hav Mohan Lal" in ack["description"] and "attended" in ack["description"]
      and state()["mom"]["attendees"][-1]["name"] == "Hav Mohan Lal", ack["description"])
ANSWERS[:] = [[ch(op="undo")]]
send("undo")
check("(set back to before Hav Mohan Lal)", [a["name"] for a in state()["mom"]["attendees"]]
      == ["Maj Rohit Negi", "Shubham Pandey", "Teena"])

print("a real rewrite keeps the edited version for undo")
ack = send("focus more on the budget", [ch(op="cannot", value="needs the document")])
check("written again from the document", len(WRITES) == 2 and ack["message"] == "SUCCESS", len(WRITES))
check("the reply says how to get the changes back", ack["description"].startswith("Written again") and "undo" in ack["description"],
      ack["description"])
fresh = state()
check("the new version has the writer's attendees", [a["name"] for a in fresh["mom"]["attendees"]] == ["Maj Rohit Negi", "Ariz Khan"])
check("history kept, the edited version on top", fresh["history"] and
      [a["name"] for a in fresh["history"][-1]["mom"]["attendees"]] == ["Maj Rohit Negi", "Shubham Pandey", "Teena"],
      len(fresh.get("history") or []))
check("the rewrite is listed among earlier requests", "written again from the document" in me._earlier(fresh), me._earlier(fresh))
ack = send("undo", [ch(op="undo")])
back = state()
check("undo brings back Teena and Shubham Pandey", ack["message"] == "SUCCESS" and
      [a["name"] for a in back["mom"]["attendees"]] == ["Maj Rohit Negi", "Shubham Pandey", "Teena"],
      (ack["description"], back["mom"]["attendees"]))
check("  and the header the user gave", back["meta"] == edited["meta"])

print("a new meeting is still a new meeting")
WRITES.clear()
ack = minutes.process(parse_job({**BASE, "conversationId": "conv-2", "prompt": "minutes please"}), c)
s2 = minutes_state.load(parse_job({**BASE, "conversationId": "conv-2"}), c.store)
check("fresh conversation: no history, no note", s2["history"] == [] and not ack["description"].startswith("Written again"),
      (s2["history"], ack["description"]))
ack = send("focus on the convoy", [ch(op="cannot", value="needs the document")])
check("rewrite of never-edited minutes... (conv-1 was edited: note shown)", ack["description"].startswith("Written again"))

print(f"\n{ok} passed, {fail} failed")
