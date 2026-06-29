"""
Telegram-бот на базе Gemini (Google Gen AI API).

Возможности:
- Хранение истории диалога в SQLite (контекст не теряется при перезапуске).
- Стриминг ответов: текст появляется по мере генерации.
- Поддержка изображений: можно отправить фото и задать по нему вопрос.
- Команды: /start, /reset, /help.

Использует актуальный SDK google-genai (старый google-generativeai устарел).
Запуск: см. README.md
"""

import asyncio
import base64
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

# --- Конфигурация ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Flash-модели бесплатны и поддерживают изображения
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты дружелюбный и полезный ассистент. Отвечай чётко и по делу.",
)

MAX_HISTORY = 20            # сколько последних сообщений брать из истории
TELEGRAM_LIMIT = 4096       # лимит длины сообщения Telegram
STREAM_UPDATE_INTERVAL = 0.7  # как часто обновлять сообщение при стриминге (сек)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN в переменных окружения (.env)")
if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY в переменных окружения (.env)")

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Клиент Gemini. Асинхронные вызовы — через client.aio
client = genai.Client(api_key=GEMINI_API_KEY)


# --- Команды ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.clear_history(update.effective_user.id)
    await update.message.reply_text(
        "Привет! Я бот на базе Gemini.\n\n"
        "• Напиши мне текст — отвечу.\n"
        "• Отправь фото (можно с подписью-вопросом) — разберу изображение.\n\n"
        "Команды:\n"
        "/reset — очистить историю\n"
        "/help — помощь"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.clear_history(update.effective_user.id)
    await update.message.reply_text("История диалога очищена 🧹")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Напиши вопрос или отправь фото — я отвечу с помощью Gemini.\n\n"
        "/start — начать заново\n"
        "/reset — очистить контекст\n"
        "/help — это сообщение"
    )


# --- Обработка текстовых сообщений ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    parts = [{"type": "text", "text": update.message.text}]
    db.add_message(user_id, "user", parts)
    await _generate_and_stream(update, context, user_id)


# --- Обработка изображений ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    caption = update.message.caption or "Что на этом изображении?"

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    # Берём фото в наилучшем качестве (последний элемент массива)
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    parts = [
        {"type": "text", "text": caption},
        {"type": "image", "data": b64, "mime": "image/jpeg"},
    ]
    db.add_message(user_id, "user", parts)
    await _generate_and_stream(update, context, user_id)


# --- Преобразование истории из БД в формат Gemini ---
def _build_contents(history: list[dict]) -> list[types.Content]:
    contents = []
    for msg in history:
        parts = []
        for p in msg["parts"]:
            if p["type"] == "text":
                parts.append(types.Part.from_text(text=p["text"]))
            elif p["type"] == "image":
                raw = base64.b64decode(p["data"])
                parts.append(
                    types.Part.from_bytes(
                        data=raw, mime_type=p.get("mime", "image/jpeg")
                    )
                )
        if parts:
            contents.append(types.Content(role=msg["role"], parts=parts))
    return contents


# --- Общая логика генерации со стримингом ---
async def _generate_and_stream(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    history = db.get_history(user_id, MAX_HISTORY)
    contents = _build_contents(history)

    # Заготовка сообщения, которую будем редактировать по мере генерации
    sent = await update.message.reply_text("…")

    full_answer = ""
    last_update = asyncio.get_event_loop().time()
    last_shown = ""

    try:
        stream = await client.aio.models.generate_content_stream(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )

        async for chunk in stream:
            delta = getattr(chunk, "text", None) or ""
            if not delta:
                continue
            full_answer += delta

            now = asyncio.get_event_loop().time()
            if now - last_update >= STREAM_UPDATE_INTERVAL:
                preview = full_answer[:TELEGRAM_LIMIT]
                if preview != last_shown and preview.strip():
                    try:
                        await sent.edit_text(preview)
                        last_shown = preview
                    except BadRequest:
                        pass  # «message is not modified» и т.п. — игнорируем
                last_update = now

    except Exception as e:
        logger.error("Ошибка при запросе к Gemini: %s", e)
        await sent.edit_text("⚠️ Ошибка при обращении к Gemini. Попробуй ещё раз.")
        return

    if not full_answer.strip():
        await sent.edit_text("(пустой ответ)")
        return

    # Финальный вывод. Длинные ответы режем под лимит Telegram.
    chunks = _split_text(full_answer, TELEGRAM_LIMIT)
    try:
        await sent.edit_text(chunks[0])
    except BadRequest:
        pass
    for extra in chunks[1:]:
        await update.message.reply_text(extra)

    db.add_message(user_id, "model", [{"type": "text", "text": full_answer}])


def _split_text(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


# --- Точка входа ---
def main() -> None:
    db.init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
