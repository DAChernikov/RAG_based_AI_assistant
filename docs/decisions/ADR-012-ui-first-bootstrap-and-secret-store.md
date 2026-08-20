# ADR-012 — UI-first bootstrap and encrypted credential references

Status: Accepted

## Decision

The local product starts as a complete Compose stack and exposes a first-run Web Setup Wizard. A singleton PostgreSQL row and a locked administrator count make creation atomic across API replicas. The public bootstrap mutation exists only while no administrator has ever been created. Development exposes Web on loopback. Production disables setup unless a strong bootstrap token is supplied through platform secret storage; completion permanently closes the mutation.

Credential values cross the API once and are encrypted before PostgreSQL persistence. Local development creates a Fernet master key with restrictive permissions in a dedicated persistent volume. Production must inject the master key from Docker/Kubernetes Secret storage (an external provider can replace the abstraction). Database rows, responses, audit, logs and metrics contain only opaque references and masked metadata. Rotation updates the encrypted value and key version without rebuilding services.

Model definitions and Telegram configuration are versioned database state. Workers resolve active definitions for the next job. The Telegram process starts idle and reloads an enabled config version. Settings tied to process/network/security initialization are explicitly marked restart-required; the UI never controls Docker or Kubernetes.

## Consequences

- Setup remains usable while a cold embedding model reports `model_loading`; readiness is not weakened.
- Losing the local master-key volume makes stored credentials unrecoverable, so production backup and rotation procedures must include the external master key.
- `make bootstrap-admin` remains a recovery path, not the normal owner journey.
