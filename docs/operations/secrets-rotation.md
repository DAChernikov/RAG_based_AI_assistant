# Secrets rotation

Secrets include JWT signing material, PostgreSQL/Redis passwords, API keys, refresh tokens, connector credentials, model endpoint tokens, Telegram token and optional S3 credentials. Store them in Docker/Kubernetes/External Secrets; never Git, Helm values, logs or screenshots.

1. Take a configuration backup and announce a short session invalidation window.
2. Create a new random value in the secret manager. Rotate downstream DB/Redis/model/S3 credential first with an overlap where supported.
3. For model/connector/Telegram values, enter the replacement in **Секреты** using the same opaque reference. The encrypted row and key version rotate atomically; model clients use it on the next job and Telegram reloads its config version without image rebuild. The plaintext is removed from browser state and is never returned.
4. For `JWT_SECRET`, revoke refresh sessions/API keys as policy requires, replace the secret and roll API; all old access JWTs become invalid.
5. Rotate Telegram token at BotFather, rotate its credential reference in Web UI and run **Telegram → Проверить**. No container restart is required.
6. Revoke old values, test login/inference/connectors and inspect redacted audit/operational logs.

Never print secret values during verification. Record actor, timestamp, affected reference and outcome—not the value—in the change record.

The SecretStore master key is a platform secret, not a dynamic credential. Rotating it requires a controlled re-encryption maintenance procedure and coordinated service restart; do not replace it while encrypted rows still depend on the old key. Back up the external production key separately from PostgreSQL. Local development keeps the auto-generated key in the `secret_keys` volume; deleting that volume makes stored credential ciphertext unrecoverable.
