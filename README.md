# mom-prompt-service

JSSD-format Minutes of Meeting from a prompt and, optionally, a document (PDF, DOCX, DOC, TXT). No audio.
Same Kafka / MinIO / Elasticsearch flow and the same message and acknowledgement as the audio MoM
service (`~/offline-mom-api/docs/kafka-contract.md`), plus one field: `prompt`.

- **Build:** `docker build -t mom-prompt-service .` from this folder.
- **Run:** one container; it serves HTTP on 8000 and consumes Kafka in the same process.
- **Kafka:** jobs on `mom-prompt.jobs`, acknowledgements on `mom-prompt.acks` (`KAFKA_JOB_TOPIC`, `KAFKA_ACK_TOPIC`).
- **HTTP:** `POST /v1/mom-prompt` with the same JSON as a Kafka job returns the acknowledgement. `GET /docs` shows examples.
- **Needs:** llama-service (`LLAMA_URL`), MinIO (`MINIO_*`), Elasticsearch (`ELASTIC_*`, `CHUNK_INDEX`). All settings are in `app/config.py`.
- **OpenShift:** `deploy/openshift.yaml`. Keep the route's 3600 s timeout, because a job holds the request open until the minutes are written.

## Layout

One flat package — every module imports its neighbours by plain name, and `app/` is the
working directory in the image, so nothing needs a package prefix.

```
app/  config.py          every setting, from env
      main.py            FastAPI: /, /health, POST /v1/mom-prompt; starts the consumer
      kafka_consumer.py  the Kafka loop: one job at a time, commit after the ack
      minutes.py         the job: read documents, build the text, get minutes, store, index, ack
      kafka_contract.py  the inbound message and the acknowledgement
      setup.py           MinIO / Elasticsearch / llama-service clients, made on first use
      document_checker.py  is this attachment readable, and small enough
      logger_config.py   log format and level (UTC; the user reads IST = UTC+5:30)
      mom.py             calls llama-service /summarize, maps its answer to the MoM shape
      minio_client.py    MinIO
      es_client.py       Elasticsearch: the minutes record and the search chunks
      documents.py       PDF / DOCX / DOC / TXT → text, with OCR for scanned pages
      docx_export.py     the JSSD .docx renderer
Dockerfile
requirements.txt
deploy/openshift.yaml
```

`documents.py`, `docx_export.py` and `minio_client.py` are **byte-identical copies** of files in
`~/offline-mom-api/api`. They import nothing but `config`, which is what keeps them copyable: when
one changes there, `cp` it here and nothing else has to be touched.

See `CLAUDE.md` for how it works, its status and what is next.
