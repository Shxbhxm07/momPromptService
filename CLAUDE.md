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
              minutes ─▶ JSSD .docx ─▶ MinIO {tenant}/summaries/{md5}/MoM-<file name>.docx
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
| `tools/check_jssd_layout.py` | **the layout conformance check** — renders a specimen and asserts 38 rules of the manual on it, page by page. Run after any `docx_export.py` change |
| `app/minutes_edit.py` | **the second prompt**: changes the minutes already written instead of writing them again. The model answers with CHANGES only — never text for the document — and four guards check them. See *Editing* |
| `app/minutes_state.py` | what a finished job leaves in MinIO so the next prompt can edit it: the minutes, the header, the ITEM grouping, and a history for undo |
| `app/prompt_leak.py` | drops the user's own request when the writer minutes it as a decision or an action |
| `app/essence.py` | **brevity**: one model call picks, BY INDEX, at most `ESSENCE_POINTS_PER_ITEM` (4) discussion points per ITEM to keep (Ch 6 para 10). Never writes text; decisions, actions and figures never cut; any doubt → full length. Dropped points leave `key_points` for real so state, search and edits match the Word file |
| `app/meeting_date.py` | blanks the writer's meeting date unless the source gives it as THIS meeting's ("held on", "Date:", "today is"…) — it once took the date of the previous meeting's minutes |
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

## Editing — a second prompt changes the minutes (built 2026-09-17)

`{"mode": "edit", "conversationId": <the same one>, "prompt": "change the venue to Conference Room B"}`.
No document, no template, no `mom_meta` needed. `mode` is also read as `momMode` / `requestType`, and
accepts edit | update | revise | modify | change — **confirm the real field name with the backend developer.**

Why not re-run the job with the old prompt plus the new instruction: measured on the cluster, the SAME job
run twice gave 5 ITEMs then 4, 132 paragraphs then 126, 15 decisions then 13. One change would hand back a
different document. Why not let the model rewrite the minutes: it silently drops and rewords lines nobody
asked about, and nothing can check that.

So the model is shown the saved minutes as numbered lists and may answer ONLY with changes —
`set_meta`, `set_title`, `delete`, `replace`, `add`, `set_owner`, `set_role`, `undo`, `cannot` — each naming
its target by INDEX and quoting the line it means. Code applies them. **Four guards, none optional:**
1. **Wrong line** — a quote that does not match the line at that index is dropped, so "point 41" cannot hit 42.
2. **Invented detail** — names and numbers in new text must already be in the instruction, the minutes or the
   header ("31 Dec 2027" is refused when the user said "30 Sep"). Months are matched Sep/September either way.
3. **Classification and precedence are never editable from chat text** (`EDITABLE_META`), so a misread
   "remove the secret part" cannot declassify a document. Same rule as templates.
4. **Nothing else moves** — untouched lines are copied exactly, and `_regroup` remaps the ITEM grouping from
   the old lines to the new with difflib instead of grouping again, so ITEMs do not reshuffle and no second
   model call is made. Deletions are applied last, together, so indices never shift underneath.

`minutes_state` writes `{scope}/minutes-state/{conversationId}.json` beside the .docx — the minutes, the
merged header, the template's contribution, and a history of the last 10 versions. Every version's .docx
stays in MinIO under its own hash, which is what makes `undo` work. **Minutes written before 2026-09-17 have
no state file and cannot be edited**; the ack says so and the job is untouched.

Verified with no model calls and no cluster: 12 guard tests (right line deleted, wrong quote skipped, index
out of range refused, three deletions at once, invented date refused, classification refused, role fixed,
ITEM remap on delete and on add) and 15 end-to-end (two changes in one prompt → the new venue in the title,
the named point gone, the other eight kept, three ITEMs intact, untouched content carried through; undo
restores the previous version and keeps the edited file; an impossible edit and an unknown conversation both
fail with a sentence a user can read). `tools/check_jssd_layout.py` still passes all 38 rules.

**Not done yet:** one real model call. The JSON-schema mechanism is already proven on watsonx by
`agenda_items`, so the untested part is only how well the model picks the right indices — which the first
cluster test shows.

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
- Returned **exactly as sent** (same value and JSON type): **every field the job carried**, except the
  service's own inputs (`prompt`, `file_urls`, `document_names`, `document_ids`, `tenant_id`, `mom_meta`,
  `template_url`, `template_name` and their other spellings). So `accessVar`, `userId`, `fileName`,
  `parentId`, `summaryFolderId`, `fileIds`, `tenantId`, `conversationId`, `clientSessionId`, `queryId`,
  `metaData`, `uploadType`, `grading`, `data`, `themes`, `path`, `user` — and any field the backend adds
  later — come back untouched. **`path` is no longer overwritten** with `bucket/key` (it was until
  2026-09-16; the IMIR backend's reference ack keeps `"AsItIs"` beside SUCCESS).

A job needs a prompt or a file, at least `MIN_SOURCE_CHARS` (80) characters of text in total (a bare
"make the MoM of yesterday's meeting" is refused; the model would invent the meeting), and at most
`MAX_SOURCE_CHARS`.

**Out** — the job's own fields (above), plus ONLY these five, which the service writes (decided with the
IMIR backend 2026-09-16, from its reference ack): `action: "save"`, `message: SUCCESS|FAILED`
(`ACK_FAILURE_MESSAGE`; the audio service says FAILURE), `description` (the summary's **first sentence**,
at most 300 characters and cut at a word — it used to be the first 300 characters cut anywhere, e.g.
"…nearly 4"; on failure, the reason), and on success `summaryBucketName` + `summaryObjectKey`
(omitted on failure). `fileIds` / `tenantId` / `conversationId` are filled from the snake_case spellings
only when the job did not send the camelCase ones. HTTP: 200 SUCCESS; 422 nothing to process; 503
MinIO/Elastic unreachable; 500 the job failed. The body is always the ack.

**The file's name (2026-09-17).** `summaryObjectKey` is `{tenant}/summaries/{md5}/MoM-<file name>.docx`, so the
download is `MoM-transcripts.docx`, not `78606119fe8f….docx`. The name is the first document's
`document_names` / `fileName`, else its key's last part, without a .pdf/.docx/.doc/.txt extension; a
hash or UUID is not a name, so then — and for a prompt with no document — the meeting title stands in,
and with neither it is plain `MoM`. Characters that break keys, URLs or a Content-Disposition header
(see `_UNSAFE` in `kafka_contract.py`) become `_`; spaces and Hindi stay; at most 100
characters. **The hash stays, as a folder**: the name alone would let two meetings uploaded as `notes.pdf`
in one tenant overwrite each other, and an edit overwrite the version before it. An edit keeps the name
of the version it changes (`minutes_edit._file_name`). Checked on a real MinIO: names with spaces, Hindi
and replaced characters store, read back and download through a presigned URL.

**Where the ack goes.** A Kafka job → `mom-prompt.acks`. An HTTP job → the response body **and**
`mom-prompt.acks` (`HTTP_ACKS_TO_KAFKA`, default on): the IMIR backend listens on the topic but was
submitting jobs over HTTP, so the minutes never reached it, and a caller whose connection times out on a
3-minute job still gets its result on the topic. Published before the HTTP reply; a Kafka outage costs
only the topic copy, never the reply. Verified 2026-09-16 on a real broker: the topic copy is identical
to the HTTP body, 22 of 22 fields of the backend's reference ack.

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
- **Wrong meeting date — fixed 2026-09-18** (`app/meeting_date.py`). The writer's prompt already said "only
  the date of THIS meeting" and watsonx still took "approval of the **February 17 2022** meeting minutes".
  Now code checks it: the model's date is kept only if some place in the source where that date is written
  is introduced as this meeting's by the words just before it ("held on", "convened on", "Date:", "today
  is"…), with no "last/next/previous…" in that window and no nearer "minutes/approval/due/by…". A date the
  job sent in `mom_meta` is always kept (and wins in the title anyway). Unsure → blank, never guessed; a
  blank date just drops "ON <date>" from the title. 19 of 19 cases on the real sources: t2 dropped in three
  spellings, LEP "today is January 28th" kept and its last/next-meeting dates dropped, t3.doc "Date: 16
  September 2026" kept, "held on …" prompts kept, "next meeting will be held on …" dropped.
- **Minutes too long — step 1 built 2026-09-18** (`app/essence.py`, after `agenda_items.group`). The model sees
  each ITEM's numbered points, its decisions and its figures, and returns only the indices to keep, at most
  `ESSENCE_POINTS_PER_ITEM` (default 4; 3× that when there is a single ITEM; 0 = off). Code keeps only indices
  from that ITEM, drops repeats and anything past the cap, and leaves the minutes at full length on a failed
  call, bad JSON, or fewer than half the long ITEMs answered. Rejected: `MOM_WINDOW_KEY_POINTS=false` (that pass
  IS the quote grounding) and dropping the writer's "no specific detail" points (a wording flag, not importance).
  18 offline tests with a faked reply. One real call on the cluster's 12-page t2 minutes (read back from the
  .docx): **68 → 15 points in ~3 s, 10 → 7 pages** re-rendered; every figure the dropped points carried is still
  printed in the ITEM's figures list, because figures are never cut. Showing the figures to the model stopped
  it spending picks on points that only repeat one. What fills the pages now: 22 "Decision." lines and 25
  figure lines — the non-decisions among them are the next fix.
- **Tasks printed as `Decision.` are CORRECT — do not "fix" it.** Checked 2026-09-18 against the manual:
  JSSD minutes have no action-item section. A task the meeting settles IS a decision (para 9: "the decisions
  made and the action required"; 16.15: minutes are executive orders), with the responsible appointment in
  the ACTION column against it (16.8), and Appendix AD draws exactly that (`6. Decision. …  SGO  SO (Ops)`).
  What was wrong in `ocp-combo-004`'s 14 "decisions" is the entries that are not decisions at all — a fact
  ("permits expire on June 30th"), a procedure ("a vote on the adjournment was called"), a duplicate.
- **Full layout audit against the manual, 2026-09-16 — every page, every element.** Triggered by the
  margin bug below, which a page-1-only audit had missed. Ground truth re-read from the PDF:
  **Appendix AD is at PDF pages 52, 53, 54** (printed 328-330), its **explanatory notes 1-6 at PDF 58-59**
  (printed 334-335), and Ch 6 paras 9-20 at PDF 13-20. `tools/check_jssd_layout.py` now renders a
  5-page specimen and asserts **38 of the manual's numbered rules** on the converted PDF — margins per
  page, line feeds between every superscription element, tab stops, page numbering, the ITEM blocks,
  the signature block and the distribution table. **All 38 pass.** Run it after any `docx_export.py`
  change; it needs no model, no network and no job.
  Checked and found ALREADY CORRECT (do not "fix" these): continuation lines return to the left margin
  under the paragraph number, NOT hanging-indented (Part 1 paras 33.3.7, 49.4, Note 26 — and AD draws
  it that way at paras 4, 7, 9, 12); the ITEM heading is bold AND underlined (**AD note 4**: "always
  centre-aligned, and underlined; it may be bold" — the AD drawing shows bold only, and Part 1 para 51
  says headings are not underlined, so the manual contradicts itself; note 4 is the minutes-specific
  rule and wins); the distribution table has AD's **four** columns, not para 69's five with `Method`;
  the tables are borderless though para 39.6.6 asks for gridlines (AD draws none); the page count is
  one line feed below the classification and everything else two (paras 18.1, 18.3); dates are
  `dd Mmm yy` with a leading zero (para 40) and the time four figures + HR (para 42.2).
  Two more notes: **the manual never states a font size** — Part 1 para 13 ends literally at
  "typeface 'Arial', font-size." — so our 12 pt is a choice, not a rule; and AD's own third specimen
  page is misnumbered `2`, so its page break before the distribution list is illustrative, **not** a
  requirement, which is why the distribution still follows the signature block on the same page.
- **Alternating page margins — fixed 2026-09-16.** The user's file `f7fc5592…docx` had its text starting
  at 1.31 in on odd pages and **0.49 in on even pages**, so alternate pages sat visibly further left.
  Cause: `_page_setup` wrote `w:mirrorMargins` into settings.xml, reasoning that minutes are printed on
  both sides, and Word then swaps the 0.8 in binding margin to the RIGHT on even pages. Measured on the
  user's 12-page file: 1.31 / 0.49 / 1.29 / 0.50 … all the way down. `w:mirrorMargins` is now removed
  (and stripped if ever present); the gutter stays on the left throughout. Re-measured on a fresh 8-page
  render: 1.31 in on every page. **Do not add mirror margins back.**
- **Two content fixes, 2026-09-16.** (1) A due date that already carries its own preposition is no
  longer given a second one — a real run printed "... update them as necessary **by After** the
  reevaluation". `_CARRIES_PREP` keeps "by/after/within/w.e.f. ..." as written, `_STANDS_ALONE` keeps
  "immediately"/"asap"/"ongoing" bare, and everything else still gets "by". (2) `app/prompt_leak.py`
  drops the user's own request when the writer minutes it — a real run printed
  "7. **Decision.** Prepare the minutes of the meeting.", which nobody at that meeting decided.
  Deliberately narrow, because losing a real decision is far worse: all four must hold — the line opens
  with an instruction verb ("prepare", "record", "list"...), every content word of it also occurs in the
  prompt, it is at most 15 words, and (for an action) nobody owns it and nothing is due. 13 tests cover
  both sides, including "A motion to approve the minutes was made and seconded." which must survive.
  What is dropped is logged per job.
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
