#!/bin/sh
# Offline checks: re-prompting (labels, questions, "his/her", spelling, replies) and ITEM grouping.
# No model, no network, no cluster — every model answer is a stand-in. Run from the repo root after
# `docker compose build`:   sh tests/run.sh
cd "$(dirname "$0")/.." || exit 1
IMAGE=${IMAGE:-mom-prompt-service:local}
# The scanned test PDFs (not in git) are used when present, e.g. to check every scanned page is read.
SCANS=""; [ -d "$PWD/scanned-transcripts" ] && SCANS="-v $PWD/scanned-transcripts:/scans:ro"
# The long test documents (t5.txt …, not in git) are used when present, for the timing and same-minutes checks.
SCANS="$SCANS -v $PWD:/repo:ro"
for t in tests/t_*.py; do
  printf '%-22s ' "$(basename "$t")"
  docker run --rm -e PYTHONPATH=/app -e ENABLE_KAFKA=false -e ENABLE_CHUNK_INDEX=false -e LLM_API_KEY=offline-test $SCANS \
    -v "$PWD/app:/app" -v "$PWD/tests:/t:ro" -w /app --entrypoint python "$IMAGE" "/t/$(basename "$t")" 2>&1 \
    | grep -E "passed|Traceback|Error" | tail -1
done
