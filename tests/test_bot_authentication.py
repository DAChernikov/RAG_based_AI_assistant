import pytest

from app.bot.api_client import APIClient
from app.bot.main import build_application


def test_telegram_client_sends_explicit_scoped_api_key():
    client = APIClient(api_key="test-telegram-api-key")

    assert client.headers == {"X-API-Key": "test-telegram-api-key"}


def test_telegram_client_omits_empty_api_key():
    client = APIClient()

    assert client.headers == {}


def test_bot_token_is_validated_only_when_polling_application_is_built():
    with pytest.raises(RuntimeError, match="token is required"):
        build_application("")
