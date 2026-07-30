# RAG-based AI Assistant

MVP self-hosted RAG-ассистента для ответов по технической документации, шаблонам кода
и метаданным PostgreSQL. FastAPI предоставляет HTTP API, Telegram-бот работает как его
клиент. Генерация выполняется через локальный или self-hosted model server с
OpenAI-compatible HTTP API; внешние коммерческие LLM API не используются.

Проект постепенно развивается в многопользовательский продукт. Целевая архитектура и
нереализованные компоненты описаны в
[`docs/architecture/target-architecture.md`](docs/architecture/target-architecture.md),
а порядок итераций — в [`docs/roadmap.md`](docs/roadmap.md).

## Что работает сейчас

- `/health`, `/ready`, `/ask` и `/ask/stream` на FastAPI;
- baseline routing `rag_docs | rag_code | sql`;
- retrieval из существующего локального корпуса;
- скачивание retriever artifacts из S3-compatible storage при наличии конфигурации;
- RAG-ответы через self-hosted OpenAI-compatible model API;
- генерация SQL, статическая проверка таблиц и колонок, опциональный PostgreSQL `EXPLAIN`
  и ограниченный repair;
- Telegram-бот как API-клиент.

Web UI, аутентификация, PostgreSQL application state, pgvector, Redis Streams, workers,
source catalog, Web/Git/JDBC connectors, BGE-M3 и multi-label routing пока не реализованы.

## Текущая схема

```text
Telegram Bot / HTTP client
        |
        v
FastAPI API
        |
        +-- RouterService: rag_docs | rag_code | sql
        +-- RetrieverLoader: existing local artifacts
        +-- RAGService
        +-- SQLService: static validation + optional EXPLAIN/repair
        |
        v
Self-hosted OpenAI-compatible model API
```

Model weights не входят в репозиторий или Docker image. На macOS model server запускается
нативно, чтобы использовать Apple Metal; API-контейнер обращается к нему через
`host.docker.internal`.

## Требования

- Python 3.11 или 3.12;
- Poetry 2;
- Docker Compose — для контейнерного запуска;
- Ollama или OpenAI-compatible server на базе llama.cpp — для генерации;
- retriever artifacts локально либо read-only credentials для их существующего S3-источника.

## Установка

```bash
make install
cp .env.example .env
```

Не добавляйте `.env` в Git. Все credentials задаются только через environment variables.
`MODEL_API_TOKEN` для локального сервера обычно остаётся пустым.

## Локальный model server и API

### 1. Запустите model server нативно

Вариант с Ollama:

```bash
ollama serve
```

В отдельном терминале вручную подготовьте модель:

```bash
ollama pull qwen2.5-coder:7b
```

Загрузка модели не автоматизирована проектом. Вместо Ollama можно запустить llama.cpp с
OpenAI-compatible endpoint и указать его URL и model id в `.env`.

### 2. Настройте `.env`

Для API в Docker Compose:

```env
MODEL_API_BASE_URL=http://host.docker.internal:11434/v1
GENERATION_MODEL=qwen2.5-coder:7b
MODEL_API_TOKEN=
```

Для API, запущенного напрямую на macOS:

```env
MODEL_API_BASE_URL=http://127.0.0.1:11434/v1
GENERATION_MODEL=qwen2.5-coder:7b
MODEL_API_TOKEN=
```

При использовании другого OpenAI-compatible server измените URL и model id. Приложение
вызывает только `{MODEL_API_BASE_URL}/chat/completions`.

### 3. Запустите API

На host:

```bash
make run-api-reload
```

Или в Docker Compose:

```bash
make up
```

Compose добавляет `host.docker.internal:host-gateway`: это сохраняет стандартный путь на
macOS и даёт совместимый host alias на поддерживаемых Linux-установках Docker.

### 4. Проверьте состояние

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

`/ready` сообщает о готовности retriever artifacts и RAG runtime. Отсутствие автоматически
загруженной generation model не является ошибкой сборки; доступность model server
проверяется фактическим запросом.

### 5. Отправьте тестовый запрос

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Apache Spark?"}'
```

Streaming endpoint:

```bash
curl -N -X POST http://127.0.0.1:8000/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question":"How do I safely read a nested Python dictionary?"}'
```

SQL baseline:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Show revenue by customer segment","mode":"sql","top_k":10}'
```

## Telegram-бот

Для запуска на host:

```env
API_BASE_URL=http://127.0.0.1:8000
TELEGRAM_BOT_TOKEN=
```

```bash
make run-bot
```

Для Docker Compose используйте `API_BASE_URL=http://api:8000`. Bot token хранится только
в локальном `.env` или secret store.

## Конфигурация Model Gateway

| Variable | Назначение |
| --- | --- |
| `MODEL_API_BASE_URL` | Base URL self-hosted OpenAI-compatible API |
| `GENERATION_MODEL` | Model id, передаваемый в `chat/completions` |
| `MODEL_API_TOKEN` | Опциональный Bearer token; пустое значение не создаёт header |
| `MODEL_REQUEST_TIMEOUT` | Timeout запроса в секундах |
| `MODEL_RETRIES` | Число повторов после первого запроса |
| `MODEL_RETRY_BACKOFF_SEC` | Базовая задержка линейного backoff |
| `MODEL_TEMPERATURE` | Default temperature |
| `MODEL_MAX_CONTEXT_CHARS` | Временный символьный лимит prompt context |

Старые Gemini-specific `LLM_PROVIDER`, `LLM_API_BASE_URL`, `LLM_MODEL` и `LLM_API_KEY`
не поддерживаются и не используются как fallback.

## Проверки

```bash
make format-check
make lint
make test
make check
docker compose config --quiet
git diff --check
```

`make fmt` применяет Black и isort, поэтому используйте его только для намеренного
форматирования.
