# RAG-based AI Assistant

Прототип корпоративного AI-ассистента на основе RAG-системы для дипломной работы магистерской программы "Искусственный интеллект" НИУ ВШЭ "Разработка ИИ-ассистента на основе RAG-системы".

Сервис отвечает на вопросы по изолированной технической документации, шаблонам кода и структуре реляционной базы данных. 
Основной пользовательский интерфейс — Telegram-бот, backend реализован на FastAPI.

**Основной пользовательский интерфейс доступен по ссылке:** https://t.me/rag_based_ai_bot 

> Доступ открыт до окончания сдачи дипломной работы - `01.07.2026`

## Основные сценарии прототипа

- поиск ответов по официальной документации Spark, Trino и Hive;
- ответы на вопросы по Python/code corpus шаблонам;
- генерация PostgreSQL-запросов по естественно-языковому описанию аналитической задачи;
- проверка релевантности SQL-ответа через статическую валидацию таблиц/колонок и PostgreSQL команды `EXPLAIN`;
- загрузка артефактов retriever из S3-хранилища.

## Архитектура

```text
Telegram Bot / HTTP client
        |
        v
FastAPI API
        |
        +-- RouterService
        |      +-- rag_docs
        |      +-- rag_code
        |      +-- sql
        |
        +-- RetrieverLoader
        |      +-- corpus.joblib
        |      +-- corpus_emb.npy
        |      +-- retriever_model/
        |
        +-- RAGService
        |      +-- context retrieval
        |      +-- LLM answer generation
        |
        +-- SQLService
               +-- schema-only retrieval
               +-- SQL generation
               +-- static validation
               +-- PostgreSQL EXPLAIN validation
```

## Структура проекта

```text
app/api/              FastAPI backend
app/api/routes/       HTTP endpoints
app/api/services/     RAG, SQL, LLM and artifact services
app/bot/              Telegram bot
tests/                Unit tests
infra/                Dockerfiles for API and bot
notebooks/            Retriever training/tuning and upload to S3
render.yaml           Render deployment config
```

## Локальный запуск

```bash
poetry install --with bot,dev
cp .env.example .env
```

Необходимо заполнить `.env` необходимыми ключами для GeminiAI, S3, Telegram и Postgres.

## Запуск API

```bash
make run-api-reload
```

Проверка состояния:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

## Проверка запросов к API

Вопрос по текстовой документации:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What is Apache Spark?"}'
```

Вопрос по шаблону кода:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"How to sort list in Python?"}'
```

Вопрос по актуальному SQL-коду:

```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"Convert daily order revenue from EUR to USD","mode":"sql","top_k":10}'
```

## Запуск Telegram-бота

```bash
make run-bot
```

Для локального запуска в `.env` используйте:

```env
API_BASE_URL=http://127.0.0.1:8000   # вместо 127.0.0.1 можно также localhost
```

Для Docker Compose внутри контейнерной сети:

```env
API_BASE_URL=http://api:8000
```

В `render.com` используется сгенерированный при деплое адрес API-сервиса.
На текущий момент `API_BASE_URL` от `render`: https://rag-ai-assistant-api.onrender.com, 
соответственно использовать сервис можно также по этой ссылке.

## Docker Compose вариант запуска

```bash
make up     # Поднять композицию Docker-контейнеров
make down   # Отключить композицию Docker-контейнеров
```

## Render

Проект адаптирован для деплоя на сайте Render.com.
Для этого в `render.yaml` описаны два сервиса:

- `rag-ai-assistant-api` — FastAPI backend;
- `rag-ai-assistant-bot` — Telegram worker.

Все секреты задаются через Environment Variables проекта на платформе. На момент создания прототипа в публичном доступе доступны следующие ссылки:
1. Основной пользовательский интерфейс ассистента (Telegram-бот): https://t.me/rag_based_ai_bot
2. Публичный API base URL: https://rag-ai-assistant-api.onrender.com

> Перечисленные ссылки станут недоступными после `01.07.2026`

## Примечания

**В проекте также встроены проверки качества кода и тесты**

```bash
make fmt
make lint
make test
```
