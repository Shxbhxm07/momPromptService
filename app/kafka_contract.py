"""Parse the inbound Kafka job and build the acknowledgement.

Kept separate from any Kafka client on purpose: this module is pure data-in/data-out, so the
message shape can be tested without a broker, and swapping the client later touches nothing here.

TWO NAMING CONVENTIONS, DELIBERATELY TOLERATED. The inbound message is mostly snake_case
(tenant_id, file_urls, compare_mode) but not entirely — conversationId and userId are camelCase in
the very same object — while the acknowledgement is camelCase throughout (tenantId, fileIds,
compareMode). Reading both spellings costs one helper and removes a whole class of silent
mis-wiring, where a renamed field simply arrives as None and the job processes the wrong thing.
"""
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _get(msg: Dict[str, Any], *names, default=None):
    """First present key among `names`, tolerating snake_case / camelCase drift."""
    for n in names:
        if msg.get(n) not in (None, "", []):
            return msg[n]
    return default


@dataclass
class KafkaJob:
    tenant_id: str = ""
    user_id: str = ""
    conversation_id: str = ""
    mode: str = ""
    compare_mode: str = ""
    # Correlation key echoed back as "fileIds" in the ack. Confirmed against the reference pair:
    # the ack's fileIds carried document_ids ("comparison1"), NOT file_fids ("FileOne0").
    document_ids: List[str] = field(default_factory=list)
    # MinIO OBJECT KEYS, not URLs — "docutalk/doccomparecheck1/9f3a..." has no scheme or host, so
    # these need credentials rather than being fetchable as-is.
    file_urls: List[str] = field(default_factory=list)
    file_fids: List[str] = field(default_factory=list)
    document_names: List[str] = field(default_factory=list)
    # What the user asked for, in their words: a description of the meeting, instructions for the
    # minutes, or both. The document in file_urls, if any, is the source material.
    prompt: str = ""
    # Optional details for the Word minutes that no recording contains: classification, file
    # reference, address, secretary, distribution and so on (see docs/kafka-contract.md).
    mom_meta: Dict[str, Any] = field(default_factory=dict)
    # The issuing HQ's official minutes TEMPLATE (a MinIO key, like a file_urls entry). It is not a
    # source document: nothing in it is summarised. It supplies only the standing details that stay
    # the same from meeting to meeting — address, telephone, file reference, signature block and
    # distribution list — and the layout still comes from the manual. See template_details.py.
    template_url: str = ""
    template_name: str = ""
    # The backend's own fields, kept EXACTLY as they arrived (same value, same type) so the ack can
    # hand them back untouched. Never parsed, never normalised — `accessVar` in particular is an
    # opaque credential-shaped string and `isUser` is a boolean the backend may also send as text.
    echo: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_edit(self) -> bool:
        """A change to minutes already written, not a new meeting — see minutes_edit.

        The backend marks it with `mode`. The exact field name is still to be confirmed with the
        backend developer, so the usual spellings are all read (see parse_job) and several words are
        accepted: a screen may call it edit, update or revise for the same button.
        """
        return self.mode.strip().lower() in EDIT_MODES

    @property
    def has_attachments(self) -> bool:
        return bool(self.file_urls)

    @property
    def has_ingested(self) -> bool:
        return bool(self.file_fids or self.document_ids)


# WHAT THE ACK RETURNS: EVERYTHING THE BACKEND SENT, EXCEPT THE TWO GROUPS BELOW — same value, same
# type ("{}" stays a string, null stays null), and anything not sent stays absent.
#
# Why "everything" rather than a list: the IMIR backend's reference ack (2026-09-16) carries fields
# no earlier message had — parentId, summaryFolderId — and a fixed list silently dropped them. The
# backend's model is plain: its own message back, plus the result. A field it adds tomorrow comes
# back too, with no rebuild here.
#
# 1. The service's INPUTS. They say what to make the minutes from; they are not the backend's
#    bookkeeping, and a prompt or a template path has no business in the answer.
_INPUT_FIELDS = frozenset({
    "prompt", "file_urls", "fileUrls", "document_names", "documentNames", "document_ids", "documentIds",
    "file_fids", "fileFids", "fIds", "tenant_id", "conversation_id", "compare_mode", "mode",
    "momMode", "mom_mode", "requestType", "request_type",
    "mom_meta", "momMeta", "template_url", "templateUrl", "template_name", "templateName",
})
# 2. The RESULT — the only fields the service writes. A value the job itself carried for one of
#    these (a resent ack, say) is ignored, never echoed as if it were the outcome.
_RESULT_FIELDS = frozenset({"action", "message", "description", "summaryBucketName", "summaryObjectKey"})


# What `mode` may say for "change the minutes I already have".
EDIT_MODES = frozenset({"edit", "update", "revise", "modify", "change"})


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


# The extensions this service can read, repeated from utils.documents.SUPPORTED_EXTENSIONS rather
# than imported: this module stays free of the parser dependencies so the message shape can be
# tested with nothing installed. Keep the two in step.
_READABLE_EXTENSIONS = (".pdf", ".docx", ".doc", ".txt")


def _looks_like_a_file(value: Any) -> bool:
    """Is this `path` a MinIO object key, or one of the backend's own markers?

    The audio service takes any non-empty `path` as the file to fetch. Here that is unsafe: this
    backend also sends `path` as a field to hand back, with values like "AsItIs", and a prompt-only
    job would then try to download an object called "AsItIs" and fail on a confusing error.

    The extension decides, and only the extension. A bucket prefix cannot be required as well: with
    MINIO_INPUT_BUCKET set the whole path is the key, so a bare "notes.pdf" is a legitimate object
    (see core/storage.split_object_path). "AsItIs" is excluded either way.
    """
    return isinstance(value, str) and value.strip().lower().endswith(_READABLE_EXTENSIONS)


def _paths(msg: Dict[str, Any]) -> List[str]:
    """The documents to read, from `file_urls`, or from `path` when it names a readable file.

    Blank entries are skipped: a caller filling a fixed-size list sends "" for the unused slot, and
    "" as a MinIO key fails on a confusing error.
    """
    urls = [u for u in (_get(msg, "file_urls", "fileUrls", default=[]) or []) if isinstance(u, str) and u.strip()]
    if urls:
        return urls
    single = _get(msg, "path", default="")
    return [single.strip()] if _looks_like_a_file(single) else []


def _names(msg: Dict[str, Any]) -> List[str]:
    """The names of the files in `file_urls`, from `document_names` or, failing that, `fileName`.

    `fileName` is the backend's name for the uploaded document — the same field name its chunk index
    uses. Taking it here is what makes a MinIO object stored WITHOUT an extension readable: the key
    stays "mom/mom-docs/transcripts" while fileName says "transcripts.txt", and the reader dispatches
    on the name, not on the key. An explicit document_names list wins, since it can name every file.
    """
    names = list(_get(msg, "document_names", "documentNames", default=[]) or [])
    if names:
        return names
    one = str(_get(msg, "fileName", "file_name", default="") or "").strip()
    return [one] if one else []


def parse_job(msg: Dict[str, Any]) -> KafkaJob:
    return KafkaJob(
        tenant_id=_get(msg, "tenant_id", "tenantId", default="") or "",
        user_id=_get(msg, "userId", "user_id", default="") or "",
        conversation_id=_get(msg, "conversationId", "conversation_id", default="") or "",
        mode=_get(msg, "mode", "momMode", "mom_mode", "requestType", "request_type", default="") or "",
        compare_mode=_get(msg, "compare_mode", "compareMode", default="") or "",
        document_ids=list(_get(msg, "document_ids", "documentIds", default=[]) or []),
        file_urls=_paths(msg),
        file_fids=list(_get(msg, "file_fids", "fileFids", "fIds", default=[]) or []),
        document_names=_names(msg),
        prompt=str(_get(msg, "prompt", default="") or "").strip(),
        mom_meta=_dict(_get(msg, "mom_meta", "momMeta", default={})),
        template_url=str(_get(msg, "template_url", "templateUrl", default="") or "").strip(),
        template_name=str(_get(msg, "template_name", "templateName", default="") or "").strip(),
        echo={k: v for k, v in msg.items() if k not in _INPUT_FIELDS and k not in _RESULT_FIELDS},
        raw=msg,
    )


# The failure description has no length cap but is required to be "short and clear", so a long
# Python traceback is truncated to one readable sentence rather than pasted in whole.
_MAX_DESCRIPTION = 400


def build_ack(job: KafkaJob, *, success: bool, description: str,
              bucket: str = "", object_key: str = "", action: str = "save",
              limit: Optional[int] = None) -> Dict[str, Any]:
    """The acknowledgement: the backend's own message back, plus the result.

    The IMIR backend's reference ack (2026-09-16) fixes the rule — the service writes ONLY `action`,
    `message` (SUCCESS, or ACK_FAILURE_MESSAGE), `summaryBucketName`, `summaryObjectKey` and
    `description`. Every other field goes back exactly as it arrived, `path` included: it used to be
    overwritten with the stored file's "bucket/key", and the reference keeps "AsItIs" beside SUCCESS.

    On failure the bucket/key are omitted rather than sent empty: an empty objectKey reads as
    "there is a file at ''" to anything that tries to fetch it.

    `limit`: the longest description, `_MAX_DESCRIPTION` unless given — an answer to a user's question
    about the minutes (minutes_edit) is allowed more than one sentence.
    """
    from config import ACK_FAILURE_MESSAGE

    desc = " ".join((description or "").split())
    most = limit or _MAX_DESCRIPTION
    if len(desc) > most:
        desc = desc[:most - 1].rsplit(" ", 1)[0] + "…"

    ack: Dict[str, Any] = dict(job.echo)
    # Three the backend matches on. Returned as sent when it sent them in this spelling; filled from
    # the snake_case spelling otherwise, so an older-style job (tenant_id, document_ids,
    # conversation_id) is still answered in the ack's camelCase. Never added when nothing was sent.
    if "fileIds" not in ack and job.document_ids:
        ack["fileIds"] = job.document_ids
    if "tenantId" not in ack and job.tenant_id:
        ack["tenantId"] = job.tenant_id
    if "conversationId" not in ack and job.conversation_id:
        ack["conversationId"] = job.conversation_id
    if "compareMode" not in ack and job.compare_mode:
        ack["compareMode"] = job.compare_mode

    ack["action"] = action
    ack["message"] = "SUCCESS" if success else ACK_FAILURE_MESSAGE
    if success and bucket and object_key:
        ack["summaryBucketName"] = bucket
        ack["summaryObjectKey"] = object_key
    ack["description"] = desc
    return ack


def unique_number() -> str:
    """20 digits, the UTC time to the microsecond: sorts in upload order, and never repeats in one pod."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def summary_object_key(job: KafkaJob, digest: str, ext: str = "docx", folder: str = "summaries",
                       name: str = "") -> str:
    """Tenant-scoped path: {tenantId}/summaries/{hash}/{name}.{ext}, or {hash}.{ext} with no name.

    The newer backend sends no tenant, and "" would produce a key starting with "/" — a folder
    named nothing, at the bucket root. The conversation id takes its place, then the user id, and
    "shared" only if a message carried none of the three.

    WHY THE HASH BECAME A FOLDER. The minutes used to be stored as `{hash}.docx`, and a download is
    named after the key's last part, so users got "78606119fe8f….docx". The name alone
    (`summaries/MoM-notes.docx`) would let two meetings uploaded as "notes.pdf" in one tenant
    overwrite each other, and an edit overwrite the version before it. Under its own hash folder
    every version still has its own object, exactly as before, and downloads as `MoM-notes.docx`.

    A UNIQUE NUMBER ENDS EVERY NAMED FILE (`MoM-notes-20260923143015123456.docx`, the UTC time to the
    microsecond). The hash alone repeats when the same minutes are generated twice, and the second
    upload then overwrote the first; with the number every upload is a new object. The state file
    (no name) keeps its fixed key, because the next prompt has to find it again.
    """
    scope = (job.tenant_id or job.conversation_id or job.user_id or "shared").strip("/") or "shared"
    if name:
        return f"{scope}/{folder}/{digest}/{name}-{unique_number()}.{ext}"
    return f"{scope}/{folder}/{digest}.{ext}"


# A "file name" that is really a storage id — the backend's upload keys are hashes and UUIDs — is no
# name at all: "MoM-9f3a51c0….docx" is the very kind of name summary_file_name replaces.
_STORAGE_ID = re.compile(r"[0-9a-f]{16,}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
# What breaks an object key, a URL built by joining strings, or a download's Content-Disposition
# header (an unquoted comma or semicolon there fails the download in Chrome). Replaced with "_";
# everything else stays as the user typed it, spaces and Hindi included.
_UNSAFE = re.compile(r'[\\/:*?"<>|#%{}^~\[\]`+&=;,$@]')
MAX_NAME_CHARS = 100


def _file_stem(name: str) -> str:
    """The user's file name, made safe for a MinIO key, without its extension. "" if it is no name."""
    name = str(name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(name)
    # Only a document extension is dropped: "Review 12.09.2026" keeps its ".2026".
    if ext.lower() in _READABLE_EXTENSIONS:
        name = stem
    if _STORAGE_ID.fullmatch(name.strip()):
        return ""
    # Control and invisible formatting characters go entirely — a right-to-left override would make
    # the name display as something other than it is.
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = " ".join(_UNSAFE.sub("_", name).split()).strip(" ._-")
    if len(name) > MAX_NAME_CHARS:
        cut = name[:MAX_NAME_CHARS]
        name = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ._-")
    return name


def summary_file_name(job: KafkaJob, title: str = "") -> str:
    """What the minutes are called: "MoM-" + the name of the file the user gave, without extension.

    The first document with a real name wins — its `document_names` entry (or `fileName`), else the
    last part of its MinIO key, the same name the reader used. A prompt with no document has no file
    name, so the meeting's title stands in; with neither, the minutes are plain "MoM".
    """
    names = [(job.document_names[i] if i < len(job.document_names) else "") or url.rsplit("/", 1)[-1]
             for i, url in enumerate(job.file_urls)] or list(job.document_names)
    for stem in (_file_stem(n) for n in [*names, title]):
        if stem:
            return f"MoM-{stem}"
    return "MoM"
