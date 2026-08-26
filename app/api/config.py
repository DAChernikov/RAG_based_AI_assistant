import ipaddress
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime настройки параметров окружения приложения.
    Обозначены основные дефолты при отстуствии ENVIRONMENT VARIABLES (.env)
    """

    app_env: str = "dev"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    trusted_proxy_cidrs: str = "127.0.0.1"
    max_request_body_bytes: int = 1_048_576
    sse_max_lifetime_sec: int = 900
    inference_api_concurrency: int = 32
    otel_service_name: str = "rag-api"
    otel_exporter_otlp_endpoint: str | None = None

    default_mode: str = "rag"
    top_k: int = 5
    doc_top_k: int = 5
    code_top_k: int = 3
    max_new_tokens: int = 1000
    doc_max_new_tokens: int = 1500
    code_max_new_tokens: int = 2000

    sql_top_k: int = 10
    sql_schema_source: str = "database_schema"
    sql_dialect: str = "postgres"
    sql_max_repair_attempts: int = 1
    sql_max_new_tokens: int = 900
    sql_temperature: float = 0.0
    sql_enable_explain_validation: bool = False
    sql_explain_timeout_sec: float = 10.0

    postgres_host: str | None = None
    postgres_port: int = 5432
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_sslmode: str = "require"

    min_confident_code_score: float = 0.58
    min_confident_doc_score: float = 0.45

    model_api_base_url: str = "http://127.0.0.1:11434/v1"
    generation_model: str = "qwen2.5-coder:14b"
    model_api_token: str | None = None
    model_request_timeout: float = 90.0
    model_retries: int = 3
    model_retry_backoff_sec: float = 3.0
    model_temperature: float = 0.15
    model_max_context_chars: int = 12000
    model_readiness_timeout: float = 2.0
    model_readiness_path: str = "/models"
    model_http_allowed_hosts: str = ""

    database_url: str = "postgresql+psycopg://rag:rag@127.0.0.1:5432/rag"
    redis_url: str = "redis://127.0.0.1:6379/0"
    inference_jobs_stream: str = "rag:inference:jobs"
    inference_consumer_group: str = "rag-inference-workers"
    inference_events_prefix: str = "rag:inference:events"
    inference_dlq_stream: str = "rag:inference:dlq"
    inference_heartbeat_prefix: str = "rag:inference:heartbeat"
    inference_event_maxlen: int = 1000
    inference_event_ttl_sec: int = 86400
    worker_heartbeat_ttl_sec: int = 20
    worker_stale_after_sec: int = 30
    worker_health_dir: str = tempfile.gettempdir()
    worker_id: str = "worker-1"
    worker_block_ms: int = 2000
    worker_claim_idle_ms: int = 60000
    inference_wait_timeout_sec: float = 120.0
    inference_poll_interval_sec: float = 0.2
    inference_max_attempts: int = 3
    inference_retry_backoff_sec: float = 1.0
    ingestion_jobs_stream: str = "rag:ingestion:jobs"
    ingestion_consumer_group: str = "rag-ingestion-workers"
    ingestion_events_prefix: str = "rag:ingestion:events"
    ingestion_dlq_stream: str = "rag:ingestion:dlq"
    ingestion_heartbeat_prefix: str = "rag:ingestion:heartbeat"
    ingestion_event_maxlen: int = 1000
    ingestion_event_ttl_sec: int = 86400
    ingestion_worker_id: str = "ingestion-worker-1"
    ingestion_worker_block_ms: int = 2000
    ingestion_claim_idle_ms: int = 60000
    ingestion_lease_sec: int = 300
    ingestion_max_attempts: int = 3
    embedding_api_base_url: str = "http://127.0.0.1:8001/v1"
    embedding_api_token: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    embedding_model_version: str = "bge-m3/1"
    embedding_dimensions: int = 1024
    embedding_request_timeout: float = 60.0
    embedding_batch_size: int = 32
    reranker_api_base_url: str | None = None
    reranker_api_token: str | None = None
    reranker_request_timeout: float = 20.0
    indexing_jobs_stream: str = "rag:indexing:jobs"
    indexing_consumer_group: str = "rag-indexing-workers"
    indexing_dlq_stream: str = "rag:indexing:dlq"
    indexing_heartbeat_prefix: str = "rag:indexing:heartbeat"
    indexing_stream_maxlen: int = 10_000
    indexing_worker_id: str = "indexing-worker-1"
    indexing_worker_block_ms: int = 2000
    indexing_claim_idle_ms: int = 60_000
    indexing_lease_sec: int = 300
    indexing_max_attempts: int = 3
    scheduler_id: str = "scheduler-1"
    scheduler_poll_sec: int = 15
    scheduler_leader_ttl_sec: int = 45
    scheduler_max_catchup: int = 20
    redis_stream_maxlen: int = 10_000
    maintenance_batch_size: int = 500
    compatibility_tenant_slug: str = "development"
    compatibility_user_external_id: str = "development-user"
    auth_disabled: bool = False
    jwt_secret: str | None = None
    jwt_issuer: str = "rag-based-ai-assistant"
    jwt_audience: str = "rag-api"
    access_token_ttl_sec: int = 900
    refresh_token_ttl_sec: int = 2_592_000
    api_key_default_ttl_sec: int = 7_776_000
    auth_cookie_secure: bool = False
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cors_allowed_origins: str = "http://localhost:3000,http://localhost:5173"
    login_rate_limit_attempts: int = 5
    login_rate_limit_window_sec: int = 60
    login_rate_limit_prefix: str = "rag:auth:login"
    inference_rate_limit_per_minute: int = 60
    admin_mutation_rate_limit_per_minute: int = 30
    worker_lease_sec: int = 300
    setup_bootstrap_token: str | None = None
    setup_bootstrap_token_file: str | None = None
    secret_store_provider: Literal["encrypted_db"] = "encrypted_db"
    secret_store_master_key: str | None = None
    secret_store_master_key_file: str | None = None
    secret_store_key_file: str = ".runtime/secrets/master.key"

    stream_edit_interval_sec: float = 1.0
    stream_min_chars_delta: int = 40

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_allowed_origins.split(",") if item.strip()]

    @model_validator(mode="after")
    def fail_closed_in_production(self):
        if self.app_env == "production":
            placeholder_markers = ("replace", "change-me", ".invalid", "example.com")

            def validate_endpoint(
                name: str, value: str, *, schemes: set[str], allow_internal_http: bool = False
            ) -> None:
                lowered = value.lower()
                if any(marker in lowered for marker in placeholder_markers):
                    raise ValueError(f"{name} contains a placeholder.")
                parsed = urlsplit(value)
                http_allowlist = {
                    item.strip().casefold()
                    for item in self.model_http_allowed_hosts.split(",")
                    if item.strip()
                }
                http_allowed = (
                    allow_internal_http
                    and parsed.scheme == "http"
                    and parsed.hostname
                    and parsed.hostname.casefold() in http_allowlist
                )
                if (parsed.scheme not in schemes and not http_allowed) or not parsed.hostname:
                    raise ValueError(f"{name} has an invalid scheme or host.")
                hostname = parsed.hostname.lower()
                if hostname == "localhost":
                    raise ValueError(f"{name} must not use localhost in production.")
                try:
                    if ipaddress.ip_address(hostname).is_loopback:
                        raise ValueError(f"{name} must not use a loopback address in production.")
                except ValueError as exc:
                    if "loopback" in str(exc):
                        raise

            if self.auth_disabled:
                raise ValueError("AUTH_DISABLED is forbidden in production.")
            if not (self.secret_store_master_key or self.secret_store_master_key_file):
                raise ValueError("A secret-store master key source is required in production.")
            if self.setup_bootstrap_token and (
                len(self.setup_bootstrap_token) < 32
                or any(
                    marker in self.setup_bootstrap_token.lower() for marker in placeholder_markers
                )
            ):
                raise ValueError("SETUP_BOOTSTRAP_TOKEN must be a strong secret when enabled.")
            if self.setup_bootstrap_token_file:
                try:
                    file_token = Path(self.setup_bootstrap_token_file).read_text().strip()
                except OSError as exc:
                    raise ValueError("SETUP_BOOTSTRAP_TOKEN_FILE is unavailable.") from exc
                if file_token and (
                    len(file_token) < 32
                    or any(marker in file_token.lower() for marker in placeholder_markers)
                ):
                    raise ValueError(
                        "SETUP_BOOTSTRAP_TOKEN_FILE must contain a strong secret when enabled."
                    )
            if not self.auth_cookie_secure:
                raise ValueError("AUTH_COOKIE_SECURE must be true in production.")
            if self.auth_cookie_samesite == "none" and not self.auth_cookie_secure:
                raise ValueError("SameSite=None requires secure authentication cookies.")
            jwt = self.jwt_secret or ""
            if len(jwt) < 48 or any(marker in jwt.lower() for marker in placeholder_markers):
                raise ValueError("A strong production JWT_SECRET is required.")
            for name, token in (
                ("MODEL_API_TOKEN", self.model_api_token),
                ("EMBEDDING_API_TOKEN", self.embedding_api_token),
                ("RERANKER_API_TOKEN", self.reranker_api_token),
            ):
                if token and any(marker in token.lower() for marker in placeholder_markers):
                    raise ValueError(f"{name} contains a placeholder.")
            if not self.cors_origins or any(
                origin == "*" or not origin.startswith("https://") for origin in self.cors_origins
            ):
                raise ValueError("Production CORS origins must use HTTPS.")
            validate_endpoint(
                "DATABASE_URL",
                self.database_url,
                schemes={"postgresql", "postgresql+psycopg"},
            )
            database_sslmode = parse_qs(urlsplit(self.database_url).query).get("sslmode", [None])[
                -1
            ]
            if database_sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("DATABASE_URL must enable PostgreSQL TLS in production.")
            validate_endpoint("REDIS_URL", self.redis_url, schemes={"rediss"})
            validate_endpoint(
                "MODEL_API_BASE_URL",
                self.model_api_base_url,
                schemes={"https"},
                allow_internal_http=True,
            )
            validate_endpoint(
                "EMBEDDING_API_BASE_URL",
                self.embedding_api_base_url,
                schemes={"https"},
                allow_internal_http=True,
            )
            if self.reranker_api_base_url:
                validate_endpoint(
                    "RERANKER_API_BASE_URL",
                    self.reranker_api_base_url,
                    schemes={"https"},
                    allow_internal_http=True,
                )
            if self.postgres_sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ValueError("POSTGRES_SSLMODE must require TLS in production.")
            if self.trusted_proxy_cidrs.strip() in {"*", "0.0.0.0/0", "::/0"}:
                raise ValueError("Production trusted proxies must be explicit CIDRs.")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=("settings_",),
    )


settings = Settings()
