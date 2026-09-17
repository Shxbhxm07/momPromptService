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
| `job-message-template.json` | Job with the HQ's **official JSSD template** + a prompt |
| `sample_template_unit.docx` | A unit's filled template: its address, telephone, file reference, secretary and distribution |
| `sample_template_blank_specimen.docx` | The Appendix AD specimen with placeholders only — should contribute **nothing** |
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
4. **Open the Word file** — MinIO console → **mom** → **test** → **summaries** → the newest folder → `MoM-<your file name>.docx`.
   Its exact name is in the acknowledgement's `summaryObjectKey`.

**Layout.** Every page of the minutes has the same margins: the text starts 1.30 in from the left edge on page 1 and on page 12 alike (0.5 in margin + the 0.8 in binding gutter, Part 1 paras 10.1-10.3). Before 16 Sep 26 alternate pages sat 0.8 in further left; if you still see that, you are testing an image built before that date.

**Timing.** A short document takes about 4 minutes; a 13,000-character transcript took 4½ minutes.
Long transcripts are split into pieces and take much longer — a 57,000-character one was still
running after 30 minutes. For routine testing, keep files under about 15,000 characters.

## The job fields

| field | required | notes |
|---|---|---|
| `prompt` | if no file | what the meeting was, or what the minutes should focus on |
| `file_urls` | if no prompt | MinIO path `bucket/folder/file`, e.g. `mom/mom-docs/notes.pdf` |
| `document_names` | recommended | the file name **with its extension** — see *Files without an extension* |
| `fileName` | no | the uploaded document's name. Returned in the acknowledgement, and used as the document's name when `document_names` is absent |
| `conversationId` | yes | use a new one per test; it names the Elasticsearch record |
| `document_ids` | yes | comes back as `fileIds` |
| `tenant_id` | yes | the Word file is stored under `<tenant_id>/summaries/` |
| `mom_meta` | no | the JSSD header details: classification, file reference, venue, secretary, distribution… |
| `template_url` | no | MinIO path of the HQ's official JSSD template (DOCX, DOC, PDF or TXT), e.g. `mom/templates/hq.docx` |
| `template_name` | no | the template's file name, shown in logs and Elasticsearch |
| `accessVar`, `userId`, `isUser`, `user`, `path`, `clientSessionId`, `queryId`, `metaData`, `uploadType`, `grading`, `data`, `themes` | no | returned in the acknowledgement **exactly as sent** |

## The acknowledgement

The acknowledgement is **your own message back, with five fields filled in**: `action`, `message`,
`summaryBucketName`, `summaryObjectKey` and `description`. Every other field you sent — `path`,
`fileIds`, `parentId`, `summaryFolderId` and anything else — comes back **exactly as sent**. The service's
inputs (`prompt`, `file_urls`, `template_url`, `mom_meta`) are not repeated.

It arrives on Kafka topic **`mom-prompt.acks`** — for a job sent on `mom-prompt.jobs`, and also for a job
sent over HTTP (where it is the response body as well). Match it to your request on `conversationId`.

**Success**

```json
{
  "...": "every field you sent, unchanged — e.g. conversationId, fileIds, tenantId, path",
  "action": "save",
  "message": "SUCCESS",
  "summaryBucketName": "mom",
  "summaryObjectKey": "<tenantId>/summaries/<md5>/MoM-<file name>.docx",
  "description": "<the first sentence of the summary>"
}
```

**Failure** — `message` is `FAILED`, `summaryBucketName` and `summaryObjectKey` are left out, and `description` says why.

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

## Testing the template feature

The template supplies **only the issuing HQ's standing details** — address, telephone, file reference,
signature block and distribution list. The **layout always follows the JSSD manual**, and the **meeting
content always comes from the prompt**. Upload `sample_template_unit.docx` to `mom/templates/`, then send
`job-message-template.json`.

- [ ] Telephone, the three address lines, the signature block and the distribution rows match the template
- [ ] Anything the job sets in `mom_meta` **wins** over the template (e.g. put a `file_ref` in the job — it must replace the template's)
- [ ] Title, date, attendees, items and decisions come from the **prompt**, not the template
- [ ] **No template specimen text appears**: no "Nagin Range", "FIRING PRACTICE", "felicitating", "SGO", "Address line", "Addressee 1", "xx/yy"
- [ ] Classification comes **only** from `mom_meta`, never from the template
- [ ] The same result from the template saved as **PDF**
- [ ] Elasticsearch record has `template_name`, `template_status: used`, `template_fields`

| send as template | expected |
|---|---|
| `sample_template_blank_specimen.docx` | SUCCESS; nothing taken from it; `template_status: no_details` |
| a non-JSSD document (letter, company profile) | SUCCESS; template ignored; `template_status: not_jssd` |
| a path that does not exist | SUCCESS; template ignored; `template_status: unreadable` |
| a spreadsheet | SUCCESS; template ignored; `template_status: unreadable` |
| a template **only**, no prompt and no file | FAILED — a template is not a meeting source |

A template problem never fails the job — the minutes are still produced.

## Changing the minutes with a second prompt

Send the SAME `conversationId` again with `"mode": "edit"` and a prompt saying what to change. No document
and no template are needed. Only what you ask for changes; every other line stays exactly as it was.

```json
{
  "tenant_id": "test", "conversationId": "<the same one>", "mode": "edit",
  "prompt": "Change the venue to Conference Room B, remove the point about the website, and make Ananya Krishnan the Chairman."
}
```

| ask for | expected |
|---|---|
| change the venue, date, time, file reference, amendments date, telephone or secretary | done; the title and header change |
| remove / reword a point, decision or action | done; everything else is untouched |
| add an action with an owner and a due date **you state** | done |
| set an attendee as Chairman or Secretary | done; they move to the top or bottom of the list |
| "undo that" | the previous version comes back; the edited file stays in MinIO |
| change the **classification** | refused — classification comes only from `mom_meta` |
| a due date or a name you never gave | refused, and the ack says which words were not yours |
| "focus more on the budget" | FAILED — that needs the document read again; send a new job instead |
| an edit for a `conversationId` that has no minutes | FAILED — *No minutes to edit* |

The acknowledgement's `description` lists what changed, e.g. *"venue → Conference Room B; removed from
key_points: …"*. Minutes generated before 17 Sep 26 cannot be edited — generate them once more first.

## Negative tests

| send | expected |
|---|---|
| no `prompt` and no `file_urls` | FAILED — *nothing to write minutes from* |
| `prompt` under 80 characters, no file | FAILED — *too little to write minutes from* |
| `file_urls` pointing at a file that does not exist | FAILED — *MinIO get failed … NoSuchKey* |
| a spreadsheet or image as the file | FAILED — the file is refused |
| two JSON objects pasted in one message | no acknowledgement; the service logs *unparseable message* and moves on |
| the same `conversationId` sent twice | SUCCESS both times; Elasticsearch keeps **one** record, overwritten |

## Known limitations — please do not log these as new bugs

These are known and tracked. Log them only if the behaviour is **different** from what is described.

| # | what you will see | why |
|---|---|---|
| 1 | `Info` column always empty | not produced yet; `Action` is filled from the owner of an action item |
| 2 | `Action` column **empty for most decisions** | only action items carry an owner; plain decisions do not |
| 3 | Decisions in the **past tense** (*"the motion passed"*) instead of *"It was decided that…"* | prompt wording, not yet changed |
| 4 | **No Secretary** in the attendee list | only listed if the source names one |
| 5 | Title has no venue, time or date | these come from `mom_meta`; send it to see them |
| 6 | A long name in the `Action` column **breaks mid-word** | the column is 1 inch wide, as in the manual, which uses short forms such as `SO (Ops)` |
| 7 | Opened in LibreOffice, the font looks like a **serif** font | the file uses Arial; LibreOffice substitutes when Arial is not installed. Check in Microsoft Word |
| 8 | A template's **own layout, letterhead image or Hindi headings** are not copied | by design: the template supplies standing details only; the layout follows the manual and the minutes are English |
| 9 | In the distribution table, a template's Remarks value `NA` prints as an **empty** cell | `NA` is treated as "no value" so it can never appear as a false entry elsewhere |

### Files without an extension

A file stored with no extension (e.g. `transcripts`) is **refused**, even if it is plain text.
Give the name with its extension and it is read normally — `file_urls` keeps the real key. Either field
works, `document_names` first:

```json
"file_urls": ["mom/mom-docs/transcripts"],
"document_names": ["transcripts.txt"]
```
```json
"file_urls": ["mom/mom-docs/transcripts"],
"fileName": "transcripts.txt"
```

## Reporting a bug

Include: the job message you sent, the acknowledgement you got, the `.docx` from MinIO, and the
service log from around that time (`docker logs momp-service`). Log timestamps are **UTC** —
add 5 h 30 min for IST.
