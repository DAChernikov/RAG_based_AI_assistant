import pytest

from app.bot.api_client import APIClient
from app.bot.config import bot_settings
from app.bot.main import build_application


def test_telegram_client_sends_configured_api_key(monkeypatch):
    monkeypatch.setattr(bot_settings, "api_key", "test-telegram-api-key")

    client = APIClient()

    assert client.headers == {"X-API-Key": "test-telegram-api-key"}


def test_telegram_client_omits_empty_api_key(monkeypatch):
    monkeypatch.setattr(bot_settings, "api_key", "")

    client = APIClient()

    assert client.headers == {}


def test_bot_token_is_validated_only_at_startup(monkeypatch):
    monkeypatch.setattr(bot_settings, "telegram_bot_token", None)

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        build_application()
