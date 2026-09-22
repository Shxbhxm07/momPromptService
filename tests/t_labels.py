"""Re-prompt: the two cluster failures of 2026-09-19/21, the guards still holding, and the one retry."""
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


ATT = [{"name": "Maj Rohit Negi", "role": "Station Commander"}, {"name": "Capt Arjun Bhatia", "role": ""},
       {"name": "Mr R K Gupta", "role": "Assistant Garrison Engineer (MES)"}, {"name": "Sub Karan Singh", "role": ""},
       {"name": "Hav Mohan Lal", "role": ""}, {"name": "Col. Ariz Khan", "role": "Team Leader of AI"}]
MOM = {"title": "Weekly maintenance review", "summary": "", "attendees": ATT, "agenda": ["Street lights"],
       "key_points": ["Street lights behind the JCO mess are not working."], "decisions": [],
       "action_items": [{"task": "Repair the street lights", "assigned_to": "Capt Arjun Bhatia", "due": ""}],
       "key_figures": []}


def run(changes, instruction, earlier=""):
    mom, meta = copy.deepcopy(MOM), {}
    done, refused, _ = me.apply(mom, meta, changes, instruction, earlier)
    return mom, done, refused


print("the two real failures, replayed with the model's own answers")
mom, done, refused = run([ch(op="add", list="attendees", value="Teena [role: Intern in AI]")],
                         "add Teena as an intern in AI")
check("'Teena [role: Intern in AI]' is added", done and not refused, refused)
check("  as name 'Teena', role 'Intern in AI'", mom["attendees"][-1] == {"name": "Teena", "role": "Intern in AI"},
      mom["attendees"][-1])

mom, done, refused = run([ch(op="replace", list="attendees", index=5, quote="Col. Ariz Khan [role: Team Leader of AI]",
                             value="Shubham Pandey [role: Team Leader of AI]")],
                         "Update Col. Ariz Khan name to Shubham Pandey")
check("the rename is made", done and not refused, refused)
check("  name Shubham Pandey, role kept", mom["attendees"][5] == {"name": "Shubham Pandey", "role": "Team Leader of AI"},
      mom["attendees"][5])
check("  nobody else touched", mom["attendees"][:5] == ATT[:5])

print("the new answer format")
mom, done, refused = run([ch(op="add", list="attendees", value="Teena", role="intern in AI")],
                         "add Teena as an intern in AI")
check("value 'Teena' + role 'intern in AI'", mom["attendees"][-1] == {"name": "Teena", "role": "intern in AI"}, refused)
mom, done, refused = run([ch(op="add", list="attendees", value="Teena", owner="intern in AI")],
                         "add Teena as an intern in AI")
check("older answers with the role in owner still work", mom["attendees"][-1] == {"name": "Teena", "role": "intern in AI"},
      refused)
mom, done, refused = run([ch(op="set_role", list="attendees", index=1, quote="Capt Arjun Bhatia", role="Quartermaster")],
                         "make Capt Arjun Bhatia the Quartermaster")
check("set_role with role", mom["attendees"][1]["role"] == "Quartermaster", refused)
mom, done, refused = run([ch(op="set_role", list="attendees", index=1, quote="Capt Arjun Bhatia", value="Quartermaster")],
                         "make Capt Arjun Bhatia the Quartermaster")
check("set_role with value (older answers)", mom["attendees"][1]["role"] == "Quartermaster", refused)
mom, done, refused = run([ch(op="replace", list="attendees", index=5, quote="Col. Ariz Khan",
                             value="Shubham Pandey [role: AI lead]")],
                         "rename Col. Ariz Khan to Shubham Pandey, AI lead")
check("rename + new role in a label", mom["attendees"][5] == {"name": "Shubham Pandey", "role": "AI lead"},
      (mom["attendees"][5], refused))
mom, done, refused = run([ch(op="replace", list="attendees", index=5, quote="Col. Ariz Khan", role="AI lead")],
                         "change Col. Ariz Khan's role to AI lead")
check("replace with only a role changes the role", mom["attendees"][5] == {"name": "Col. Ariz Khan", "role": "AI lead"},
      (mom["attendees"][5], refused))
mom, done, refused = run([ch(op="add", list="action_items", value="Check the generator [owner: Hav Mohan Lal] [due: 30 Sep]")],
                         "add an action: check the generator, Hav Mohan Lal, by 30 Sep")
check("action labels go to owner and due",
      mom["action_items"][-1] == {"task": "Check the generator", "assigned_to": "Hav Mohan Lal", "assigned_by": "", "due": "30 Sep"},
      (mom["action_items"][-1], refused))

print("the guards still refuse what the user never wrote")
mom, done, refused = run([ch(op="add", list="attendees", value="Teena Sharma", role="intern in AI")],
                         "add Teena as an intern in AI")
check("an invented surname", not done and refused, done)
mom, done, refused = run([ch(op="add", list="attendees", value="Teena [role: Senior Scientist]")],
                         "add Teena as an intern in AI")
check("an invented role in a label", not done and refused, done)
mom, done, refused = run([ch(op="add", list="attendees", value="Teena", role="Scientist")], "add Teena")
check("an invented role in role", not done and refused, done)
mom, done, refused = run([ch(op="replace", list="attendees", index=5, quote="Col. Ariz Khan",
                             value="Shubham Pandey [role: Director]")],
                         "Update Col. Ariz Khan name to Shubham Pandey")
check("a rename carrying an invented role", not done and refused, done)
mom, done, refused = run([ch(op="replace", list="attendees", index=5, quote="Col. Ariz Khan", value="Col. Shubham Pandey")],
                         "Update Col. Ariz Khan name to Shubham")
check("a rename to a name never written", not done and refused, done)
mom, done, refused = run([ch(op="set_role", list="attendees", index=2, quote="Mr R K Gupta", role="Garrison Commander")],
                         "update his position")
check("'Garrison Commander' built from two other roles", not done and refused, done)
mom, done, refused = run([ch(op="add", list="action_items", value="Check the generator [due: 31 Dec 2027]")],
                         "add an action: check the generator by 30 Sep")
check("an invented due date in a label", not done and refused, done)
mom, done, refused = run([ch(op="replace", list="attendees", index=4, quote="Col. Ariz Khan", value="Shubham Pandey")],
                         "Update Col. Ariz Khan name to Shubham Pandey")
check("wrong index, quote wins", mom["attendees"][5]["name"] == "Shubham Pandey" and mom["attendees"][4] == ATT[4])


print("process(): one retry after a refusal, never after cannot")


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


me.build_mom_docx = lambda mom, meta, template=None: json.dumps(mom).encode()


def session(answers, instruction, follow=False):
    c = C()
    job = parse_job({"tenant_id": "t", "conversationId": "conv-1", "prompt": "make the minutes"})
    minutes_state.save(job, c.store, mom=copy.deepcopy(MOM), meta={}, template={}, object_key="t/summaries/x/MoM-a.docx",
                       bucket="mom")
    calls = []

    def fake(mom, meta, instr, earlier="", refused=""):
        calls.append(refused)
        return {"changes": answers[min(len(calls), len(answers)) - 1]}

    me._ask = fake
    edit = parse_job({"tenant_id": "t", "conversationId": "conv-1", "prompt": instruction})
    ack = me.process(edit, c, follow=follow)
    state = minutes_state.load(edit, c.store)
    return ack, state, calls


bad = [ch(op="add", list="attendees", value="Teena Sharma", role="intern in AI")]
good = [ch(op="add", list="attendees", value="Teena", role="intern in AI")]
ack, state, calls = session([bad, good], "add Teena as an intern in AI")
check("refused, then right on the second try: SUCCESS", ack["message"] == "SUCCESS", ack.get("description"))
check("  two calls, the reason sent back", len(calls) == 2 and "Sharma" in calls[1] and "Refused" in calls[1], calls)
check("  Teena saved", state["mom"]["attendees"][-1] == {"name": "Teena", "role": "intern in AI"})

ack, state, calls = session([bad, bad], "add Teena as an intern in AI")
check("refused twice: FAILED with the reason", ack["message"] != "SUCCESS" and "Sharma" in ack["description"],
      ack.get("description"))
check("  exactly two calls", len(calls) == 2, len(calls))
check("  the saved minutes untouched", state["mom"] == MOM)

ack, state, calls = session([good], "add Teena as an intern in AI")
check("right first time: one call", ack["message"] == "SUCCESS" and len(calls) == 1, (ack.get("description"), len(calls)))

ack, state, calls = session([[ch(op="cannot", value="needs the document")]], "focus more on the budget", follow=True)
check("cannot on a follow-up: written again, no retry", ack is None and len(calls) == 1, (ack, len(calls)))

two = [ch(op="add", list="attendees", value="Teena", role="intern in AI"),
       ch(op="set_role", list="attendees", index=1, quote="Capt Arjun Bhatia", role="Commander")]
ack, state, calls = session([two, [good[0]]], "add Teena as an intern in AI and make Capt Arjun Bhatia the Quartermaster")
check("the retry is kept only when it does more", len(calls) == 2 and state["mom"]["attendees"][-1]["name"] == "Teena",
      (len(calls), ack.get("description")))

print(f"\n{ok} passed, {fail} failed")
