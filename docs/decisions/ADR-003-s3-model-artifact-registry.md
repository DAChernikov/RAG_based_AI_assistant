# ADR-003: S3-compatible storage as model artifact registry

- Status: Superseded by ADR-010 for runtime behavior; its integrity model is implemented by the optional read-only cache
- Date: 2026-07-30

## Context

Available S3-compatible storage is limited to 10 GB. Model servers require verified local
files and cannot use object storage as an inference filesystem. Artifacts must be identifiable,
integrity-checked, and activated without exposing partially downloaded files.

## Decision

Use S3-compatible storage as cold storage and a model artifact registry, never as inference
runtime. Store each artifact with a manifest containing model id, role, version, format,
quantization, size, SHA-256, and object key.

The cache manager downloads to a temporary local file, verifies size and SHA-256,
atomically moves the verified file into a local cache, and activates it only afterward. Keep
one active quantized generator, one embedding model, their manifests, and minimal additional
artifacts within the storage limit. Credentials are supplied only by environment variables or
credential references.

## Consequences

- Corrupt or incomplete downloads cannot become active models.
- Storage retention must be deliberately managed under the 10 GB limit.
- Manifest and cache operations need audit logs, locking, and rollback behavior.
- Pickle/joblib retriever artifacts and remote writes are forbidden.

No model artifact upload, remote S3 write, or `ModelArtifactStore` implementation is included
in iteration 1.
