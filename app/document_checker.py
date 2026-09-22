"""Is this attachment something we can write minutes from?

Split out for the same reason ~/offline-mom-api has audio_checker.py: the answer is a judgement about
the FILE, not about the job, and it is the one place a bad attachment can be turned into a sentence
the user can act on ("Send PDF, DOCX, DOC or TXT") instead of a parser traceback.

WHY THE TYPE IS CHECKED AT ALL. utils/documents.extract_text_blocks reads anything it does not
recognise as plain text, which is right for a .txt with an odd name and wrong for everything else:
an image or a spreadsheet would come back as mojibake and the model would write minutes from it,
confidently. So the name's extension OR the file's own magic bytes must say PDF, DOCX, DOC or TXT.
The magic bytes are consulted second because a MinIO key often has no extension at all.
"""
import os

from config import MAX_DOC_MB
from documents import SUPPORTED_EXTENSIONS, _sniff


class UnreadableDocument(ValueError):
    """Carries a message meant for the acknowledgement: it says what to send instead."""


def check(raw: bytes, name: str) -> None:
    """Raise UnreadableDocument if this file is too big or is not a document. Otherwise return."""
    size_mb = len(raw) / 1048576
    if MAX_DOC_MB and size_mb > MAX_DOC_MB:
        raise UnreadableDocument(f"{name} is {size_mb:.0f} MB; the limit is {MAX_DOC_MB} MB.")
    if os.path.splitext(name)[1].lower() not in SUPPORTED_EXTENSIONS and not _sniff(raw):
        raise UnreadableDocument(f"{name}: unsupported file type. Send PDF, DOCX, DOC or TXT.")
