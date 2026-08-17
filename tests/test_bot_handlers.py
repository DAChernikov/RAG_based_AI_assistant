from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.bot import handlers


class Message:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        if text == "Думаю...":
            return self
        return None

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def update(text=""):
    return SimpleNamespace(message=Message(text), effective_chat=SimpleNamespace(id=42))


class Bot:
    def __init__(self):
        self.actions = []

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)


def context():
    return SimpleNamespace(bot=Bot())


def test_telegram_formatting_escapes_content_and_reports_sql_validation():
    text = handlers._build_final_text(
        "SQL: SELECT * FROM users;",
        {
            "mode": "sql",
            "confidence": {
                "top1_score": 0.8,
                "validation": {
                    "is_valid": True,
                    "used_tables": ["public.users"],
                    "used_columns": ["id"],
                    "explain_checked": True,
                },
            },
            "retrieved": [{"title": "<Users>", "source": "jdbc", "score": 0.9}],
        },
    )
    assert '<pre><code class="language-sql">' in text
    assert "&lt;Users&gt;" in text
    assert "SQL проверен" in text
    assert handlers._truncate("x" * 20, 10) == "xxxxxxx..."
    assert handlers._ensure_sql_answer_has_code_fence("plain") == "plain"


def test_telegram_http_error_messages_are_sanitized():
    request = httpx.Request("GET", "http://api.test")
    detail = httpx.Response(400, request=request, json={"detail": "bad input"})
    assert (
        handlers._extract_api_error_message(
            httpx.HTTPStatusError("bad", request=request, response=detail)
        )
        == "bad input"
    )
    busy = httpx.Response(429, request=request, text="proxy")
    assert "лимит" in handlers._extract_api_error_message(
        httpx.HTTPStatusError("bad", request=request, response=busy)
    )
    unavailable = httpx.Response(503, request=request, text="proxy")
    assert "недоступен" in handlers._extract_api_error_message(
        httpx.HTTPStatusError("bad", request=request, response=unavailable)
    )


@pytest.mark.asyncio
async def test_start_ready_and_text_handlers(monkeypatch):
    item = update("question")
    ctx = context()

    class Client:
        async def ready(self):
            return {
                "status": "ready",
                "components": {
                    "database": "ready",
                    "redis": "ready",
                    "inference_worker": "ready",
                    "generation_model": "ready",
                    "embedding_model": "ready",
                },
            }

        async def ask(self, **kwargs):
            assert kwargs["question"] == "question"
            return {
                "answer": "Grounded answer",
                "mode": "rag_docs",
                "retrieved": [{"title": "Guide", "source": "website", "score": 0.8}],
            }

    monkeypatch.setattr(handlers, "api_client", Client())
    await handlers.start_handler(item, ctx)
    await handlers.ready_handler(item, ctx)
    await handlers.text_handler(item, ctx)
    assert any("Привет" in value[0] for value in item.message.replies)
    assert any("database: ready" in value[0] for value in item.message.replies)
    assert "Grounded answer" in item.message.edits[-1][0]
    assert ctx.bot.actions[0]["chat_id"] == 42


@pytest.mark.asyncio
async def test_handlers_cover_empty_message_and_api_failures(monkeypatch):
    ctx = context()
    empty = update(" ")
    await handlers.text_handler(empty, ctx)
    assert empty.message.replies[0][0] == "Пустой вопрос."
    await handlers.start_handler(SimpleNamespace(message=None), ctx)
    await handlers.ready_handler(SimpleNamespace(message=None), ctx)
    await handlers.text_handler(SimpleNamespace(message=None), ctx)

    class Failure:
        async def ready(self):
            raise RuntimeError("offline")

        async def ask(self, **_kwargs):
            request = httpx.Request("POST", "http://api.test/ask")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("offline", request=request, response=response)

    monkeypatch.setattr(handlers, "api_client", Failure())
    ready = update()
    await handlers.ready_handler(ready, ctx)
    assert "offline" in ready.message.replies[0][0]
    question = update("question")
    await handlers.text_handler(question, ctx)
    assert "недоступен" in question.message.edits[-1][0]
