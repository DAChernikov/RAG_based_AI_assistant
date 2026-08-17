# Observability

Services emit structured allowlisted JSON logs, Prometheus metrics and optional OTLP traces. Prompts, retrieved content, SQL text, auth headers, cookies, credentials and connection strings are redacted/omitted. Configure `OTEL_EXPORTER_OTLP_ENDPOINT` only for a trusted collector.

Scrape `/metrics` through a private network. Import `deploy/observability/grafana-dashboard.json` and `prometheus-alerts.yml`. Add platform alerts for readiness, queue oldest age, DLQ growth, ingestion/index failures, model/embedding outage, PostgreSQL pool/saturation, Redis memory/noeviction, disk and certificate expiry. Verify redaction with synthetic canary secrets before launch and after logging changes.
