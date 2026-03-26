import random
import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import EndPointNotFound, TelegramError
from telegram.ext import ContextTypes

from app.bot.api_client import APIClient
from app.bot.config import bot_settings

api_client = APIClient()


def _truncate(text: str, limit: int = 3900) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _build_final_text(answer: str, meta: dict | None) -> str:
    lines = [_truncate(answer, limit=2800)]

    if meta:
        mode = meta.get("mode")
        confidence = meta.get("confidence") or {}
        retrieved = meta.get("retrieved") or []

        if mode:
            lines.insert(0, f"Режим: {mode}")

        if confidence:
            lines.extend(
                [
                    "",
                    "Уверенность:",
                    f"- top1_score: {confidence.get('top1_score')}",
                    f"- gap12: {confidence.get('gap12')}",
                    f"- top_k: {confidence.get('top_k')}",
                    f"- top1_source: {confidence.get('top1_source')}",
                ]
            )

        if retrieved:
            lines.append("")
            lines.append("Источники:")
            for item in retrieved[:3]:
                title = item.get("title") or item.get("doc_id")
                score = item.get("score", 0.0)
                source = item.get("source", "unknown")
                lines.append(f"- {title} | {source} | score={score:.4f}")

    return _truncate("\n".join(lines), limit=4000)


async def _send_message_draft(
    bot,
    *,
    chat_id: int,
    draft_id: int,
    text: str,
) -> None:
    await bot.do_api_request(
        "sendMessageDraft",
        api_kwargs={
            "chat_id": chat_id,
            "draft_id": draft_id,
            "text": _truncate(text, limit=4096),
        },
    )


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

    chat_id = update.effective_chat.id
    draft_id = random.randint(1, 2_000_000_000)

    accumulated = ""
    meta = None

    use_drafts = bot_settings.use_message_drafts
    placeholder_message = None

    last_push_ts = 0.0
    last_push_len = 0
    last_typing_ts = 0.0

    try:
        async for event in api_client.ask_stream(question):
            now = time.monotonic()

            if now - last_typing_ts >= bot_settings.typing_refresh_sec:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                last_typing_ts = now

            event_type = event.get("type")
            data = event.get("data")

            if event_type == "meta":
                meta = data
                continue

            if event_type == "token":
                accumulated += data or ""

                should_push = (
                    now - last_push_ts >= bot_settings.draft_push_interval_sec
                    and len(accumulated) - last_push_len >= bot_settings.stream_min_chars_delta
                )

                if not should_push:
                    continue

                preview = _truncate(accumulated, limit=3500)

                if use_drafts:
                    try:
                        await _send_message_draft(
                            context.bot,
                            chat_id=chat_id,
                            draft_id=draft_id,
                            text=preview,
                        )
                    except EndPointNotFound:
                        use_drafts = False
                    except TelegramError:
                        use_drafts = False

                if not use_drafts:
                    if placeholder_message is None:
                        placeholder_message = await update.message.reply_text(preview)
                    else:
                        try:
                            await placeholder_message.edit_text(preview)
                        except TelegramError:
                            pass

                last_push_ts = now
                last_push_len = len(accumulated)

            elif event_type == "done":
                accumulated = data or accumulated

            elif event_type == "error":
                error_text = f"Ошибка генерации: {data}"
                if placeholder_message is not None:
                    await placeholder_message.edit_text(error_text)
                else:
                    await update.message.reply_text(error_text)
                return

        final_text = _build_final_text(accumulated, meta)

        if placeholder_message is not None:
            await placeholder_message.edit_text(final_text)
        else:
            await update.message.reply_text(final_text)

    except Exception as exc:
        await update.message.reply_text(f"API error: {exc}")
