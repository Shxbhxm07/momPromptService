"""What a finished job leaves behind so the next prompt can EDIT the minutes instead of rewriting them.

WHY A FILE OF ITS OWN. Elasticsearch already holds the minutes' text, but not the two things a
re-render needs: `meta` — the header the job assembled from `mom_meta` and the template (classification,
venue, file reference, address, telephone, secretary, distribution) — and `item_groups`, the ITEM I/II/III
grouping that cost a model call to work out. Without them an edit could not produce the same document,
only a similar one. The ES record is also their index, with a mapping we do not own; a JSON object beside
the .docx in MinIO adds no field to it and no schema to agree.

WHERE. `{scope}/minutes-state/{conversationId}.json`, next to `{scope}/summaries/{md5}.docx` and scoped
the same way (tenant, else conversation, else user). One per conversation: an edit overwrites it, and the
.docx of every version stays in MinIO under its own hash, which is what makes undo possible.

NEVER FATAL. A job that cannot write its state is still a finished job — the minutes are already stored.
Only the next edit suffers, and it says so plainly instead of guessing.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from config import MINIO_SUMMARY_BUCKET
from kafka_contract import KafkaJob, summary_object_key

logger = logging.getLogger("state")

# Bumped when the shape below changes in a way older files cannot satisfy. An edit reads the number
# first and refuses politely rather than half-applying changes to a file it does not understand.
SCHEMA = 1
# How many past versions to keep for undo. Each is one MoM (~15 KB of JSON), so ten is cheap and is
# far more than the handful of corrections a user makes in one sitting.
MAX_HISTORY = 10


def key_for(job: KafkaJob) -> str:
    """The state file's object key — `{scope}/minutes-state/{conversationId}.json`."""
    ident = (job.conversation_id or "-".join(job.document_ids) or "shared").strip("/").replace("/", "_")
    return summary_object_key(job, ident, ext="json", folder="minutes-state")


def save(job: KafkaJob, store, *, mom: Dict[str, Any], meta: Dict[str, Any], template: Dict[str, Any],
         object_key: str, bucket: str, instruction: str = "", changes: Optional[List[str]] = None,
         history: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Write the state for this conversation. Returns whether it was stored; never raises."""
    state = {
        "schema": SCHEMA,
        "conversation_id": job.conversation_id,
        "mom": mom,
        "meta": meta,
        "template": template or {},
        "summary_bucket": bucket,
        "summary_object_key": object_key,
        # What produced this version: "" for the original job, the user's words for an edit.
        "instruction": instruction,
        "changes": changes or [],
        "history": (history or [])[-MAX_HISTORY:],
    }
    try:
        store.upload(key_for(job), json.dumps(state).encode("utf-8"), "application/json")
        return True
    except Exception as e:
        logger.warning(f"[JOB {job.conversation_id}] could not save the state for editing "
                       f"({type(e).__name__}: {e}) — the minutes are stored, the next edit will refuse")
        return False


def load(job: KafkaJob, store) -> Optional[Dict[str, Any]]:
    """The state for this conversation, or None when there is none to edit. Never raises."""
    key = key_for(job)
    try:
        raw = store.download(f"{MINIO_SUMMARY_BUCKET}/{key}")
    except Exception as e:
        logger.info(f"[JOB {job.conversation_id}] no minutes to edit at {key!r} ({type(e).__name__})")
        return None
    try:
        state = json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.warning(f"[JOB {job.conversation_id}] the saved state is unreadable ({type(e).__name__})")
        return None
    if not isinstance(state, dict) or state.get("schema") != SCHEMA or not isinstance(state.get("mom"), dict):
        logger.warning(f"[JOB {job.conversation_id}] the saved state is version "
                       f"{state.get('schema') if isinstance(state, dict) else '?'}, this build reads {SCHEMA}")
        return None
    return state


def snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a state worth keeping for undo — the minutes, the header, and where the file went."""
    return {k: state.get(k) for k in ("mom", "meta", "summary_bucket", "summary_object_key",
                                      "instruction", "changes")}
