# The minutes writer for THIS service, and only this service.
#
# WHY IT LIVES HERE. It began as a copy of ~/offline-mom-api/llama-service, which on the cluster is
# ONE Deployment answering mom-consumer, translate-consumer and this service. Sharing it means the
# MoM prompt cannot be changed for documents without changing it for recordings too, and the prompt
# is hard-coded in prompts.py with no environment override — so a separate prompt needs a separate
# image. This service exists to write minutes from notes and reports, not transcripts, so that
# change is the whole point. Vendored WHOLE and UNMODIFIED on 2026-09-15 so it works unchanged from
# day one; trimming the nine endpoints this service never calls is a later, separate decision.
#
# It is a pure FastAPI/httpx PROXY to the LLM API — no local GPU/PyTorch needed.
# python:3.11-slim (~50MB) pulls fast and reliably; the old nvcr.io NVIDIA PyTorch base (~20GB)
# caused flaky "connection reset" pulls in CI.
FROM python:3.11-slim

WORKDIR /app

# Full English wordlist (104,334 words) at /usr/share/dict/american-english.
#
# This is the term-corrector's OVER-CORRECTION GUARD, and it must be IN THE IMAGE. It used to come
# only from a docker-compose bind mount, which meant it existed on the GB10 and nowhere else — so on
# OCP the guard fell back to the 293-word list in lexicon/common_words.txt and the conservative
# phonetic matcher stopped being conservative.
#
# Measured against the real lexicon: with the mount, 0 of 72,255 English words are altered. Without
# it, 102 ordinary words get rewritten into product names — "coherent" -> "Qdrant", "catholic" ->
# "Docutalk", "derision" -> "diarization". Those are words that appear in real meetings, and the
# corruption lands in the transcript AND the minutes with nothing logged.
RUN apt-get update && apt-get install -y --no-install-recommends wamerican \
    && rm -rf /var/lib/apt/lists/* \
    && test -s /usr/share/dict/american-english

# Install dependencies
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
    httpx \
    fastapi \
    uvicorn[standard] \
    python-multipart
COPY . /app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
