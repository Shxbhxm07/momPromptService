# mom-prompt-service

Minutes of Meeting in the client's **JSSD format** from a user's **prompt** and, optionally, a
**document** (PDF, DOCX, DOC or TXT). **No audio.** The user either describes the meeting in the prompt,
or attaches notes/a report and says what they want, or both.

It lives in its own folder (`/home/admin/mom-prompt-service`), separate from `~/offline-mom-api` (audio → MoM,
and Hindi⇄English translation), and works
the same way: **Kafka in, MinIO + Elasticsearch, Kafka ack out**, with the same message and ack. The only
new input field is `prompt`. Read `~/offline-mom-api/docs/kafka-contract.md` for the full contract of the audio service.

## How a job runs

```
Kafka mom-prompt.jobs ─┐                                   ┌─▶ Kafka mom-prompt.acks
                       ├─▶ job.process ─▶ same ack ─────────┤
POST /v1/mom-prompt ───┘                                   └─▶ HTTP response body

job.process:  file_urls ─▶ MinIO ─▶ text (PDF text layer; OCR for scanned pages) ─┐
                                                          prompt ─────────────────┴─▶ llama-service /summarize
              minutes ─▶ JSSD .docx ─▶ MinIO {tenant}/summaries/{md5}.docx
                      ─▶ Elasticsearch: record in ELASTIC_INDEX_ATTACHED (id = conversationId)
                                        + chunks in CHUNK_INDEX (their doc-ingest index, never created here)
```

One pod: uvicorn serves HTTP, and `main.py` starts the Kafka consumer as a thread (`ENABLE_KAFKA`).
`GET /` fails if that thread dies, so the pod gets restarted.

`JOB_KIND` is named after the audio service's (`mom` / `translate`) so the three Deployments read alike.
Here `prompt` is the only accepted value: anything else refuses to start, which catches a Deployment
copied from mom-consumer whose topics were changed but not its kind.

## Files

Everything is one flat package in `app/`, the way `Amit-K-Jha/transcription-service` lays out its
`app/` folder — no `core/` or `utils/` subfolders. `app/` is the working directory in the image, so
every module imports its neighbours by plain name (`from config import ...`).

| file | what |
|---|---|
| `app/main.py` | FastAPI: `/`, `/health`, `POST /v1/mom-prompt`; starts the consumer thread |
| `app/kafka_consumer.py` | Kafka loop: one job at a time, paused-partition polling, commit after the ack |
| `app/minutes.py` | the job: read documents, build the text, get minutes, render, store, index, ack |
| `app/kafka_contract.py` | copied from `~/offline-mom-api/api`, **adapted**: `prompt` field; `path` is a file fallback only when it ends `.pdf/.docx/.doc/.txt`, so `"AsItIs"` is ignored |
| `app/config.py` | every setting, from env. Shared names/defaults match `~/offline-mom-api/api/config.py` |
| `app/setup.py` | MinIO, Elasticsearch and llama-service clients, made once on first use (not at startup) |
| `app/document_checker.py` | size limit and file-type check for an attachment, with the message the user gets |
| `app/logger_config.py` | log format and level. Timestamps are UTC; the user reads IST (UTC+5:30) |
| `app/mom.py` | `MomGenerator` (calls /summarize) + `to_mom_response` (**copied** from `~/offline-mom-api/api/core/mom.py`) |
| `app/minio_client.py` | copied **unchanged** from `api/core/storage.py` |
| `app/es_client.py` | copied from `api/core/search_index.py`, minutes only (translation index removed) |
| `app/docx_export.py` | **the JSSD renderer**, copied **unchanged** from `api/utils/docx_export.py` |
| `app/documents.py` | PDF/DOCX/DOC/TXT text extraction with OCR, copied **unchanged** from `api/utils/documents.py` |
| `Dockerfile`, `requirements.txt` | at the repo root, beside `app/` |
| `llama/` | **this service's own minutes writer**, vendored whole from `~/offline-mom-api/llama-service` on 2026-09-15. Its own image, its own Deployment. See below |
| `deploy/openshift.yaml` | 5 manifests: both Deployments, both Services, the Route (3600 s timeout) |
| `docker-compose.yml`, `.env.example` | the whole local test environment, built from this repo alone |

### Why `llama/` is here

On the cluster ONE `llama-service` Deployment used to answer mom-consumer, translate-consumer **and**
this service. The MoM prompt lives in `llama/prompts.py` and has **no environment override** — checked,
there is none — so changing it for documents would change it for recordings too. This service exists to
write minutes from notes and reports, not transcripts, so that change is the whole point of it.

Vendored **whole and unmodified**, so it works from day one. The nine endpoints this service never calls
(translation, speaker mapping, transcript correction, localisation — it uses only `/` and `/summarize`)
are still in there; trimming them is a separate decision, not a prerequisite. It is a pure FastAPI/httpx
proxy — no GPU, no PyTorch, ~408 KB of source, deps `httpx fastapi uvicorn python-multipart` plus the
`wamerican` wordlist the Dockerfile installs.

**Jenkins needs a second job** for it: same repo, build context **`llama/`**, image tag
`mom-prompt-llama`. The service's own job stays at the repo root.

The copied files are copies, not imports, because this service is separate from offline-mom-api. When
one of them changes in `~/offline-mom-api/api` (especially `docx_export.py`), copy the change here too:

```
cp ~/offline-mom-api/api/utils/docx_export.py  app/docx_export.py
cp ~/offline-mom-api/api/utils/documents.py    app/documents.py
cp ~/offline-mom-api/api/core/storage.py       app/minio_client.py
```

Those three are **byte-identical** and stay that way, because they import nothing but `config` —
which is why flattening the tree cost nothing. `mom.py` and `es_client.py` are adapted (the
transcript and translation parts removed), so those two need a diff, not a cp.

**`docx_export.py` has diverged since 2026-09-15**: the placeholder fix below was made HERE first.
The audio service has the same bug, so the copy has to go the other way once, then they are identical
again and the usual direction resumes:

```
cp app/docx_export.py ~/offline-mom-api/api/utils/docx_export.py     # then commit in that repo too
```

## The contract

**In** (Kafka message or HTTP body), the same fields as `mom.jobs`:

- `prompt`: the user's words. Optional if there is a document.
- `file_urls`: MinIO keys `"bucket/path/file.pdf"`, optional if there is a prompt. Blank entries are skipped.
  When `file_urls` is absent, `path` is used instead — but **only** if it ends `.pdf`, `.docx`, `.doc` or `.txt`,
  so the backend's `"path": "AsItIs"` marker never becomes a download. Same as the audio service otherwise.
- `document_names`, `document_ids`, `tenant_id`, `conversation_id` / `conversationId`.
- `mom_meta` (optional): JSSD details no text contains: classification, file_ref, meeting_date,
  meeting_time, venue, secretary, distribution… (see `~/offline-mom-api/docs/kafka-contract.md`).
- Returned **exactly as sent** (same value and JSON type): `accessVar`, `userId`, `isUser`, `user`,
  `path`, `conversationId`, `clientSessionId`, `queryId`, `metaData`, `uploadType`, `grading`, `data`,
  `themes`. `conversationId` is filled from the job; `path` becomes `bucket/key` on success.

A job needs a prompt or a file, at least `MIN_SOURCE_CHARS` (80) characters of text in total (a bare
"make the MoM of yesterday's meeting" is refused; the model would invent the meeting), and at most
`MAX_SOURCE_CHARS`.

**Out**: `fileIds`, `tenantId`, `compareMode`, `action: "save"`, `message: SUCCESS|FAILURE`,
`description` (first 300 characters of the summary, or the reason it failed), and on success
`summaryBucketName` + `summaryObjectKey`. HTTP: 200 SUCCESS; 422 nothing to process; 503 MinIO/Elastic
unreachable; 500 the job failed. The body is always the ack.

## Status (2026-09-14)

- **Full end-to-end run on 2026-09-14, everything real**, through `docker compose up -d` in this
  repo: a 7,211-character meeting transcript (.txt) in MinIO + a prompt, produced on
  `mom-prompt.jobs` in Kafka UI. SUCCESS in **239 s**. The ack carried `path` filled with
  `mom/test/summaries/<md5>.docx` and every backend field returned with its original value and JSON
  type. The .docx (41 KB, 3 tables) opened with the JSSD layout — numbered paragraphs, "The
  following were present:-", a numbered agenda. Elasticsearch `mom-attached/momp-002` held 6
  attendees, 7 agenda items, 18 key points, 8 decisions and 8 action items, which is right for a
  county-commission transcript full of motions and votes.
- Also tested inside the `offline-mom-api:latest` image: all outcomes with MinIO/Elastic faked,
  plus the live server with Kafka and Elastic unreachable.
- One **real** run through the local llama-service (OpenRouter, Llama 3.3 70B), on
  `~/offline-mom-api/Meeting 2026-07-21 09_51 UTC_report.pdf` plus a prompt: SUCCESS in 198 s. Title, date, all 5
  agenda items and the key points matched the PDF; the prompt did not leak into the minutes; decisions
  and action items were empty, which is right for that source (a one-speaker talk).
- Local Kafka, MinIO and Elasticsearch are this repo's own (`docker-compose.yml`), on network
  `mom-prompt_default`. Nothing is shared with `~/offline-mom-api` any more.
- The Kafka message is **identical to the audio service's** (`mom.jobs`): same fields in, same ack out.
  Only `file_urls` points at a document instead of an .mp3, and `prompt` may be added. Confirmed by
  parsing the audio service's own example message here. Topics stay separate (`mom-prompt.*`) so an
  .mp3 never reaches this service and a .pdf never reaches the audio one.
- **Measured against the JSSD manual on 2026-09-15** (`JSSD_VOLUME1_PART2`, Appendix AD p.328-330,
  rules Ch 6 paras 9-19). The skeleton is right: numbering, `The following were present:-`,
  INTRODUCTION, the centred `ITEM I – …` with its bracketed classification, Action/Info headings,
  `Decision.` prefix, amendments-by line, left-aligned signature block, distribution ending in File,
  Arial 12, a real PAGE field, and a 1.30 in left margin (0.5 in + a 0.8 in mirrored gutter) — all
  match the specimen. What does NOT match, and why:
    - **One `ITEM I` only.** AD wants one item per agenda entry (Ch 6 para 16.7), each ending in its
      own decision (16.14). /summarize returns flat lists with no link to the agenda, so `_items()`
      can only make one. Needs a llama-service change; see Next steps 5.
    - **Action/Info columns mostly empty** (16.8, 16.16, mandatory). The renderer already maps
      `action_items[].assigned_to` into Action — the model simply leaves it blank, and `decisions`
      has no owner field at all. Same root cause.
    - **Decisions are past tense** ("Resolution … passed") where 16.9 and 16.15 want the future
      imperative prefaced "It was decided". Prompt-level.
    - **No Secretary in the attendee list** (AD note 3, 16.4 — the secretary is always listed last).
    - Title carries no date, time or venue (AD note 2, mandatory) unless `mom_meta` supplies them.
- **Fixed 2026-09-15**: placeholder answers from the model leaked into the minutes — a real document
  read "…to fund the sheriff's department **by None stated**." `_clean` trapped "none" and "not
  stated" but not "None stated", "TBD" or "to be decided". `_EMPTY` in `docx_export.py` now covers
  that family; "asap", "immediate" and "ongoing" are deliberately kept, being what was actually said.
- **Fully separate since 2026-09-15.** Own repo, own images (both of them), own network, own Kafka,
  MinIO, Elasticsearch and minutes writer. Verified end to end after the split: job `sep-001`,
  7,211-character transcript + prompt + full `mom_meta`, SUCCESS in **231 s**. The ack returned
  `accessVar`, `userId` and `isUser` exactly as sent; the .docx (43 KB, 3 tables) carried the
  CONFIDENTIAL marking, file reference, venue and time in the title, secretary block, distribution
  table and amendments-by date, with no placeholder leak.
- `deploy/openshift.yaml` also gained `strategy: Recreate` and `terminationGracePeriodSeconds: 3600`,
  which mom-consumer has and this did not: without them a redeploy runs two consumers in one group
  and kills a job that is minutes into an LLM call.
- **429 "Too Many Requests" on every call — fixed 2026-09-15.** Not the OpenRouter account, not
  the key, not credits (only $0.04 used), and not too many calls: the very first request of a fresh
  job was refused, 20 times running. Cause: the writer was pinned to ONE host,
  `LLM_PROVIDER_ORDER=DeepInfra`, and sends `allow_fallbacks=false`, so when DeepInfra was
  "rate-limited upstream" nothing could succeed. Proved by calling OpenRouter directly — pinned to
  DeepInfra: 429; same model unpinned: 200. The pin exists for a real reason (unpinned calls land on
  hosts with different quantisation), so it was widened, not dropped: `DeepInfra,AkashML,Parasail`,
  the only hosts matching DeepInfra on BOTH fp8 quantisation and the 131,072 context (Cloudflare is
  fp8 but 24k, too small for a long transcript). Before: 0 of 20 calls succeeded. After, same file
  (`t1.txt`, 13,408 chars): 20 of 20, SUCCESS in 263 s. Then, at the user's request, **DeepInfra
  removed**: the list is now `AkashML,Parasail`. The model never changed — it is Llama 3.3 70B on
  every host; a host is only whose machines run it, which the user had read as a different model. **This only affects local testing** — on
  the cluster the writer talks to watsonx, where there are no providers to pin.
- **Kafka messages are lost on `docker compose down`** — known, not yet fixed. `docker-compose.yml`
  mounts `/var/lib/kafka/data`, but apache/kafka writes to `/tmp/kafka-logs`. MinIO and
  Elasticsearch persist correctly.
- **Not yet**: deployed, run against the cluster's real Kafka/MinIO/Elastic, or tried on
  watsonx (the cluster's model, `ibm/granite-4-h-small`).

## Next steps

1. Jenkins. The code is on GitHub at **`Shxbhxm07/momPromptService`**, branch `main` (pushed
   2026-09-14; SSH remote `git@github.com:Shxbhxm07/momPromptService.git`). Its own repo, holding only
   this service — not a folder inside `momTranscriptionService`. New Jenkins job from that repo,
   **build context = the repo root**, since `Dockerfile` sits beside `app/`.
2. Kafka UI: create topics `mom-prompt.jobs` and `mom-prompt.acks` (1 partition, replication 1, like the others).
3. OCP (trino project): Deployment + Service + Route from `deploy/openshift.yaml`. Env values are the
   same ones mom-consumer uses; passwords from a Secret.
4. Test: upload a PDF to MinIO `mom/mom-docs/`, produce a message on `mom-prompt.jobs` (example below),
   check the ack, the .docx in MinIO, and `GET mom-attached/_doc/<conversationId>` in Kibana.
5. Quality: /summarize's prompt was written for **transcripts**. `job.compose_source` labels the parts
   ("USER'S REQUEST FOR THESE MINUTES", "SOURCE DOCUMENT 1 (name)"). If minutes from documents come out
   wrong on watsonx, the next step is a dedicated endpoint in `~/offline-mom-api/llama-service` with a prompt for
   notes/reports. That costs a llama-service rebuild, so measure first.

```json
{
  "tenant_id": "test", "conversationId": "momp-001", "document_ids": ["momp-001"],
  "prompt": "Prepare the minutes of this meeting. Focus on the decisions and who owns each action.",
  "file_urls": ["mom/mom-docs/notes.pdf"], "document_names": ["notes.pdf"],
  "metaData": "{}", "uploadType": "", "grading": "", "data": null, "themes": "",
  "path": "AsItIs", "user": true, "clientSessionId": "sess-1", "queryId": "q-1"
}
```

## Open questions for the backend developer

- Topic names (`mom-prompt.jobs` / `mom-prompt.acks` are our defaults, settable by env).
- Which of `metaData`, `uploadType`, `grading`, `data`, `themes`, `user` we should fill, and with what
  (today they come back unchanged).
- Whether `path` should be filled with the stored file (current behaviour, as agreed for the audio
  service) or returned unchanged.

## The test cluster (OpenShift, web console)

- Our deployments are in project **trino**: transcribe-api, mom-consumer, translate-consumer, whisper
  (pod label `app=whisper`, Service `whisper-server:8080`), llama (Service `llama-service:8001`, on
  IBM watsonx). **No GPU.**
- Kafka, MinIO and Elasticsearch are in project **teamsync**:
  `kafka-service.teamsync.svc.cluster.local:9092`, `minio-service.teamsync.svc.cluster.local:9000`
  (bucket `mom`), `elasticsearch-service.teamsync.svc.cluster.local:9200` (single node, so replicas 0;
  `mom_v1` was created by hand in Kibana with their `e5-teamsync-faq-pipeline`).
- The audio service's images are built by Jenkins from GitHub `Shxbhxm07/momTranscriptionService`
  (branch main). This service is **not** in that repo; the user asked for it to be separate, and confirmed
  that again on 2026-09-14 when a merge into it was offered. It lives in `Shxbhxm07/momPromptService`.
- The layout follows `Amit-K-Jha/transcription-service` (private; the user showed it as a screenshot),
  which the user picked as the reference: one flat `app/` folder, `Dockerfile` and `requirements.txt`
  at the repo root.
- Test messages are produced in **Kafka UI**. Pod logs are in UTC; the user is on IST (UTC+5:30).
- An OpenShift route cuts a request off at its timeout (30 s by default); ours set 3600 s.

## Working with this user

- Plain, simple English, short. Give exact values and **UI click paths** (OCP console, Kafka UI, MinIO
  console), not CLI commands. Give times in IST.
- Before a fix: a short pros/cons table, pick one, apply it in one go. One change at a time.
- They often ask for a one-line Zoho Sprint entry or a Teams update: keep those to one or two lines.
- Commit and push to GitHub when a piece of work is done; they rebuild from it in Jenkins.
  This repo is `Shxbhxm07/momPromptService`, branch `main`; the audio one is `Shxbhxm07/momTranscriptionService`.
- **Never put credentials in files.** The MinIO and Elastic passwords and the IBM key have been pasted
  in chat before; they belong in OpenShift Secrets. Do not send the user's email to any service.

## Testing locally

`docker compose up -d` is the whole environment: this service, MinIO, Elasticsearch, Kafka, Kafka UI
and a minutes writer, on their own network (`mom-prompt_default`), with nothing borrowed from
`~/offline-mom-api`. The key goes in `.env` (gitignored; `cp .env.example .env`), never in a
committed file. Ports: service 8010, llama 8011, Kafka UI 8090, MinIO 9000/9001, Elastic 9200,
Kafka 29092.

**Nothing is borrowed any more.** Both images are built from this repo: `Dockerfile` (the service)
and `llama/Dockerfile` (the minutes writer). `LLAMA_URL` + `--scale llama=0` points at an existing
llama-service instead, if you ever want that.

### The older way, mounting into the audio image

The `offline-mom-api:latest` image has every dependency (tesseract, LibreOffice, minio, elasticsearch,
kafka-python), so mount this folder into it:

```bash
docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -v "$PWD/app:/app" -w /app \
  --entrypoint python offline-mom-api:latest your_check.py
```

The local llama-service is the container `offline-mom-llama` on network `offline-mom-api_default`
(`LLAMA_URL=http://offline-mom-llama:8001`). It calls OpenRouter, which costs credits, so check with fakes
first and make real calls deliberately.
