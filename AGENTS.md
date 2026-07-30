# Repository working instructions

- This repository is evolving into a multi-user, self-hosted RAG assistant for documentation,
  code, websites, and relational database metadata.
- Product work belongs on the long-lived `feature/product-v1` branch. Never commit or merge
  product changes directly into `main`.
- Install with `make install`. Run `make test`, `make lint`, and `make format-check`; use
  `make fmt` only when formatting changes are intended.
- Keep secrets out of Git, documentation, logs, examples, and test fixtures. Use environment
  variables or credential references and keep `.env.example` limited to safe placeholders.
- Preserve backward compatibility unless an iteration explicitly authorizes a breaking change.
- Update tests and documentation whenever behavior or configuration changes.
- Do not add commercial external LLM APIs. Local or self-hosted models must be called over HTTP.
- End every iteration by reviewing the complete diff and running the available checks.
- Do not perform destructive remote actions, force operations, remote data writes, or branch
  deletion without explicit approval.
- Classify every iteration as `internal`, `admin-visible`, or `user-visible`. For
  `admin-visible` and `user-visible` work, update `docs/testing/` with commands, expected
  results, negative cases, cleanup, and troubleshooting.
- Final reports must distinguish automated tests, integration tests, manual tests performed
  by Codex, owner-required manual tests, and checks that could not be performed.
