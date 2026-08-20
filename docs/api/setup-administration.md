# Setup and platform administration API

The Web UI is the supported owner interface; these contracts document its backend boundary. Responses never contain stored secret values.

## Public first-run boundary

- `GET /v1/setup/status` returns only `required`, `setup_available`, `token_required`, current onboarding step and config version.
- `POST /v1/setup/bootstrap` accepts tenant/admin/password once. Development is protected by loopback Web binding and rate limiting. Production returns 404 unless a strong bootstrap token is mounted from platform secret storage (`SETUP_BOOTSTRAP_TOKEN_FILE` in Compose, or a Kubernetes Secret); the browser sends it once as `X-Setup-Token`. PostgreSQL locking makes concurrent attempts single-winner. After any administrator exists, the endpoint permanently returns 409.

The successful response creates the normal short access/opaque refresh session and never echoes the password or setup token. `PUT /v1/setup/progress` is admin-authenticated and persists `models → telegram → readiness → complete`.

## Authenticated tenant administration

- `GET /v1/admin/system` — sanitized API, PostgreSQL, Redis, worker, scheduler, model and Telegram states. `model_loading` is distinct from ready/error.
- `GET /v1/admin/diagnostics` — component states, bounded DLQ counts and the request correlation ID.
- `GET /v1/admin/settings` — dynamic and restart-required setting classes, without environment values.
- `GET|PUT|DELETE /v1/admin/credentials/**` — masked metadata and encrypted create/rotate/delete by opaque reference.
- `GET|PUT /v1/admin/telegram`, `POST /v1/admin/telegram/test` — versioned enablement and sanitized connection test.
- `/v1/admin/models/**` — versioned definitions, activation and connection test. `base_url`, model ID/version, capabilities and optional credential reference become effective for subsequent jobs.

All tenant/user identity comes from the authenticated principal. Admin mutations are audited with actor, tenant, action, resource, outcome, correlation ID and non-sensitive metadata.
