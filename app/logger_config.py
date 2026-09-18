"""One place that decides what the logs look like.

Every module does `logger = logging.getLogger(__name__)` and nothing else; this is the only
module that configures a handler. Worth having its own file because pod logs are the only way
anyone sees this service work — there is no UI — and two rules matter for reading them:

  * TIMESTAMPS ARE UTC, because the pod's clock is. The user reads them in IST (UTC+5:30), so
    the offset is printed rather than left to be remembered.
  * ONE LINE PER EVENT. A job's progress is grepped by conversation id, and a traceback split
    over forty lines in the OpenShift log viewer hides the line that matters.

kafka and elasticsearch are quietened: at INFO they narrate every poll and every request, which
buries this service's own lines at roughly fifty to one.

WHAT IS LEFT OUT, measured on a real 15-minute job on the cluster (2026-09-18): of 536 lines, 127 were
the readiness probe's `GET / 200`, 162 were httpx's `HTTP Request: POST ...` for every model call and
161 were `[KeyPool] Using key #0` — 450 lines that said nothing, around 86 that did. The probe lines
are dropped only when they succeed, so a failing probe still shows; httpx still logs its warnings;
and LOG_LEVEL=DEBUG brings those three back for troubleshooting (kafka and elasticsearch stay quiet).

PLAIN ASCII. The OpenShift log viewer showed `↓ ✓ ✗ — →` as `â†“ âœ“ âœ— â€” â†’`, 44 lines in that job.
They are replaced when the line is written, here, so the modules copied byte for byte from the audio
service keep their text.
"""
import logging
import os
import time

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

# Third-party loggers that are useful at WARNING and noise at INFO.
_NOISY = ("kafka", "kafka.conn", "kafka.client", "kafka.cluster", "kafka.coordinator",
          "kafka.consumer", "kafka.producer", "elasticsearch", "elastic_transport",
          "urllib3", "minio")
# One line per model call. Kept for LOG_LEVEL=DEBUG, where the call-by-call timing is the point.
_PER_CALL = ("httpx", "httpcore")
# Its retry warnings carry a full traceback — about 40 lines per retry, three retries per request when
# Elasticsearch is down. The job logs the failure itself, in one line, so only its errors are kept.
_RETRIES_WITH_TRACEBACKS = ("elastic_transport",)

# What the log viewer mangles, and what reads the same in plain ASCII.
_ASCII = str.maketrans({"—": "-", "–": "-", "→": "->", "←": "<-", "✓": "OK", "✗": "x",
                        "↓": "get", "↑": "put", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"'})

# The pod's readiness and liveness probes. A success says nothing; a failure is still logged.
_PROBE_PATHS = {"/", "/health"}

_configured = False


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).translate(_ASCII)


class _NoProbeSuccess(logging.Filter):
    """Drops uvicorn's access line for a successful GET / or GET /health — one every few seconds."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status = args[1], str(args[2]).split("?", 1)[0], args[4]
            if method == "GET" and path in _PROBE_PATHS and isinstance(status, int) and status < 400:
                return False
        return True


def configure() -> None:
    """Install the handler. Safe to call more than once; the second call does nothing."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(_PlainFormatter("%(asctime)s UTC %(levelname)-7s [%(name)s] %(message)s",
                                         datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        handlers=[handler],
        force=True,          # uvicorn installs its own root handler first; replace it
    )
    logging.Formatter.converter = time.gmtime
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _RETRIES_WITH_TRACEBACKS:
        logging.getLogger(name).setLevel(logging.ERROR)
    if LOG_LEVEL != "DEBUG":
        for name in _PER_CALL:
            logging.getLogger(name).setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").addFilter(_NoProbeSuccess())
    _configured = True
