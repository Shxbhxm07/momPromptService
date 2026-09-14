"""The clients this service talks to: MinIO, Elasticsearch and llama-service.

MADE ON FIRST USE, NOT AT STARTUP, and that is deliberate. An Elasticsearch that is down must
not stop the pod coming up — the service would then crash-loop and nobody could reach /health to
find out why. Instead the first job to need it fails, and says so in its acknowledgement, which
is the message the backend already reads.

They are made once and shared: each holds its own connection pool, and a new client per job
would open a new pool per job.

NOTE the filename matches ~/offline-mom-api's convention, not setuptools'. Nothing here packages
anything; this service is run from its source in the image, never pip-installed.
"""
import logging
import threading
from dataclasses import dataclass

from es_client import ChunkIndex, MomIndex
from minio_client import ObjectStore
from mom import MomGenerator

logger = logging.getLogger(__name__)


@dataclass
class Clients:
    store: ObjectStore
    index: MomIndex
    chunks: ChunkIndex
    llm: MomGenerator


_clients = None
_lock = threading.Lock()


def clients() -> Clients:
    """The shared clients, built on the first call. Raises if the indices cannot be ensured."""
    global _clients
    with _lock:
        if _clients is None:
            index = MomIndex()
            index.ensure_indices()
            _clients = Clients(ObjectStore(), index, ChunkIndex(), MomGenerator())
            logger.info("clients ready: MinIO, Elasticsearch, llama-service")
        return _clients
