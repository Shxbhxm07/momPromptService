"""One job → one acknowledgement: the user's prompt and documents in, JSSD minutes out.

The same steps, in the same order, as the audio service's consumer.process (~/offline-mom-api/api/consumer.py), with
the recording replaced by text:

    file_urls ─▶ MinIO ─▶ text (text layer; OCR for scanned pages) ─┐
                                              prompt ───────────────┴─▶ llama-service /summarize
    minutes ─▶ JSSD .docx ─▶ MinIO {tenant}/summaries/{hash}/MoM-<file name>.docx ─▶ Elasticsearch (record + chunks) ─▶ ack

Both ways in use it: the Kafka consumer (kafka_consumer.py) and POST /v1/mom-prompt (main.py).
The clients it needs are built once, on first use, in setup.py.
"""
import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import Future
from typing import Dict, List, Tuple

import agenda_items
import document_checker
import essence
import figures_check
import meeting_date
import model_notes
import minutes_state
import prompt_leak
import repository
import template_details
import tidy_minutes
from config import MAX_SOURCE_CHARS, MIN_SOURCE_CHARS
from docx_export import build_mom_docx, flatten_items
from documents import extract_text_blocks
from kafka_contract import KafkaJob, build_ack, summary_file_name, summary_object_key
from mom import to_mom_response
from setup import Clients

logger = logging.getLogger("minutes")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def compose_source(prompt: str, documents: List[Tuple[str, str]]) -> str:
    """The text llama-service writes the minutes from.

    /summarize was built for transcripts, so each part is labelled: the prompt is the user's request
    (what the meeting was, what to cover), each document is source material. Unlabelled, the model can
    take "focus on the budget" for something a participant said.
    """
    parts = []
    if prompt:
        parts.append(f"USER'S REQUEST FOR THESE MINUTES:\n{prompt}")
    for i, (name, text) in enumerate(documents, start=1):
        parts.append(f"SOURCE DOCUMENT {i} ({name}):\n{text}")
    return "\n\n".join(parts)


# Titles and short forms whose full stop does not end a sentence: "chaired by Lt Col. Menon".
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "lt", "col", "gen", "maj", "capt", "brig", "cdr", "cmdr", "sgt",
                  "no", "st", "sr", "jr", "vs", "etc", "e.g", "i.e", "hq", "rs", "approx", "dept", "govt"}
_SENTENCE_END = re.compile(r"[.!?](?=\s+[A-Z(\"'])")
DESCRIPTION_CHARS = 300
# A follow-up the saved minutes cannot answer by a change ("focus more on the budget") writes them again from
# the document. What the history records for it, and what the user is told when their changes are left behind.
REWRITTEN = "the minutes were written again from the document"
REWRITTEN_NOTE = 'Written again from the document; your earlier changes are not in it. Send "undo" to get them back.'


def one_sentence(text: str, limit: int = DESCRIPTION_CHARS) -> str:
    """The summary's opening sentence, for the ack's `description`.

    The IMIR backend's reference ack describes the result in one sentence. The first 300 characters
    used to be sent instead, cut wherever they fell — a real ack ended "…nearly 4". A sentence longer
    than `limit` is cut at a word, with an ellipsis, never mid-word.
    """
    text = " ".join((text or "").split())
    for m in _SENTENCE_END.finditer(text):
        last_word = text[:m.start()].rsplit(" ", 1)[-1].lower().rstrip(".")
        if last_word not in _ABBREVIATIONS and not (len(last_word) == 1 and last_word.isalpha()):
            text = text[:m.end()]
            break
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _metadata(mom_meta: Dict) -> Dict[str, str]:
    """The job's stated date, time and venue, in the names /summarize takes. Empty ones are left out."""
    pairs = (("date", "meeting_date"), ("time", "meeting_time"), ("venue", "venue"))
    return {ours: str(mom_meta[theirs]).strip() for ours, theirs in pairs
            if str(mom_meta.get(theirs) or "").strip()}


def _read_documents(job: KafkaJob, c: Clients) -> List[Tuple[str, str]]:
    """The text of every file in the job: a repository file from Elasticsearch, an attachment from MinIO.

    The user's rule (2026-09-18): a file picked "From repository" is already indexed, so its text is read
    from the repository index — it is not in MinIO at all; the backend sends mom/<conversation>/<file id>
    for it, the path uploads use. A file added with "Attach file" is uploaded to MinIO and read from there.
    The two are told apart by the file id (repository.is_repository_id). `file_fids` are repository ids.
    """
    documents = []
    for i, path in enumerate(job.file_urls):
        name = (job.document_names[i] if i < len(job.document_names) else "") or path.rsplit("/", 1)[-1]
        fid = repository.file_id(path)
        if repository.is_repository_id(fid):
            documents.append(_from_repository(job, c, i, fid, name))
            continue
        try:
            raw = c.store.download(path)
        except RuntimeError as e:
            if "NoSuchKey" not in str(e):
                raise
            raise ValueError(f"The attached document {name!r} could not be found in file storage "
                             f"({path}).") from e
        document_checker.check(raw, name)
        extraction = extract_text_blocks(raw, name)
        text = "\n\n".join(b for b in extraction.blocks if b.strip())
        if not text.strip():
            logger.warning(f"[JOB {job.conversation_id}] {name}: no text found, even with OCR")
        logger.info(f"[JOB {job.conversation_id}] read {name} "
                    f"({len(raw)/1048576:.1f} MB, {len(text)} chars)")
        documents.append((name, text))
    for fid in job.file_fids:
        documents.append(_from_repository(job, c, len(documents), fid, ""))
    return documents


def _from_repository(job: KafkaJob, c: Clients, i: int, fid: str, name: str) -> Tuple[str, str]:
    """(name, text) of repository document `fid` from the repository index, named after its real file.
    Raises a ValueError the user can read when the index holds no text for it."""
    found = repository.read(c.index.client, fid)
    if not found:
        raise ValueError(f"The document {name if name and name != fid else fid!r} was picked from the repository "
                         f"but has no text in the repository index (file id {fid}). It may not be ingested yet.")
    real_name, text = found
    # The minutes are called "MoM-<file name>": the repository's name for it, not its id.
    if real_name and (not name or name == fid):
        names = job.document_names
        names.extend([""] * (i + 1 - len(names)))
        names[i] = real_name
        name = real_name
    logger.info(f"[JOB {job.conversation_id}] read {name or fid} from the repository ({len(text)} chars)")
    return name or fid, text


# ONE RUN PER REQUEST (2026-09-22). "Try again" on the IMIR screen sent the same 227,000-character job while the first
# was still running, and the pod did all the work twice, each copy at half speed. A request identical to one running
# now — same conversation, prompt, files, template, header details and kind — waits for that run and is answered
# with its result, under its own fields (queryId, clientSessionId… come back as it sent them).
_RUNNING: Dict[str, Future] = {}
_RUNNING_LOCK = threading.Lock()


def _request_key(job: KafkaJob) -> str:
    return json.dumps([job.tenant_id, job.conversation_id, job.prompt.strip(), job.file_urls, job.file_fids,
                       job.document_ids, job.template_url, job.mom_meta, job.is_edit], sort_keys=True, default=str)


def process(job: KafkaJob, c: Clients) -> dict:
    """One job → an acknowledgement, or — when the very same request is already running — that run's result."""
    key = _request_key(job)
    with _RUNNING_LOCK:
        running = _RUNNING.get(key)
        if running is None:
            mine = _RUNNING[key] = Future()
    if running is not None:
        logger.info(f"[JOB {job.conversation_id}] the same request is already running — waiting for it instead "
                    "of doing the work again")
        first = running.result()
        desc = first.get("description") or ""
        return build_ack(job, success=first.get("message") == "SUCCESS", bucket=first.get("summaryBucketName", ""),
                         object_key=first.get("summaryObjectKey", ""), description=desc, limit=max(1, len(desc)))
    try:
        ack = _process(job, c)
        mine.set_result(ack)
        return ack
    except BaseException as e:            # _process never raises; still, a waiting request must never hang
        mine.set_exception(e)
        raise
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(key, None)


def _process(job: KafkaJob, c: Clients) -> dict:
    """One job → an acknowledgement. Never raises: a crash here would lose the ack."""
    t0 = time.time()
    try:
        # A second prompt changing minutes this conversation already has takes a different path
        # entirely: no document is read and no minutes are written again. Imported here rather than
        # at the top because minutes_edit imports this module back for the shared pieces.
        import minutes_edit
        if job.is_edit:
            return minutes_edit.process(job, c)
        # A FOLLOW-UP needs no marker: a message on a conversation that already has minutes, carrying the
        # same document or none, is tried as an edit first ("add tele 9654396200" fills the header instead
        # of the minutes being written again with the sentence taken for something said in the meeting).
        # What cannot be done as a change comes back None, and the minutes are written again as before.
        ack = minutes_edit.follow_up(job, c)
        if ack is not None:
            return ack
        # Written again from the same document: keep the header details the user already gave, and the
        # minutes being replaced, so "undo" brings them back with every change the user made.
        previous = minutes_edit.previous_state(job, c)
        so_far = dict((previous or {}).get("meta") or {})
        if not job.prompt and not job.file_urls and not job.file_fids:
            raise ValueError("Nothing to write minutes from: the job has no prompt and no file_urls.")
        documents = _read_documents(job, c)
        chars = len(job.prompt) + sum(len(t) for _, t in documents)
        if chars < MIN_SOURCE_CHARS:
            raise ValueError(f"Too little to write minutes from: {chars} characters of prompt and document "
                             "text. Describe the meeting in the prompt, or attach its notes.")
        source = compose_source(job.prompt, documents)
        if MAX_SOURCE_CHARS and len(source) > MAX_SOURCE_CHARS:
            raise ValueError(f"The documents are too long: {len(source)} characters, the limit is "
                             f"{MAX_SOURCE_CHARS}.")

        # The template, if any, is read on its own path and never joins `source`: its specimen text
        # would otherwise pass the writer's grounding check and reach the minutes. Only its validated
        # standing details come back, and the job's own mom_meta wins over them field by field.
        # load() never raises — a bad template costs its details, not the job.
        template = template_details.load(job, c.store)
        meta, from_template = template_details.merge(job.mom_meta, template.details)
        # What the user gave in earlier messages beats the template; the job's own mom_meta beats both.
        for k, v in so_far.items():
            if v and not job.mom_meta.get(k):
                meta[k] = v

        mom = to_mom_response(c.llm.generate(source, _metadata(job.mom_meta)))
        if not any(mom.get(k) for k in ("summary", "key_points", "decisions", "action_items")):
            raise RuntimeError("no minutes produced")

        # The model's own preamble and footnotes, printed as business in a real run: "Decision. Here are
        # the action items extracted from the meeting transcript:". See model_notes.
        notes = model_notes.strip(mom)
        if notes:
            logger.info(f"[JOB {job.conversation_id}] dropped {len(notes)} line(s) that are the model's own "
                        f"notes, e.g. {notes[0][:80]!r}")
        # Numbers copied from the writer's own prompt examples ("$51,840", "7,30,340" in a New Zealand
        # meeting), and rupee notation the source never used. See figures_check.
        leaked, renotated = figures_check.check(mom, source)
        if leaked:
            logger.info(f"[JOB {job.conversation_id}] dropped {len(leaked)} line(s) carrying a number that is "
                        f"only in the writer's prompt, e.g. {leaked[0][:80]!r}")
        if renotated:
            logger.info(f"[JOB {job.conversation_id}] {renotated} line(s) put back in the source's currency "
                        "notation (no rupees in the source)")

        # With no meeting_date in mom_meta the title takes the writer's, and the writer has taken it
        # from another meeting — "approval of the February 17 2022 meeting minutes". Kept only when the
        # source gives it as this meeting's date; see meeting_date for the test.
        dropped = meeting_date.check(mom, source, job.mom_meta.get("meeting_date"))
        if dropped:
            logger.info(f"[JOB {job.conversation_id}] meeting date {dropped!r} dropped — the source "
                        "never gives it as this meeting's date")

        # compose_source labels the prompt "USER'S REQUEST FOR THESE MINUTES" and the writer still
        # minutes it now and then — a real run printed "Decision. Prepare the minutes of the meeting."
        # This drops only a line that reads as the request itself; see prompt_leak for the four tests.
        for line in prompt_leak.strip(mom, job.prompt):
            logger.info(f"[JOB {job.conversation_id}] dropped the request back out of the "
                        f"minutes — {line}")

        # Nothing said twice, owners by full name — before the grouping, so its indices are built on the
        # final lists. The writer's decisions and action items are two lists, de-duplicated each on its
        # own; the renderer prints both as "Decision." lines. See tidy_minutes.
        tidy_minutes.merge_repeats(mom)
        tidy_minutes.full_owner_names(mom)

        # Appendix AD records one ITEM per agenda entry (Ch 6 para 16.7), each ending in its own
        # Decision. The writer returns flat lists, so the grouping is worked out here and passed to the
        # renderer as INDEXES into the very lists it prints — no text crosses back, and a failed or
        # unconvincing grouping simply leaves the single item the service produced before.
        points, figures, decisions = flatten_items(mom)
        groups = agenda_items.group(mom, points, decisions, figures)
        if groups:
            mom["item_groups"] = groups

        # Only the essence of the discussion (Ch 6 para 10): a real run gave 12 pages for a 15-minute
        # meeting. One call picks which points each ITEM keeps, by index; decisions and actions are never
        # cut, and any doubt leaves the minutes at full length. See essence.
        shortened = essence.select(mom, points, decisions, figures, groups)
        if shortened:
            logger.info(f"[JOB {job.conversation_id}] kept {shortened[1]} of {shortened[0]} discussion "
                        "points, the essence per item")
        # Figures already said in a point or decision, or no figure at all, go; a few per ITEM remain.
        # After essence, so it is judged against the points that will actually print.
        points, figures, decisions = flatten_items(mom)
        tidy_minutes.trim_figures(mom, points, decisions, figures, mom.get("item_groups"))

        # Laid out by the job's own fill-in template when it sent one, otherwise by the default template.
        docx_bytes = build_mom_docx(mom, meta, template=template.fillable)
        # Downloaded as "MoM-<the user's file name>.docx", not as its hash — see summary_object_key.
        bucket, key = c.store.upload(summary_object_key(job, hashlib.md5(docx_bytes).hexdigest(),
                                                        name=summary_file_name(job, mom.get("title") or "")),
                                     docx_bytes, DOCX_MIME)
        c.index.index_mom(job, mom, source="attached", summary_bucket=bucket, summary_object_key=key,
                          template={"name": template.name, "status": template.status,
                                    "fields": from_template, "reason": template.reason})
        # Last, and it never raises: the minutes are stored by now, and a failed search copy must not
        # turn a finished job into a failed one. Does nothing until ENABLE_CHUNK_INDEX and CHUNK_INDEX.
        c.chunks.index_mom(job, mom, summary_bucket=bucket, summary_object_key=key)
        # What a later prompt needs to EDIT these minutes rather than write them again: the minutes
        # themselves, the header this job assembled, and the ITEM grouping. Never fatal — see
        # minutes_state. Only after the file is stored, so the state can never point at nothing.
        # A fill-in template's location goes with the state, so an edit lays the minutes out the same way.
        minutes_state.save(job, c.store, mom=mom, meta=meta, object_key=key, bucket=bucket,
                           template={"name": template.name, "status": template.status,
                                     "fields": from_template,
                                     "url": job.template_url if template.fillable else ""},
                           sources=minutes_state.sources_of(job),
                           instruction=job.prompt if previous else "",
                           changes=[REWRITTEN] if previous else None,
                           history=((previous.get("history") or []) + [minutes_state.snapshot(previous)]
                                    if previous else None))
        if job.template_url:
            logger.info(f"[JOB {job.conversation_id}] template {template.name!r}: {template.status}"
                        + (f", used {from_template}" if from_template else "")
                        + (f" — {template.reason}" if template.reason else ""))
        logger.info(f"[JOB {job.conversation_id}] done in {time.time()-t0:.0f}s — {bucket}/{key}")
        said = one_sentence(mom.get("summary") or "")
        if previous and (previous.get("history") or previous.get("changes")):
            said = f"{REWRITTEN_NOTE} {said}"
        return build_ack(job, success=True, bucket=bucket, object_key=key, description=said)
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        # ValueError is only ever raised here with a sentence meant for the user, so it is passed on
        # as it stands. Any other exception keeps its class name, which is what makes a MinIO or model
        # failure diagnosable from the ack alone.
        return build_ack(job, success=False,
                         description=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
