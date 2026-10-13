"""Make a MoM on this computer, with no cluster, no MinIO and no model key, and open the Word file.

    docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -e LLM_API_KEY=offline-test \
      -v "$PWD/app:/app" -v "$PWD/tests:/t:ro" -v "$PWD:/repo" -w /app \
      --entrypoint python mom-prompt-service:local /repo/tools/try_locally.py \
      --file /repo/scanned-transcripts/scanned-transcript-05min.pdf \
      --prompt "Prepare the minutes. Tele: 0194-2450101, Venue: Station HQ" \
      --screen '{"meeting_date": "22 Sep 26"}'

The whole job runs for real — the document is read (OCR included), the header values are collected, the ITEMs are
grouped, the Word file is written — except that the model is a STAND-IN (tests/fake_model.py) unless --real is
given, so nothing is sent anywhere and nothing is paid for. The minutes it writes are therefore nonsense; what
this shows is the LAYOUT, the HEADER VALUES and where each of them came from. With --real (and a key in
LLM_API_KEY) the minutes are the real thing. The .docx lands in out/ beside the repo.
"""
import argparse
import io
import json
import logging
import os
import sys

sys.path.insert(0, "/t")                      # tests/, for the stand-in model
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/tests")

p = argparse.ArgumentParser()
p.add_argument("--file", help="the meeting's document: PDF, DOCX, DOC or TXT")
p.add_argument("--prompt", default="Prepare the minutes of this meeting.")
p.add_argument("--screen", default="{}", help="the screen's fields as JSON, e.g. '{\"venue\": \"Room B\"}'")
p.add_argument("--template", help="an HQ template (.docx) to lay the minutes out with")
p.add_argument("--real", action="store_true", help="use the real model (needs LLM_API_KEY and credit)")
p.add_argument("--out", default="/repo/out")
a = p.parse_args()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
for noisy in ("httpx", "elastic_transport", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

import mom as mom_module

if not a.real:
    import fake_model
    from llama.core.llm_manager import LLMManager
    writer = LLMManager()
    writer.generate = fake_model.generate
    mom_module._get_writer = lambda: writer

import minutes
from kafka_contract import parse_job


class Store:
    """MinIO, in memory: the document goes in, the minutes come out."""

    def __init__(self):
        self.files = {}

    def download(self, url):
        for key in (url, url.split("/", 1)[-1]):
            if key in self.files:
                return self.files[key]
        raise RuntimeError(f"NoSuchKey: {url}")

    def upload(self, key, data, mime):
        self.files[key] = data
        return "mom", key


class Quiet:
    def index_mom(self, *args, **kw):
        pass


class Clients:
    def __init__(self):
        self.store, self.index, self.chunks = Store(), Quiet(), Quiet()
        self.llm = mom_module.MomGenerator()


c = Clients()
msg = {"tenant_id": "local", "conversationId": "local-1", "prompt": a.prompt,
       "mom_meta": json.loads(a.screen), "fileName": os.path.basename(a.file) if a.file else ""}
if a.file:
    raw = open(a.file, "rb").read()
    c.store.files["local-1/" + os.path.basename(a.file)] = raw
    msg["file_urls"] = ["mom/local-1/" + os.path.basename(a.file)]
    print(f"document: {a.file} ({len(raw)/1048576:.1f} MB)")
if a.template:
    c.store.files["templates/" + os.path.basename(a.template)] = open(a.template, "rb").read()
    msg["template_url"] = "mom/templates/" + os.path.basename(a.template)

ack = minutes.process(parse_job(msg), c)
print("\n" + ack["message"], "-", ack.get("description", "")[:200])
if ack.get("summaryObjectKey"):
    os.makedirs(a.out, exist_ok=True)
    name = os.path.join(a.out, ack["summaryObjectKey"].rsplit("/", 1)[-1])
    with open(name, "wb") as f:
        f.write(c.store.files[ack["summaryObjectKey"]])
    print("Word file:", name.replace("/repo", "."), f"({len(c.store.files[ack['summaryObjectKey']])/1024:.0f} KB)")
    import docx
    d = docx.Document(io.BytesIO(c.store.files[ack["summaryObjectKey"]]))
    head = [p.text for p in d.paragraphs if p.text.strip()][:6]
    print("\nthe first lines of the minutes:")
    for line in head:
        print("   ", line)
