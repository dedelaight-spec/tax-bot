"""
Telegram-бот "Налоговый Компас" - помощник по НДС/УСН 2026
Использует Claude API (через OpenRouter) для ответов на основе системного промпта.
Поддерживает оплату через Telegram Stars и USDT (с ручным подтверждением).
"""

import os
import logging
from datetime import date, datetime, timedelta
from openai import OpenAI
from telegram import Update, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
    ContextTypes,
)

# ── Настройки ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # твой личный chat_id для команд /grant
USDT_WALLET_ADDRESS = os.environ.get("USDT_WALLET_ADDRESS", "УКАЖИ_СВОЙ_АДРЕС_В_ПЕРЕМЕННЫХ")

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

# Стоимость подписки
STARS_PRICE = 250  # звёзд в месяц, примерно $5 (курс звёзд плавает, проверь актуальный на момент запуска)
USDT_PRICE = 5  # долларов в месяц

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

questions_logger = logging.getLogger("questions")
questions_handler = logging.FileHandler("questions.log", encoding="utf-8")
questions_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
questions_logger.addHandler(questions_handler)
questions_logger.setLevel(logging.INFO)

# ── Системный промпт ──────────────────────────────────────────────
SYSTEM_PROMPT = """Ты - помощник для предпринимателей на УСН в России, который простым языком объясняет изменения по НДС и УСН, вступившие в силу с 1 января 2026 года. Ты НЕ заменяешь бухгалтера.

ТОН: простой, дружелюбный язык, без канцелярита, короткие абзацы, конкретные цифры.

СТИЛЬ ПУНКТУАЦИИ: никогда не используй длинное тире (—) нигде в тексте. Многих людей оно настораживает, так как ассоциируется с текстом, сгенерированным ИИ. Вместо тире используй обычный дефис (-), запятую, точку или просто перестраивай фразу.

ФОРМАТИРОВАНИЕ: ты отвечаешь в Telegram, где НЕ поддерживаются заголовки (## и ###). Вместо заголовков используй **жирный текст** или эмодзи в начале смыслового блока (например 📌, ⚠️, 💡). Никогда не используй символы # в начале строки.

ЕСЛИ ВОПРОС НЕ ПРО НАЛОГИ/НДС/УСН: вежливо скажи, что ты специализируешься именно на теме НДС и УСН 2026 года, и предложи вернуться к этой теме.

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

Если вопрос выходит за рамки этой базы знаний - честно скажи, что нужен профильный специалист, не выдумывай ответ."""

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
    "Команды:\n"
    "/subscribe - безлимитный доступ по подписке\n"
    "/reset - начать диалог заново"
)

# ── Хранилища данных (в памяти - для MVP достаточно) ──────────────────
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10

user_message_counts: dict[int, dict] = {}

# Активные подписки: {chat_id: дата_окончания}
subscriptions: dict[int, datetime] = {}


def has_active_subscription(chat_id: int) -> bool:
    expiry = subscriptions.get(chat_id)
    return expiry is not None and expiry > datetime.now()


def check_and_increment_limit(chat_id: int) -> bool:
    """True если лимит не исчерпан или есть активная подписка."""
    if has_active_subscription(chat_id):
        return True

    today = str(date.today())
    record = user_message_counts.get(chat_id)

    if record is None or record["date"] != today:
        record = {"date": today, "count": 0}
        user_message_counts[chat_id] = record

    if record["count"] >= FREE_MESSAGES_PER_DAY:
        return False

    record["count"] += 1
    return True


def grant_subscription(chat_id: int, days: int = 30) -> None:
    current_expiry = subscriptions.get(chat_id)
    base = current_expiry if current_expiry and current_expiry > datetime.now() else datetime.now()
    subscriptions[chat_id] = base + timedelta(days=days)


# ── Команды ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text(START_MESSAGE)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text("Диалог сброшен, начнём заново 🙂")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает варианты оплаты."""
    chat_id = update.effective_chat.id

    if has_active_subscription(chat_id):
        expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
        await update.message.reply_text(f"У тебя уже есть активная подписка до {expiry} 🎉")
        return

    text = (
        "💳 Безлимитная подписка на месяц\n\n"
        f"Вариант 1 - Telegram Stars ({STARS_PRICE} ⭐):\n"
        "/pay_stars\n\n"
        f"Вариант 2 - USDT (${USDT_PRICE}):\n"
        "/pay_usdt"
    )
    await update.message.reply_text(text)


async def pay_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет инвойс на оплату через Telegram Stars."""
    chat_id = update.effective_chat.id
    prices = [LabeledPrice("Подписка на месяц", STARS_PRICE)]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Налоговый Компас - подписка на месяц",
        description="Безлимитные вопросы по НДС и УСН на 30 дней",
        payload=f"subscription_{chat_id}",
        provider_token="",  # для Stars provider_token оставляем пустым
        currency="XTR",
        prices=prices,
    )


async def pay_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Инструкция по оплате в USDT с ручным подтверждением."""
    chat_id = update.effective_chat.id
    text = (
        f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
        f"`{USDT_WALLET_ADDRESS}`\n\n"
        "После оплаты пришли сюда хэш транзакции (TXID) - "
        "я проверю и активирую подписку в течение нескольких часов.\n\n"
        f"Твой chat_id для справки: `{chat_id}`"
    )
    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception:
        logger.exception("Ошибка отправки сообщения pay_usdt (возможно, проблема с USDT_WALLET_ADDRESS)")
        # Отправляем без форматирования, чтобы точно дошло
        plain_text = (
            f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
            f"{USDT_WALLET_ADDRESS}\n\n"
            "После оплаты пришли сюда хэш транзакции (TXID) - "
            "я проверю и активирую подписку в течение нескольких часов.\n\n"
            f"Твой chat_id для справки: {chat_id}"
        )
        await update.message.reply_text(plain_text)


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обязательный шаг перед оплатой Stars - подтверждаем что всё ок."""
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает после успешной оплаты через Stars."""
    chat_id = update.effective_chat.id
    grant_subscription(chat_id, days=30)
    expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"Оплата прошла успешно! ✅ Подписка активна до {expiry}.\n"
        "Теперь можно задавать сколько угодно вопросов."
    )


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Админ-команда для ручного подтверждения оплаты USDT.
    Использование: /grant <chat_id>
    Доступна только тебе (ADMIN_CHAT_ID).
    """
    caller_id = str(update.effective_chat.id)
    if not ADMIN_CHAT_ID or caller_id != ADMIN_CHAT_ID:
        return  # молча игнорируем, если пишет не админ

    if not context.args:
        await update.message.reply_text("Использование: /grant <chat_id>")
        return

    try:
        target_chat_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("chat_id должен быть числом.")
        return

    grant_subscription(target_chat_id, days=30)
    expiry = subscriptions[target_chat_id].strftime("%d.%m.%Y")
    await update.message.reply_text(f"Подписка выдана для {target_chat_id} до {expiry}.")

    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"Оплата подтверждена! ✅ Подписка активна до {expiry}.",
        )
    except Exception:
        logger.warning(f"Не удалось уведомить пользователя {target_chat_id}")


# ── Обработка обычных сообщений ──────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    username = update.effective_user.username or update.effective_user.first_name or str(chat_id)

    questions_logger.info(f"user={username} | chat_id={chat_id} | question={user_text}")

    if not check_and_increment_limit(chat_id):
        await update.message.reply_text(
            f"На сегодня бесплатный лимит в {FREE_MESSAGES_PER_DAY} вопросов исчерпан 🙏\n\n"
            "Лимит обновится завтра, либо оформи безлимитную подписку: /subscribe"
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


async def post_init(application: Application) -> None:
    """Настраивает красивое меню команд, всплывающее у пользователя в Telegram."""
    commands = [
        ("start", "Начать работу с ботом"),
        ("subscribe", "Оформить безлимитную подписку"),
        ("reset", "Начать диалог заново"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("pay_stars", pay_stars))
    app.add_handler(CommandHandler("pay_usdt", pay_usdt))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен, ждём сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
