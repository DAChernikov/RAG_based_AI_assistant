# ADR-006: Local authentication, tenant isolation and RBAC

Status: accepted and implemented in Iteration 3.

## Context

The self-hosted product must protect conversations, jobs and answers before external clients
and a Web UI are added. A hosted identity provider would conflict with that deployment model.

## Decision

- Store local users per tenant and hash passwords with Argon2id.
- Issue short-lived HS256 JWT access tokens with configured issuer/audience and a deployment
  secret. JWTs are never persisted.
- Use opaque rotating refresh tokens. Persist only SHA-256 hashes; reuse revokes the family.
- Support `admin` and `user`; administrators manage users only in their own tenant.
- Derive tenant/user ownership only from the authenticated principal.
- Give Telegram and external clients scoped opaque API keys. Persist only hash, safe prefix,
  expiry, last use and revocation state.
- Record security events without passwords, tokens, credentials or prompts.
- Allow compatibility identity only with `AUTH_DISABLED=true` and `APP_ENV=dev|test`.

## Consequences

Deployments configure a secret of at least 32 characters and bootstrap the first administrator
locally. Asymmetric signing is an optional deployment extension; signing-secret rotation is operationalized. Iteration 4 moved
login throttling to an atomic Redis-backed limiter shared by API processes.
