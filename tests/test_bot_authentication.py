from app.bot.api_client import APIClient
from app.bot.config import bot_settings


def test_telegram_client_sends_configured_api_key(monkeypatch):
    monkeypatch.setattr(bot_settings, "api_key", "test-telegram-api-key")

    client = APIClient()

    assert client.headers == {"X-API-Key": "test-telegram-api-key"}


def test_telegram_client_omits_empty_api_key(monkeypatch):
    monkeypatch.setattr(bot_settings, "api_key", "")

    client = APIClient()

    assert client.headers == {}
