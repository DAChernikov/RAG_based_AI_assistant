# Owner acceptance scenario

This is the single manual acceptance path for a release. Use a local fake website, a disposable local Git repository and a disposable tenant; never production credentials.

1. Copy `.env.example`, start native Ollama or llama.cpp with Qwen, then `docker compose up --build`. Confirm every service is healthy and `/ready` is sanitized.
2. Bootstrap tenant `acceptance` and admin using `read -s ADMIN_PASSWORD` plus `make bootstrap-admin`. Log in at `http://localhost:8080`; confirm refresh survives a page reload and logout invalidates the session.
3. Create a user and a scoped API key; copy the full key once. Confirm it is not shown on reload and a revoked key returns 401.
4. Create a knowledge base. Register a local Website source and a local Git source with include/exclude rules, link both, refresh them and inspect progress/events. Activate versions where auto-activation is disabled.
5. Start indexing. Confirm run reaches active, sources/provenance are visible and a second identical request does not create duplicate content/embeddings.
6. Log in as the user, select the KB and ask a question covering docs and code. Confirm streaming, route targets, citations, history, feedback, cancel/retry and reconnect. A missing citation must not be invented.
7. Register a disposable PostgreSQL metadata source using only a credential reference and allowlists. Ask a SQL question; verify one read-only SELECT/WITH, schema validation and safe EXPLAIN. Attempt DDL, multiple statements and an unknown table; each must be rejected.
8. Create another tenant/admin. Verify source, index, job, conversation and SSE identifiers from tenant A return 404/403 in tenant B.
9. Update/delete source content, refresh and index. Confirm unchanged embeddings are reused, removed content disappears from the new active index, then rollback to the previous index.
10. Enable a schedule and confirm repeated scheduler passes are idempotent. Pause/resume it. Run retention dry-run then run; active/pinned versions remain.
11. Stop/restart each worker during a job. Confirm reclaim is fenced, attempts are bounded and no duplicate answer/version appears. Inspect DLQ recovery and audit without prompts/secrets.
12. Configure Telegram with the scoped API key, ask a question and revoke the key. Confirm the next request is unauthorized.
13. Review metrics/dashboard/alerts, JSON redaction, backup restore drill and `docs/operations/production-checklist.md`. Record result, release SHA, hardware and deviations.

Cleanup: revoke test keys/sessions, delete disposable sources/users/tenant according to policy, stop Compose without deleting volumes needed for diagnosis. Troubleshooting starts with sanitized `/ready`, container health, PostgreSQL durable status and worker event/DLQ runbooks.
