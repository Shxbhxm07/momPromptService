"""mom-prompt-service: Minutes of Meeting in the JSSD format from a user's prompt and, optionally, a
document (PDF, DOCX, DOC or TXT). No audio.

Two ways in, one job (minutes.py):
  * Kafka: a message on KAFKA_JOB_TOPIC, the acknowledgement on KAFKA_ACK_TOPIC (kafka_consumer.py, started here).
  * HTTP:  POST /v1/mom-prompt with the same JSON; the response body is the same acknowledgement, and
           that ack is ALSO published to KAFKA_ACK_TOPIC (HTTP_ACKS_TO_KAFKA), where the backend listens.

The message and the acknowledgement are the audio service's (~/offline-mom-api/docs/kafka-contract.md) plus one field,
`prompt`. The body is taken as a plain JSON object, not a typed model, so the backend's fields go back
exactly as sent: "{}" stays a string, null stays null.
"""
import logging
from typing import Any, Dict

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

import kafka_consumer
import logger_config
import minutes
import setup
from config import (ENABLE_KAFKA, ENABLE_OCR, HTTP_ACKS_TO_KAFKA, JOB_KIND, KAFKA_ACK_TOPIC, KAFKA_JOB_TOPIC,
                    MIN_PAGE_TEXT_CHARS, OCR_DPI, OCR_LANGS, OCR_MAX_PAGES)
from kafka_contract import build_ack, parse_job
from mom import MomGenerator

logger_config.configure()
logger = logging.getLogger("main")

app = FastAPI(title="MoM from prompt",
              description="Minutes of Meeting in the JSSD format from a prompt and, optionally, a document.")


def ocr_status() -> Dict[str, Any]:
    """Which OCR reads scanned pages, with what settings, and whether it can — for /health and the log.

    The settings come from env (config.py), so a typo is one console edit away. A language that is not
    installed fails every scanned page with a TesseractError while typed PDFs keep working, which is easy
    to miss; this names it the moment the pod starts, instead of on the first scanned upload.
    """
    status: Dict[str, Any] = {"engine": "tesseract", "enabled": ENABLE_OCR, "languages": OCR_LANGS,
                              "dpi": OCR_DPI, "max_pages": OCR_MAX_PAGES or "no limit",
                              "scan_below_chars": MIN_PAGE_TEXT_CHARS}
    try:
        import pytesseract
        status["version"] = str(pytesseract.get_tesseract_version()).split()[0]
        installed = sorted(set(pytesseract.get_languages(config="")) - {"osd"})
        status["installed"] = installed
        missing = [lang for lang in OCR_LANGS.split("+") if lang not in installed]
        status["ok"] = not missing
        if missing:
            status["problem"] = (f"OCR_LANGS={OCR_LANGS!r} names {', '.join(missing)}, which this image does "
                                 f"not have; installed: {', '.join(installed)}. Every scanned page will fail.")
    except Exception as e:
        status.update(ok=False, problem=f"Tesseract is not usable in this image: {type(e).__name__}: {e}")
    return status


@app.on_event("startup")
def _startup():
    if ENABLE_KAFKA:
        kafka_consumer.start()
    w = MomGenerator.describe()
    logger.info(f"✓ Ready | job={JOB_KIND} | kafka={'on' if ENABLE_KAFKA else 'off'} "
                f"| minutes writer in process: {w['model']} at {w['url']}")
    ocr = ocr_status()
    if not ocr["enabled"]:
        logger.warning("OCR is OFF (ENABLE_OCR=false): scanned pages are skipped, and a PDF that is only "
                       "scans fails")
    elif not ocr["ok"]:
        logger.error(f"OCR: {ocr['problem']}")
    else:
        pages = f"up to {OCR_MAX_PAGES} scanned pages" if OCR_MAX_PAGES else "every scanned page"
        logger.info(f"OCR: Tesseract {ocr['version']}, languages {OCR_LANGS}, {OCR_DPI} dpi, {pages} per document, "
                    f"a page is a scan below {MIN_PAGE_TEXT_CHARS} characters of its own text")


@app.on_event("shutdown")
def _shutdown():
    kafka_consumer.stop.set()


@app.get("/")
def root():
    """Liveness. Static, except that a dead consumer thread fails it, so the pod is restarted."""
    if ENABLE_KAFKA and not kafka_consumer.is_alive():
        return JSONResponse(status_code=503, content={"service": "mom-prompt-service",
                                                      "status": "kafka consumer thread stopped"})
    return {"service": "mom-prompt-service", "status": "ok"}


@app.get("/health")
def health():
    llm_ok = MomGenerator().is_ready()
    ocr = ocr_status()
    return {
        "status": "healthy" if llm_ok and (kafka_consumer.is_alive() or not ENABLE_KAFKA)
                  and (ocr["ok"] or not ocr["enabled"]) else "degraded",
        # In process, so "configured" rather than "reachable": an endpoint and a credential are set.
        # A round-trip to the model on every probe would cost time and tokens; a job surfaces a bad one.
        "minutes_writer": {"in_process": True, **MomGenerator.describe(), "configured": llm_ok},
        "kafka": {"enabled": ENABLE_KAFKA, "consumer_running": kafka_consumer.is_alive(),
                  "jobs": KAFKA_JOB_TOPIC, "acks": KAFKA_ACK_TOPIC},
        # Scanned pages only; a typed PDF, DOCX, DOC or TXT never goes near it.
        "ocr": ocr,
    }


_EXAMPLE = {
    "document_ids": ["momp-001"],
    "tenant_id": "test",
    "conversation_id": "momp-001",
    "prompt": "Minutes of the quarterly budget review held on 12 Sep 2026 at HQ. Chaired by the Director "
              "Finance; attended by the heads of IT, Admin and Procurement. Focus on the approved "
              "allocations and who does what by when.",
    "file_urls": ["mom/mom-docs/budget-review-notes.pdf"],
    "document_names": ["budget-review-notes.pdf"],
    "metaData": "{}", "uploadType": "", "grading": "", "data": None, "themes": "",
    "path": "AsItIs", "user": True, "clientSessionId": "sess-8f2c1a", "queryId": "q-41c9",
}


@app.post("/v1/mom-prompt")
def mom_prompt(payload: Dict[str, Any] = Body(..., openapi_examples={
        "prompt_and_pdf": {"summary": "A prompt and a PDF already in MinIO", "value": _EXAMPLE},
        "prompt_only": {"summary": "A prompt describing the meeting, no document",
                        "value": {k: v for k, v in _EXAMPLE.items()
                                  if k not in ("file_urls", "document_names")}},
        "prompt_and_template": {"summary": "A prompt plus the HQ's official JSSD template",
                                "value": {**{k: v for k, v in _EXAMPLE.items()
                                             if k not in ("file_urls", "document_names")},
                                          "template_url": "mom/templates/hq-jssd-template.docx",
                                          "template_name": "hq-jssd-template.docx"}}})):
    """Minutes from a prompt and/or documents: the Kafka job, over HTTP.

    200 carries the SUCCESS acknowledgement; 422 (no prompt and no file), 503 (MinIO or Elasticsearch
    unreachable) and 500 (the job failed) carry a FAILURE acknowledgement saying why.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="The body must be a JSON object, like a Kafka job.")
    job = parse_job(payload)
    if not job.prompt and not job.file_urls and not job.file_fids:
        return _answer(422, build_ack(
            job, success=False, description="No prompt and no file_urls: nothing to write minutes from."))
    try:
        c = setup.clients()
    except Exception as e:
        logger.error(f"[JOB {job.conversation_id}] MinIO / Elasticsearch not ready: {e}")
        return _answer(503, build_ack(
            job, success=False, description=f"Storage not ready — {type(e).__name__}: {e}"))
    ack = minutes.process(job, c)
    return _answer(200 if ack.get("message") == "SUCCESS" else 500, ack)


def _answer(status: int, ack: Dict[str, Any]) -> JSONResponse:
    """The HTTP reply — and the same ack on KAFKA_ACK_TOPIC, where the backend listens for it.

    Published before replying, so a caller that has stopped waiting (a route or client timeout on a
    long job) still gets its result on the topic.
    """
    if ENABLE_KAFKA and HTTP_ACKS_TO_KAFKA:
        kafka_consumer.publish_ack(ack)
    return JSONResponse(status_code=status, content=ack)
