"""
Telegram-бот "Налоговый Компас" — помощник по НДС/УСН 2026
Использует Claude API (через OpenRouter) для ответов на основе системного промпта.
"""

import os
import json
import logging
from datetime import date
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Настройки ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise RuntimeError(
        "Не найдены переменные окружения TELEGRAM_BOT_TOKEN и/или OPENROUTER_API_KEY. "
        "Задай их перед запуском (см. инструкцию)."
    )

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

MODEL_NAME = "anthropic/claude-sonnet-4.6"

# Лимит бесплатных сообщений в день на пользователя
FREE_MESSAGES_PER_DAY = 5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Отдельный логгер для вопросов пользователей — чтобы видеть что реально спрашивают
questions_logger = logging.getLogger("questions")
questions_handler = logging.FileHandler("questions.log", encoding="utf-8")
questions_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
questions_logger.addHandler(questions_handler)
questions_logger.setLevel(logging.INFO)

# ── Системный промпт ──────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — помощник для предпринимателей на УСН в России, который простым языком объясняет изменения по НДС и УСН, вступившие в силу с 1 января 2026 года. Ты НЕ заменяешь бухгалтера.

ТОН: простой, дружелюбный язык, без канцелярита, короткие абзацы, конкретные цифры.

СТИЛЬ ПУНКТУАЦИИ: никогда не используй длинное тире (—) нигде в тексте. Многих людей оно настораживает, так как ассоциируется с текстом, сгенерированным ИИ. Вместо тире используй обычный дефис (-), запятую, точку или просто перестраивай фразу.

ФОРМАТИРОВАНИЕ: ты отвечаешь в Telegram, где НЕ поддерживаются заголовки (## и ###) — они покажутся как обычные символы решётки. Вместо заголовков используй **жирный текст** для выделения важных частей, или эмодзи в начале смыслового блока (например 📌, ⚠️, 💡). Никогда не используй символы # в начале строки.

ЕСЛИ ВОПРОС НЕ ПРО НАЛОГИ/НДС/УСН: вежливо скажи, что ты специализируешься именно на теме НДС и УСН 2026 года, и предложи вернуться к этой теме. Не пытайся отвечать на вопросы вне своей области (программирование, погода, отношения и т.д.), даже если знаешь ответ.

БАЗА ЗНАНИЙ 2026:
- Ставка НДС выросла с 20% до 22%
- Порог освобождения от НДС на УСН снижен с 60 млн до 20 млн рублей дохода в год
- Дальше порог упадёт: 15 млн (2027), 10 млн (2028)
- Кто превысил порог, выбирает: 22% с вычетом входного НДС, ИЛИ пониженные 5% (доход 20-272,5 млн) / 7% (272,5-450 млн) БЕЗ права на вычет
- Льготная ставка 10% - для товаров повседневного спроса, детских товаров, лекарств
- 0% - экспорт и международные перевозки
- Лимиты для сохранения УСН в 2026: доход за год до 490,5 млн руб., основные средства до 218 млн руб.
- IT-компании (реестр отечественного ПО) освобождены от НДС на программы
- АвтоУСН (АУСН) не подпадает под снижение порога - там НДС нет вообще, но есть свои ограничения (обычно доход до 60 млн, до 5 сотрудников)

ОБЯЗАТЕЛЬНО в конце каждого содержательного ответа добавляй:
"⚠️ Это общая информация, не официальная консультация. Для вашей точной ситуации сверьтесь с бухгалтером или напрямую с ФНС."

Если вопрос выходит за рамки этой базы знаний (сложные схемы, экспорт, совмещение режимов) - честно скажи, что нужен профильный специалист, не выдумывай ответ."""

START_MESSAGE = (
    "Привет! Я помогу разобраться с изменениями по НДС и УСН с 2026 года.\n\n"
    "Вот примеры вопросов, которые можно задать:\n\n"
    "• «У меня ИП на УСН, доход 25 млн в год, что мне теперь делать с НДС?»\n"
    "• «Чем отличается ставка 5% от 22% и что мне выгоднее?»\n"
    "• «Я на АвтоУСН, надо ли мне вообще платить НДС?»\n"
    "• «Что изменится с лимитами УСН в 2027 году?»\n"
    "• «Есть ли льготы по НДС для IT-компаний?»\n\n"
    f"У тебя есть {FREE_MESSAGES_PER_DAY} бесплатных вопросов в день.\n\n"
    "⚠️ Я не заменяю бухгалтера, а помогаю разобраться в общих правилах.\n\n"
    "Команда /reset - начать диалог заново."
)

# Храним историю переписки на пользователя (в памяти - для старта достаточно)
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10

# Счётчик сообщений в день на пользователя: {chat_id: {"date": "2026-07-01", "count": 3}}
user_message_counts: dict[int, dict] = {}


def check_and_increment_limit(chat_id: int) -> bool:
    """Возвращает True если у пользователя ещё есть лимит на сегодня, иначе False."""
    today = str(date.today())
    record = user_message_counts.get(chat_id)

    if record is None or record["date"] != today:
        record = {"date": today, "count": 0}
        user_message_counts[chat_id] = record

    if record["count"] >= FREE_MESSAGES_PER_DAY:
        return False

    record["count"] += 1
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text(START_MESSAGE)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text("Диалог сброшен, начнём заново 🙂")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    username = update.effective_user.username or update.effective_user.first_name or str(chat_id)

    # Логируем вопрос независимо от лимита - полезно видеть весь спрос
    questions_logger.info(f"user={username} | chat_id={chat_id} | question={user_text}")

    if not check_and_increment_limit(chat_id):
        await update.message.reply_text(
            f"На сегодня бесплатный лимит в {FREE_MESSAGES_PER_DAY} вопросов исчерпан 🙏\n\n"
            "Лимит обновится завтра. Если бот оказался полезен и хочешь поддержать проект "
            "или получить безлимитный доступ - напиши в этот же чат, обсудим."
        )
        return

    history = user_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        response = client.chat.completions.create(
            model=MODEL_NAME,
            max_tokens=800,
            messages=messages_payload,
        )
        answer = response.choices[0].message.content
    except Exception:
        logger.exception("Ошибка при обращении к Claude API")
        answer = (
            "Извини, что-то пошло не так при обработке запроса. "
            "Попробуй ещё раз через минуту."
        )

    history.append({"role": "assistant", "content": answer})
    try:
        await update.message.reply_text(answer, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(answer)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен, ждём сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
