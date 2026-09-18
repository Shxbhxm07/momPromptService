"""Read a repository document's text from Elasticsearch, where the platform already keeps it.

WHY. A file picked "From repository" on the IMIR screen is not in the MoM bucket. On the cluster
(2026-09-18) the backend sent file_urls = mom/<conversationId>/<file id> and MinIO answered NoSuchKey:
repository files live in each user's DMS bucket, and — more usefully — they are already INGESTED. Their
text sits in the repository index, teamsync_v1, as chunks {fId, text, pageNo, para, fileName, path,
username}, _id "<fId>_<pageNo>_<para>" (seen on the cluster: t1.docx is fId 6aad24087fb955220d1ad0cf),
about 120 words each with a 30-word overlap (the backend's doc_ingest.py). Reading those chunks back
gives the document's text with no download, no OCR and no storage path to get right — which is why the
user asked for every file to be read from here first.

HOW. Every chunk of one fId, sorted by pageNo then para, read in pages of CHUNKS_PER_CALL with
search_after so a long document comes back whole. The overlap between neighbouring chunks is removed by
matching the end of one chunk to the start of the next — at least MIN_OVERLAP words, exactly — so a
different chunk size, or no overlap at all, still reassembles correctly. Pages are separated by a blank
line. The document's own name comes from the chunks' fileName.

Nothing here writes to the index, and the index is never created: it is the platform's.
"""
import logging
import re
from typing import Any, List, Optional, Tuple

from config import REPOSITORY_INDEX

logger = logging.getLogger("repository")

CHUNKS_PER_CALL = 500
# Fewer words than this matching at a chunk boundary is coincidence ("of the"), not an overlap.
MIN_OVERLAP = 5
MAX_OVERLAP = 80


# A repository file's id: 24 hex characters (t1.docx is 6aad24087fb955220d1ad0cf on the cluster). An
# attached file's key ends in a UUID instead (a404f703-84bd-4c13-a9c3-70b26d65e4e8), so the two buttons on
# the screen — "From repository" and "Attach file" — can be told apart from the job alone.
_REPOSITORY_ID = re.compile(r"[0-9a-f]{24}", re.I)


def is_repository_id(fid: str) -> bool:
    """True for a file picked "From repository": its id is a repository id, not an upload's UUID."""
    return bool(_REPOSITORY_ID.fullmatch(fid or ""))


def file_id(path: str) -> str:
    """The repository file's id from a storage key: its last part (mom/<conversation>/<file id>)."""
    return (path or "").rstrip("/").rsplit("/", 1)[-1].strip()


def _join(pieces: List[str]) -> str:
    """Chunks of one page, in order, with each overlap kept once."""
    words: List[str] = []
    for piece in pieces:
        new = piece.split()
        for k in range(min(MAX_OVERLAP, len(words), len(new)), MIN_OVERLAP - 1, -1):
            if words[-k:] == new[:k]:
                new = new[k:]
                break
        words += new
    return " ".join(words)


def read(client: Any, fid: str, index: str = REPOSITORY_INDEX) -> Optional[Tuple[str, str]]:
    """(file name, text) of the repository document `fid`, or None when the index has no chunk of it."""
    if not (index and fid):
        return None
    body = {"query": {"term": {"fId": fid}},
            "sort": [{"pageNo": "asc"}, {"para": "asc"}],
            "_source": ["text", "pageNo", "fileName"],
            "size": CHUNKS_PER_CALL}
    pages: dict = {}
    name, after, chunks = "", None, 0
    while True:
        if after:
            body["search_after"] = after
        try:
            # NOT routed by fId: on the cluster teamsync_v1's chunks carry _routing "<username>_<n>"
            # ("sahil_imir.in_2"), so a query routed by fId asks the wrong shard and finds nothing.
            hits = client.search(index=index, body=body)["hits"]["hits"]
        except Exception as e:
            # A wrong REPOSITORY_INDEX must read as that, not as an Elasticsearch stack trace in the ack.
            if getattr(e, "status_code", None) == 404 or "index_not_found" in str(e):
                logger.warning(f"[REPOSITORY] index {index!r} does not exist — check REPOSITORY_INDEX")
                return None
            raise
        for h in hits:
            src = h.get("_source") or {}
            pages.setdefault(src.get("pageNo") or 0, []).append(str(src.get("text") or ""))
            name = name or str(src.get("fileName") or "")
        chunks += len(hits)
        if len(hits) < CHUNKS_PER_CALL:
            break
        after = hits[-1]["sort"]
    if not chunks:
        return None
    text = "\n\n".join(_join(pages[p]) for p in sorted(pages))
    logger.info(f"[REPOSITORY] {fid}: {chunks} chunk(s) over {len(pages)} page(s) from {index!r}, "
                f"{len(text)} chars ({name or 'no file name'})")
    return name, text
