from __future__ import annotations

import html
import re
from typing import Any

import httpx
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from app.bot.api_client import APIClient

api_client = APIClient()

TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_MESSAGE_LIMIT = 3950
CODE_FENCE_RE = re.compile(r"```(\w+)?\s*\n?(.*?)```", flags=re.DOTALL)

RATE_LIMIT_MESSAGE = (
    "Сервис временно упёрся в лимит LLM API. " "Попробуйте повторить запрос через некоторое время."
)


def _truncate(text: str, limit: int = SAFE_MESSAGE_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_score(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _html_with_code_blocks(text: str) -> str:
    """Escape normal text and render fenced code blocks as Telegram HTML code blocks."""
    if not text:
        return ""

    parts: list[str] = []
    pos = 0

    for match in CODE_FENCE_RE.finditer(text):
        parts.append(html.escape(text[pos : match.start()]))
        language = (match.group(1) or "").strip().lower()
        code = html.escape(match.group(2).strip())

        if language:
            parts.append(f'<pre><code class="language-{language}">{code}</code></pre>')
        else:
            parts.append(f"<pre><code>{code}</code></pre>")

        pos = match.end()

    parts.append(html.escape(text[pos:]))
    return "".join(parts)


def _ensure_sql_answer_has_code_fence(answer: str) -> str:
    if "```" in answer or not re.search(r"\bSQL\s*:", answer, flags=re.I):
        return answer

    match = re.search(r"\bSQL\s*:\s*", answer, flags=re.I)
    if not match:
        return answer

    before = answer[: match.end()].rstrip()
    sql_part = answer[match.end() :].strip()
    if not sql_part:
        return answer

    return f"{before}\n```sql\n{sql_part.rstrip(';')};\n```"


def _format_validation(confidence: dict[str, Any]) -> list[str]:
    validation = confidence.get("validation")
    if not isinstance(validation, dict):
        return []

    lines = [f"- sql_valid: {validation.get('is_valid')}"]

    used_tables = validation.get("used_tables") or []
    if used_tables:
        lines.append(f"- used_tables: {', '.join(used_tables)}")

    used_columns = validation.get("used_columns") or []
    if used_columns:
        joined = ", ".join(used_columns[:8])
        suffix = "..." if len(used_columns) > 8 else ""
        lines.append(f"- used_columns: {joined}{suffix}")

    explain_checked = validation.get("explain_checked")
    explain_error = validation.get("explain_error")
    if explain_checked is not None:
        lines.append(f"- explain_checked: {explain_checked}")

    if explain_checked and validation.get("is_valid"):
        lines.append("- SQL проверен через Postgres EXPLAIN")
    elif explain_checked and explain_error:
        lines.append(f"- explain_error: {_truncate(str(explain_error), limit=250)}")

    errors = validation.get("errors") or []
    if errors:
        lines.append(f"- validation_errors: {len(errors)}")
        lines.append(f"- first_error: {_truncate(str(errors[0]), limit=250)}")

    return lines


def _build_final_text(answer: str, meta: dict[str, Any] | None) -> str:
    mode = meta.get("mode") if meta else None
    if mode == "sql":
        answer = _ensure_sql_answer_has_code_fence(answer)

    plain_lines: list[str] = []
    if mode:
        plain_lines.append(f"Режим: {mode}")

    plain_lines.append(answer.strip())

    if meta:
        confidence = meta.get("confidence") or {}
        retrieved = meta.get("retrieved") or []

        if confidence:
            plain_lines.extend(["", "Уверенность:"])
            for key in ["top1_score", "gap12", "top_k", "sql_top_k", "top1_source"]:
                if confidence.get(key) is not None:
                    plain_lines.append(f"- {key}: {_format_score(confidence.get(key))}")

            plain_lines.extend(_format_validation(confidence))

        if retrieved:
            plain_lines.extend(["", "Источники:"])
            for item in retrieved[:3]:
                title = item.get("title") or item.get("doc_id")
                score = item.get("score", 0.0)
                source = item.get("source", "unknown")

                if isinstance(score, (int, float)):
                    plain_lines.append(f"- {title} | {source} | score={score:.4f}")
                else:
                    plain_lines.append(f"- {title} | {source}")

    plain_text = _truncate("\n".join(plain_lines), limit=SAFE_MESSAGE_LIMIT)
    return _html_with_code_blocks(plain_text)


def _extract_api_error_message(exc: httpx.HTTPStatusError) -> str:
    try:
        payload = exc.response.json()
        detail = payload.get("detail")
        if detail:
            return str(detail)
    except Exception:
        pass

    if exc.response.status_code == 429:
        return RATE_LIMIT_MESSAGE

    return str(exc)


async def _safe_edit_or_reply(update: Update, message, text: str) -> None:
    if message is not None:
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            return
        except (BadRequest, TelegramError):
            await message.edit_text(re.sub(r"<[^>]+>", "", text))
            return

    if update.message:
        try:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except (BadRequest, TelegramError):
            await update.message.reply_text(re.sub(r"<[^>]+>", "", text))


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = (
        "Привет. Я AI assistant bot.\n\n"
        "Отправь вопрос обычным сообщением.\n"
        "Команда /ready покажет статус API."
    )
    await update.message.reply_text(text)


async def ready_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    try:
        data = await api_client.ready()
        text = (
            "Статус API:\n"
            f"- status: {data.get('status')}\n"
            f"- artifacts_ready: {data.get('artifacts_ready')}\n"
            f"- rag_ready: {data.get('rag_ready')}\n"
            f"- startup_error: {data.get('startup_error')}"
        )
        await update.message.reply_text(text)
    except httpx.HTTPStatusError as exc:
        await update.message.reply_text(_extract_api_error_message(exc))
    except Exception as exc:
        await update.message.reply_text(f"API error: {exc}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    question = (update.message.text or "").strip()
    if not question:
        await update.message.reply_text("Пустой вопрос.")
        return

    placeholder_message = await update.message.reply_text("Думаю...")

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )

        data = await api_client.ask(question=question)
        final_text = _build_final_text(data.get("answer", ""), data)
        await _safe_edit_or_reply(update, placeholder_message, final_text)

    except httpx.HTTPStatusError as exc:
        await placeholder_message.edit_text(_extract_api_error_message(exc))

    except Exception as exc:
        await placeholder_message.edit_text(f"API error: {exc}")
