import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.bot.api_client import APIClient
from app.bot.config import bot_settings

api_client = APIClient()


def _truncate(text: str, limit: int = 3500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


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
    except Exception as exc:
        await update.message.reply_text(f"API error: {exc}")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    question = (update.message.text or "").strip()
    if not question:
        await update.message.reply_text("Пустой вопрос.")
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    msg = await update.message.reply_text("Думаю...")

    accumulated = ""
    meta = None
    last_edit_ts = 0.0
    last_edit_len = 0

    try:
        async for event in api_client.ask_stream(question):
            event_type = event.get("type")
            data = event.get("data")

            if event_type == "meta":
                meta = data
                continue

            if event_type == "token":
                accumulated += data or ""

                now = time.monotonic()
                should_edit = (
                    now - last_edit_ts >= bot_settings.stream_edit_interval_sec
                    and len(accumulated) - last_edit_len >= bot_settings.stream_min_chars_delta
                )

                if should_edit:
                    text = _truncate(accumulated)
                    if text:
                        try:
                            await msg.edit_text(text)
                            last_edit_ts = now
                            last_edit_len = len(accumulated)
                        except Exception:
                            pass

            elif event_type == "done":
                accumulated = data or accumulated

            elif event_type == "error":
                await msg.edit_text(f"Ошибка генерации: {data}")
                return

        final_lines = [_truncate(accumulated)]

        if meta:
            confidence = meta.get("confidence") or {}
            retrieved = meta.get("retrieved") or []

            if confidence:
                final_lines.extend(
                    [
                        "",
                        "Уверенность:",
                        f"- top1_score: {confidence.get('top1_score')}",
                        f"- gap12: {confidence.get('gap12')}",
                        f"- top_k: {confidence.get('top_k')}",
                    ]
                )

            if retrieved:
                final_lines.append("")
                final_lines.append("Источники:")
                for item in retrieved[:3]:
                    title = item.get("title") or item.get("doc_id")
                    score = item.get("score", 0.0)
                    final_lines.append(f"- {title} | score={score:.4f}")

        await msg.edit_text(_truncate("\n".join(final_lines), limit=4000))

    except Exception as exc:
        await msg.edit_text(f"API error: {exc}")
