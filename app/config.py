"""Settings, all from the environment. Where a setting is shared with the audio service, the name and
the default are the same as in ~/offline-mom-api/api/config.py, so one set of cluster values serves both."""
import os
import re

# ── the minutes writer ─────────────────────────────────────────────────────────
# The writer runs in this process (the `llama` package); its endpoint, model, credentials and budgets
# are read by llama/config.py — VLLM_API_BASE, LLM_MODEL_PATH, LLM_AUTH_MODE, CP4D_*, WATSONX_API_KEY
# and the rest. Only the one setting this service passes to it on each call lives here. There is no
# LLAMA_URL any more: nothing is reached over HTTP.
MOM_TEMPERATURE = float(os.getenv("MOM_TEMPERATURE", "0.05"))
# At most this many discussion points per ITEM survive (essence.py). OFF (0) since 2026-09-19, by the user's
# rule: "it does not matter if the MoM is long or short, my accuracy levels must be 100" — the step deleted
# real, quote-checked points to keep a few per ITEM (Ch 6 para 10's "only the essence"). 0 prints every point
# the writer verified. A number >0 brings the shortening back, and with it the loss of points.
ESSENCE_POINTS_PER_ITEM = int(os.getenv("ESSENCE_POINTS_PER_ITEM", "0"))

# ── what a job may carry ───────────────────────────────────────────────────────
# Below this many characters of prompt + document text there is no meeting to write up, and the model
# would invent one. "Make the minutes of yesterday's meeting" is a request, not a source.
MIN_SOURCE_CHARS = int(os.getenv("MIN_SOURCE_CHARS", "80"))
# Above this the job is refused rather than sent: llama-service splits long text and calls the model per
# part, so a 300-page file would be dozens of calls for minutes nobody asked for. ~75k tokens.
MAX_SOURCE_CHARS = int(os.getenv("MAX_SOURCE_CHARS", "300000"))
MAX_DOC_MB = int(os.getenv("MAX_DOC_MB", "50"))

# ── reading documents (utils/documents.py) ─────────────────────────────────────
# OCR is Tesseract (the tesseract-ocr package in the image, with English and Hindi data), run in this
# pod. It reads only the PDF pages that have no text of their own — a scan — so a typed PDF never
# touches it. Every setting below can be changed in the Deployment's env; /health shows what is in force.
# Off: a scanned page is skipped, and a PDF that is nothing but scans fails with a sentence saying so.
ENABLE_OCR = os.getenv("ENABLE_OCR", "true").lower() == "true"
# A page with fewer characters of its own text than this is treated as a scan. Not 0: a scanner often
# stamps a line (a date, a page number) onto an otherwise image-only page.
MIN_PAGE_TEXT_CHARS = int(os.getenv("MIN_PAGE_TEXT_CHARS", "40"))
# The resolution a scanned page is rendered at before it is read. Below 300, Hindi vowel signs start to
# drop out; above it, each page takes longer for no measured gain.
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
# ENGLISH FIRST. Tesseract treats the first language as the main one, and with "hin+eng" it lost the
# digit 1 on scanned pages — "16 Sep 26" read "6 Sep 26", "1030 hr" read "030 hr", "12 Corps" read
# "l2 Corps": 9 of 13 numbers right on an English scan, 6 of 8 on a Hindi one. "eng+hin" read every
# word and number on both, clean and noisy scans alike (measured 2026-09-19, tesseract 5.5.0).
# Tesseract wants the codes joined by "+"; "eng, hin" or "eng hin" typed into the console is joined
# here, since "hin,eng" reaches Tesseract as one language that does not exist and every scan fails.
# Only languages installed in the image work (eng, hin) — /health and the startup log say if not.
OCR_LANGS = "+".join(p for p in re.split(r"[\s,;+]+", os.getenv("OCR_LANGS", "eng+hin")) if p) or "eng+hin"
# The most scanned pages read per document, at about a second each. Later scanned pages are skipped
# with a warning in the log; typed pages are always read.
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "200"))
SOFFICE_TIMEOUT = int(os.getenv("SOFFICE_TIMEOUT", "180"))

# ── MinIO ──────────────────────────────────────────────────────────────────────
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"
# Empty: the first part of each file_urls entry is the bucket ("mom/mom-docs/notes.pdf").
MINIO_INPUT_BUCKET = os.getenv("MINIO_INPUT_BUCKET", "").strip()
# Where the Word minutes go, as {tenant}/summaries/{hash}/MoM-<file name>.docx; returned as summaryBucketName.
MINIO_SUMMARY_BUCKET = os.getenv("MINIO_SUMMARY_BUCKET", "summaries")

# ── Elasticsearch ──────────────────────────────────────────────────────────────
ELASTIC_URL = os.getenv("ELASTIC_URL", "http://elasticsearch:9200")
ELASTIC_USER = os.getenv("ELASTIC_USER", "").strip()
ELASTIC_PASSWORD = os.getenv("ELASTIC_PASSWORD", "").strip()
# The same indices as the audio minutes, so a search over "all minutes" finds these too.
ELASTIC_INDEX_ATTACHED = os.getenv("ELASTIC_INDEX_ATTACHED", "mom-attached")
ELASTIC_INDEX_INGESTED = os.getenv("ELASTIC_INDEX_INGESTED", "mom-ingested")
ELASTIC_CREATE_INDICES = os.getenv("ELASTIC_CREATE_INDICES", "true").lower() == "true"
# The platform's repository index: a file picked "From repository" is read from here, as the chunks the
# platform ingested it into ({fId, text, pageNo, para, fileName}), when it is not in MinIO — see
# repository.py. Read-only; never created here. Empty turns repository reading off.
REPOSITORY_INDEX = os.getenv("REPOSITORY_INDEX", "teamsync_v1").strip()
# Their doc-ingest chunk index (e.g. mom_v1). Never created here; see core/search_index.py.
ENABLE_CHUNK_INDEX = os.getenv("ENABLE_CHUNK_INDEX", "false").lower() == "true"
CHUNK_INDEX = os.getenv("CHUNK_INDEX", "").strip()
CHUNK_WORDS = int(os.getenv("CHUNK_WORDS", "120"))
CHUNK_OVERLAP_WORDS = int(os.getenv("CHUNK_OVERLAP_WORDS", "30"))
MIN_CHUNK_WORDS = int(os.getenv("MIN_CHUNK_WORDS", "15"))
MAX_CHUNK_CHARS = int(os.getenv("MAX_CHUNK_CHARS", "1200"))

# ── Kafka ──────────────────────────────────────────────────────────────────────
# The consumer runs as a thread in the web process. ENABLE_KAFKA=false leaves only the HTTP endpoint.
ENABLE_KAFKA = os.getenv("ENABLE_KAFKA", "true").lower() == "true"
# Which job this consumer takes, named the same way as the audio service's JOB_KIND (mom /
# translate) so the three Deployments read alike. This service only writes minutes from a prompt,
# so "prompt" is the only accepted value — it exists to catch a Deployment that was copied from
# mom-consumer and had its topics changed but not this, which would otherwise sit on
# mom-prompt.jobs quietly doing the wrong thing.
JOB_KIND = os.getenv("JOB_KIND", "prompt").strip().lower()
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
KAFKA_JOB_TOPIC = os.getenv("KAFKA_JOB_TOPIC", "mom-prompt.jobs")
KAFKA_ACK_TOPIC = os.getenv("KAFKA_ACK_TOPIC", "mom-prompt.acks")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "mom-prompt-consumer")
KAFKA_MAX_POLL_INTERVAL_MS = int(os.getenv("KAFKA_MAX_POLL_INTERVAL_MS", str(20 * 60 * 1000)))

# ── The acknowledgement ────────────────────────────────────────────────────────
# `message` on a failed job. The IMIR backend's ack reads SUCCESS or FAILED (2026-09-16); the audio
# service sends FAILURE. A setting, so a backend that expects the other word needs no rebuild.
ACK_FAILURE_MESSAGE = os.getenv("ACK_FAILURE_MESSAGE", "FAILED").strip() or "FAILED"
# A job that arrives over HTTP ALSO publishes its ack to KAFKA_ACK_TOPIC. The IMIR backend listens on
# mom-prompt.acks for the result, and on 2026-09-16 it was submitting jobs over HTTP — so without this
# the finished minutes never reached it. A job that arrived on Kafka acks there anyway, so no job is
# ever acknowledged twice on the topic.
HTTP_ACKS_TO_KAFKA = os.getenv("HTTP_ACKS_TO_KAFKA", "true").lower() == "true"
