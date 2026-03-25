# RAG Based AI Assistant

Production-like локальный MVP AI-assistant на базе:
- FastAPI API
- retrieval pipeline
- внешней LLM для генерации ответа
- Telegram bot
- S3-compatible storage для артефактов retriever

## Возможности

- `/health` и `/ready` для диагностики API
- `/ask` для обычного ответа
- `/ask/stream` для потоковой генерации
- Telegram bot с потоковым обновлением ответа
- автоматическая загрузка артефактов из S3 при старте сервиса
- запуск как локально, так и через Docker Compose

## Установка
```bash
poetry install --with bot,dev
```

## Настройка окружения

1. Создайте .env на основе .env.example.

2. Настройка параметров запуска:

- Для локального запуска бота параметры следующие:

```bash
API_BASE_URL=http://127.0.0.1:8000
ARTIFACTS_DIR=artifacts/artifacts_rag_baseline_latest
```
- Для Docker Compose запуска:
```bash
API_BASE_URL=http://api:8000
ARTIFACTS_DIR=/app/artifacts/artifacts_rag_baseline_latest
```

Также должны быть заданы:
- `TELEGRAM_BOT_TOKEN`
- `LLM_API_KEY`
- `S3_BUCKET`
- `S3_ARTIFACT_KEY`
- `S3 credentials`

## Локальный запуск

- API
```bash
make run-api
```

- Bot
```bash
make run-bot
```

## Docker Compose запуск
```bash
make up
```

## Остановка сервиса:
```bash
make down
```

### Тесты
```
make test
```

### Форматирование и линтинг
```bash
make fmt
make lint
```

## Проверка API

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

### Пример вопроса:
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Apache Spark?"}'
```
