from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    api_base_url: str = "http://api:8000"
    bot_config_reload_interval_sec: float = 5.0
    request_timeout: float = 90.0
    stream_connect_timeout: float = 10.0
    stream_read_timeout: float = 120.0
    stream_write_timeout: float = 15.0
    stream_pool_timeout: float = 10.0

    stream_edit_interval_sec: float = 1.0
    stream_min_chars_delta: int = 40

    draft_push_interval_sec: float = 0.7
    typing_refresh_sec: float = 4.0
    use_message_drafts: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


bot_settings = BotSettings()
