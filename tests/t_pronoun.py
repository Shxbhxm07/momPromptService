"""Problem 2 (cluster, 2026-09-21): "add his position as an AI engineeg" changed Teena, not Shubham Pandey."""
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
MOM = {"title": "Weekly maintenance review", "summary": "", "agenda": ["Water supply"],
       "attendees": [{"name": "Maj Rohit Negi", "role": "Station Commander"},
                     {"name": "Capt Arjun Bhatia", "role": ""},
                     {"name": "Mr R K Gupta", "role": "Assistant Garrison Engineer (MES)"}],
       "key_points": ["42 families in Block C have had no water since 1900 hrs yesterday."], "decisions": [],
       "action_items": [{"task": "Check all 12 vehicles for the convoy", "assigned_to": "", "due": "1600 hrs"}],
       "key_figures": []}
SEEN = []


def new_session():
    c = C()
    job = parse_job({"tenant_id": "t", "conversationId": "conv-1", "prompt": "minutes"})
    minutes_state.save(job, c.store, mom=copy.deepcopy(MOM), meta={}, template={},
                       object_key="t/summaries/x/MoM-a.docx", bucket="mom")
    return c


def send(c, prompt, answer):
    def fake(mom, meta, instr, earlier="", refused=""):
        SEEN.append(earlier)
        return {"changes": answer}
    me._ask = fake
    edit = parse_job({"tenant_id": "t", "conversationId": "conv-1", "prompt": prompt})
    return me.process(edit, c, follow=True)


def people(c):
    st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
    return {a["name"]: a["role"] for a in st["mom"]["attendees"]}


def teena_session():
    c = new_session()
    send(c, "add Teena as an intern in AI", [ch(op="add", list="attendees", value="Teena", role="intern in AI")])
    send(c, "add Ariz Khan as an Team Leader in AI",
         [ch(op="add", list="attendees", value="Ariz Khan", role="Team Leader in AI")])
    send(c, "Update Ariz Khan name to Shubham Pandey",
         [ch(op="replace", list="attendees", index=4, quote="Ariz Khan [role: Team Leader in AI]", value="Shubham Pandey")])
    return c


print("the session of 2026-09-21, replayed")
c = teena_session()
check("set up: Teena and Shubham Pandey", people(c).get("Teena") == "intern in AI"
      and people(c).get("Shubham Pandey") == "Team Leader in AI", people(c))
# the real model's mistake: it picked Teena (line 3)
ack = send(c, "ass his position as an AI engineeg",
           [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="AI engineeg")])
check("'his' goes to Shubham Pandey, changed last", people(c).get("Shubham Pandey") == "AI engineeg", people(c))
check("Teena is untouched", people(c).get("Teena") == "intern in AI", people(c))
check("the reply names Shubham Pandey", "Shubham Pandey" in ack["description"], ack["description"])
check("the model was told who 'his' means", "means Shubham Pandey" in SEEN[-1], SEEN[-1][-200:])

print("the same rule for every pronoun")
for word in ("her", "their", "him"):
    c = teena_session()
    send(c, f"make {word} role Data Scientist" if word != "him" else "make him a Data Scientist",
         [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="Data Scientist")])
    check(f"'{word}' → the person changed last", people(c).get("Shubham Pandey") == "Data Scientist"
          and people(c).get("Teena") == "intern in AI", people(c))

print("a name wins over the rule")
c = teena_session()
send(c, "change Teena's role to AI engineer, she is not an intern",
     [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="AI engineer")])
check("'Teena … she' changes Teena", people(c).get("Teena") == "AI engineer"
      and people(c).get("Shubham Pandey") == "Team Leader in AI", people(c))
c = teena_session()
send(c, "give Bhatia his role: Quartermaster",
     [ch(op="set_role", list="attendees", index=1, quote="Capt Arjun Bhatia [role: —]", role="Quartermaster")])
check("a surname is a name", people(c).get("Capt Arjun Bhatia") == "Quartermaster", people(c))

print("changes that are not about a person are left alone")
c = teena_session()
send(c, "add tele 9654396200, their office number", [ch(op="set_meta", field="telephone", value="9654396200")])
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
check("telephone set", st["meta"].get("telephone") == "9654396200", st["meta"])
send(c, "remove the point where they talk about water",
     [ch(op="delete", list="key_points", index=0, quote="42 families in Block C")])
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
check("point deleted", st["mom"]["key_points"] == [], st["mom"]["key_points"])
ack = send(c, "update his position to AI engineer",
           [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="AI engineer")])
check("after a telephone and a point, 'his' is still Shubham Pandey",
      people(c).get("Shubham Pandey") == "AI engineer" and people(c).get("Teena") == "intern in AI", people(c))

print("the owner of a task")
c = teena_session()
send(c, "make him the owner of the convoy task",
     [ch(op="set_owner", list="action_items", index=0, quote="Check all 12 vehicles", owner="Teena")])
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
check("owner → Shubham Pandey", st["mom"]["action_items"][0]["assigned_to"] == "Shubham Pandey",
      st["mom"]["action_items"][0])

c = teena_session()
send(c, "make him the secretary", [ch(op="set_meta", field="secretary_name", value="Teena")])
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
check("'make him the secretary' → the person changed last", (st["meta"].get("secretary") or {}).get("name") == "Shubham Pandey",
      st["meta"])
c = teena_session()
send(c, "make Teena the secretary", [ch(op="set_meta", field="secretary_name", value="Teena")])
st = minutes_state.load(parse_job({"tenant_id": "t", "conversationId": "conv-1"}), c.store)
check("'make Teena the secretary' → Teena", (st["meta"].get("secretary") or {}).get("name") == "Teena", st["meta"])

print("when nobody was changed last, ask — never guess")
c = new_session()
ack = send(c, "update his position to AI engineer",
           [ch(op="set_role", list="attendees", index=2, quote="Mr R K Gupta", role="AI engineer")])
check("first message says 'his': asked for a name", ack["message"] != "SUCCESS" and "write the person's name" in ack["description"],
      ack["description"])
check("  nothing changed", people(c).get("Mr R K Gupta") == "Assistant Garrison Engineer (MES)")
c = teena_session()
send(c, "remove Teena", [ch(op="delete", list="attendees", index=3, quote="Teena [role: intern in AI]")])
ack = send(c, "update his position to AI engineer",
           [ch(op="set_role", list="attendees", index=3, quote="Shubham Pandey", role="AI engineer")])
check("last change removed someone: asked for a name", ack["message"] != "SUCCESS"
      and "write the person's name" in ack["description"], ack["description"])
c = teena_session()
send(c, "undo", [ch(op="undo")])
send(c, "change his role to AI engineer",
     [ch(op="set_role", list="attendees", index=3, quote="Teena [role: intern in AI]", role="AI engineer")])
check("undo of the rename: 'his' is Ariz Khan, the person set back", people(c).get("Ariz Khan") == "AI engineer"
      and people(c).get("Teena") == "intern in AI", people(c))
c = new_session()
send(c, "add Teena as an intern in AI", [ch(op="add", list="attendees", value="Teena", role="intern in AI")])
send(c, "undo", [ch(op="undo")])
ack = send(c, "change her role to AI engineer",
           [ch(op="set_role", list="attendees", index=2, quote="Mr R K Gupta", role="AI engineer")])
check("undo of an add (nobody left to mean): asked for a name", ack["message"] != "SUCCESS"
      and "write the person's name" in ack["description"], ack["description"])

print("no pronoun: nothing different")
c = teena_session()
send(c, "make Capt Arjun Bhatia the Quartermaster",
     [ch(op="set_role", list="attendees", index=1, quote="Capt Arjun Bhatia", role="Quartermaster")])
check("named change as before", people(c).get("Capt Arjun Bhatia") == "Quartermaster", people(c))
check("  no pronoun hint shown", "means" not in SEEN[-1], SEEN[-1][-120:])

print(f"\n{ok} passed, {fail} failed")
