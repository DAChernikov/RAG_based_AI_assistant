# Authentication and authorization API

Status: implemented product contract.

## Sessions

- `POST /v1/auth/login` accepts `tenant_slug`, `username`, `password` and returns a short-lived
  Bearer access token plus an opaque refresh token for non-browser clients. Browser mode stores
  refresh in an HttpOnly cookie and uses a CSRF cookie/header pair.
- `POST /v1/auth/refresh` consumes and rotates a refresh token. Reuse, expiry or revocation
  returns HTTP 401 and revokes the token family when it can be identified.
- `POST /v1/auth/logout` revokes the supplied refresh session.
- `GET /v1/auth/me` returns the authenticated user, tenant, role, method and scopes.

Clients send access tokens as `Authorization: Bearer <token>`. Access JWTs are not persisted;
only hashes of refresh tokens are stored.

## Users and roles

`GET|POST /v1/admin/users` and `GET|PATCH|DELETE /v1/admin/users/{id}` require role `admin`.
Delete is a recoverable deactivation. All operations are constrained to the administrator's
tenant. Roles are `admin` and `user`.

## API keys

Authenticated users manage their own keys with:

- `GET|POST /v1/api-keys`;
- `DELETE /v1/api-keys/{id}`.

The create response is the only response containing the full key. Clients send it through
`X-API-Key`. Supported scopes are `profile:read`, `inference:read` and `inference:write`.
Stored records contain only SHA-256 hash, safe prefix, scopes, expiry, last-use and revocation
timestamps. API keys cannot be used to create or manage other keys.

Login throttling uses an atomic Redis counter shared by API processes. Redis keys contain a
hash of the tenant/username identity rather than the plaintext username.

## Public and protected paths

`/health` and sanitized `/ready` are public. `/admin/runtime` is admin-only. `/ask`,
`/ask/stream`, `/v1/inference-jobs/**` and `/v1/conversations/**` require authentication.
Tenant and user IDs are always obtained from the principal, never from request content.
