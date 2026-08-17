# Secrets rotation

Secrets include JWT signing material, PostgreSQL/Redis passwords, API keys, refresh tokens, connector credentials, model endpoint tokens, Telegram token and optional S3 credentials. Store them in Docker/Kubernetes/External Secrets; never Git, Helm values, logs or screenshots.

1. Take a configuration backup and announce a short session invalidation window.
2. Create a new random value in the secret manager. Rotate downstream DB/Redis/model/S3 credential first with an overlap where supported.
3. Update the credential reference or runtime Secret, roll affected services and verify readiness.
4. For `JWT_SECRET`, revoke refresh sessions/API keys as policy requires, replace the secret and roll API; all old access JWTs become invalid.
5. Rotate Telegram token at BotFather, update only the bot Secret and restart the bot.
6. Revoke old values, test login/inference/connectors and inspect redacted audit/operational logs.

Never print secret values during verification. Record actor, timestamp, affected reference and outcome—not the value—in the change record.
