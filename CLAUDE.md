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
                                                          prompt ─────────────────┴─▶ minutes writer, IN PROCESS (app/llama)
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
| `app/setup.py` | MinIO, Elasticsearch and minutes-writer clients, made once on first use (not at startup) |
| `app/document_checker.py` | size limit and file-type check for an attachment, with the message the user gets |
| `app/template_details.py` | the official JSSD **template** → the HQ's standing details (gate, one model call, grounding, specimen filter, merge, cache). See *Templates* |
| `app/agenda_items.py` | sorts the verified points, decisions and figures under the agenda items so the minutes carry **ITEM I, II, III** as Appendix AD draws them. Index numbers only — it never writes text |
| `app/logger_config.py` | log format and level. Timestamps are UTC; the user reads IST (UTC+5:30) |
| `app/mom.py` | `MomGenerator` (calls /summarize) + `to_mom_response` (**copied** from `~/offline-mom-api/api/core/mom.py`) |
| `app/minio_client.py` | copied **unchanged** from `api/core/storage.py` |
| `app/es_client.py` | copied from `api/core/search_index.py`, minutes only (translation index removed) |
| `app/docx_export.py` | **the JSSD renderer**, copied **unchanged** from `api/utils/docx_export.py` |
| `app/documents.py` | PDF/DOCX/DOC/TXT text extraction with OCR, copied **unchanged** from `api/utils/documents.py` |
| `Dockerfile`, `requirements.txt` | at the repo root, beside `app/` |
| `app/llama/` | **the minutes writer, a package inside the service** — what used to be llama-service. Called in process by `app/mom.py`: no HTTP, no second pod. See below |
| `deploy/openshift/01-deployment.yaml` … `03-route.yaml` | **the deployment, in apply order** — the ONE Deployment, its Service, the Route (3600 s timeout). Every setting a plain env value, **no Secret object** (the user's choice); credentials are `CHANGE_ME` in the repo |
| `docker-compose.yml`, `.env.example` | the whole local test environment, built from this repo alone |

### ONE service — why the writer is a package, not a pod

**The user's rule, stated 2026-09-14 and enforced 2026-09-15: everything in one service** — one image,
one Deployment, one pod, like `Amit-K-Jha/transcription-service`. For a day the writer was split out as a
second Deployment (`mom-prompt-llama`) that the service called over HTTP via `LLAMA_URL`; that broke the
rule and was undone. **Do not reintroduce a second pod, a second image, or `LLAMA_URL`.**

`app/llama/` is the former llama-service, merged in. `app/mom.py` builds one `LLMManager` on first use
and calls `generate_mom()` directly — the exact function the old `/summarize` endpoint wrapped — so the
result shape reaching `to_mom_response` is unchanged. Merged, not rewritten:
- Moved **with its folder structure intact**, because it finds `lexicon/` and `localization/glossary.json`
  relative to its own files.
- Its internal imports carry a `llama.` prefix (`from llama.config import …`), so `config` still means the
  service's `app/config.py` everywhere else. The two configs read **no common environment variable**
  (checked), so neither can silently set the other.
- Dropped: its FastAPI `main.py`, its Dockerfile, `models/schemas.py` and `core/translation_cache.py` —
  reachable only from that web server.
- Safe in the consumer's worker thread: the minutes path is synchronous (only translation was async) and
  has no Python 3.11-only syntax, so it runs on the service's 3.10 image.
- The image gains `httpx==0.28.1` (the version it ran on) and the `wamerican` wordlist its term-corrector
  guard reads. Measured after a full job: the merged process sits at ~85 MiB, so resources are unchanged.
- `/health` reports the writer as `configured` (endpoint + credential set), not `reachable`: a real model
  round-trip on every probe would cost time and tokens, and a job surfaces a bad credential anyway.

Why it is vendored at all: the MoM prompt in `app/llama/prompts.py` has **no environment override**, so
tuning it for documents rather than transcripts needs this repo's own copy.

**Jenkins: ONE job**, build context the repo root, image `mom-prompt-service`.

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

## Templates — what they contribute, and why only that (decided 2026-09-15)

A JSSD template (Appendix AD) holds: **layout** fixed by the manual (already in `docx_export.py`), **specimen
placeholders** ("Telephone number here", "FIRING PRACTICE", dotted lines), and the **issuing HQ's standing
details** (address, telephone, file reference, signature block, distribution — Appendix AD explanatory note 1).
Only the last differs between one unit's template and another's, so `app/template_details.py` extracts only that,
and the minutes' layout still comes from the manual.

Options weighed, and why they lost: tagged `{{placeholders}}` (official templates have none); filling the Word
file in place (a second renderer, cannot read PDF templates, and only differs from ours on templates that break
the manual — the ones rules cannot parse); the model rewriting the document (loses classification marks, watermark,
page count); feeding template text to the writer (**its specimen text would pass the grounding check and reach the
minutes**). **Do not add the template to `compose_source`.**

Measured before building: a unit template gave the same 11 correct values as DOCX and as PDF (the PDF distribution
table flattens to one line, which fixed rules cannot split). But the model, told to skip placeholders, returned
"Telephone number here" and "Addressee 1" from a blank specimen, and a company's street address from a non-JSSD
document. So three guards, none optional: a **gate** (≥3 of the manual's mandated phrases), **grounding** (every
value word for word in the template), a **specimen filter** in code. Classification and precedence are never taken
from a template. Extraction is one call per distinct template, cached in process by content hash. A template
problem never fails a job; the outcome is in the log and in Elasticsearch (`template_name`, `template_status`
= used | no_details | not_jssd | unreadable | failed, `template_fields`).

Not done, deliberately: a template's non-standard layout, letterhead image or Hindi headings are not copied —
the minutes follow the manual and are English.

## The contract

**In** (Kafka message or HTTP body), the same fields as `mom.jobs`:

- `prompt`: the user's words. Optional if there is a document.
- `file_urls`: MinIO keys `"bucket/path/file.pdf"`, optional if there is a prompt. Blank entries are skipped.
  When `file_urls` is absent, `path` is used instead — but **only** if it ends `.pdf`, `.docx`, `.doc` or `.txt`,
  so the backend's `"path": "AsItIs"` marker never becomes a download. Same as the audio service otherwise.
- `document_names`, `document_ids`, `tenant_id`, `conversation_id` / `conversationId`.
- `mom_meta` (optional): JSSD details no text contains: classification, file_ref, meeting_date,
  meeting_time, venue, secretary, distribution… (see `~/offline-mom-api/docs/kafka-contract.md`).
- `template_url` / `templateUrl`, `template_name` / `templateName` (optional): the issuing HQ's **official
  JSSD minutes template** in MinIO (DOCX, DOC, PDF or TXT). **Not a source document** — nothing in it is
  summarised. It supplies only the HQ's standing details: `telephone`, `address`, `file_ref`, `secretary`,
  `distribution`. The job's own `mom_meta` wins over it field by field. See *Templates* below.
- `fileName` / `file_name` (optional): the uploaded document's name — the same field name the backend's
  chunk index uses. Echoed back like the rest, and **used as the document's name when `document_names` is
  absent**. That is what makes a MinIO object stored without an extension readable: the key stays
  `mom/mom-docs/transcripts` while `fileName` says `transcripts.txt`, and the reader dispatches on the
  name. An explicit `document_names` list wins, since it can name every file.
- Returned **exactly as sent** (same value and JSON type): `accessVar`, `userId`, `isUser`, `user`,
  `path`, `conversationId`, `clientSessionId`, `queryId`, `metaData`, `uploadType`, `grading`, `data`,
  `themes`, `fileName`. `conversationId` is filled from the job; `path` becomes `bucket/key` on success.

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
    - ~~**One `ITEM I` only.**~~ **Fixed 2026-09-16** — `app/agenda_items.py` groups the verified lines
      under the agenda and `_items()` builds one item per entry, so the minutes read ITEM I, II, III…
      with continuous paragraph numbering, as AD draws them (Ch 6 paras 16.7, 16.14).
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
- **Cluster model corrected 2026-09-15: Llama 3.3 70B, not Granite.** This file used to say the
  cluster ran `ibm/granite-4-h-small`, and `deploy/openshift.yaml` had copied that. Both cluster
  overlays of the audio service (`~/offline-mom-api/deploy/openshift/overlays/*/offline-mom.env`)
  actually deploy `meta-llama/llama-3-3-70b-instruct` on **Cloud Pak for Data** (`LLM_AUTH_MODE=cp4d`,
  `https://cpd-watsonx-imir.apps.ocp4.iaf.in`, `LLM_VERIFY_SSL=false`, `MODEL_CONTEXT_LIMIT=32768`),
  and the user wants Llama 70B. The yaml now matches. Note the audio service's filled onboarding form
  says IBM Cloud / `iam` — that form is older and wrong on this point; the overlays are what is applied.
- **Onboarding form filled**: `deploy/Service_Onboarding_Form_mom-prompt-service.xlsx`, from the
  blank template. Two services, 32 environment variables; every value cross-checked against
  `deploy/openshift.yaml` (30 settings, all agree). Secrets are marked and never written into it.
  Still open in it: **Owner Team** and **WATSONX_PROJECT_ID**.
- **Deployment YAML split for DevOps, 2026-09-15**: `deploy/openshift.yaml` became four numbered
  files in `deploy/openshift/`, applied in order (01 Secret, 02 both Deployments, 03 both Services,
  04 Route), env values inline so they paste straight into the OCP console. Settings are identical to
  the single file they replace, which was cross-checked against the onboarding form. Validated with
  kubeconform in strict mode against the Kubernetes and OpenShift 4.15 schemas — 6 resources, 6
  valid — plus 22 cross-file checks: every secretKeyRef names a key the Secret has, every Secret key
  is used, each Service selects exactly one Deployment on a port it opens, and the Route reaches the
  service port. (Superseded the same day by the one-service merge below.) Placeholders DevOps must fill:
  `CHANGE_ME_REGISTRY` (both images), `CHANGE_ME` (six Secret values, `WATSONX_PROJECT_ID`).
- **Merged into ONE service, 2026-09-15** — the user's rule (see *ONE service* above). The separate
  `mom-prompt-llama` pod is gone; the writer runs in process. Verified in the built image: all 13
  service modules and 8 writer modules import, the two configs stay distinct modules, the lexicon,
  glossary and wordlist are found at their new paths (31 of 31). End to end with everything real — job
  `one-001`, `t1.txt` (13,408 chars) + full `mom_meta`: **SUCCESS in 170 s** (263 s through two pods),
  ack with `accessVar`/`userId`/`isUser` intact, .docx with the CONFIDENTIAL marking, file reference,
  venue and secretary, Elasticsearch with 7 attendees, 39 key points, 6 decisions, 10 actions. The
  DevOps YAML is ONE Deployment and ONE Service: 4 resources valid under strict kubeconform, 28 of 28
  cross-file checks. The onboarding form is one service row and 31 variables, matching the YAML.
- **Deployed to OCP 2026-09-15 (project `mom-ai`, not `trino` as the YAML says).** Kafka, MinIO and
  Elasticsearch connected on the first job. The writer did not: `Name or service not known` for
  `cpd-watsonx-imir.apps.ocp4.iaf.in` — **the Cloud Pak host does not resolve from this cluster.** The
  test cluster uses **IBM Cloud watsonx**: `https://us-south.ml.cloud.ibm.com/ml/v1/text/chat?version=2023-05-29`,
  `LLM_AUTH_MODE=iam`, `WATSONX_API_KEY` (Secret), project `7ade6186-5397-4ed2-99f7-dce2cc6eda66`,
  `LLM_VERIFY_SSL=true`. **This corrects an earlier entry here**: the audio service's older onboarding form
  said IBM Cloud / iam and was dismissed in favour of the CP4D overlays — for THIS cluster the form was
  right; the overlays describe the IAF client's network. YAML, Secret and form now say IBM Cloud.
  **Two traps**: (1) `LLM_AUTH_MODE` set twice with the second empty → Kubernetes keeps the last → empty →
  config guesses `cp4d` while `CP4D_AUTH_URL` exists, straight back to the dead host (verified). So
  `CP4D_*` must be removed, not just ignored. (2) An API key retyped from a screenshot fails IAM with
  `BXNIM0415E Provided API key could not be found` — lowercase l and capital I look identical.
- **No Secret object, by the user's decision (2026-09-15).** Every setting — MinIO keys, Elasticsearch
  login and `WATSONX_API_KEY` included — is a plain `env` value in the Deployment. `01-secret.yaml` is
  gone; the files are now `01-deployment`, `02-service`, `03-route`. The repo copy carries `CHANGE_ME` for
  each credential and must never be committed filled in. Consequence the user accepted: anyone who can
  view the Deployment can read them. `imagePullSecrets: dockerocp-secret` STAYS — it is the registry
  pull credential, not an env secret; without it the pod cannot pull `bajpai92/repo:mom-prompt-service…`.
  The repo YAML now also carries the real namespace (`mom-ai`) and that pull secret.
- **Template feature built 2026-09-15** (see *Templates*). Verified: 47 no-AI tests fed with the model's real
  replies (one caught a real bug — the "Tele:" prefix was stripped before the placeholder check, so
  "Telephone number here" survived as "number here"); 7/7 live on MinIO + the model (unit template identical
  from DOCX and PDF, blank specimen → nothing, "About Us" rejected with no model call, missing file and
  spreadsheet → no crash, repeat template from cache in 0.01 s); end to end `tpl-e2e-001` SUCCESS in 135 s,
  30/30 — address, telephone, secretary and distribution from the template, the job's own `file_ref` and
  classification winning over it, meeting content from the prompt, no specimen text in the minutes, and the
  template recorded in Elasticsearch. Sample templates in `templates/`.
- **Cluster, 2026-09-15: working end to end on IBM Cloud watsonx** — `ocp-test-003` (document) and
  `ocp-combo-001` (template + prompt + transcript) both SUCCESS; `template_status: used`. One gap found and
  fixed: watsonx returned the template's file reference empty (OpenRouter had read it), so a file reference is
  now also taken from the line where it stands before "dt" (Ch 6 para 16.2) when the model gives none.
- **No "Groq" in the logs.** The key pool module is `app/llama/core/key_pool.py` (was `groq_key_pool.py`),
  labels are `[KeyPool]` / `[LLM]`, and the old "No Groq keys set — backend is local … Running offline" line —
  wrong on a cloud endpoint — now says an API key is not needed for endpoints that authenticate another way.
  Key variables read, in order: `WATSONX_API_KEY`, `LLM_API_KEY`, legacy `GROQ_API_KEYS` (still works). Local
  compose uses `LLM_API_KEY`.
- **Minutes content problems seen on `ocp-combo-001`** (writer, not template — not yet fixed): the title took
  the date of the minutes being approved as the meeting's date; an action the transcript never states
  ("Review the advisories"); the dedup step kept "approval … is needed" over "approved"; "Mr. Chair" as a name
  and its correct role downgraded to Unknown because "Chairperson" wasn't said verbatim; correct names
  ("Carroll", "Comptroller") dropped because the transcript's speech-to-text spelling differs; 11 pages for a
  15-minute meeting. Candidate fixes, in order: meeting date only from mom_meta or an explicit statement;
  ground decisions and actions like key points; brevity for JSSD.
- **Spacing audited against the manual 2026-09-16 and found ALREADY CORRECT — do not "fix" it.**
  Appendix AD's "Two Lines" means two line FEEDS, ie ONE blank line: Part 1 para 7 ("a line-space implies
  a line-feed; it does not imply a blank line") and 7.1 ("a two-line spacing implies two line feeds, or
  one blank line"). Measured on the rendered PDF, one line = 16.3 pt: classification→telephone 1.96
  lines, telephone→address 1.97, between address lines 1.00, address→title 1.98, every body block one
  blank line, signature 8 blanks + its own line = 9 feeds ("as required, usually nine"). Also confirmed
  from Part 1: 0.5 in margins with a 0.8 in gutter (10.1, 10.2), 1.15 inside a paragraph (8.3), single
  spacing inside the address block (8.2), page count in words below ten (18.2), never capitals or
  underlined (18.4).
- **ITEM I/II/III built 2026-09-16.** The renderer could already draw several items; only the grouping
  was missing. `agenda_items.group()` shows the model the agenda and the numbered lists and takes back
  INDEX NUMBERS only, so nothing can be invented. Guards: fewer than two agenda entries or no points →
  no call; out-of-range, repeated or non-integer indices dropped; under `MIN_PLACED` (50%) of points
  placed → the single item is kept; leftovers go to the last item. Verified by 17 tests and a live run
  on the stored `momp-002` minutes: 6 items from a 7-entry agenda, 89% placed, numbering 5→12 unbroken,
  owners in the Action column.

## Next steps

1. Jenkins. The code is on GitHub at **`Shxbhxm07/momPromptService`**, branch `main` (pushed
   2026-09-14; SSH remote `git@github.com:Shxbhxm07/momPromptService.git`). Its own repo, holding only
   this service — not a folder inside `momTranscriptionService`. New Jenkins job from that repo,
   **build context = the repo root**, since `Dockerfile` sits beside `app/`.
2. Kafka UI: create topics `mom-prompt.jobs` and `mom-prompt.acks` (1 partition, replication 1, like the others).
3. OCP (project `mom-ai`): apply `deploy/openshift/01-deployment.yaml` → `02-service.yaml` → `03-route.yaml`, in that order. Env values are the
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

`docker compose up -d` is the whole environment: ONE service container (with the minutes writer inside
it), plus MinIO, Elasticsearch, Kafka and Kafka UI, on their own network (`mom-prompt_default`), with
nothing borrowed from `~/offline-mom-api`. The key goes in `.env` (gitignored; `cp .env.example .env`),
never in a committed file. Ports: service 8010, Kafka UI 8090, MinIO 9000/9001, Elastic 9200, Kafka 29092.
One image, built from this repo's `Dockerfile`.

### The older way, mounting into the audio image

The `offline-mom-api:latest` image has every dependency (tesseract, LibreOffice, minio, elasticsearch,
kafka-python), so mount this folder into it:

```bash
docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -v "$PWD/app:/app" -w /app \
  --entrypoint python offline-mom-api:latest your_check.py
```

The writer calls OpenRouter locally, which costs credits, so check with fakes first and make real calls
deliberately.
