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
import logging
import re
import time
from typing import Dict, List, Tuple

import agenda_items
import document_checker
import essence
import meeting_date
import minutes_state
import prompt_leak
import template_details
from config import MAX_SOURCE_CHARS, MIN_SOURCE_CHARS
from docx_export import build_mom_docx, flatten_items
from documents import extract_text_blocks
from kafka_contract import KafkaJob, build_ack, summary_file_name, summary_object_key
from minio_client import ObjectStore
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


def _read_documents(job: KafkaJob, store: ObjectStore) -> List[Tuple[str, str]]:
    documents = []
    for i, path in enumerate(job.file_urls):
        raw = store.download(path)
        name = (job.document_names[i] if i < len(job.document_names) else "") or path.rsplit("/", 1)[-1]
        document_checker.check(raw, name)
        extraction = extract_text_blocks(raw, name)
        text = "\n\n".join(b for b in extraction.blocks if b.strip())
        if not text.strip():
            logger.warning(f"[JOB {job.conversation_id}] {name}: no text found, even with OCR")
        logger.info(f"[JOB {job.conversation_id}] read {name} "
                    f"({len(raw)/1048576:.1f} MB, {len(text)} chars)")
        documents.append((name, text))
    return documents


def process(job: KafkaJob, c: Clients) -> dict:
    """One job → an acknowledgement. Never raises: a crash here would lose the ack."""
    t0 = time.time()
    try:
        # A second prompt changing minutes this conversation already has takes a different path
        # entirely: no document is read and no minutes are written again. Imported here rather than
        # at the top because minutes_edit imports this module back for the shared pieces.
        if job.is_edit:
            import minutes_edit
            return minutes_edit.process(job, c)
        if not job.prompt and not job.file_urls:
            raise ValueError("Nothing to write minutes from: the job has no prompt and no file_urls.")
        documents = _read_documents(job, c.store)
        chars = len(job.prompt) + sum(len(t) for _, t in documents)
        if chars < MIN_SOURCE_CHARS:
            raise ValueError(f"Too little to write minutes from: {chars} characters of prompt and document "
                             "text. Describe the meeting in the prompt, or attach its notes.")
        source = compose_source(job.prompt, documents)
        if len(source) > MAX_SOURCE_CHARS:
            raise ValueError(f"The documents are too long: {len(source)} characters, the limit is "
                             f"{MAX_SOURCE_CHARS}.")

        # The template, if any, is read on its own path and never joins `source`: its specimen text
        # would otherwise pass the writer's grounding check and reach the minutes. Only its validated
        # standing details come back, and the job's own mom_meta wins over them field by field.
        # load() never raises — a bad template costs its details, not the job.
        template = template_details.load(job, c.store)
        meta, from_template = template_details.merge(job.mom_meta, template.details)

        mom = to_mom_response(c.llm.generate(source, _metadata(job.mom_meta)))
        if not any(mom.get(k) for k in ("summary", "key_points", "decisions", "action_items")):
            raise RuntimeError("no minutes produced")

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

        docx_bytes = build_mom_docx(mom, meta)
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
        minutes_state.save(job, c.store, mom=mom, meta=meta, object_key=key, bucket=bucket,
                           template={"name": template.name, "status": template.status,
                                     "fields": from_template})
        if job.template_url:
            logger.info(f"[JOB {job.conversation_id}] template {template.name!r}: {template.status}"
                        + (f", used {from_template}" if from_template else "")
                        + (f" — {template.reason}" if template.reason else ""))
        logger.info(f"[JOB {job.conversation_id}] done in {time.time()-t0:.0f}s — {bucket}/{key}")
        return build_ack(job, success=True, bucket=bucket, object_key=key,
                         description=one_sentence(mom.get("summary") or ""))
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] failed after {time.time()-t0:.0f}s: {e}")
        # ValueError is only ever raised here with a sentence meant for the user, so it is passed on
        # as it stands. Any other exception keeps its class name, which is what makes a MinIO or model
        # failure diagnosable from the ack alone.
        return build_ack(job, success=False,
                         description=str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {e}")
