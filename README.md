# Self-hosted RAG Assistant

Многопользовательский self-hosted ассистент для документации, Git-кода и метаданных PostgreSQL. Продукт включает защищённый Web UI, Telegram-клиент и HTTP API; immutable ingestion/index lifecycle; hybrid pgvector/full-text retrieval; multi-label routing; grounded citations; SQL AST/schema/EXPLAIN validation; scheduler, retention, audit и observability. Коммерческие LLM API не используются: generation, embeddings и optional reranking вызываются только как self-hosted HTTP endpoints.

## Архитектура

PostgreSQL с pgvector — единственный system of record. Redis Streams используются только для доставки и coordination. API, inference/ingestion/indexing workers и scheduler — отдельные процессы модульного монолита; model services изолированы по HTTP. Web UI обслуживается Nginx. Tenant берётся только из authenticated principal, а composite constraints защищают ключевые связи в БД.

Подробности: [целевая архитектура](docs/architecture/target-architecture.md), [runtime ADR](docs/decisions/ADR-010-final-product-runtime.md), [API](docs/api/inference-contracts.md), [threat model](docs/security/threat-model.md).

## Быстрый локальный запуск

Требуются Docker/Compose, 12 ГБ RAM для CPU embedding-профиля и нативный Ollama/llama.cpp для macOS. Модели не скачиваются при build или startup API.

```bash
cp .env.example .env
# замените только local-only placeholders; подготовьте qwen2.5-coder:7b в Ollama вручную
docker compose up --build
docker compose ps
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/ready
```

На macOS generation server остаётся нативным и доступен контейнерам по `host.docker.internal:11434`. Для нативного embedding endpoint используйте `-f compose.native-models.yml`; dev-порты БД/Redis включаются только через `-f compose.dev.yml`. Создание первого администратора:

```bash
read -s ADMIN_PASSWORD; export ADMIN_PASSWORD
TENANT_SLUG=acme TENANT_NAME=Acme ADMIN_USERNAME=admin ADMIN_DISPLAY_NAME=Administrator make bootstrap-admin
unset ADMIN_PASSWORD
```

Откройте `http://localhost:8080`, войдите, создайте knowledge base, source, выполните refresh/indexing и задайте вопрос. Полный проверочный путь: [owner acceptance](docs/testing/owner-acceptance.md).

## Конфигурация и secrets

`.env.example` содержит безопасные local defaults; `.env.production.example` — fail-closed production template. Обязательны PostgreSQL/Redis URLs, стойкий `JWT_SECRET`, HTTPS CORS origin, secure cookies и self-hosted model endpoints. Connector/API/model/S3/Telegram secrets передаются через environment, Docker/Kubernetes secrets или `CredentialResolver`; не сохраняются в Git, UI, audit и обычных logs. `AUTH_DISABLED=true` разрешён только в `dev/test`.

Ключевые model variables: `MODEL_API_BASE_URL`, `GENERATION_MODEL`, `MODEL_API_TOKEN`, `EMBEDDING_API_BASE_URL`, `EMBEDDING_MODEL`, `EMBEDDING_API_TOKEN`, optional `RERANKER_API_BASE_URL`. Local generator ожидает OpenAI-compatible `/v1/chat/completions`, embedding service — `/v1/embeddings`.

## Использование

- Web UI: chat, resumable SSE, history/citations/feedback и tenant admin operations.
- HTTP: `/ask`, `/ask/stream`, `/v1/inference-jobs/**`, `/v1/conversations/**`; OpenAPI — `/docs`.
- Telegram: создайте API key со scopes `inference:read,inference:write`, передайте как `RAG_API_KEY`; бот вызывает API и не получает DB/model credentials.
- Public: `/health`; sanitized `/ready`. `/metrics` и `/admin/runtime` должны быть доступны только из trusted network/reverse proxy; runtime endpoint дополнительно admin-only.

## Разработка и тестирование

```bash
make install
make check
make integration-test
make evaluation
docker compose --env-file .env.example config --quiet
helm lint deploy/helm/rag-assistant
```

Standard tests используют fake deterministic HTTP models/site/Git и не обращаются к Ollama, S3 или Neon. Real-model evaluation — только opt-in `RUN_REAL_MODEL_EVAL=1 make evaluation-real`. CI проверяет backend/frontend, migrations, security, images, SBOM и Helm. См. [testing guide](docs/testing/owner-acceptance.md) и [evaluation](docs/evaluation/report-template.md).

## Production launch: от готового репозитория до работающего окружения

Репозиторий предоставляет deployment artifacts и воспроизводимые проверки, но не утверждает, что ваше production-окружение уже развёрнуто.

1. **Выберите topology.** Для одного Linux host используйте hardened Compose и внешний либо локальный pgvector/Redis. Для managed Kubernetes используйте Helm. Generation/embedding endpoints могут быть на том же GPU host либо выделенных узлах. Минимум для CPU smoke: 4 CPU/12 ГБ RAM/40 ГБ; рекомендуемо: 8 CPU/32 ГБ/100 ГБ; generation GPU: 12–16 ГБ VRAM для Qwen2.5-Coder-7B Q4, с запасом под concurrency. Подробности: [single host](docs/deployment/single-host.md), [Kubernetes](docs/deployment/kubernetes.md).
2. **Подготовьте платформу.** Нужны Docker/Compose или Kubernetes/Helm, DNS, TLS, PostgreSQL 16+ с pgvector, Redis 7+ и заранее подготовленные self-hosted model weights. S3 нужен только при включённом read-only model cache; SMTP не используется.
3. **DNS/TLS.** Создайте A/AAAA/CNAME на reverse proxy/Ingress, подключите существующий сертификат либо ACME/cert-manager, включите HTTPS redirect и renewal. Установите secure cookies, exact CORS origins и trusted proxy CIDRs. PostgreSQL/Redis не публикуйте в интернет.
4. **Создайте secrets вне репозитория.** Сгенерируйте `JWT_SECRET` (`openssl rand -base64 48`), DB/Redis passwords; добавьте connector, Telegram, optional model/S3 credentials только в Docker/Kubernetes/External Secrets. Не помещайте значения в Git, image, Helm values, command history или logs. Порядок ротации: [secrets rotation](docs/operations/secrets-rotation.md).
5. **Подготовьте PostgreSQL/Redis.** Создайте отдельную БД и least-privilege application user, включите `vector`, TLS и bounded pool. Выполните migrations отдельным one-shot job. Для Redis включите auth/TLS, AOF/RDB по требованиям, `noeviction`; Streams имеют bounded maxlen, а durable state остаётся в PostgreSQL. Neon demo JDBC source не является готовым production application DB profile.
6. **Подготовьте модельное железо.** Заранее загрузите и проверьте Qwen2.5-Coder-7B и BGE-M3 на CPU, Apple MPS или NVIDIA CUDA host. Настройте OpenAI-compatible endpoints, health/readiness, token, concurrency/memory limits. API не скачивает weights. При outage readiness деградирует, новые jobs ограниченно retry и затем уходят в durable failed/DLQ state.
7. **Заполните production configuration.** Скопируйте `.env.production.example` в защищённое secret/config хранилище, замените все placeholders, установите `APP_ENV=production`, `AUTH_DISABLED=false`, HTTPS origins и service URLs. До запуска выполните `poetry run python -m app.state.validate_config` с этим окружением, затем `docker compose --env-file <protected-file> config --quiet` или `helm template`; preflight выводит только имена ошибочных полей и приложение fail closed на слабом JWT, insecure cookie/CORS, disabled auth и небезопасные DB/Redis/model endpoints.
8. **Single host.** Получите проверенный release/tag, задайте `*_IMAGE` как immutable `repository@sha256:digest`, выполните `pull` и запускайте с `--no-build`. Создайте backup/model-cache volumes, установите secrets, запустите DB/Redis, затем one-shot migration job, model services и application services. Создайте первого tenant/admin через bootstrap command. Проверьте `/health`, `/ready`, Web UI, затем Website/Git ingestion → indexing → grounded chat и optional Telegram. Установите restart policy и host monitoring. Полные команды — в [single-host runbook](docs/deployment/single-host.md).
9. **Kubernetes.** Создайте namespace, Secrets/ExternalSecrets и production values; проверьте Helm render. Запустите migration Job до Deployments, настройте Ingress/TLS/PVC, probes, resources, HPA/PDB и NetworkPolicies. Проверьте rollout/smoke; rollback выполняйте `helm rollback`, учитывая совместимость schema. См. [Kubernetes runbook](docs/deployment/kubernetes.md).
10. **Backup/DR.** Делайте регулярный encrypted PostgreSQL backup и restore drills; сохраняйте manifests/configuration, но не считайте Redis backup источником истины. Active source/index versions восстанавливаются из PostgreSQL/content blobs; model cache можно загрузить заново с проверкой SHA-256. Владелец задаёт RPO/RTO. См. [backup/restore](docs/operations/backup-restore.md).
11. **Monitoring.** Подключите `/metrics` к Prometheus и OTLP к collector. Импортируйте `deploy/observability/grafana-dashboard.json` и alerts. Контролируйте readiness, queue age/DLQ, failures, DB pool/disk и model outage; задайте log retention и регулярно проверяйте redaction prompts, retrieved content, auth headers и credentials.
12. **Upgrade/rollback.** Перед schema change сделайте backup, прочитайте release notes, проверьте migration в staging, выполните migration job и rolling update. Application rollback возможен только при schema compatibility; DB downgrade выполняйте по runbook. Source/index/model/prompt versions переключаются атомарной activation/rollback операцией. См. [upgrade](docs/operations/upgrade-rollback.md).
13. **Production acceptance checklist.** Используйте готовый [операторский checklist](docs/operations/production-checklist.md), включающий DNS/TLS, auth, secrets, pgvector/migrations, backup restore, readiness, tenant isolation, ingestion/indexing, citations, SQL, scheduler/retention, monitoring, DLQ и owner acceptance.
14. **Ответственность.** Репозиторий создаёт API/workers/UI, migrations, Compose/Helm, security defaults, tests/SBOM и runbooks. Владелец предоставляет домен, policies, secrets, connector endpoints, Telegram token и model choice. Platform administrator создаёт network/TLS/managed DB/Redis/GPU, backups и alerts. Без реальных DNS/TLS, credentials, managed services и выбранного hardware невозможно подтвердить production rollout, certificate renewal, external backup restore и real-model capacity.

## Backup, security и ограничения

Операционные документы: [secrets](docs/operations/secrets-rotation.md), [backup](docs/operations/backup-restore.md), [DLQ/worker recovery](docs/operations/worker-recovery.md), [upgrade](docs/operations/upgrade-rollback.md), [retention](docs/operations/retention.md), [observability](docs/operations/observability.md).

Поддерживаются статические HTML/sitemap, local Git и PostgreSQL metadata. JS-rendered crawling, дополнительные JDBC vendors и provider-specific infrastructure — optional extensions. SQL выполняется только как validated read-only `SELECT/WITH SELECT`; safe `EXPLAIN` требует отдельной read-only credential reference. Модель может ошибаться, поэтому citations и SQL validation должны оставаться видимыми пользователю.
