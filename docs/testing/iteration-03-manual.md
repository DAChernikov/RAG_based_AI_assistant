# Iteration 03 manual verification

Run commands from the repository root on `feature/product-v1`. Replace placeholders locally;
never commit their values.

## Configure and start

Copy `.env.example` to `.env`, set `JWT_SECRET` to a random value of at least 32 characters,
keep `AUTH_DISABLED=false`, then run:

```bash
make infra-up
make migrate
poetry run python -m app.state.bootstrap_admin \
  --tenant-slug demo \
  --tenant-name "Demo tenant" \
  --username admin \
  --display-name "Demo administrator"
make run-worker
make run-api
```

The bootstrap command prompts for the password without echo. Start Ollama natively as described
in the README.

## Login, refresh and logout

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"tenant_slug":"demo","username":"admin","password":"<admin-password>"}'

export ACCESS_TOKEN='<access_token>'
export REFRESH_TOKEN='<refresh_token>'

curl -sS http://127.0.0.1:8000/v1/auth/me \
  -H "Authorization: Bearer $ACCESS_TOKEN"
curl -sS -X POST http://127.0.0.1:8000/v1/auth/refresh \
  -H 'Content-Type: application/json' \
  -d "{\"refresh_token\":\"$REFRESH_TOKEN\"}"
```

Reusing the old refresh token must return 401 and revoke the family. Logout the newest refresh
token with `POST /v1/auth/logout` and the same JSON field.

## Users, API key and inference

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/admin/users \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"username":"reader","display_name":"Reader","password":"<12+-character-password>","role":"user"}'

curl -sS -X POST http://127.0.0.1:8000/v1/api-keys \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"manual-client","scopes":["inference:read","inference:write"]}'

export API_KEY='<api_key shown once>'
curl -sS -X POST http://127.0.0.1:8000/ask \
  -H "X-API-Key: $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: manual-iteration-03-1' \
  -d '{"question":"What is Apache Spark?"}'
```

List keys with the JWT: only `prefix`, never the full key, must be returned. Revoke with
`DELETE /v1/api-keys/<key-id>` using the JWT; the revoked key must then receive 401. A normal
user must receive 403 from `/v1/admin/users` and `/admin/runtime`.

## Tenant isolation

Bootstrap a second tenant administrator with another tenant slug and login to both tenants.
Create a job with tenant A:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/inference-jobs \
  -H "Authorization: Bearer <tenant-A-token>" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Isolation check"}'
```

Use tenant B's token against the returned IDs:

```bash
curl -i http://127.0.0.1:8000/v1/inference-jobs/<job-id> \
  -H "Authorization: Bearer <tenant-B-token>"
curl -i http://127.0.0.1:8000/v1/inference-jobs/<job-id>/events \
  -H "Authorization: Bearer <tenant-B-token>"
curl -i http://127.0.0.1:8000/v1/conversations/<conversation-id> \
  -H "Authorization: Bearer <tenant-B-token>"
```

All must return 404. Calls without credentials return 401. `/health` and sanitized `/ready`
remain public.

## Telegram and audit

Set the one-time key in `API_KEY` and `API_BASE_URL=http://api:8000` for Compose (localhost for
native execution), then run `make run-bot`. After revocation, the bot must receive an
authentication error.

Inspect audit events:

```bash
docker compose exec postgres psql -U rag -d rag -c \
  "SELECT created_at, action, outcome, tenant_id, actor_user_id, resource_type, correlation_id FROM audit_events ORDER BY created_at DESC LIMIT 30;"
```

Verify login success/failure, refresh/logout, user/API-key changes, denied access and admin
operations. No password, access/refresh token, API key or prompt may appear.

## Automated checks

```bash
make test
make integration-test
make lint
make format-check
docker compose config --quiet
git diff --check
```
