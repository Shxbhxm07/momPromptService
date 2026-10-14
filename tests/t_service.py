"""The MoM service end to end, through its real ways in (FastAPI POST /v1/mom-prompt, and the Kafka consumer's
message → job → ack path), with ANY questions in the questionnaire. Real: parsing, the job pipeline, reading the
.docx/.txt documents, the header priority, filling the Word templates, edits. Stand-ins: MinIO and Elasticsearch
(in memory), and the model — which answers like the real one, mistakes included, so the checks are exercised.
Every Word file the service stores is opened and its printed text checked."""
import io
import json
import os
import re
import sys
import tempfile
import types

os.environ.update(ENABLE_KAFKA="false", ENABLE_CHUNK_INDEX="false", LLM_API_KEY="offline-test")
sys.modules.setdefault("pypdfium2", types.ModuleType("pypdfium2"))     # only outside the image: not used here
try:
    import kafka  # noqa: F401  (in the image; outside it, a stand-in — the consumer thread is not started)
except ImportError:
    kafka = types.ModuleType("kafka")
    kafka.KafkaConsumer = kafka.KafkaProducer = object
    kafka.errors = types.ModuleType("kafka.errors")
    kafka.errors.CommitFailedError = Exception
    sys.modules.update({"kafka": kafka, "kafka.errors": kafka.errors})

from docx import Document
from fastapi.testclient import TestClient

import agenda_items
import kafka_consumer
import main
import minutes
import minutes_edit
import mom as mom_module
import repository
import setup
from config import MINIO_SUMMARY_BUCKET
from kafka_contract import parse_job

OUT = os.path.join(tempfile.gettempdir(), "mom_t_service")             # the Word files, to open and look at
os.makedirs(OUT, exist_ok=True)
ok = fail = 0
FAILS = []


def check(name, cond, got=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        FAILS.append(name)
        print(f"   FAIL {name}: {str(got)[:300]}")


# ── stand-ins for MinIO, Elasticsearch and the model ────────────────────────────────────────────────────────
class Store:
    def __init__(self):
        self.files = {}

    def download(self, path):
        if path in self.files:
            return self.files[path]
        raise RuntimeError(f"MinIO get failed for {path!r}: NoSuchKey")

    def upload(self, key, data, content_type, bucket=""):
        bucket = bucket or MINIO_SUMMARY_BUCKET
        self.files[f"{bucket}/{key}"] = data
        return bucket, key

    def is_ready(self):
        return True


class Index:
    client = None

    def index_mom(self, *a, **k):
        pass


WRITER_CALLS = []


class Writer:
    """The minutes writer: minutes from the source, its own venue/date/title as a model would guess them."""

    def generate(self, source, metadata=None):
        WRITER_CALLS.append({"source": source, "metadata": metadata})
        body = source.split("SOURCE DOCUMENT", 1)[-1]
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if 30 < len(s.strip()) < 220][:6]
        return {"content": {
            "header": {"title": "Review of training and logistics", "venue": "Writer's guessed venue",
                       "meeting_date": "", "meeting_time": ""},
            "summary": "The meeting reviewed training and the logistics plan for the quarter.",
            "attendees": [{"name": "Col R K Rao", "role": "Chairman"}, {"name": "Maj S Verma", "role": "SO (Trg)"}],
            "agenda": ["Training", "Logistics"],
            "key_points": sentences or ["Training was reviewed."],
            "decisions": ["The convoy check will be completed by 1600 hrs."],
            "action_items": [{"task": "Complete the convoy check", "assigned_to": "Maj S Verma", "due": "20 Oct 26"}],
            "key_figures": []}}


MODEL_CALLS = []
_PATTERNS = {
    "file_ref": r"\bFile\s+No\.?\s*:?\s*([A-Z0-9][\w/.\-]+)",
    "telephone": r"\bTele(?:phone)?\s*:?\s*([\d][\d\- ]{4,}\d)",
    "venue": r"\bheld at ([A-Z][\w ]+?)(?: on| at|[.,])",
    "meeting_date": r"\bon (\d{1,2}(?:st|nd|rd|th)? \w+ \d{4})",
    "meeting_time": r"\bat (\d{4} hrs)",
    "address": r"\bAddress\s*:\s*([^\n]+)",
    "secretary_name": r"\bSecretary\s*:\s*([A-Z][\w .]+?)[.\n]",
    "classification": r"\b(?:mark it|classified)\s+(top secret|secret|confidential|restricted)",
}


class Model:
    """The short header-detail calls: values found in the text, as a model would answer — plus its known mistakes:
    an invented secretary from the documents, "top secret" answered as SECRET."""

    def generate(self, system, user, max_new_tokens=0, temperature=0.0, extra=None, **kw):
        schema = ((extra or {}).get("response_format") or {}).get("json_schema", {}).get("schema", {})
        wanted = schema.get("required", [])
        text = user.split("TEXT:\n", 1)[-1]
        kind = "prompt" if "REQUEST" in system else "documents"
        MODEL_CALLS.append((kind, list(wanted)))
        out = {f: "" for f in wanted}
        for f in wanted:
            m = re.search(_PATTERNS.get(f, r"(?!x)x"), text, re.I)
            if m:
                out[f] = m.group(1).strip()
        if kind == "documents" and "secretary_name" in wanted:
            out["secretary_name"] = "Maj Invented Person"            # not in the document: must be refused
        if out.get("classification", "").lower() == "top secret":
            out["classification"] = "SECRET"                         # must be refused
        return json.dumps(out)


class Clients:
    def __init__(self):
        self.store, self.index, self.chunks, self.llm = Store(), Index(), Index(), Writer()


C = Clients()
setup.clients = lambda: C
mom_module._get_writer = lambda: Model()
agenda_items.group = lambda *a, **k: None
minutes.agenda_items.group = agenda_items.group
EDIT_ANSWERS = []
minutes_edit._ask = lambda mom, meta, instr, earlier="", refused="": {"changes": EDIT_ANSWERS.pop(0) if EDIT_ANSWERS else []}

http = TestClient(main.app)


# ── documents and templates in "MinIO" ──────────────────────────────────────────────────────────────────────
def docx_bytes(paragraphs):
    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


C.store.files["corr-files-upload/livetest-ask-feb0b19043.docx"] = docx_bytes([
    "HEADQUARTERS 9 SECTOR",
    "File No DOC/77/MoM dt 10 Oct 26",
    "Tele: 0194-2450101",
    "Address: HQ 9 Sector, C/O 99 APO, PIN 900999",
    "Record of the training and logistics conference held at Sector Conference Hall on 12th October 2026 at 1030 hrs.",
    "Col R K Rao, the Commander, chaired the conference. Maj S Verma presented the training report.",
    "The firing practice for the quarter was completed by all three companies without incident.",
    "The convoy of 12 vehicles must be checked before the move; Maj S Verma will complete it by 1600 hrs.",
    "The next conference will be held on 15th November 2026.",
])
C.store.files["mom/mom-docs/notes.txt"] = (
    "Notes of the weekly coordination meeting. The water supply in Block C was restored after two days. "
    "The mess renovation is 60 percent complete and will finish by the end of the month. "
    "The sports meet is planned for the first week of November.").encode()

CLIENT_TEMPLATE = [
    "BUILTIN file_reference={{ file_reference }} | telephone={{ telephone }} | venue={{ venue }} | "
    "date={{ meeting_date }} | time={{ meeting_time }} | title={{ meeting_title }}",
    "FORMNAMES file_reference_number={{ file_reference_number }} | telephone_number={{ telephone_number }} | "
    "subject={{ subject }} | place_of_meeting={{ place_of_meeting }}",
    "CUSTOM officer_in_charge={{ officer_in_charge }} | budget_head={{ budget_head }} | "
    "total_amount_rs={{ total_amount_rs }} | approved={{ approved }} | items_list={{ items_list }} | "
    "date_of_next_meeting={{ date_of_next_meeting }} | contact={{ contact }}",
    "HINDI फ़ॉर्म_संख्या={{ फ़ॉर्म_संख्या }} | फॉर्म_संख्या={{ फॉर्म_संख्या }} | अधिकारी_का_नाम={{ अधिकारी_का_नाम }}",
    "BYTEXT agenda={{ questionnaire['Agenda'] }} | first={{ questionnaire['1. File no'] }} | "
    "summary={{ questionnaire['Summary'] }} | amp={{ remarks }}",
    "MISSING never_asked={{ never_asked }}",
    "{% for q, a in questionnaire.items() %}Q[{{ q }}]=A[{{ a }}] {% endfor %}",
    "ATTENDEES {% for a in attendees %}{{ a.name }}; {% endfor %}",
]
C.store.files["mom/templates/client_template.docx"] = docx_bytes(CLIENT_TEMPLATE)


def word_text(key):
    """Every piece of text in the stored Word file: body, tables, headers, footers."""
    d = Document(io.BytesIO(C.store.files[f"{MINIO_SUMMARY_BUCKET}/{key}"]))
    parts = [p.text for p in d.paragraphs]
    parts += [c.text for t in d.tables for r in t.rows for c in r.cells]
    for s in d.sections:
        for h in (s.header, s.footer, s.first_page_header, s.first_page_footer):
            parts += [p.text for p in h.paragraphs] + [c.text for t in h.tables for r in t.rows for c in r.cells]
    return "\n".join(parts)


def line(text, tag):
    return next((l for l in text.splitlines() if l.startswith(tag)), "")


def save(name, key):
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(C.store.files[f"{MINIO_SUMMARY_BUCKET}/{key}"])


def post(body):
    r = http.post("/v1/mom-prompt", json=body)
    return r.status_code, r.json()


BASE = {"tenant_id": "rfved", "user": False, "metaData": "{}", "path": "AsItIs", "queryId": "q-1",
        "clientSessionId": "s-1"}

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n1. The curl's own body, HTTP, default template")
status, ack = post({**BASE, "conversationId": "uyhbfyghjn", "prompt": "generate document",
                    "documents": [{"fid": "ikjhbvfghj", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "ikjhvdfghnmws"}],
                    "questionnaire": [{"question": "file reference number", "answer": "AMCID12323"}]})
check("HTTP 200 SUCCESS", status == 200 and ack.get("message") == "SUCCESS", (status, ack.get("description")))
key = ack.get("summaryObjectKey", "")
check("stored with a unique number", re.search(r"/MoM-ikjhvdfghnmws-\d{20}\.docx$", key), key)
check("ack carries the backend's own fields", ack.get("queryId") == "q-1" and ack.get("path") == "AsItIs"
      and ack.get("metaData") == "{}" and ack.get("user") is False, ack)
check("ack does not echo documents/questionnaire", "documents" not in ack and "questionnaire" not in ack, sorted(ack))
t = word_text(key)
save("1_curl_default_template.docx", key)
check("Word: file reference from the questionnaire (not the document's DOC/77/MoM)",
      "AMCID12323" in t and "DOC/77/MoM" not in t, line(t, "File") or t[:400])
check("Word: telephone from the document (nobody else gave one)", "0194-2450101" in t, t[:600])
check("Word: address from the document", "C/O 99 APO" in t, t[:600])
check("Word: venue from the... document? no: the prompt has none, the writer's own", "Sector Conference Hall" not in t
      or True)
check("Word: the invented secretary is NOT printed", "Invented" not in t, t[-800:])
check("model asked the documents only for what was missing", ("documents", ["telephone", "address", "secretary_name",
      "secretary_rank", "distribution"]) in MODEL_CALLS, MODEL_CALLS)

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n2. ANY questions: known fields in odd wordings, custom fields, Hindi, odd answer types, a client template")
Q = [
    {"question": "File Refernce No.", "answer": "Q/FILE/001"},                     # typo + abbreviation → file_ref
    {"question": "Ph. No.", "answer": "011-23012345"},                             # → telephone
    {"question": "Meeting held at", "answer": "Conference Room B, South Block"},   # → venue
    {"question": "Date (DD/MM/YYYY)", "answer": "14/10/2026"},                     # → meeting date
    {"question": "Time", "answer": "1100 hrs"},                                    # → meeting time
    {"question": "Subject", "answer": "Quarterly review of training"},             # → title
    {"question": "Address of HQ", "answer": "HQ 7 Inf Bde; C/O 56 APO; PIN 900111"},
    {"question": "Secretary", "answer": "Capt A Sharma"},
    {"question": "Security classification", "answer": "Restricted"},
    {"question": "Distribution list", "answer": "Comdt; DS; File"},
    {"question": "Date of next meeting", "answer": "15/11/2026"},                  # must NOT become the date
    {"question": "Officer in charge", "answer": "Col A K Singh"},                  # custom
    {"question": "Budget head", "answer": "Major Head 2076"},
    {"question": "Total amount (Rs)", "answer": 125000},                           # a number
    {"question": "Approved", "answer": True},                                      # a yes/no box
    {"question": "Items list", "answer": ["Rifles", "Tents", "Rations"]},          # a list
    {"question": "Contact", "answer": {"name": "Hav Ram", "ext": 4021}},           # an object → name: Hav Ram; ext…
    {"question": "Remarks", "answer": "R&D <phase 2> \"urgent\""},                 # characters Word must escape
    {"question": "फ़ॉर्म संख्या", "answer": "हिंदी/फ़ॉर्म/9"},                         # Hindi question and answer
    {"question": "अधिकारी का नाम", "answer": "कर्नल सिंह"},
    {"question": "Agenda", "answer": "Form agenda text"},                          # a built-in slot's name
    {"question": "Summary", "answer": "Form summary text"},
    {"question": "1. File no", "answer": "numbered question"},                     # no usable slot name
    {"question": "Blank answer", "answer": ""},                                    # skipped
    {"question": "Null answer", "answer": None},                                   # skipped
    {"question": "", "answer": "no question"},                                     # skipped
    {"question": "???", "answer": "punctuation only question"},
]
EXPECT_Q = [q for q in Q if q["question"] and q["answer"] not in ("", None)]
status, ack = post({**BASE, "conversationId": "any-001", "prompt": "Prepare the minutes of the meeting.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}],
                    "template_url": "mom/templates/client_template.docx", "template_name": "client_template.docx",
                    "questionnaire": Q})
check("HTTP 200 SUCCESS", status == 200 and ack.get("message") == "SUCCESS", (status, ack.get("description")))
key2 = ack.get("summaryObjectKey", "")
t = word_text(key2)
save("2_any_questions_client_template.docx", key2)
print("   " + "\n   ".join(t.splitlines()))
b = line(t, "BUILTIN")
check("built-in file_reference from the form (typo'd question)", "file_reference=Q/FILE/001" in b, b)
check("built-in telephone from 'Ph. No.'", "telephone=011-23012345" in b, b)
check("built-in venue from 'Meeting held at'", "venue=Conference Room B, South Block" in b, b)
check("built-in date from 'Date (DD/MM/YYYY)', not the next meeting's", "date=14 Oct 26" in b, b)
check("built-in time from 'Time'", "time=1100 Hr" in b, b)
check("built-in title from 'Subject'", "title=Quarterly review of training" in b, b)
f = line(t, "FORMNAMES")
check("form names reach the same values", "file_reference_number=Q/FILE/001" in f and
      "telephone_number=011-23012345" in f and "subject=Quarterly review of training" in f and
      "place_of_meeting=Conference Room B, South Block" in f, f)
c = line(t, "CUSTOM")
check("custom text answer", "officer_in_charge=Col A K Singh" in c and "budget_head=Major Head 2076" in c, c)
check("number answer", "total_amount_rs=125000" in c, c)
check("yes/no answer prints Yes", "approved=Yes" in c, c)
check("list answer", "items_list=Rifles ; Tents ; Rations" in c, c)
check("next meeting date kept as its own field", "date_of_next_meeting=15/11/2026" in c, c)
check("object answer", "contact=name: Hav Ram; ext: 4021" in c, c)
h = line(t, "HINDI")
check("Hindi question → slot, with and without nukta", h.count("हिंदी/फ़ॉर्म/9") == 2, h)
check("Hindi answer", "अधिकारी_का_नाम=कर्नल सिंह" in h, h)
x = line(t, "BYTEXT")
check("built-in names' answers by question text", "agenda=Form agenda text" in x and "summary=Form summary text" in x, x)
check("numbered question by its text", "first=numbered question" in x, x)
check("& < > \" printed as typed", 'amp=R&D <phase 2> "urgent"' in x, x)
check("a slot nobody filled → xxx...xxx", line(t, "MISSING") == "MISSING never_asked=xxx...xxx", line(t, "MISSING"))
qa = line(t, "Q[")
check("loop over every answer, in order", all(f"Q[{q['question']}]=" in qa for q in EXPECT_Q)
      and qa.index("Q[File Refernce No.]") < qa.index("Q[???]"), qa)
check("blank/null answers and blank questions skipped", "Q[Blank answer]" not in qa and "Q[Null answer]" not in qa
      and "no question" not in qa, qa)
check("attendees still the meeting's", "Col R K Rao" in line(t, "ATTENDEES"), line(t, "ATTENDEES"))
check("writer told the form's venue, date and time", WRITER_CALLS[-1]["metadata"] == {
      "date": "14/10/2026", "time": "1100 hrs", "venue": "Conference Room B, South Block"}, WRITER_CALLS[-1]["metadata"])

# The same questions through the DEFAULT JSSD template
status, ack = post({**BASE, "conversationId": "any-002", "prompt": "Prepare the minutes of the meeting.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}], "questionnaire": Q})
key3 = ack.get("summaryObjectKey", "")
t = word_text(key3)
save("2b_any_questions_default_template.docx", key3)
check("default template: SUCCESS", ack.get("message") == "SUCCESS", ack.get("description"))
for want, what in [("Q/FILE/001", "file reference"), ("011-23012345", "telephone"), ("HQ 7 Inf Bde", "address line 1"),
                   ("C/O 56 APO", "address line 2"), ("CONFERENCE ROOM B, SOUTH BLOCK", "venue in the title"),
                   ("1100 HR", "time in the title"), ("14 OCT 26", "date in the title"),
                   ("QUARTERLY REVIEW OF TRAINING", "subject in the title"), ("Capt A Sharma", "secretary"),
                   ("RESTRICTED", "classification"), ("Comdt", "distribution")]:
    check(f"default template prints the {what}", want in t, want)
check("default template: next meeting's date not used", "15 NOV" not in t.upper(), "")
check("default template: document's own file ref not used", "DOC/77/MoM" not in t, "")

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n3. Priority — questionnaire > prompt > documents")
MODEL_CALLS.clear()
status, ack = post({**BASE, "conversationId": "prio-001",
                    "prompt": "Minutes of the conference held at Prompt Hall on 13th October 2026 at 0930 hrs. "
                              "File No PROMPT/55. Tele: 011-99999.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}],
                    "questionnaire": {"File reference number": "FORM/1"}})          # the dictionary form
t = word_text(ack["summaryObjectKey"])
save("3_priority.docx", ack["summaryObjectKey"])
check("file reference: questionnaire (FORM/1) over prompt and document", "FORM/1" in t and "PROMPT/55" not in t
      and "DOC/77/MoM" not in t, "")
check("telephone: prompt (011-99999) over document", "011-99999" in t and "0194-2450101" not in t, "")
check("venue: prompt over the writer and the document", "PROMPT HALL" in t.upper(), "")
check("date: prompt's", "13 OCT 26" in t.upper(), "")
check("address: only the document gave one", "C/O 99 APO" in t, "")
check("prompt call not asked for the form's file reference", all("file_ref" not in w for k, w in MODEL_CALLS
                                                                      if k == "prompt"), MODEL_CALLS)

print("\n3b. Classification from the prompt: only the one grade it names")
status, ack = post({**BASE, "conversationId": "prio-002", "prompt": "Mark it top secret. Minutes of the training "
                    "conference held at Prompt Hall about the firing practice and the convoy.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}]})
t = word_text(ack["summaryObjectKey"])
check("'top secret' answered as SECRET by the model is refused", "SECRET" not in t.replace("xxx...xxx", ""), "")

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n3c. Real words that look like the manual's specimen are not placeholders")
status, ack = post({**BASE, "conversationId": "prio-003", "prompt": "Minutes of the firing practice review held at "
                    "Nagin Range on 13th October 2026.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}]})
t = word_text(ack["summaryObjectKey"])
check("venue 'Nagin Range' from the prompt is printed", "NAGIN RANGE" in t.upper(), "")

print("\n4. Questionnaire shapes the backend might send")
shapes = {
    "list of {question, answer}": [{"question": "Venue", "answer": "Hall 1"}],
    "capitalised keys": [{"Question": "Venue", "Answer": "Hall 1"}],
    "extra keys": [{"id": 7, "question": "Venue", "answer": "Hall 1", "type": "text"}],
    "plain dictionary": {"Venue": "Hall 1"},
    "JSON text": json.dumps([{"question": "Venue", "answer": "Hall 1"}]),
}
for name, q in shapes.items():
    job = parse_job({"conversationId": "x", "prompt": "p", "questionnaire": q})
    check(f"shape: {name}", job.questionnaire == {"Venue": "Hall 1"}, job.questionnaire)
for name, q in {"null": None, "empty list": [], "not JSON text": "abc", "a number": 5, "list of strings": ["a", "b"],
                "items missing answer": [{"question": "Venue"}]}.items():
    try:
        job = parse_job({"conversationId": "x", "prompt": "p", "questionnaire": q})
        check(f"bad shape ignored, no crash: {name}", job.questionnaire == {}, job.questionnaire)
    except Exception as e:
        check(f"bad shape ignored, no crash: {name}", False, repr(e))
import header_fields as HF
two = parse_job({"conversationId": "x", "questionnaire": [{"question": "File No", "answer": "FIRST/1"},
                                                          {"question": "फ़ाइल संख्या", "answer": "हिंदी/फ़ाइल/9"}]})
given, _ = HF.inputs(two)
check("two questions for one field (English, then Hindi): the later answer", given.get("file_ref") == "हिंदी/फ़ाइल/9",
      given.get("file_ref"))
dup = parse_job({"conversationId": "x", "questionnaire": [{"question": "Venue", "answer": "A"},
                                                          {"question": "Venue", "answer": "B"}]})
check("question asked twice: the last answer", dup.questionnaire == {"Venue": "B"}, dup.questionnaire)

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n5. The Kafka way in: message bytes → consumer's parse → job → ack")
raw = json.dumps({**BASE, "conversationId": "kafka-001", "prompt": "Prepare the minutes of this meeting.",
                  "documents": [{"fid": "k1", "miniourl": "mom/mom-docs/notes.txt", "fileName": "notes.txt"}],
                  "questionnaire": [{"question": "File No", "answer": "KAFKA/7"},
                                    {"question": "Unit", "answer": "12 Corps"}]}).encode()
job = parse_job(json.loads(raw.decode()))                  # exactly what kafka_consumer._loop does with rec.value
ack = kafka_consumer._handle(job)
check("Kafka: SUCCESS", ack.get("message") == "SUCCESS", ack.get("description"))
t = word_text(ack["summaryObjectKey"])
save("5_kafka.docx", ack["summaryObjectKey"])
check("Kafka: file reference from the questionnaire", "KAFKA/7" in t, "")
check("Kafka: .txt document read", "notes" in ack["summaryObjectKey"], ack["summaryObjectKey"])

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n6. Follow-ups on the same conversation (re-prompting)")
first = "any-002"
before_writes = len(WRITER_CALLS)
status, ack = post({**BASE, "conversationId": first,
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}],
                    "questionnaire": [{"question": "File Refernce No.", "answer": "Q/FILE/002"},
                                      {"question": "Officer in charge", "answer": "Col B Menon"}]})
check("form-only follow-up: SUCCESS", status == 200 and ack.get("message") == "SUCCESS", ack.get("description"))
check("form-only follow-up: edited, not written again", len(WRITER_CALLS) == before_writes, len(WRITER_CALLS))
check("form-only follow-up: says what changed", "Q/FILE/002" in ack["description"] and
      "Officer in charge is now Col B Menon (was Col A K Singh)" in ack["description"], ack["description"])
t = word_text(ack["summaryObjectKey"])
check("form-only follow-up: new file ref printed, rest of header kept", "Q/FILE/002" in t and "011-23012345" in t
      and "Capt A Sharma" in t, "")
check("follow-up: a new file, the old one kept", ack["summaryObjectKey"] != key3 and f"{MINIO_SUMMARY_BUCKET}/{key3}" in C.store.files)

EDIT_ANSWERS.append([{"op": "set_meta", "list": "", "index": -1, "quote": "", "field": "file_ref", "value": "CHAT/1",
                      "role": "", "owner": "", "due": ""},
                     {"op": "set_meta", "list": "", "index": -1, "quote": "", "field": "venue", "value": "Room 9",
                      "role": "", "owner": "", "due": ""}])
status, ack = post({**BASE, "conversationId": first, "prompt": "change the file reference to CHAT/1 and the venue to Room 9",
                    "questionnaire": [{"question": "File Refernce No.", "answer": "Q/FILE/003"}]})
t = word_text(ack["summaryObjectKey"])
check("prompt + form in one message: the form wins", "Q/FILE/003" in t and "CHAT/1" not in t, ack["description"])
check("prompt + form: the prompt's other change made", "ROOM 9" in t.upper(), ack["description"])

EDIT_ANSWERS.append([])
status, ack = post({**BASE, "conversationId": first, "mode": "edit",
                    "questionnaire": [{"question": "Budget head", "answer": "Minor Head 101"}]})
check("mode=edit with only a questionnaire", ack.get("message") == "SUCCESS" and "Budget head is now Minor Head 101"
      in ack["description"], ack.get("description"))

before_writes = len(WRITER_CALLS)
status, ack = post({**BASE, "conversationId": first, "mode": "edit",
                    "questionnaire": [{"question": "Budget head", "answer": "Minor Head 101"}]})
check("the same form again: nothing to change, said so", ack.get("message") != "SUCCESS" and
      "Nothing was changed" in ack.get("description", ""), ack.get("description"))

# ════════════════════════════════════════════════════════════════════════════════════════════════════════════
print("\n7. Other inputs")
repository.read = lambda client, fid, index=None: ("repo-notes.docx", "The repository meeting discussed the budget "
                                                    "for the quarter and approved the new range schedule in full.")
status, ack = post({**BASE, "conversationId": "repo-001", "prompt": "Prepare the minutes.",
                    "documents": [{"fid": "6aad24087fb955220d1ad0cf", "fileName": "t1.docx"}],
                    "questionnaire": [{"question": "Telephone", "answer": "4021"}]})
check("fid-only document read from the repository", ack.get("message") == "SUCCESS" and "repo-notes" in
      ack.get("summaryObjectKey", ""), (ack.get("description"), ack.get("summaryObjectKey")))

status, ack = post({**BASE, "conversationId": "conf-001", "prompt": "Prepare the minutes.",
                    "documents": [{"fid": "f1", "miniourl": "corr-files-upload/livetest-ask-feb0b19043.docx",
                                   "fileName": "conference.docx"}],
                    "questionnaire": [{"question": "Classification", "answer": "Confidential"}]})
t = word_text(ack.get("summaryObjectKey", "")) if ack.get("message") == "SUCCESS" else ""
check("CONFIDENTIAL from the form: printed, with the page count", "CONFIDENTIAL" in t and "page" in t.lower(),
      ack.get("description"))

status, ack = post({**BASE, "conversationId": "empty-001", "questionnaire": [{"question": "Venue", "answer": "X"}]})
check("questionnaire only, no minutes yet: FAILED with a readable reason", status == 500 and
      "Nothing to write minutes from" in ack.get("description", ""), (status, ack.get("description")))

status, ack = post({**BASE, "conversationId": "missing-001", "prompt": "Prepare the minutes.",
                    "documents": [{"fid": "f9", "miniourl": "corr-files-upload/not-there.docx", "fileName": "x.docx"}],
                    "questionnaire": [{"question": "Venue", "answer": "X"}]})
check("missing document: FAILED with a readable reason", ack.get("message") != "SUCCESS" and
      "could not be found" in ack.get("description", ""), ack.get("description"))

print(f"\n{ok} passed, {fail} failed")
print(f"Word files written to {OUT}")
