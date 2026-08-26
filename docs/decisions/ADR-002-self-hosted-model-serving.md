# ADR-002: Self-hosted HTTP model serving

- Status: Accepted; default model amended by ADR-013
- Date: 2026-07-30

## Context

The product must not depend on commercial external LLM APIs. API and worker processes should
not load model weights directly, and local macOS development should use Apple Metal. Model
selection must remain replaceable without coupling RAG and SQL services to one runtime.

## Decision

Use provider-neutral HTTP boundaries for all models. The initial generator is
Qwen2.5-Coder-7B-Instruct, GGUF Q4_K_M, served by native Ollama or llama.cpp through an
OpenAI-compatible API. Docker workloads reach the host runtime through
`host.docker.internal`. At most one generator model remains resident in memory.

The Model Gateway owns timeouts, retry/backoff, optional authentication, streaming, and error
translation. It contains no hardcoded external cloud endpoint. BAAI/bge-m3 will later run as
an embedding HTTP service. XiYanSQL-QwenCoder-7B-2504 is deferred; a larger production model
requires separate hardware and is not the laptop default.

## Consequences

- RAG and SQL application interfaces stay stable when the model runtime changes.
- Native macOS inference can use Metal while the rest of the stack remains containerized.
- Operators must provision and monitor model servers and prepare weights separately.
- Readiness and capacity checks will need to distinguish application readiness from model
  availability.

ADR-010 extends this decision with the generator and embedding HTTP services. It does not download weights or start a
model server, integrate BGE-M3, or add an SQL expert.
