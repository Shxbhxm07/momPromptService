# mom-prompt-service

JSSD-format Minutes of Meeting from a prompt and, optionally, a document (PDF, DOCX, DOC, TXT). No audio.
Same Kafka / MinIO / Elasticsearch flow and the same message and acknowledgement as the audio MoM
service (`~/offline-mom-api/docs/kafka-contract.md`), plus one field: `prompt`.

- **Run it all locally:** `cp .env.example .env`, put your OpenRouter key in it, then
  `docker compose up -d`. That brings up this service plus its own MinIO, Elasticsearch, Kafka,
  Kafka UI and minutes writer — its own network, nothing shared with another project.
- **Build only:** `docker build -t mom-prompt-service .` from this folder.
- **Run only:** one container; it serves HTTP on 8000 and consumes Kafka in the same process.
- **Kafka:** jobs on `mom-prompt.jobs`, acknowledgements on `mom-prompt.acks` (`KAFKA_JOB_TOPIC`, `KAFKA_ACK_TOPIC`).
- **HTTP:** `POST /v1/mom-prompt` with the same JSON as a Kafka job returns the acknowledgement. `GET /docs` shows examples.
- **Needs:** an LLM endpoint for the in-process minutes writer (`VLLM_API_BASE`, `LLM_MODEL_PATH`, credentials), MinIO (`MINIO_*`), Elasticsearch (`ELASTIC_*`, `CHUNK_INDEX`). All settings are in `app/config.py`.
- **OpenShift:** apply `deploy/openshift/01-deployment.yaml` → `02-service.yaml` → `03-route.yaml`, in order (project `mom-ai`). Every setting is a plain env value; fill each `CHANGE_ME` first. Keep the route's 3600 s timeout, because a job holds the request open until the minutes are written.

## Layout

One flat package — every module imports its neighbours by plain name, and `app/` is the
working directory in the image, so nothing needs a package prefix.

```
app/  config.py          every setting, from env
      main.py            FastAPI: /, /health, POST /v1/mom-prompt; starts the consumer
      kafka_consumer.py  the Kafka loop: one job at a time, commit after the ack
      minutes.py         the job: read documents, build the text, get minutes, store, index, ack
      kafka_contract.py  the inbound message and the acknowledgement
      setup.py           MinIO / Elasticsearch / minutes-writer clients, made on first use
      document_checker.py  is this attachment readable, and small enough
      logger_config.py   log format and level (UTC; the user reads IST = UTC+5:30)
      mom.py             runs the minutes writer in process, maps its answer to the MoM shape
      llama/             the minutes writer (prompts, LLM calls) — a package, not a separate service
      minio_client.py    MinIO
      es_client.py       Elasticsearch: the minutes record and the search chunks
      documents.py       PDF / DOCX / DOC / TXT → text, with OCR for scanned pages
      docx_export.py     the JSSD .docx renderer
Dockerfile
requirements.txt
deploy/openshift/  01-deployment, 02-service, 03-route
```

`documents.py`, `docx_export.py` and `minio_client.py` are **byte-identical copies** of files in
`~/offline-mom-api/api`. They import nothing but `config`, which is what keeps them copyable: when
one changes there, `cp` it here and nothing else has to be touched.

## Local test environment

`docker compose up -d` gives you, on `localhost`:

| | |
|---|---|
| this service | http://localhost:8010/docs |
| Kafka UI | http://localhost:8090 |
| MinIO console | http://localhost:9001 (`minioadmin` / `minioadmin`) |
| Elasticsearch | http://localhost:9200 |

Topics `mom-prompt.jobs` / `mom-prompt.acks` and the `mom` bucket are created on first start.
Upload a PDF/DOCX/DOC/TXT to `mom/mom-docs/` in the MinIO console, then produce a job in Kafka UI
on `mom-prompt.jobs` and read the acknowledgement on `mom-prompt.acks`.

**One service.** The minutes writer runs inside the service container — there is no separate LLM
pod and no `LLAMA_URL`. One image, one Deployment.

See `CLAUDE.md` for how it works, its status and what is next.
