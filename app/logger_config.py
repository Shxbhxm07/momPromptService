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
"""
import logging
import os
import time

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

# Third-party loggers that are useful at WARNING and noise at INFO.
_NOISY = ("kafka", "kafka.conn", "kafka.client", "kafka.cluster", "kafka.coordinator",
          "kafka.consumer", "kafka.producer", "elasticsearch", "elastic_transport",
          "urllib3", "minio")

_configured = False


def configure() -> None:
    """Install the handler. Safe to call more than once; the second call does nothing."""
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s UTC %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,          # uvicorn installs its own root handler first; replace it
    )
    logging.Formatter.converter = time.gmtime
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured = True
