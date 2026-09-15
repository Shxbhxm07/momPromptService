# MoM Prompt Service — Test Pack

The service writes **Minutes of Meeting in the JSSD format** (Vol I Part 2, Appendix AD) from a
meeting document, a written description of the meeting, or both. You send a job on Kafka; you get an
acknowledgement back on Kafka and a Word file in MinIO.

## What is in this folder

| file | use it for |
|---|---|
| `MoM_Specimen_Full.docx` | **What a complete MoM looks like.** Every JSSD section filled in. Compare real outputs against this |
| `MoM_Specimen_Minimal.docx` | What you get when the job sends **no** `mom_meta` — no header block, no classification |
| `job-message-full.json` | Job with every optional field, including `mom_meta` |
| `job-message-minimal.json` | Job with a document and a prompt, nothing optional |
| `job-message-prompt-only.json` | Job with **no document** — the meeting described in the prompt |
| `specimen_data.json` | The exact data the two specimens were rendered from |

All names, addresses and file numbers in these files are **specimen values**, not real ones.

## Running a test

1. **Upload a document** — MinIO console → **Object Browser** → **mom** → **mom-docs** → **Upload**.
   PDF, DOCX, DOC or TXT.
2. **Produce the job** — Kafka UI → **Topics** → **mom-prompt.jobs** → **Produce Message**.
   Copy a job file into **Value**. Leave **Key** empty. Change `conversationId`, `file_urls` and
   `document_names` to match your file.
3. **Read the acknowledgement** — Kafka UI → **Topics** → **mom-prompt.acks** → **Messages**.
   Set **Seek type** to **Oldest**, or you only see messages that arrive after the page opened.
4. **Open the Word file** — MinIO console → **mom** → **test** → **summaries** → the newest `.docx`.
   Its exact name is in the acknowledgement's `summaryObjectKey`.

**Timing.** A short document takes about 4 minutes; a 13,000-character transcript took 4½ minutes.
Long transcripts are split into pieces and take much longer — a 57,000-character one was still
running after 30 minutes. For routine testing, keep files under about 15,000 characters.

## The job fields

| field | required | notes |
|---|---|---|
| `prompt` | if no file | what the meeting was, or what the minutes should focus on |
| `file_urls` | if no prompt | MinIO path `bucket/folder/file`, e.g. `mom/mom-docs/notes.pdf` |
| `document_names` | recommended | the file name **with its extension** — see *Files without an extension* |
| `conversationId` | yes | use a new one per test; it names the Elasticsearch record |
| `document_ids` | yes | comes back as `fileIds` |
| `tenant_id` | yes | the Word file is stored under `<tenant_id>/summaries/` |
| `mom_meta` | no | the JSSD header details: classification, file reference, venue, secretary, distribution… |
| `accessVar`, `userId`, `isUser`, `user`, `path`, `clientSessionId`, `queryId`, `metaData`, `uploadType`, `grading`, `data`, `themes` | no | returned in the acknowledgement **exactly as sent** |

## The acknowledgement

**Success**

```json
{
  "fileIds": ["test-001"],
  "tenantId": "test",
  "compareMode": "",
  "action": "save",
  "message": "SUCCESS",
  "description": "<first 300 characters of the summary>",
  "summaryBucketName": "mom",
  "summaryObjectKey": "test/summaries/<id>.docx",
  "path": "mom/test/summaries/<id>.docx",
  "...": "every other field you sent, unchanged"
}
```

**Failure** — `summaryBucketName` and `summaryObjectKey` are left out, and `description` says why.

## What to check in the Word file

Use `MoM_Specimen_Full.docx` as the reference.

**Header block** (only when `mom_meta` is sent)
- [ ] Classification centred at the **top and bottom of every page**
- [ ] Page count under it on page 1, e.g. `(Three pages)` — CONFIDENTIAL and above only
- [ ] Classification watermark across each page
- [ ] Telephone on the left, precedence on the right, same line
- [ ] Copy number under the precedence
- [ ] Three address lines
- [ ] File reference and `dt <date>` on one line

**Title**
- [ ] Centred, capitals: `MINUTES OF THE MEETING HELD AT <VENUE> / AT <TIME> HR ON <DATE> / TO DISCUSS <PURPOSE>`
- [ ] Time as four figures and `HR` (`1030 HR`); date as `12 SEP 26`

**Body**
- [ ] `1. The following were present:-`, then 1.1, 1.2 … with name, appointment, role
- [ ] **Chairman listed first, Secretary listed last**
- [ ] `INTRODUCTION` heading, then the agenda
- [ ] `Action` and `Info` column headings
- [ ] `ITEM I – <TITLE>` centred, bold, underlined, with the item classification in brackets below
- [ ] Paragraphs numbered continuously through the whole document
- [ ] Each decision starts with **Decision.** in bold
- [ ] The owner of an action appears in the **Action** column
- [ ] Due dates written as `by 30 Sep 26`

**Closing**
- [ ] `Agreement with the minutes will be assumed unless amendments are received by <date>.`
- [ ] Signature block — name in brackets, rank, `Secretary` — **left-aligned**
- [ ] Distribution table: Distribution / No of Copies / Copy No / Remarks, **ending with `File`**

**Content**
- [ ] Nothing in the minutes that is not in the source document or prompt
- [ ] No placeholder text such as `by None stated`, `by TBD`, `by N/A`
- [ ] The prompt's instructions are followed but **not copied into** the minutes

## Negative tests

| send | expected |
|---|---|
| no `prompt` and no `file_urls` | FAILURE — *nothing to write minutes from* |
| `prompt` under 80 characters, no file | FAILURE — *too little to write minutes from* |
| `file_urls` pointing at a file that does not exist | FAILURE — *MinIO get failed … NoSuchKey* |
| a spreadsheet or image as the file | FAILURE — the file is refused |
| two JSON objects pasted in one message | no acknowledgement; the service logs *unparseable message* and moves on |
| the same `conversationId` sent twice | SUCCESS both times; Elasticsearch keeps **one** record, overwritten |

## Known limitations — please do not log these as new bugs

These are known and tracked. Log them only if the behaviour is **different** from what is described.

| # | what you will see | why |
|---|---|---|
| 1 | **Only `ITEM I`**, even when the agenda lists several items | the model does not yet say which point belongs to which agenda item |
| 2 | **`Info` column always empty** | not produced yet |
| 3 | `Action` column **empty for most decisions** | only action items carry an owner; plain decisions do not |
| 4 | Decisions in the **past tense** (*"the motion passed"*) instead of *"It was decided that…"* | prompt wording, not yet changed |
| 5 | **No Secretary** in the attendee list | only listed if the source names one |
| 6 | Title has no venue, time or date | these come from `mom_meta`; send it to see them |
| 7 | A long name in the `Action` column **breaks mid-word** | the column is 1 inch wide, as in the manual, which uses short forms such as `SO (Ops)` |
| 8 | Opened in LibreOffice, the font looks like a **serif** font | the file uses Arial; LibreOffice substitutes when Arial is not installed. Check in Microsoft Word |

### Files without an extension

A file stored with no extension (e.g. `transcripts`) is **refused**, even if it is plain text.
Put the extension in `document_names` and it is read normally — `file_urls` keeps the real name:

```json
"file_urls": ["mom/mom-docs/transcripts"],
"document_names": ["transcripts.txt"]
```

## Reporting a bug

Include: the job message you sent, the acknowledgement you got, the `.docx` from MinIO, and the
service log from around that time (`docker logs momp-service`). Log timestamps are **UTC** —
add 5 h 30 min for IST.
