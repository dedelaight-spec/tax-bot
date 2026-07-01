"""
Telegram-бот "Налоговый Компас" — помощник по НДС/УСН 2026
Использует Claude API для ответов на основе системного промпта.
"""

import os
import logging
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ── Настройки ──────────────────────────────────────────────
# Токены НЕ хардкодим в коде — берём из переменных окружения.
# Это безопаснее: код можно спокойно выкладывать хоть на GitHub.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise RuntimeError(
        "Не найдены переменные окружения TELEGRAM_BOT_TOKEN и/или OPENROUTER_API_KEY. "
        "Задай их перед запуском (см. инструкцию)."
    )

# OpenRouter полностью совместим с OpenAI SDK — просто меняем base_url
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Модель Claude через OpenRouter (можно сменить на другую при желании)
MODEL_NAME = "anthropic/claude-sonnet-4.6"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Системный промпт (сокращённая версия для кода, полная — в системном файле) ──
SYSTEM_PROMPT = """Ты — помощник для предпринимателей на УСН в России, который простым языком объясняет изменения по НДС и УСН, вступившие в силу с 1 января 2026 года. Ты НЕ заменяешь бухгалтера.

ТОН: простой, дружелюбный язык, без канцелярита, короткие абзацы, конкретные цифры.

БАЗА ЗНАНИЙ 2026:
- Ставка НДС выросла с 20% до 22%
- Порог освобождения от НДС на УСН снижен с 60 млн до 20 млн рублей дохода в год
- Дальше порог упадёт: 15 млн (2027), 10 млн (2028)
- Кто превысил порог, выбирает: 22% с вычетом входного НДС, ИЛИ пониженные 5% (доход 20-272,5 млн) / 7% (272,5-450 млн) БЕЗ права на вычет
- Льготная ставка 10% — для товаров повседневного спроса, детских товаров, лекарств
- 0% — экспорт и международные перевозки
- Лимиты для сохранения УСН в 2026: доход за год до 490,5 млн руб., основные средства до 218 млн руб.
- IT-компании (реестр отечественного ПО) освобождены от НДС на программы
- АвтоУСН (АУСН) не подпадает под снижение порога — там НДС нет вообще, но есть свои ограничения (обычно доход до 60 млн, до 5 сотрудников)

ОБЯЗАТЕЛЬНО в конце каждого содержательного ответа добавляй:
"⚠️ Это общая информация, не официальная консультация. Для вашей точной ситуации сверьтесь с бухгалтером или напрямую с ФНС."

Если вопрос выходит за рамки этой базы знаний (сложные схемы, экспорт, совмещение режимов) — честно скажи, что нужен профильный специалист, не выдумывай ответ."""

# Храним историю переписки на пользователя (в памяти — для старта достаточно)
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10  # ограничиваем чтобы не раздувать контекст и расходы


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text(
        "Привет! Я помогу разобраться с изменениями по НДС и УСН с 2026 года.\n\n"
        "Просто опиши свою ситуацию своими словами — например:\n"
        "«У меня ИП на УСН, доход 25 млн в год, что мне теперь делать с НДС?»\n\n"
        "⚠️ Я не заменяю бухгалтера, а помогаю разобраться в общих правилах."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text("Диалог сброшен, начнём заново 🙂")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text

    history = user_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]  # обрезаем старую историю

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Формат OpenAI-совместимого API: system идёт первым сообщением в messages
        messages_payload = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        response = client.chat.completions.create(
            model=MODEL_NAME,
            max_tokens=800,
            messages=messages_payload,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        logger.exception("Ошибка при обращении к Claude API")
        answer = (
            "Извини, что-то пошло не так при обработке запроса. "
            "Попробуй ещё раз через минуту."
        )

    history.append({"role": "assistant", "content": answer})
    try:
        await update.message.reply_text(answer, parse_mode="Markdown")
    except Exception:
        # Если Markdown сломан (например, незакрытые звёздочки) — шлём как обычный текст
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
