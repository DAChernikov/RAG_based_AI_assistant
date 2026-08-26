# ADR-013: Ollama model discovery and local generation default

- Status: Accepted
- Date: 2026-08-26

## Context

A configured Ollama endpoint can be reachable while the selected Model ID is absent. Requiring
administrators to type model identifiers also makes this mismatch unnecessarily likely.

## Decision

The local default generator is `qwen2.5-coder:14b`. The administration UI reads the installed
catalog from Ollama, tests every configured model automatically, and offers installed generation
models through a select. Activation remains explicit and is allowed only after the exact model
passes readiness. No model is downloaded or substituted automatically.

Provider-neutral OpenAI-compatible endpoints remain supported with an explicit Model ID because
they are not required to expose Ollama's catalog API.

## Consequences

- An available server is no longer confused with an available model.
- Existing tenant overrides remain explicit; operators choose an installed replacement in UI.
- The 14B default requires more memory than the original 7B development default.
