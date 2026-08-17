# Single-host production deployment

This runbook assumes one hardened Linux host, Docker Engine/Compose, a DNS name and TLS termination. Use a dedicated release tag; do not deploy a mutable development branch.

## Prepare

1. Provision 8 CPU, 32 GB RAM and 100 GB encrypted disk plus 12–16 GB VRAM when generation runs locally. Open only 80/443 and restricted SSH.
2. Install Docker with log rotation. Create root-owned application, backup and model-cache directories. PostgreSQL/Redis ports must not be published.
3. Point DNS to the host. Terminate TLS with Caddy/Nginx/Traefik, redirect HTTP to HTTPS, automate ACME renewal and proxy only Web/API traffic. Set the exact HTTPS origin and trusted proxy CIDRs.
4. Copy `.env.production.example` to a root-readable secret file outside the checkout. Replace every placeholder. Prefer Docker secrets for passwords/tokens. Validate it before contacting any dependency with `docker compose --env-file /run/secrets/rag.env run --rm --no-deps api python -m app.state.validate_config`. Validation reports field names only, never values.
5. Create a PostgreSQL 16 database/user with only application-schema privileges and `CREATE EXTENSION vector`; require TLS. Configure Redis 7 auth/TLS, AOF, `noeviction` and memory alerts.
6. Preload Qwen and BGE-M3 weights into model-service storage, verify origin/checksum, start their HTTP services and test `/health`, `/ready`, `/v1/models` or `/v1/embeddings`. Never download weights during API startup.

## Deploy

```bash
git fetch --tags
git checkout <reviewed-release-tag>
docker compose --env-file /run/secrets/rag.env config --quiet
docker compose --env-file /run/secrets/rag.env pull
docker compose --env-file /run/secrets/rag.env up -d postgres redis
docker compose --env-file /run/secrets/rag.env run --rm --no-deps migrate
docker compose --env-file /run/secrets/rag.env up -d --no-build embedding-service api inference-worker ingestion-worker indexing-worker scheduler web
docker compose --env-file /run/secrets/rag.env ps
```

Set every `*_IMAGE` variable to a reviewed immutable `repository@sha256:digest`; `pull` plus `--no-build` prevents production from building a mutable checkout. If PostgreSQL/Redis/models are external, use a private override file that removes local services and injects TLS URLs. Bootstrap admin without putting the password on the command line:

```bash
read -s ADMIN_PASSWORD; export ADMIN_PASSWORD
docker compose --env-file /run/secrets/rag.env run --rm \
  -e ADMIN_PASSWORD -e TENANT_SLUG=acme -e TENANT_NAME=Acme \
  -e ADMIN_USERNAME=admin -e ADMIN_DISPLAY_NAME=Administrator api \
  python -m app.state.bootstrap_admin
unset ADMIN_PASSWORD
```

Check HTTPS `/health`, sanitized `/ready`, login, source refresh, indexing and cited chat. Create a scoped Telegram API key only after this smoke test. Enable service restart, host backups, Prometheus scraping and OTLP export.

## Rollback

Keep the previous image digests. Read migration release notes before downgrading application images; restore PostgreSQL when a migration is not backward compatible. Index/source/model/prompt rollback is performed through admin activation endpoints and does not require Redis recovery.
