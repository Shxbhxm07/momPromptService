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
| `deploy/openshift.yaml` | Deployment + Service + Route (with the 3600 s timeout), `JOB_KIND=prompt` |

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

- Built and tested **locally only**, inside the `offline-mom-api:latest` image (same dependencies):
  all outcomes with MinIO/Elastic faked, plus the live server with Kafka and Elastic unreachable.
- One **real** run through the local llama-service (OpenRouter, Llama 3.3 70B), on
  `~/offline-mom-api/Meeting 2026-07-21 09_51 UTC_report.pdf` plus a prompt: SUCCESS in 198 s. Title, date, all 5
  agenda items and the key points matched the PDF; the prompt did not leak into the minutes; decisions
  and action items were empty, which is right for that source (a one-speaker talk).
- The Kafka message is **identical to the audio service's** (`mom.jobs`): same fields in, same ack out.
  Only `file_urls` points at a document instead of an .mp3, and `prompt` may be added. Confirmed by
  parsing the audio service's own example message here. Topics stay separate (`mom-prompt.*`) so an
  .mp3 never reaches this service and a .pdf never reaches the audio one.
- **Not yet**: built as its own image, deployed, run against real Kafka/MinIO/Elastic, or tried on
  watsonx (the cluster's model, `ibm/granite-4-h-small`).

## Next steps

1. Push to GitHub. The repo is **initialised and committed locally** on `main`, identity
   `Shxbhxm07 <claude8@appolosys.com>`, SSH to GitHub already works. It needs an **empty** repo of its own —
   the user chose a separate repo holding only this service, NOT a folder inside `momTranscriptionService`.
   Then build this folder's `Dockerfile` in Jenkins (build context = this folder).
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
  that again on 2026-09-14 when a merge into it was offered.
- Test messages are produced in **Kafka UI**. Pod logs are in UTC; the user is on IST (UTC+5:30).
- An OpenShift route cuts a request off at its timeout (30 s by default); ours set 3600 s.

## Working with this user

- Plain, simple English, short. Give exact values and **UI click paths** (OCP console, Kafka UI, MinIO
  console), not CLI commands. Give times in IST.
- Before a fix: a short pros/cons table, pick one, apply it in one go. One change at a time.
- They often ask for a one-line Zoho Sprint entry or a Teams update: keep those to one or two lines.
- Commit and push to GitHub when a piece of work is done; they rebuild from it in Jenkins. (Local repo
  exists; the GitHub remote is not added yet — see Next steps.)
- **Never put credentials in files.** The MinIO and Elastic passwords and the IBM key have been pasted
  in chat before; they belong in OpenShift Secrets. Do not send the user's email to any service.

## Testing locally

The `offline-mom-api:latest` image has every dependency (tesseract, LibreOffice, minio, elasticsearch,
kafka-python), so mount this folder into it:

```bash
docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -v "$PWD/app:/app" -w /app \
  --entrypoint python offline-mom-api:latest your_check.py
```

The local llama-service is the container `offline-mom-llama` on network `offline-mom-api_default`
(`LLAMA_URL=http://offline-mom-llama:8001`). It calls OpenRouter, which costs credits, so check with fakes
first and make real calls deliberately.
