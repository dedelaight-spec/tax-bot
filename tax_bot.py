"""
Telegram-бот "Налоговый Компас" - помощник по НДС/УСН 2026
Использует Claude API (через OpenRouter) для ответов на основе системного промпта.
Поддерживает оплату через Telegram Stars (авто) и USDT (авто-проверка через блокчейн).
"""

import os
import re
import logging
import requests
from datetime import date, datetime, timedelta
from openai import OpenAI
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── Настройки ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
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

FREE_MESSAGES_PER_DAY = 5
STARS_PRICE = 250  # звёзд в месяц (курс плавает, проверь актуальный на момент запуска)
USDT_PRICE = 5  # долларов в месяц
TRONSCAN_API = "https://apilist.tronscanapi.com/api/transaction-info"
TXID_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")  # TRC-20 хэш транзакции - всегда 64 hex-символа

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
    "Команда /help - подробнее о боте и правилах использования."
)

HELP_MESSAGE = (
    "ℹ️ О боте\n\n"
    "Налоговый Компас объясняет изменения по НДС и УСН, действующие с 1 января 2026 года: "
    "ставки, пороги, лимиты. Отвечает простым языком на основе актуальных норм.\n\n"
    "⚠️ Важно понимать\n\n"
    "Бот не заменяет бухгалтера и не даёт официальных консультаций. Ответы носят "
    "общий информационный характер. Для решений с финансовыми последствиями "
    "сверяйтесь с бухгалтером или напрямую с ФНС.\n\n"
    "🔒 О данных\n\n"
    "Вопросы, которые ты задаёшь, сохраняются в техническом логе, чтобы дорабатывать "
    "качество ответов бота. Личные данные (кроме имени пользователя Telegram) не запрашиваются "
    "и не передаются третьим лицам.\n\n"
    f"💳 Подписка\n\n"
    f"Бесплатно: {FREE_MESSAGES_PER_DAY} вопросов в день. "
    f"Безлимитная подписка: {STARS_PRICE} ⭐ или ${USDT_PRICE} в USDT за 30 дней. Оформить: /subscribe\n\n"
    "Возврат средств: если подписка не была активирована по ошибке бота - "
    "напиши в этот же чат, разберёмся индивидуально.\n\n"
    "Команды:\n"
    "/subscribe - оформить подписку\n"
    "/status - остаток бесплатных вопросов / статус подписки\n"
    "/reset - начать диалог заново\n"
    "/help - это сообщение"
)

# ── Хранилища данных (в памяти - для MVP достаточно) ──────────────────
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10

user_message_counts: dict[int, dict] = {}
subscriptions: dict[int, datetime] = {}
used_txids: set[str] = set()  # чтобы один и тот же платёж нельзя было применить дважды


def has_active_subscription(chat_id: int) -> bool:
    expiry = subscriptions.get(chat_id)
    return expiry is not None and expiry > datetime.now()


def check_and_increment_limit(chat_id: int) -> bool:
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


def remaining_free_messages(chat_id: int) -> int:
    today = str(date.today())
    record = user_message_counts.get(chat_id)
    if record is None or record["date"] != today:
        return FREE_MESSAGES_PER_DAY
    return max(0, FREE_MESSAGES_PER_DAY - record["count"])


def grant_subscription(chat_id: int, days: int = 30) -> None:
    current_expiry = subscriptions.get(chat_id)
    base = current_expiry if current_expiry and current_expiry > datetime.now() else datetime.now()
    subscriptions[chat_id] = base + timedelta(days=days)


def verify_usdt_transaction(txid: str) -> tuple[bool, str]:
    """
    Проверяет транзакцию через публичный Tronscan API.
    Возвращает (успех, сообщение_с_подробностями).
    """
    if txid in used_txids:
        return False, "Этот хэш транзакции уже был использован ранее."

    try:
        resp = requests.get(TRONSCAN_API, params={"hash": txid}, timeout=10)
        data = resp.json()
    except Exception:
        logger.exception("Ошибка запроса к Tronscan API")
        return False, "Не удалось проверить транзакцию (проблема связи с блокчейном). Попробуй ещё раз через минуту."

    if not data or data.get("contractRet") != "SUCCESS":
        return False, "Транзакция не найдена или ещё не подтверждена в блокчейне. Подожди пару минут и попробуй снова."

    transfers = data.get("trc20TransferInfo", [])
    if not transfers:
        return False, "В этой транзакции не найден перевод USDT (TRC-20)."

    for t in transfers:
        token_abbr = t.get("tokenInfo", {}).get("tokenAbbr", "")
        to_address = t.get("to_address", "")
        decimals = t.get("tokenInfo", {}).get("tokenDecimal", 6)
        try:
            amount = float(t.get("amount_str", "0")) / (10 ** decimals)
        except (ValueError, TypeError):
            amount = 0

        if token_abbr == "USDT" and to_address == USDT_WALLET_ADDRESS and amount >= USDT_PRICE - 0.01:
            return True, f"Оплата подтверждена: {amount:.2f} USDT"

    return False, (
        "Перевод USDT на нужный адрес и нужную сумму не найден в этой транзакции.\n\n"
        "Частые причины:\n"
        "- Отправлено не в сети TRC-20 (например, по ошибке ERC-20 или BEP-20)\n"
        "- Адрес получателя не совпадает\n"
        "- Сумма меньше требуемой\n"
        "- Транзакция ещё не подтвердилась в блокчейне (подожди 1-2 минуты)"
    )


# ── Команды ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    await update.message.reply_text(START_MESSAGE)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_MESSAGE)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_histories[update.effective_chat.id] = []
    context.user_data["awaiting_usdt_txid"] = False
    await update.message.reply_text("Диалог сброшен, начнём заново 🙂")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if has_active_subscription(chat_id):
        expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
        await update.message.reply_text(f"У тебя активная подписка до {expiry} 🎉")
    else:
        left = remaining_free_messages(chat_id)
        await update.message.reply_text(
            f"Осталось бесплатных вопросов сегодня: {left} из {FREE_MESSAGES_PER_DAY}.\n"
            "Безлимит: /subscribe"
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if has_active_subscription(chat_id):
        expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
        await update.message.reply_text(f"У тебя уже есть активная подписка до {expiry} 🎉")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Telegram Stars ({STARS_PRICE})", callback_data="pay_stars")],
        [InlineKeyboardButton(f"💵 USDT (${USDT_PRICE})", callback_data="pay_usdt")],
    ])

    await update.message.reply_text(
        "💳 Безлимитная подписка на месяц\n\nВыбери удобный способ оплаты:",
        reply_markup=keyboard,
    )


async def subscribe_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "pay_stars":
        await send_stars_invoice(query.message.chat_id, context)
    elif query.data == "pay_usdt":
        await send_usdt_instructions(query.message.chat_id, context)


async def send_stars_invoice(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    prices = [LabeledPrice("Подписка на месяц", STARS_PRICE)]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Налоговый Компас - подписка на месяц",
        description="Безлимитные вопросы по НДС и УСН на 30 дней",
        payload=f"subscription_{chat_id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def send_usdt_instructions(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Инструкция по оплате в USDT. После этого бот переходит в режим ожидания хэша -
    пользователь просто присылает TXID обычным сообщением, без команд.
    """
    context.user_data["awaiting_usdt_txid"] = True

    text = (
        f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
        f"`{USDT_WALLET_ADDRESS}`\n\n"
        "После оплаты просто пришли сюда хэш транзакции (TXID) одним сообщением - "
        "никаких команд вводить не нужно. Бот сам проверит платёж в блокчейне "
        "и активирует подписку автоматически, обычно в течение минуты."
    )
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Ошибка отправки сообщения pay_usdt (возможно, проблема с USDT_WALLET_ADDRESS)")
        plain_text = (
            f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
            f"{USDT_WALLET_ADDRESS}\n\n"
            "После оплаты просто пришли сюда хэш транзакции (TXID) одним сообщением."
        )
        await context.bot.send_message(chat_id=chat_id, text=plain_text)


async def pay_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_stars_invoice(update.effective_chat.id, context)


async def pay_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_usdt_instructions(update.effective_chat.id, context)


async def process_usdt_txid(update: Update, context: ContextTypes.DEFAULT_TYPE, txid: str) -> None:
    """Автоматическая проверка присланного хэша транзакции и выдача подписки."""
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or str(chat_id)

    await update.message.reply_text("Проверяю транзакцию в блокчейне, подожди немного... ⏳")

    success, message = verify_usdt_transaction(txid.strip())

    if success:
        used_txids.add(txid.strip())
        grant_subscription(chat_id, days=30)
        expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
        await update.message.reply_text(
            f"✅ {message}\n\nПодписка активирована до {expiry}. "
            "Теперь можно задавать сколько угодно вопросов."
        )
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"✅ Автоматически подтверждён платёж USDT\nОт: @{username} (chat_id: {chat_id})\nTXID: {txid}",
                )
            except Exception:
                pass
    else:
        await update.message.reply_text(
            f"⚠️ {message}\n\n"
            "Если уверен, что оплата прошла корректно - напиши в этот же чат, разберёмся вручную."
        )
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"⚠️ Не удалось авто-подтвердить платёж\n"
                        f"От: @{username} (chat_id: {chat_id})\n"
                        f"TXID: {txid}\n"
                        f"Причина: {message}\n\n"
                        f"Если платёж реальный - подтверди вручную: /grant {chat_id}"
                    ),
                )
            except Exception:
                pass


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    grant_subscription(chat_id, days=30)
    expiry = subscriptions[chat_id].strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"Оплата прошла успешно! ✅ Подписка активна до {expiry}.\n"
        "Теперь можно задавать сколько угодно вопросов."
    )


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ-команда для ручного подтверждения оплаты, на случай если авто-проверка не сработала."""
    caller_id = str(update.effective_chat.id)
    if not ADMIN_CHAT_ID or caller_id != ADMIN_CHAT_ID:
        return

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
    text_stripped = user_text.strip()

    # Хэш транзакции распознаём по формату (64 hex-символа) - это надёжнее,
    # чем полагаться только на "режим ожидания". Работает даже если пользователь
    # ошибся в первый раз и присылает исправленный хэш заново, без повторного нажатия кнопки.
    if TXID_PATTERN.match(text_stripped):
        context.user_data["awaiting_usdt_txid"] = False
        await process_usdt_txid(update, context, text_stripped)
        return

    # Если бот ждал хэш, но получил что-то на него не похожее - не молчим и не ломаем диалог,
    # а мягко поясняем и продолжаем как обычный вопрос (вдруг это правда был вопрос про налоги)
    was_awaiting_txid = context.user_data.get("awaiting_usdt_txid", False)
    if was_awaiting_txid:
        context.user_data["awaiting_usdt_txid"] = False

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
    """Настраивает меню команд, всплывающее у пользователя в Telegram."""
    commands = [
        ("start", "Начать работу с ботом"),
        ("subscribe", "Оформить безлимитную подписку"),
        ("status", "Сколько вопросов осталось / статус подписки"),
        ("help", "О боте и правилах использования"),
        ("reset", "Начать диалог заново"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("pay_stars", pay_stars))
    app.add_handler(CommandHandler("pay_usdt", pay_usdt))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CallbackQueryHandler(subscribe_button_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен, ждём сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
