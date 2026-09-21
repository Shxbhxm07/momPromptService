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
| `app/essence.py` | **brevity**: small calls (≤80 points each, one per long ITEM) pick, BY INDEX, at most `ESSENCE_POINTS_PER_ITEM` (4) discussion points per ITEM to keep (Ch 6 para 10), answered as one plain ranked line of numbers, not JSON. Never writes text; decisions, actions and figures never cut; any doubt → full length. Dropped points leave `key_points` for real so state, search and edits match the Word file |
| `app/model_notes.py` | drops the model's own preamble and footnotes printed as business ("Decision. Here are the action items extracted from the meeting transcript:", "Note: … some assumptions have been made"). Same test (`llama.utils.text_utils.is_model_note`) as the writer's `_bullets_to_list`, where they came from |
| `app/figures_check.py` | drops a line carrying a number that is in the writer's PROMPTS but not in the source (the figures prompt's examples "$51,840", "7,30,340" were printed in a New Zealand meeting's minutes), and, when the source never mentions rupees, removes "(Rs …)" and lakh grouping. Spoken numbers count as present |
| `app/repository.py` | **"From repository" files**: a file whose id is a repository id (24 hex, e.g. `6aad24087fb955220d1ad0cf`) is read from the platform's index (`REPOSITORY_INDEX`, `teamsync_v1`) — all chunks of that `fId` in pageNo/para order, overlaps removed, NOT routed. "Attach file" uploads (key ends in a UUID) are read from MinIO. `file_fids` are repository ids. Read-only |
| `app/minutes_template.py` | **fills the minutes TEMPLATE** (docxtpl): builds every slot's value from the minutes and the job — `xxx...xxx` when absent, also for a slot it does not know (`_Missing`) — fills the job's own fill-in template or the default, then the page count (two passes, LibreOffice) and the watermark. See *Templates* |
| `app/templates/jssd_minutes.docx` | **the default minutes template**: JSSD Appendix AD as a Word file with `{{ slots }}`, editable in Word. Built by `tools/make_minutes_template.py` from `docx_export`'s building blocks; 38 layout rules pass on it |
| `app/tidy_minutes.py` | code only, no model call, **never removes a fact**: a decision an action item already records is merged into it only when the kept line holds every number, name and all but one word of the other; owners written as the attendee list has them ("Rohit" → "Maj Rohit Negi", unique match only); after essence, a figure is dropped only when ONE printed point or decision holds all its numbers and every specific word of its label. No cap |
| `app/meeting_date.py` | blanks the writer's meeting date unless the source gives it as THIS meeting's ("held on", "Date:", "today is"…) — it once took the date of the previous meeting's minutes |
| `app/agenda_items.py` | sorts the verified points, decisions and figures under the agenda items so the minutes carry **ITEM I, II, III** as Appendix AD draws them. Index numbers only — it never writes text |
| `app/logger_config.py` | log format and level. Timestamps are UTC; the user reads IST (UTC+5:30) |
| `app/mom.py` | `MomGenerator` (calls /summarize) + `to_mom_response` (**copied** from `~/offline-mom-api/api/core/mom.py`) |
| `app/minio_client.py` | copied **unchanged** from `api/core/storage.py` |
| `app/es_client.py` | copied from `api/core/search_index.py`, minutes only (translation index removed) |
| `app/docx_export.py` | **the JSSD renderer**, copied **unchanged** from `api/utils/docx_export.py` |
| `app/documents.py` | PDF/DOCX/DOC/TXT text extraction with OCR, from `api/utils/documents.py` — **diverged 2026-09-20** (form flattening, turned scans); copy it back there |
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

## Templates

**Since 2026-09-20 the minutes ARE a template.** The user gave a docxtpl service-letter template as the
model ("make our template like that … everything customisable; the value from the transcript, otherwise
xxx...xxx"). `docx_export.build_mom_docx` now fills `app/templates/jssd_minutes.docx` (via
`minutes_template.render`); a job whose `template_url` is a Word file WITH slots (`{{ … }}`/`{% … %}`,
`template_status: fillable`) is filled instead, and edits re-fetch it (its URL is in the state). A template
that cannot be filled falls back to the default, and the default to `docx_export._build_direct` — the old
code-built layout — so no job loses its minutes to a template. Filled from the default, the 38-rule check
passes; compared page by page with the code-built minutes, the text differs ONLY where the user's rule adds
`xxx...xxx` (an unstated appointment, purpose or agenda; Action and Info against a decision nobody owns).
14 tests with a client's own template (its wording kept, values filled, an unknown slot → xxx...xxx, an edit
keeps the layout, a broken template → the default, the service-letter template itself fills). Templates
WITHOUT slots are still read as below. The 2026-09-15 decision below rejected "tagged {{placeholders}}"
only because official templates had none; a fill-in template is now the user's own choice.

### Templates without slots — what they contribute, and why only that (decided 2026-09-15)

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

**A follow-up needs no marker (2026-09-20).** The IMIR frontend sends every follow-up as a NEW job, with the file
re-attached and no `mode` — "add tele 9654396200" re-read the PDF, wrote the minutes again, took the sentence for
something said, and the telephone stayed xxx...xxx. Now `minutes_edit.follow_up`: a message on a conversation that
already has minutes, carrying the SAME document(s) (`sources` in the saved state: MinIO keys, "fid:<id>") or none,
is tried as an edit first. If the model answers only "cannot" (not a change to these minutes — "focus more on the
budget", a question), the minutes are written again as before, KEEPING the header details the user gave earlier
(`header_so_far`; the job's own mom_meta still wins). A change the guards refuse is answered with the reason, never
rewritten around the guard. A different document is a new meeting; minutes saved before `sources` existed are
matched only when the message has no file. No backend change needed; `mode: "edit"` still works.
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
3. **A classification is never CHANGED from chat text**, so a misread "remove the secret part" cannot
   declassify a document. One the job never gave ("xxx...xxx") may be FILLED — only with the one grade word
   the user wrote ("mark it restricted"; "top secret" is never read as "secret"). Every other header detail
   is fillable (`EDITABLE_META`, 2026-09-20 — "user can write anything in re-prompting, it can be any missing
   item"): venue, date, time, telephone, address (lines split on ";" or ","), file reference, date of issue,
   precedence, copy number, amendments date, secretary, distribution ("addressee | copies | remarks" rows;
   copies nobody gave print "xxx...xxx"). The model is shown "xxx...xxx" against each missing field, and for a
   header value EVERY capitalised word and number must be in the user's words — the sentence rule skipped
   the first word, so an invented one-word venue ("Dhanpur") got through.
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
- **A 3-hour meeting broke both index steps — fixed 2026-09-18.** Cluster job on `t4` (a 3 h 7 min council
  meeting, 206,907 chars, 15 chapters): the writer produced 765 key points; ITEM grouping's single reply hit
  its 1,200-token limit and the length step's hit 800, both fell back safely, and the Word file was **57
  pages, 759 points in one ITEM**. Both steps now work in fixed-size calls, so no reply grows with the meeting:
  `agenda_items` sends points 80 at a time (`POINT_BATCH`) plus one call for decisions and figures, with each
  reply budget sized to what it may list, in parallel (`LLM_CONCURRENCY`); a point the model leaves out goes to
  its nearest earlier neighbour's ITEM (the writer's points follow the meeting) instead of the last ITEM.
  `essence` makes one call per 80 points of each long ITEM, the cap split exactly across batches (`_shares`),
  and asks for **one plain line of numbers, most important first** — under a strict JSON schema the local
  host returned just `{`, and under JSON mode only whitespace to the token limit (the writer's documented
  trap); a plain line came back in ~1 s with exactly the cap. Real run on the t4 minutes read back from the
  .docx (local OpenRouter, Llama 3.3 70B): 11 ITEMs in 11 grouping calls (86 s, 62% placed by the model, the
  rest by neighbour), **758 → 41 points in 10 s, 57 → 10 pages**. 23 offline tests with a fake writer that
  lists everything, cuts replies off, or chats. Not fixed here: grouping still uses JSON (2 of 11 replies
  were cut off, their points placed by neighbour); leftover decisions still go to the last ITEM.
- **"From repository" files — read from Elasticsearch, 2026-09-18.** Picking a repository file on the IMIR screen
  failed at once: `MinIO get failed for bucket='mom' key='317b0323-…/6aa7b5e57fb955220d1ad034': NoSuchKey`. The
  backend sends such a file as `mom/<conversationId>/<file id>`, the upload path, but only uploads are copied
  there; repository files sit in a per-user DMS bucket (`<user>dms//<fileId>`, per `doc_ingest.py`) and are
  already ingested into the repository index as chunks `{fId, text, pageNo, para, fileName}`, ~120 words with a
  30-word overlap. The user's call: read them from Elasticsearch (the rule below). A `file_fids` entry
  goes straight there. `repository.read` sorts by pageNo/para, pages through with
  search_after, and removes each overlap by exact word match (≥5 words), so a different chunking still
  reassembles. The minutes are named after the chunks' `fileName` (`MoM-DRISHTI-4_…`). Missing in both, or a
  wrong index name, is a sentence the user can read, not a stack trace. Tested on local Elasticsearch with the
  backend's own `extract`/`chunk_pages`/`build_documents`/`bulk_index`: meeting.pdf (14 chunks) and t4.txt (417
  chunks, also read 100 at a time) come back **word for word identical**, page by page; 11 tests.
  **Confirmed on the cluster (Kibana):** repository files are in `teamsync_v1` — t1.docx is fId
  `6aad24087fb955220d1ad0cf`, t2.docx `6aad24087fb955220d1ad0d0`, _id `<fId>_<page>_<para>`, `path` "T_" (a DMS
  folder, not a MinIO key). **Their `_routing` is `<username>_<n>` (`sahil_imir.in_2`), NOT the fId**, so the
  first version's `routing=fid` asked the wrong shard: on a 3-shard test index routed the same way it missed 3 of
  3 files. The query is now unrouted. The DRISHTI file (`6aa7b5e5…d034`) is in no index — never ingested.
  **The rule (the user's, same day): "From repository" → Elasticsearch, "Attach file" → MinIO**, told apart by
  the id at the end of the `file_urls` key — a repository id is 24 hex (`6aad24087fb955220d1ad0cf`), an upload's
  key ends in a UUID (`a404f703-84bd-4c13-a9c3-70b26d65e4e8`). A repository file is never looked for in MinIO
  (it is not there) and an attachment never in Elasticsearch. A repository file with no chunks (DRISHTI) or an
  attachment missing from MinIO each fail with a sentence the user can read. If the backend ever changes its id
  format, `repository.is_repository_id` is the one place to change. 13 tests on a 3-shard, user-routed index.
  Not done: `minio_client.split_object_path` keeps the leading "/" of a DMS key (`bucket//id` → key "/id"), so
  a DMS path sent in file_urls would still miss; not needed while repository files come from Elasticsearch.
- **The model's notes and its prompt's example figures printed as minutes — fixed 2026-09-18.** The t4 minutes
  had "Decision. Here are the action items extracted from the meeting transcript:.", "Decision. Note: The owners
  and due dates … some assumptions have been made", a figure "Here are the figures mentioned in the transcript."
  and a point "Note: … I have only listed each point once". Cause: the four focused passes return bullet text
  and `_bullets_to_list` kept every line. `is_model_note()` (text_utils) now skips openers ("Here are/is",
  "Below is", "Sure,"), footers ("Note:", "N.B.:"), lines about the extraction ("extracted from the
  transcript", "assumptions have been made", "I have only listed") and short headings ending in ":"; the same
  test sweeps the finished minutes (`model_notes.py`). 9 real notes caught, 10 real lines kept (incl. "Notes
  from the previous meeting…", "Sure Start funding…", content mentioning a transcript).
  **The figures "$51,840 (Rs 51,840)", "$32,000", "7,30,340 (Rs 7,30,340)" were INVENTED** — none is in t4; they
  are the examples in `FIGURES_EXTRACTION_PROMPT`, copied. The "Rs" came from a rule in four prompts that said
  "Money is stated in Indian notation". Fixed in three layers: the rule now says keep the source's currency,
  never add one in brackets (lakh guidance kept for rupee meetings); the figures prompt says its examples are
  format only; and `figures_check.py` drops any line carrying a number found in the prompts (4+ digits, not a
  year, not a power of ten — read from `llama.prompts` itself) that the source does not contain in digits or
  words, and renotates rupee brackets / lakh grouping when the source has no rupees. On the real t4 minutes: the
  3 invented figures and 5 notes dropped, every real figure kept ($800,000 … $635, 4.5%). 14 tests.
- **Logs cut to what matters — 2026-09-18** (`app/logger_config.py`). The cluster log of the 15-minute `t4` job
  was 536 lines, 450 of them noise: 127 readiness probes (`GET / 200`), 162 httpx `HTTP Request: POST`, 161
  `[KeyPool] Using key #0`. Now: successful `GET /` and `GET /health` are filtered from uvicorn's access log
  (a failing probe still shows), httpx is WARNING, the per-call and once-per-start lines (KeyPool, TERMCORR,
  "Connecting to watsonx", IBM token refresh) are DEBUG, and `elastic_transport` is ERROR — its retry warnings
  carried ~40-line tracebacks, three per request, while the job already logs an outage in one line.
  `LOG_LEVEL=DEBUG` brings the per-call lines back (kafka/elasticsearch stay quiet). The formatter turns
  `— → ✓ ✗ ↓ ↑` into `- -> OK x get put`, because the OCP viewer showed them as `â€” â†’ âœ“…` — done in the
  formatter so the byte-identical copies (`minio_client.py`) are untouched. The grounding line now says
  "quote too short to check" apart from "quote not in transcript" (it had called "since 1985" missing when it
  was only under `_MIN_QUOTE_CHARS`), and the window pass prints progress every 20 windows. Same job: 537 → ~88.
- **OCR reads English first — fixed 2026-09-19.** OCR was already built in (Tesseract 5.5.0 with `eng` + `hin`
  in the image; a PDF page with under `MIN_PAGE_TEXT_CHARS` (40) characters of text is rendered at 300 dpi and
  OCR'd). But `OCR_LANGS` was `hin+eng`, and Tesseract takes the first language as the main one: it lost the digit
  1 — "16 Sep 26" → "6 Sep 26", "1030 hr" → "030 hr", "12 Corps" → "l2 Corps". Measured on drawn A4 scans, clean
  and noisy (tilt, blur, speckle), through `extract_text_blocks` in the service image: `hin+eng` 9/13 numbers on
  English, 6/8 on Hindi; **`eng+hin` 13/13 and 8/8, every word right on all four pages**, Hindi included. A
  typed + scanned PDF reads as `pdf-mixed`. Only the default in `config.py` changed; `documents.py` stays
  byte-identical. The audio service's `api/config.py` has the same `hin+eng` default.
  **All OCR settings are in the Deployment's env since 2026-09-19** (they were always read from env, but
  were not listed, so the OCP console did not show them): `ENABLE_OCR`, `OCR_LANGS`, `OCR_DPI`, `OCR_MAX_PAGES`,
  `MIN_PAGE_TEXT_CHARS` — in `01-deployment.yaml`, `docker-compose.yml`, `.env.example` and the onboarding form.
  `OCR_LANGS` accepts `eng,hin` / `eng hin` (joined with "+" in config.py: "hin,eng" reached Tesseract as one
  language and failed every scan). A language not in the image (`hindi`) is logged as an ERROR at startup and
  turns `/health` to degraded, with an `ocr` block showing engine, version, languages in force and installed.
  Checked by starting the server with default, "eng, hin", "eng hin", "hindi", "eng" and ENABLE_OCR=false.
- **Fewer model calls — 2026-09-19.** A job's calls were mostly windows (18 of 27 on a 45-minute meeting, 136 of
  153 on a 3-hour one). Now: **windows of 6000 characters (~2 pages), not 1800** — the ONE setting, `MOM_WINDOW_CHARS`
  ("chunk size", in the Deployment; "6,000" is read as 6000, junk falls back to 6000); the focused "list every point"
  read runs only when windows are off (it repeated the windows' job over the whole transcript); the meeting-type
  guess is gone (always "general", no call); `MODEL_CONTEXT_LIMIT` 32768 → **65536** in the YAML, so the main read
  takes up to ~215k characters in one call instead of 20k-character parts + a merge. Counted with a recording fake:
  45 min **27 → 12 calls** (66k → 47k tokens), 3 hours **153 → 39** (471k → 274k) — at 6000; the default went back
  to 1800 the same day (next entry). The user asked for exactly one knob — do not add back the per-step switches. Two fixes the bigger windows needed: the window reply allowance
  grows with the window (1500 at ≤1800, capped 3000 — a 6000-char window returned up to 36 points, ~2000 tokens), and
  a third, no-JSON-mode attempt for a broken window. The breakage (a reply that is one point then whitespace to the
  token limit, in both JSON modes) is an OpenRouter-host quirk and random, not size-driven — 2 of 3 half-page windows
  broke in one probe, 0 of 3 two-page ones — and never happened on watsonx (138/138 windows fine in the t4 cluster log).
  Real-model accuracy, answer key of known facts/decisions/actions: 5-min 15/15 (1800) vs 15/15 (6000), 10-min 17/17
  (6000); the rest of the comparison stopped when the OpenRouter credit ran out (HTTP 402). Before changing
  MOM_WINDOW_CHARS again, re-measure on `scanned-transcripts/` (their exact text is in `printed-text/`).
- **Repeats, first-name owners, a long figures list, "by 6-10 October" — fixed 2026-09-19** (`app/tidy_minutes.py`,
  due dates in `docx_export.flatten_items`). A real 15-minute job printed 7 of its 24 "Decision." lines twice (decisions
  and action items are de-duplicated each on its own and both print as "Decision."), owners as "Rohit"/"Karan", 44
  figures mostly restating a point, "by 6-10 October", and "…from 28 to 30 September by 28-30 September". A first
  version also capped figures at 5 per ITEM and merged loosely; the user's accuracy rule ended both — the cap cut 18
  facts no other line carried ("120 grenades"), one shared word dropped "Young Officers course vacancies — 2" for "course
  to start on 2 November", and a merge lost "before any test". Now every removal is provably redundant (see the files
  table). On that job, rebuilt from its Word file: 8 → 7 pages, 24 → 20 decision lines (4 merged; the 3 with a qualifier
  of their own stay double), 44 → 32 figures (each of the 12 removed shown against the one printed line that states all
  of it), no number or word of the original missing, answer key 17/18 before and after. 24 edge-case tests, a full job +
  edit, and the 38 layout rules pass. **Still open, seen on the same job:** every decision and figure landed under the
  LAST ITEM — the decisions-and-figures grouping call gave nothing usable (reason not yet in a log) — and "retest for 85
  personnel" where the transcript removes 9 first (76).
- **Essence OFF and windows back to 1800 — 2026-09-19**, by the accuracy rule: `ESSENCE_POINTS_PER_ITEM=0` (code default
  and YAML) prints every verified point; `MOM_WINDOW_CHARS` defaults to 1800, because the case for 6000 rested on essence
  dropping small finds anyway and 6000 was checked on only two transcripts. Calls with 1800 and the built-in savings:
  45 min 25 (was 27), 3 h 141 (was 153); 6000 gives 12 and 39 once the six-transcript test shows it loses nothing.
  **The live Deployment still has ESSENCE_POINTS_PER_ITEM=4** — change it to 0 in the console.
- **Nothing made up in the header — `xxx...xxx` for every missing detail, 2026-09-20.** The user saw a real job print
  "Tele: 0194-2450101", "Headquarters 99 Specimen Brigade", "RK Verma" and "HQ 15 Specimen Corps": the MADE-UP details
  of `templates/sample_template_unit.docx` (a test file from 2026-09-15), which had been attached to real jobs in IMIR
  as the unit's template. The user's rule: "if any information is not provided … put xxx...xxx. DO NOT MAKE something
  from yourself." Now (1) `docx_export.MISSING = "xxx...xxx"` prints for the telephone, address, file reference and
  its date, the title's place/time/date, each ITEM's classification ("(UNCLASSIFIED)" only when the job says so), the
  secretary's name and rank, the amendments date and the distribution — replacing "(Initials and Name)"/"Rank", the
  dotted line and the silent omissions; precedence and copy number still appear only when given (the manual uses them
  only when they apply). (2) `template_details.load` ignores a template whose details contain the word "Specimen"
  (`template_status: specimen`) — each value WAS in the template, so the grounding check could not catch it. 15 tests
  (sample ignored, the same file with real-looking values still used, every marker, no marker when all is given) and
  the 38 layout rules (the "dt" rule now accepts "dt <date>") pass. The fix in IMIR is data: attach the unit's real
  template, or none.
- **Any missing item can be filled by a re-prompt — 2026-09-20** (see *Editing*, guard 3). 20 tests: one prompt filling
  address, telephone, file reference + date of issue, precedence, secretary, amendments date, distribution and
  classification leaves a single "xxx...xxx" (the copies nobody gave); refused: a number the user never typed, changing
  a job-set classification, "top secret" read as SECRET, a grade the prompt never names, an invented one-word venue.
  The backend must send the re-prompt with `mode: "edit"` and the SAME conversationId — otherwise it is a new meeting.
- **An edit saves its state LAST — 2026-09-20.** It used to save the new state to MinIO before updating Elasticsearch;
  an Elasticsearch outage then left the edit applied behind a FAILED ack, and the user's retry applied it again ("delete
  point 5" twice deletes two points). Now: new .docx → Elasticsearch record → state → chunks (the order `minutes.process`
  already used), and a state that cannot be saved fails the edit ("…was not made. Send it again.") instead of passing
  silently. Proved: with Elasticsearch down, and with the state write failing, the minutes stay exactly as they were and
  the resent change deletes exactly one point. MinIO stays the source of truth — Elasticsearch holds neither the header
  nor the ITEM grouping, so an edit read from it would print a different document. Not covered: a Kafka redelivery after
  a pod dies between the state save and the offset commit (a narrow window); the backend should key edit messages by
  conversationId if it ever runs more than one partition.
- **Fillable-form PDFs and turned scans — fixed 2026-09-20** (`documents._extract_pdf`, `_ocr_page`). A tester's 2-page PDF
  (52 KB) failed "No text could be extracted": text layer empty and BOTH pages OCR'd to 0 characters. Reproduced with a
  fillable form — what was typed sits in form fields, which are neither page text nor drawn for OCR (drawing them with
  `init_forms` still OCR'd to nothing). Now every page is flattened first (`FPDFPage_Flatten`, FLAT_NORMALDISPLAY), so
  fields and typed-on notes become page text, read exactly. The same investigation corrected an earlier claim here:
  sideways and upside-down scans do NOT "read fine" — they read as junk (10% and 8% real words vs 86% upright; the
  count of characters had hidden it). An OCR'd page under 50% real words (system wordlist; Devanagari letters count) is
  now asked its orientation (Tesseract osd; for Devanagari, which osd often cannot place, all three turns are tried) and
  read again upright: English 90°/180° back to 86%, Hindi 90° 97% of words right; upright pages unchanged in text and
  time (all six scanned transcripts identical). A PDF with truly nothing now says to re-save it (Print → Save as PDF) or
  send DOCX. **`documents.py` is no longer byte-identical to `~/offline-mom-api/api/utils/documents.py`** — copy it back
  there once, as for docx_export.
- **Follow-up messages are edits without a marker — 2026-09-20** (see *Editing*). 16 tests played the way the IMIR screen
  sends them: the same PDF re-attached with "add tele 9654396200" fills the header with no OCR and no writer call and
  the content untouched; "focus more on the budget" writes the minutes again and keeps that telephone; a different
  file is a new meeting and carries nothing over; no file at all is an edit; an invented number is refused, not
  rewritten; undo works; pre-`sources` minutes; an explicit `mode: edit`. The backend still has two optional fixes:
  a form for header details (sent as mom_meta — exact, classification as a dropdown) and the "null" it puts in
  front of our file name ("nullMoM-scanned-transcript-05min.docx").
- **A real re-prompting session, fixed — 2026-09-20.** On the cluster: "add Col. Ariz Khan" worked, then (1) "update HIS
  position to Team Leader of AI" changed Mr R K Gupta — each follow-up reached the model alone, so "his" had no
  referent; (2) "revert the change to Gupta AND update Ariz Khan's position" did only the undo, the rest silently
  dropped (undo was exclusive); (3) two messages failed: the model gave line 5 (Gupta) while quoting "Col" for Ariz
  Khan on line 6. Now: the model is shown the last 5 requests and what each did, with OLD values ("Mr R K Gupta: role
  Assistant Garrison Engineer (MES) → Team Leader of AI"), so pronouns resolve and an earlier change can be set back;
  undo is applied first and the other changes after, on the restored minutes; a change goes to the ONE line its quote
  names when the number disagrees (none, several, or a quote under 8 characters: refused); the prompt asks for the
  name as the quote. And a new guard found by the replay: a role, owner or attendee name must be the user's own PHRASE
  (or an earlier value being put back; an owner may be anyone on the attendee list) — word by word, "Garrison
  Commander" had passed, built from two other roles. The session replayed with a stand-in making the real model's
  mistakes: 11 checks pass; every earlier suite passes. Whether the real model now reads "his" right is for the cluster.
- **Re-prompts refused because the model copied our own labels — fixed 2026-09-21.** "add Teena as an intern in AI"
  → refused "new attendee 'Teena [role: Intern in AI]': not a name you gave"; "Update Col. Ariz Khan name to Shubham
  Pandey" → refused "'Shubham Pandey [role: Team Leader of AI]' is not what you wrote". Both times the model copied
  the `[role: …]` label that `_line_of` SHOWS it into the new name, and the phrase guard (rightly) found no such
  phrase in the user's words. There was also no field for a new attendee's role (it went in `owner`). Fixed in three
  layers, the guards untouched: (1) the answer schema has its own `role` field and the prompt says labels never go
  in `value` (a separate field per fact, OpenAI's "use enums and object structure"); (2) `_labels` moves any
  `[role:|owner:|due: …]` the model still copies into its own field, and each is checked on its own — name, role,
  owner, due; (3) after a guard refusal the model is asked ONCE more with its answer and the reasons (Instructor's
  "reask" pattern), the new answer passes the same guards, and the one that does more is kept. 29 tests: both
  real answers now apply, invented surnames/roles/dates are still refused, one call when right first time, never a
  retry after "cannot".
- **A question wiped the user's edits — fixed 2026-09-21.** On the cluster, after "add Teena…" and a rename, "can you
  explain me the agenda of this meetig" came back `cannot`, so the follow-up rule wrote the minutes AGAIN from the PDF
  (2 min): Teena and the rename were gone, the reply was the new summary's first sentence, and the new state started
  an empty history, so undo could not help. Now: (1) a new op `answer` — a question is answered from the saved minutes
  only; the ack is SUCCESS with the answer as `description` (up to `ANSWER_CHARS` 1500, `build_ack(limit=…)`) and the
  CURRENT file's bucket/key; no new file, no state change, no rewrite (also when the model adds a `cannot` beside it).
  An answer naming a number or name the minutes do not hold is replaced by "I could not answer that from these
  minutes." (guard 2's test). A change + a question in one message: the change is made and the answer added.
  (2) A real rewrite ("focus more on the budget") keeps the history with the replaced version on top
  (`minutes_edit.previous_state`), records itself as an earlier request, and — when the replaced minutes had edits —
  the reply starts "Written again from the document; your earlier changes are not in it. Send "undo" to get them
  back." 24 tests replaying the session; the 29 re-prompt tests and the 38 layout rules pass.
- **"his" changed the wrong person — fixed 2026-09-21.** After "Update Ariz Khan name to Shubham Pandey", "add his
  position as an AI engineeg" changed TEENA's role; the model had the earlier requests and still guessed. Now code
  decides: an instruction with he/him/his/she/her/they/them/their that names nobody on the attendee list (a first
  name or surname counts) means the person the LAST change to the attendee list added, renamed or gave a role
  (`_last_person`: versions whose list did not change — a telephone, a deleted point — are stepped over; an undo of
  a rename counts). Every attendee change (set_role, replace, delete) is moved to that line and a set_owner's owner
  set to that name, logged when it differs from the model's choice; the model is also told who the pronoun means.
  When no ONE person was changed last (first message, a delete, an undo of an add, a rewrite) the edit is refused:
  "I could not tell who 'his' means. Please write the person's name." — asked, never guessed. The same rule for every
  pronoun; nothing is inferred from a name. Changes not about a person ("their office number" → telephone) are left
  alone. 21 tests; the 29 re-prompt and 24 question tests still pass.
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

- **ACCURACY FIRST — the user's rule, 2026-09-19: "it does not matter if the MoM is long or short, my accuracy
  levels must be 100."** Nothing true may be cut to make the minutes shorter. Every removal must be provably
  redundant — another printed line still states the same numbers, names and words. Brevity steps that delete
  content (essence's points-per-ITEM, a figures cap) stay OFF. Check any change with the scanned-transcript
  answer key and an original-vs-new comparison (no number or word lost) before calling it done.
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
