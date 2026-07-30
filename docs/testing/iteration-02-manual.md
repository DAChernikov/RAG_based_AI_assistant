# Iteration 02 manual testing

Impact: `user-visible`. Audience: repository owner testing on macOS with Terminal and
OrbStack/Docker Desktop. Commands create only local PostgreSQL/Redis/container data unless a
scenario explicitly uses the existing read-only S3 download.

Never print `.env`. Never use Neon. Never run S3 upload/delete commands.

## Common prerequisites

- checkout `feature/product-v1`;
- Python 3.11/3.12, Poetry 2, Docker Compose or OrbStack;
- native Ollama for real-model scenarios;
- `.env` created from `.env.example` with local credentials;
- one terminal per long-running process.

Stop application processes with `Ctrl-C`. `docker compose stop` preserves local volumes.

## A. Automated checks

Commands:

```bash
make install
make format-check
make lint
make test
docker compose config --quiet
make infra-up
make migrate
make integration-test
make smoke-test
```

Expected: formatting/lint pass, unit tests pass, PostgreSQL/Redis become healthy, migrations
reach `head`, integration and smoke tests pass without internet/Ollama/S3/Neon.

Troubleshooting: inspect `docker compose ps`; ensure ports 5432/6379 are free; ensure the local
application password used by Compose matches `INTEGRATION_DATABASE_URL`.

Created data: named PostgreSQL/Redis volumes and short-lived integration rows/streams.

## B. Start queued mode

```bash
make infra-up
make migrate
make seed-dev
ollama serve
make run-worker
make run-api
```

For a native worker, use localhost URLs in `.env`. For a container worker, use Compose service
names and `host.docker.internal` for Ollama.

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

Expected: `/health` is `ok`; `/ready` reports `execution_mode=queued`, database/Redis/worker,
retriever and model ready. If `/models` is unsupported, `model_status=unsupported` and
readiness remains conservative.

## C. Normal user request

```bash
curl -i -X POST http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is Apache Spark?"}'
```

Expected: HTTP 200, legacy `AskResponse`, plus `X-Inference-Job-Id` and `X-Correlation-Id`.
PostgreSQL receives a conversation, user message, completed job, assistant message and answer.

## D. Streaming

```bash
curl -N -X POST http://127.0.0.1:8000/ask/stream \
  -H 'Content-Type: application/json' \
  -d '{"question":"How do I safely read a Python dictionary key?"}'
```

Expected order: `queued`, `started`, `meta`, zero or more `token`, then `completed`; each queued
event has `id`, `event`, and `data`. Save an `id` and reconnect with:

```bash
curl -N -X GET http://127.0.0.1:8000/v1/inference-jobs/JOB_ID/events \
  -H 'Last-Event-ID: REDIS_STREAM_ID'
```

## E. Async job API

```bash
curl -i -X POST http://127.0.0.1:8000/v1/inference-jobs \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is Trino?"}'
curl http://127.0.0.1:8000/v1/inference-jobs/JOB_ID
curl -N http://127.0.0.1:8000/v1/inference-jobs/JOB_ID/events
```

Expected: create returns 202 with job/conversation/correlation IDs and URLs; GET moves from
queued/running to completed; terminal response includes answer and sources. These endpoints
are development-only until authentication/RBAC.

## F. Conversation history

Create the first request and take `conversation_id` from the async job response:

```bash
curl -X POST http://127.0.0.1:8000/v1/inference-jobs \
  -H 'Content-Type: application/json' \
  -d '{"question":"First question"}'
curl -X POST http://127.0.0.1:8000/v1/inference-jobs \
  -H 'Content-Type: application/json' \
  -d '{"question":"Second question","conversation_id":"CONVERSATION_ID"}'
curl http://127.0.0.1:8000/v1/conversations/CONVERSATION_ID
```

Expected: ordered user/assistant messages with increasing sequence numbers. A random or foreign
conversation UUID returns 404.

## G. Idempotency

```bash
curl -i -X POST http://127.0.0.1:8000/v1/inference-jobs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-same-key' \
  -d '{"question":"Idempotent question"}'
```

Repeat exactly: expected same job ID and `reused=true`; PostgreSQL has one job/answer. Change
the question with the same key: expected HTTP 409.

## H. Worker unavailable

Stop worker with `Ctrl-C`, wait longer than `WORKER_STALE_AFTER_SEC`, then check `/ready`.
Expected `worker_ready=false`, status `not_ready`. Submit an async job: it stays queued.
Restart `make run-worker`; expected processing resumes. Do not expect silent direct fallback.

## I. Model server unavailable

Stop Ollama, keep worker running, submit a job, and watch only sanitized structured logs.
Expected bounded `retrying` events; after max attempts PostgreSQL status is failed, a failed
event is emitted, and a sanitized DLQ row exists:

```bash
docker compose exec redis redis-cli XLEN rag:inference:dlq
```

No prompt, token, credential, endpoint secret, authorization header or traceback should appear
in API responses/DLQ.

## J. Existing S3 retriever artifact download

This is the only scenario that uses owner-provided S3 credentials. Put them in `.env`, never
in commands/history/docs. Point `ARTIFACTS_DIR` to a new local test directory and enable
`FORCE_ARTIFACTS_DOWNLOAD=true`, then start one worker.

Expected: one `HEAD`/download, safe staging validation and atomic activation. Disable force
download and restart; with valid local files S3 must not be initialized or called. Perform no
upload/delete. If configuration is incomplete, expect a short sanitized error.

## K. Read-only PostgreSQL inspection

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Inside `psql`:

```sql
SELECT id, slug, created_at FROM tenants ORDER BY created_at DESC LIMIT 10;
SELECT conversation_id, role, sequence_number, created_at FROM messages ORDER BY created_at DESC LIMIT 20;
SELECT id, status, attempt_count, created_at FROM inference_jobs ORDER BY created_at DESC LIMIT 20;
SELECT job_id, mode, model_name, created_at FROM answers ORDER BY created_at DESC LIMIT 20;
SELECT answer_id, source_type, source_id, rank, score FROM answer_sources ORDER BY answer_id, rank LIMIT 30;
```

These are read-only and do not reveal message contents or credentials.

## L. Cleanup

First stop processes with `Ctrl-C` and run `docker compose stop`. The following commands are
destructive to local Iteration 2 data; run them only after the owner explicitly confirms that
the local PostgreSQL/Redis volumes may be deleted:

```bash
docker compose down -v
```

To preserve databases, use only:

```bash
docker compose down
```

Never delete existing S3 objects or Neon data as cleanup.

## Negative cases and troubleshooting

- HTTP 503 plus seed message: run `make migrate && make seed-dev`.
- Database/Redis false in readiness: inspect healthchecks and URLs; no fallback occurs.
- Worker false: confirm one worker process and heartbeat TTL/clock.
- Model unsupported: verify `MODEL_READINESS_PATH`; do not replace it with a generation prompt.
- Job timeout from `/ask`: job remains active; use the ID header and async GET endpoint.
- Pending message after crash: restart worker; it uses `XAUTOCLAIM`.
- Artifact failure: previous active artifacts must remain untouched; inspect local worker logs.

Owner-required manual tests: real Ollama behavior, real local artifacts, optional existing
read-only S3 download, Terminal/OrbStack ergonomics, and resource usage on the 24 GB Mac.
