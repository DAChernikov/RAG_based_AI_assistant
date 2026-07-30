from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime настройки параметров окружения приложения.
    Обозначены основные дефолты при отстуствии ENVIRONMENT VARIABLES (.env)
    """

    app_env: str = "dev"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    artifacts_dir: str = str(PROJECT_ROOT / "artifacts")
    force_artifacts_download: bool = False

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_artifact_key: str = "rag-baseline/artifacts_rag_baseline_latest.zip"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_default_region: str = "eu-central-1"

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
    generation_model: str = "qwen2.5-coder:7b"
    model_api_token: str | None = None
    model_request_timeout: float = 90.0
    model_retries: int = 3
    model_retry_backoff_sec: float = 3.0
    model_temperature: float = 0.15
    model_max_context_chars: int = 12000

    stream_edit_interval_sec: float = 1.0
    stream_min_chars_delta: int = 40

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        protected_namespaces=("settings_",),
    )


settings = Settings()
