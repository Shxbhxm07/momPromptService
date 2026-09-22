"""Problems 3 and 4 (cluster, 2026-09-21): "AI engineeg" printed as typed; replies that read like log lines."""
import copy
import json

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


MOM = {"title": "Weekly maintenance review", "summary": "", "agenda": ["Water supply"],
       "attendees": [{"name": "Maj Rohit Negi", "role": "Station Commander"}, {"name": "Capt Arjun Bhatia", "role": ""},
                     {"name": "Teena", "role": "intern in AI"}, {"name": "Shubham Pandey", "role": "Team Leader in AI"}],
       "key_points": ["42 families in Block C have had no water since 1900 hrs yesterday.",
                      "Water bowsers will go to Block C twice a day."],
       "decisions": [], "key_figures": [],
       "action_items": [{"task": "Check all 12 vehicles for the convoy", "assigned_to": "", "due": "1600 hrs"}]}


def run(changes, instruction, earlier=""):
    mom, meta = copy.deepcopy(MOM), {}
    done, refused, _ = me.apply(mom, meta, changes, instruction, earlier)
    return mom, meta, done, refused


def roles(mom):
    return {a["name"]: a["role"] for a in mom["attendees"]}


print("3. spelling: the model corrects, code checks it is only a correction")
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="AI engineer")],
                               "ass his position as an AI engineeg")
check("'engineeg' corrected to 'engineer' is accepted", roles(mom)["Shubham Pandey"] == "AI engineer", refused)
check("  and the reply says so", done and 'spelling corrected: "engineeg" → "engineer"' in done[0], done)
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="AI engineeg")],
                               "ass his position as an AI engineeg")
check("the typo copied as typed is still accepted (code never corrects by itself)",
      roles(mom)["Shubham Pandey"] == "AI engineeg" and "spelling" not in done[0], (done, refused))
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="Senior Engineer")],
                               "make Shubham Pandey Senior Enginer")
check("a capitalised correction ('Enginer' → 'Engineer')", roles(mom)["Shubham Pandey"] == "Senior Engineer", refused)
mom, meta, done, refused = run([ch(op="add", list="attendees", value="Ankit", role="maintenance officer")],
                               "add Ankit as maintenence officer")
check("a new attendee's role", roles(mom).get("Ankit") == "maintenance officer" and "spelling" in done[0], (done, refused))
mom, meta, done, refused = run([ch(op="set_meta", field="venue", value="Conference Room B")], "venue is Conferance Room B")
check("a header detail ('Conferance' → 'Conference')", meta.get("venue") == "Conference Room B", refused)
mom, meta, done, refused = run([ch(op="add", list="key_points", value="The generator needs maintenance.")],
                               "add a point: the generator needs maintenence")
check("a line of text", mom["key_points"][-1] == "The generator needs maintenance." and "spelling" in done[0], done)

print("   what is NOT a correction is refused")
mom, meta, done, refused = run([ch(op="add", list="attendees", value="Pander", role="intern")], "add pandey as intern")
check("a name is never corrected (pandey → Pander)", not done and refused, done)
mom, meta, done, refused = run([ch(op="replace", list="attendees", index=2, quote="Teena [role: intern in AI]",
                                  value="Teena Smith")], "rename Teena to teena smiht")
check("  nor a rename, even to a dictionary word (smiht → Smith)", not done and refused, done)
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="header")],
                               "make Shubham Pandey the leader")
check("a real word is not 'corrected' into another (leader → header)", not done and refused, done)
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="AI engineers")],
                               "ass his position as an AI engineeg")
check("two letters away is not a correction (engineeg → engineers)", not done and refused, done)
mom, meta, done, refused = run([ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="AI Director")],
                               "ass his position as an AI engineeg")
check("an unrelated word is refused as before", not done and refused, done)
mom, meta, done, refused = run([ch(op="set_owner", list="action_items", index=0, quote="Check all 12 vehicles", owner="Karat")],
                               "make karan the owner of the convoy check")
check("an owner is never corrected (karan → Karat)", not done and refused, done)
mom, meta, done, refused = run([ch(op="set_meta", field="telephone", value="9654396201")], "tele 9654396200")
check("numbers are never corrected", not done and refused, done)

print("4. replies in plain sentences")


class Store:
    def __init__(self):
        self.files = {}

    def download(self, url):
        for key in (url, url.split("/", 1)[-1]):
            if key in self.files:
                return self.files[key]
        raise RuntimeError("NoSuchKey")

    def upload(self, key, data, mime):
        self.files[key] = data
        return "mom", key


class Index:
    def index_mom(self, *a, **k):
        pass


class C:
    def __init__(self):
        self.store, self.index, self.chunks = Store(), Index(), Index()


me.build_mom_docx = lambda mom, meta, template=None: json.dumps(mom, sort_keys=True).encode()
start = copy.deepcopy(MOM)
start["attendees"] = start["attendees"][:2] + [{"name": "Ariz Khan", "role": "Team Leader in AI"}]
c = C()
minutes_state.save(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store, mom=start, meta={}, template={},
                   object_key="t/summaries/x/MoM-a.docx", bucket="mom")


def send(prompt, answer):
    me._ask = lambda *a, **k: {"changes": answer}
    return me.process(parse_job({"tenant_id": "t", "conversationId": "conv-1", "prompt": prompt}), c, follow=True)["description"]


r = send("add Teena as an intern in AI", [ch(op="add", list="attendees", value="Teena", role="intern in AI")])
check("add attendee", r == "Done. Added Teena to the attendees as intern in AI.", r)
r = send("Update Ariz Khan name to Shubham Pandey",
         [ch(op="replace", list="attendees", index=2, quote="Ariz Khan", value="Shubham Pandey")])
check("rename", r == "Done. Ariz Khan is now Shubham Pandey.", r)
r = send("add his position as an AI engineeg",
         [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="AI engineer")])
check("role, with its old value and the spelling fix (and 'his' = Shubham Pandey)",
      r == 'Done. Shubham Pandey\'s role is now AI engineer (was Team Leader in AI) (spelling corrected: "engineeg" → "engineer").', r)
r = send("add tele 9654396200", [ch(op="set_meta", field="telephone", value="9654396200")])
check("telephone", r == "Done. The telephone is now 9654396200.", r)
r = send("remove the point about bowsers", [ch(op="delete", list="key_points", index=1, quote="Water bowsers will go")])
check("delete a point", r == 'Done. Removed the point "Water bowsers will go to Block C twice a day.".', r)
r = send("make Capt Arjun Bhatia the owner of the convoy check, due 30 Sep",
         [ch(op="set_owner", list="action_items", index=0, quote="Check all 12 vehicles", owner="Capt Arjun Bhatia", due="30 Sep")])
check("owner", r == 'Done. The action "Check all 12 vehicles for the convoy" is now owned by Capt Arjun Bhatia, due 30 Sep.', r)
r = send("undo", [ch(op="undo")])
check("undo", r == "Done. Undid the last change.", r)
r = send("add Teena and set tele 9654396201",
         [ch(op="add", list="attendees", value="Teena Sharma"), ch(op="set_meta", field="telephone", value="9654396201")])
check("part done", r == "Done. The telephone is now 9654396201 (was 9654396200). Not done: I did not add the attendee: "
      "Sharma is not in your message.", r)
r = send("add Col. Verma", [ch(op="add", list="attendees", value="Col. Varma")])
check("nothing done", r == "Nothing was changed. I did not add 'Col. Varma': that name is not in your message.", r)
r = send("set tele 9654396201", [ch(op="set_meta", field="telephone", value="9654396201")])
check("same value: no '(was …)'", r == "Done. The telephone is now 9654396201.", r)
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
earlier = me._earlier(st, keep=10)
check("earlier requests keep old values, for 'set it back'", "(was Team Leader in AI)" in earlier, earlier)
check("earlier requests read as sentences", "→ Ariz Khan is now Shubham Pandey." in earlier, earlier)

print(f"\n{ok} passed, {fail} failed")
