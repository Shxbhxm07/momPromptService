# Minutes of Meeting from a user's prompt and, optionally, a document. No audio, no speech model:
# the minutes are written by llama-service (watsonx on the test cluster), reached over HTTP.
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# tesseract + Hindi and English data: scanned PDFs have no text layer, and without OCR they read
# as empty. libreoffice-writer: reads legacy .doc, and counts the laid-out pages of the JSSD minutes
# (a CONFIDENTIAL document states its page count on page 1). curl: the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        tesseract-ocr \
        tesseract-ocr-hin \
        tesseract-ocr-eng \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# The minutes writer's over-correction guard needs the full English wordlist. Without it the guard
# falls back to a 293-word list and rewrites ordinary words into product names ("coherent" ->
# "Qdrant"). Its own layer, after the large one above, so adding it does not re-download LibreOffice.
RUN apt-get update && apt-get install -y --no-install-recommends wamerican \
    && rm -rf /var/lib/apt/lists/* \
    && test -s /usr/share/dict/american-english

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The whole service is one flat package. Its contents land at /app, which is the working directory
# and therefore already on sys.path, so every module imports its neighbours by plain name
# (`from config import ...`). That is what keeps docx_export.py, documents.py and minio_client.py
# BYTE-IDENTICAL to their originals in ~/offline-mom-api/api — they import nothing but `config`,
# so a change there is copied here with cp and nothing else.
COPY app/ ./

# /app is already sys.path[0] for `uvicorn main:app` run from here, but not for a script run
# from anywhere else (a check mounted into /tmp, `docker exec python -c ...`). Setting it
# explicitly means every way of running the code finds the modules.
ENV PYTHONPATH=/app

EXPOSE 8000

# "/" is static; /health calls llama-service and must not be the liveness probe (see ~/offline-mom-api/api/Dockerfile).
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
