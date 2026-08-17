# ADR-011: React Web UI and browser sessions

- Status: Accepted and implemented
- Date: 2026-08-17

Use React 18, TypeScript and Vite with a small hand-written typed API boundary. Nginx serves immutable assets and reverse-proxies API/SSE. Access JWT stays only in memory; opaque refresh token is an HttpOnly/Secure production cookie, while a separate CSRF cookie/header protects refresh/logout. Exact CORS origins and SameSite are configuration-controlled.

This keeps the frontend compact, makes streaming cancellation/reconnect explicit and preserves API-key auth for Telegram/external clients. Component tests use Vitest/Testing Library; browser acceptance uses Playwright.
