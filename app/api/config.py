from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
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
    max_new_tokens: int = 320
    sql_top_k: int = 10
    sql_dialect: str = "postgres"
    sql_max_repair_attempts: int = 1

    min_confident_code_score: float = 0.58
    min_confident_doc_score: float = 0.45

    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: str | None = None
    llm_temperature: float = 0.15
    llm_max_context_chars: int = 12000
    llm_retries: int = 2
    llm_retry_backoff_sec: float = 2.0

    request_timeout: float = 60.0
    stream_edit_interval_sec: float = 1.0
    stream_min_chars_delta: int = 40

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
