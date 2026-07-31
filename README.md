# RAG-based AI Assistant

Self-hosted RAG-ассистент с FastAPI, Telegram-клиентом, PostgreSQL application state,
Redis Streams и отдельным inference worker. Генерация выполняется только через локальный или
self-hosted OpenAI-compatible HTTP API. Коммерческие внешние LLM API не используются.

Реализованы два режима:

- `direct` — совместимый диагностический путь, где API загружает текущий retriever;
- `queued` — основной продуктовый путь: API сохраняет запрос в PostgreSQL, отправляет job в
  Redis Streams, а retriever и model client живут в inference worker.

Локальная аутентификация, tenant isolation, роли `admin`/`user`, API keys и audit log
реализованы в Iteration 3. Website/Git connectors и отдельный incremental ingestion worker
реализованы в Iteration 5. Web UI, JDBC connector, pgvector и multi-label retrieval пока не
реализованы.

Каталог поддерживает typed Website/Git/JDBC configs и immutable source versions с atomic
activation/rollback. Website crawl и Git fetch/parse работают по on-demand refresh; JDBC,
embeddings, retrieval integration и scheduler пока не реализованы.

## Реализованная runtime-схема

```text
Telegram / HTTP client
          |
          v
      FastAPI API ----------------> PostgreSQL 16 (system of record)
          |                                |
          +------ Redis Streams jobs ------+
                    /              \
                   v                v
          inference worker    ingestion worker
             |       |          |          |
             v       v          v          v
       retriever  model API  Website Git  PostgreSQL content
```

Redis хранит delivery/events/heartbeat, но не является единственным хранилищем результата.
Conversation, messages, jobs, answers и sources сохраняются в PostgreSQL. Token events имеют
ограниченную retention и не записываются по одному в PostgreSQL.

## Быстрый queued-запуск

```bash
make install
cp .env.example .env
make infra-up
make migrate
poetry run python -m app.state.bootstrap_admin \
  --tenant-slug example \
  --tenant-name "Example tenant" \
  --username admin \
  --display-name "Local administrator"
make run-worker
make run-ingestion-worker
make run-api
```

Bootstrap-команда запрашивает пароль без отображения и не принимает его аргументом командной
строки. Перед worker запустите Ollama нативно:

```bash
ollama serve
ollama pull qwen2.5-coder:7b
```

Загрузка модели выполняется вручную и никогда не происходит при Docker build, import или
unit tests.

Проверка:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl -X POST http://127.0.0.1:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"tenant_slug":"example","username":"admin","password":"<password>"}'
curl -X POST http://127.0.0.1:8000/ask \
  -H "Authorization: Bearer <access-token>" \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Apache Spark?"}'
```

`/health` проверяет только liveness API process. `/ready` в queued mode проверяет PostgreSQL,
Redis и свежий worker heartbeat, включая retriever/model readiness. Отказ queued dependencies
никогда не вызывает silent fallback в direct mode.

## API

Backward-compatible:

- `GET /health`
- `GET /ready`
- `POST /ask`
- `POST /ask/stream`

Protected asynchronous API:

- `POST /v1/inference-jobs`
- `GET /v1/inference-jobs/{job_id}`
- `GET /v1/inference-jobs/{job_id}/events`
- `GET /v1/conversations/{conversation_id}`

Authentication and administration:

- `POST /v1/auth/login`, `/v1/auth/refresh`, `/v1/auth/logout`;
- `GET /v1/auth/me`;
- `GET|POST /v1/admin/users`;
- `GET|PATCH|DELETE /v1/admin/users/{user_id}`;
- `GET|POST /v1/api-keys`;
- `DELETE /v1/api-keys/{key_id}`.

Admin knowledge catalog:

- `GET|POST /v1/admin/knowledge-bases`;
- `GET|PATCH|DELETE /v1/admin/knowledge-bases/{id}`;
- `GET|POST /v1/admin/knowledge-sources`;
- `GET|PATCH|DELETE /v1/admin/knowledge-sources/{id}`;
- knowledge base/source linking;
- version and ingestion-run inspection;
- on-demand source refresh, ingestion events/status and cancellation;
- version activation and rollback.

`POST /v1/admin/knowledge-sources/{id}/refresh` требует `Idempotency-Key` и создаёт durable
ingestion run. Website connector соблюдает allowlist/limits и SSRF policy; Git connector делает
изолированный fetch и фиксирует resolved commit SHA. JDBC refresh пока не реализован.

`/health` and sanitized `/ready` remain public. `/admin/runtime` is admin-only. Jobs,
conversations and SSE streams are filtered by authenticated tenant and user. Telegram and
external clients use `X-API-Key`; its full value is displayed only once.

`AskRequest` получил только optional `conversation_id`. `Idempotency-Key` поддерживается для
`/ask`, `/ask/stream` и создания async job. Одинаковый key и payload возвращает существующий
job; другой payload с тем же key получает HTTP 409.

Queued SSE содержит:

```text
id: <redis-stream-id>
event: queued|started|meta|token|retrying|completed|failed
data: <versioned JSON contract>
```

`Last-Event-ID` возобновляет чтение. Legacy `/ask/stream` дополнительно сохраняет поля
`type`/`data`; direct mode использует прежний SSE формат. Полный контракт описан в
[`docs/api/inference-contracts.md`](docs/api/inference-contracts.md).

## Application state и migrations

PostgreSQL является локальным application database. Neon зарезервирован для будущего JDBC
metadata demo и в Iteration 2 не используется.

```bash
make migrate
make seed-dev
```

Миграции не запускаются при import или API startup. Compatibility identity разрешена только
при явном `AUTH_DISABLED=true` в `APP_ENV=dev|test`; production configuration fails closed.
API не принимает доверенные `tenant_id`/`user_id` из request body.

Source versions follow:

```text
discovered -> ingesting -> staged -> validating -> ready -> active -> superseded
                         \-> failed    \---------> failed
```

Manifest and source objects become immutable after `staged`. Activation locks the source and
atomically supersedes the previous active version. A database constraint allows only one
active version per source.

Normalized documents and chunks reference tenant-scoped content-addressed blobs. Unchanged
content is reused between immutable versions. Run/version failures are sanitized together;
failed and staging versions are not used by retrieval.

## MacBook Air 24 GB profile

Рекомендуемый профиль:

- PostgreSQL, Redis и API — Docker/OrbStack;
- Ollama — нативно на macOS для Metal;
- worker — один процесс; нативно для MPS либо CPU Docker container;
- ingestion worker — отдельный лёгкий process/container без ML dependencies;
- одна generation model в памяти;
- API в queued mode не импортирует и не загружает Sentence Transformer.

Для native API/worker задайте host URLs:

```env
DATABASE_URL=postgresql+psycopg://rag:<local-password>@127.0.0.1:5432/rag
REDIS_URL=redis://127.0.0.1:6379/0
MODEL_API_BASE_URL=http://127.0.0.1:11434/v1
```

Compose API/worker используют `host.docker.internal`. API ограничен 512 MB, PostgreSQL —
512 MB, Redis — 320 MB с `noeviction`, worker — 6 GB. Redis Streams ограничены maxlen/TTL.
Worker монтирует `./artifacts` в `/app/artifacts` и использует активную версию retriever из
`/app/artifacts/artifacts_rag_baseline_latest`.

## Linux/CI CPU profile

`infra/worker.Dockerfile` устанавливает один Poetry-resolved CPU-compatible ML stack без
повторной установки Torch/Transformers. Linux worker явно выбирает `torch==2.9.0+cpu` из
PyTorch CPU index; взаимно исключённые platform metadata в lock не устанавливаются в image.
API image ставит только `api` group и не содержит Torch, Transformers, Sentence Transformers,
Jupyter, research tooling или model weights.

## Safe retriever artifacts

S3 client создаётся лениво только при фактической загрузке. Download:

1. проверяет полную конфигурацию и размер объекта;
2. пишет во временную sibling staging directory;
3. проверяет ZIP paths, traversal, absolute paths, symlinks и size limit;
4. распаковывает вручную;
5. выполняет required-files и smoke validation;
6. атомарно заменяет active directory с rollback;
7. удаляет только собственные staging/backup directories.

Текущий corpus использует `joblib`; он должен загружаться только из управляемого доверенного
artifact key. Полная замена pickle-compatible формата остаётся security debt.

## Research

Старые notebooks удалены. Новый контур находится в [`research/README.md`](research/README.md).
Notebooks не содержат outputs/execution counts и используют reusable `research/src`. Heavy
models и training opt-in; `RUN_TRAINING = False` по умолчанию. Результаты и weights ignored.

## Основные environment variables

| Variable | Purpose |
| --- | --- |
| `INFERENCE_EXECUTION_MODE` | `direct` или `queued` |
| `DATABASE_URL` | локальный PostgreSQL application state |
| `REDIS_URL` | Redis Streams transport |
| `MODEL_API_BASE_URL` | self-hosted OpenAI-compatible base URL |
| `GENERATION_MODEL` | generator model id |
| `MODEL_API_TOKEN` | optional Bearer token |
| `MODEL_READINESS_PATH` | лёгкий capability endpoint, default `/models` |
| `INFERENCE_WAIT_TIMEOUT_SEC` | ожидание backward-compatible `/ask` |
| `INFERENCE_MAX_ATTEMPTS` | bounded worker attempts |
| `WORKER_ID` | стабильный worker identity |
| `INGESTION_JOBS_STREAM` | отдельный Redis Stream ingestion jobs |
| `INGESTION_WORKER_ID` | стабильный ingestion worker identity |
| `INGESTION_LEASE_SEC` | lease для безопасного reclaim ingestion job |
| `INGESTION_MAX_ATTEMPTS` | bounded ingestion attempts до DLQ |
| `AUTH_DISABLED` | explicit dev/test compatibility mode |
| `JWT_SECRET` | local JWT signing secret, минимум 32 символа |
| `ACCESS_TOKEN_TTL_SEC` | lifetime короткоживущего access token |
| `REFRESH_TOKEN_TTL_SEC` | lifetime rotating opaque refresh token |
| `API_KEY_DEFAULT_TTL_SEC` | default lifetime API key |
| `API_KEY` | Telegram credential для заголовка `X-API-Key` |
| `LOGIN_RATE_LIMIT_PREFIX` | Redis key namespace for shared login throttling |

Остальные defaults и safe placeholders находятся в `.env.example`. Secrets не должны
попадать в Git, docs, logs или Redis contracts.

## Commands

```bash
make format-check
make lint
make test
make infra-up
make migrate
make seed-dev
make integration-test
make smoke-test
docker compose config --quiet
```

Backend-итерации до появления Web UI закрываются автоматическими и integration tests без
обязательного ручного продуктового тестирования владельцем. API catalog contract:
[Knowledge source catalog](docs/api/knowledge-catalog.md).
