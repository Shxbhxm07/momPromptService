"""Faster, same minutes (2026-09-22): the main read's chunks side by side, the windows and the decisions / actions /
figures reads beside the main read, one gate of LLM_CONCURRENCY calls for the pod, scanned pages read OCR_WORKERS at
a time, and a request identical to one already running waits for it instead of doing the work again."""
import json
import os
import sys
import threading
import time

sys.path.insert(0, "/t")
import fake_model as F
import minutes
from kafka_contract import parse_job
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


def minutes_with(text, lanes):
    lm.LLM_CONCURRENCY = lm.WINDOW_CONCURRENCY = lanes
    m = lm.LLMManager()
    m.generate = F.generate
    t0 = time.time()
    return m.generate_mom(text, 0.0)["content"], time.time() - t0


print("the same minutes, one call at a time or eight")
for name in ("/repo/scanned-transcripts/printed-text/scanned-transcript-45min.txt", "/repo/t5.txt"):
    if not os.path.exists(name):
        print(f"  (skipped: {name} not mounted)")
        continue
    text = open(name, encoding="utf-8", errors="ignore").read()
    one, t1 = minutes_with(text, 1)
    eight, t8 = minutes_with(text, 8)
    check(f"{name.rsplit('/', 1)[-1]}: identical minutes ({len(eight.get('key_points') or [])} points), "
          f"{t1:.1f}s → {t8:.1f}s", json.dumps(one, sort_keys=True) == json.dumps(eight, sort_keys=True))
    check(f"  and faster", t8 < t1 * 0.6, (t1, t8))
lm.LLM_CONCURRENCY = lm.WINDOW_CONCURRENCY = 8

print("the gate: never more calls in flight than the setting")


class Response:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


class Client:
    def __init__(self):
        self.now = self.most = 0
        self.lock = threading.Lock()

    def post(self, *a, **k):
        with self.lock:
            self.now += 1
            self.most = max(self.most, self.now)
        time.sleep(0.05)
        with self.lock:
            self.now -= 1
        return Response()


lm._INFLIGHT = threading.BoundedSemaphore(3)
m = lm.LLMManager()
m.client = Client()
threads = [threading.Thread(target=m.generate, args=("s", f"u{i}", 10, 0.0)) for i in range(20)]
[t.start() for t in threads]
[t.join() for t in threads]
check("20 calls from 20 threads, gate 3 → at most 3 at once", m.client.most == 3, m.client.most)

print("scanned pages read several at a time, same text")
import documents
SCAN = "/repo/scanned-transcripts/scanned-transcript-15min.pdf"
if os.path.exists(SCAN):
    raw = open(SCAN, "rb").read()
    documents.OCR_WORKERS = 1
    t0 = time.time()
    one = documents.extract_text_blocks(raw, "scan.pdf")
    t1 = time.time() - t0
    documents.OCR_WORKERS = 4
    t0 = time.time()
    four = documents.extract_text_blocks(raw, "scan.pdf")
    t4 = time.time() - t0
    check(f"{one.pages} scanned pages: identical text, in page order ({t1:.0f}s → {t4:.0f}s)", one.blocks == four.blocks)
    check("  and faster", t4 < t1 * 0.7, (t1, t4))
else:
    print(f"  (skipped: {SCAN} not mounted)")

print("the same request twice: done once")
runs = []


def slow(job, c):
    runs.append(job.prompt)
    time.sleep(0.5)
    from kafka_contract import build_ack
    return build_ack(job, success=True, bucket="mom", object_key=f"t/summaries/{len(runs)}/MoM-a.docx",
                     description="The meeting covered the water supply.")


minutes._process = slow
base = {"tenant_id": "t", "conversationId": "c1", "prompt": "Standard minutes", "file_urls": ["mom/c1/f"]}
acks = [None, None, None]


def send(k, extra):
    acks[k] = minutes.process(parse_job({**base, **extra}), None)


threads = [threading.Thread(target=send, args=(0, {"queryId": "q1"})),
           threading.Thread(target=send, args=(1, {"queryId": "q2"})),
           threading.Thread(target=send, args=(2, {"prompt": "Different minutes", "queryId": "q3"}))]
threads[0].start()
time.sleep(0.1)
threads[1].start()
threads[2].start()
[t.join() for t in threads]
check("'Try again' while running: the work is done once", runs.count("Standard minutes") == 1, runs)
check("  both get the same MoM", acks[0]["summaryObjectKey"] == acks[1]["summaryObjectKey"], acks)
check("  each with its own fields back", acks[0].get("queryId") == "q1" and acks[1].get("queryId") == "q2", acks)
check("  a different request is not held back", "Different minutes" in runs and acks[2]["message"] == "SUCCESS")
minutes.process(parse_job({**base}), None)
check("the same request AFTER the first finished runs again (nothing is cached)", runs.count("Standard minutes") == 2, runs)

print(f"\n{ok} passed, {fail} failed")
