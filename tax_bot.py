"""
Telegram-бот "Налоговый Компас" - помощник по НДС/УСН 2026
Использует Claude API (через OpenRouter) для ответов на основе системного промпта.
Поддерживает оплату через Telegram Stars (авто) и USDT (авто-проверка через блокчейн).
Данные о подписках/лимитах хранятся в SQLite - переживают перезапуски и деплои
(при условии подключённого Railway Volume, см. инструкцию).
"""

import os
import re
import sqlite3
import logging
import requests
from datetime import date, datetime, timedelta, time as dt_time, timezone
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
# Путь к базе данных - ОБЯЗАТЕЛЬНО указать путь внутри примонтированного Volume в Railway,
# иначе база будет стираться при каждом деплое. По умолчанию - локальный файл (для разработки).
DB_PATH = os.environ.get("DB_PATH", "bot.db")

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

FREE_MESSAGES_PER_MONTH = 3
STARS_PRICE = 250
USDT_PRICE = 5
TRONSCAN_API = "https://apilist.tronscanapi.com/api/transaction-info"
TXID_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
MAX_VERIFY_ATTEMPTS_PER_HOUR = 8
BACKUP_DIR = os.path.dirname(os.path.abspath(DB_PATH)) or "."
BACKUP_RETENTION_DAYS = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

questions_logger = logging.getLogger("questions")
questions_handler = logging.FileHandler("questions.log", encoding="utf-8")
questions_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
questions_logger.addHandler(questions_handler)
questions_logger.setLevel(logging.INFO)


# ── База данных ──────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Безопасно вызывать при каждом запуске."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY,
            expiry TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            chat_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, usage_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS used_txids (
            txid TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            used_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sources (
            chat_id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            first_seen TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"База данных инициализирована: {DB_PATH}")


# ── Бэкап базы данных ──────────────────────────────────────────────

def create_db_backup() -> str:
    """Консистентная копия БД через sqlite3 Connection.backup (не читает живой файл напрямую)."""
    backup_path = os.path.join(BACKUP_DIR, f"backup-{date.today().isoformat()}.db")
    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(backup_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return backup_path


def cleanup_old_backups(retention_days: int = BACKUP_RETENTION_DAYS) -> None:
    cutoff = datetime.now() - timedelta(days=retention_days)
    for name in os.listdir(BACKUP_DIR):
        if not (name.startswith("backup-") and name.endswith(".db")):
            continue
        path = os.path.join(BACKUP_DIR, name)
        if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
            os.remove(path)


async def send_db_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Создаёт бэкап БД и отправляет админу документом. Общая логика для job и /backup."""
    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID не задан в переменных окружения - бэкап пропущен")
        return

    try:
        backup_path = create_db_backup()
        file_size_kb = os.path.getsize(backup_path) / 1024

        conn = get_db()
        user_count = conn.execute("SELECT COUNT(*) FROM user_sources").fetchone()[0]
        conn.close()

        caption = (
            f"🗄 Бэкап базы данных\n\n"
            f"Дата: {date.today().strftime('%d.%m.%Y')}\n"
            f"Размер: {file_size_kb:.1f} КБ\n"
            f"Пользователей: {user_count}"
        )

        with open(backup_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=f,
                filename=os.path.basename(backup_path),
                caption=caption,
            )

        cleanup_old_backups()
    except Exception:
        logger.exception("Ошибка при создании/отправке бэкапа базы данных")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text="⚠️ Не удалось создать/отправить бэкап базы данных. Подробности в логах бота.",
            )
        except Exception:
            logger.exception("Не удалось уведомить админа об ошибке бэкапа")


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_db_backup(context)


# ── Системный промпт ──────────────────────────────────────────────
SYSTEM_PROMPT = """Ты - помощник по налогам и отчётности для малого бизнеса и ИП на УСН в России. Твоя основная специализация - изменения по НДС и УСН, вступившие в силу с 1 января 2026 года, но ты также можешь помочь разобраться в смежных вопросах: страховые взносы, патентная система, кассы и чеки, отчётность в целом. Ты НЕ заменяешь бухгалтера.

ТОН: простой, дружелюбный язык, без канцелярита, короткие абзацы, конкретные цифры.

СТИЛЬ ПУНКТУАЦИИ: никогда не используй длинное тире (—) нигде в тексте. Многих людей оно настораживает, так как ассоциируется с текстом, сгенерированным ИИ. Вместо тире используй обычный дефис (-), запятую, точку или просто перестраивай фразу.

ФОРМАТИРОВАНИЕ: ты отвечаешь в Telegram, где НЕ поддерживаются заголовки (## и ###). Вместо заголовков используй **жирный текст** или эмодзи в начале смыслового блока (например 📌, ⚠️, 💡). Никогда не используй символы # в начале строки.

ОБЛАСТЬ ОТВЕТОВ: отвечай на вопросы про налоги, отчётность, взносы и финансовую сторону ведения бизнеса в России - это широкая, но конкретная область. Если вопрос совсем не связан с бизнесом/налогами (например, личная жизнь, развлечения, другие страны) - вежливо скажи, что специализируешься на налогах и финансах для российского бизнеса, и предложи задать вопрос по этой теме.

ГЛУБИНА ЗНАНИЙ: по НДС и УСН 2026 года у тебя есть точная актуальная база ниже - используй именно её. По смежным темам (патент, взносы, кассы) отвечай на основе общих знаний, но будь особенно осторожен и честно предупреждай, если не уверен в актуальности конкретной цифры - предлагай сверить с ФНС или бухгалтером.

АКТУАЛЬНЫЕ ДАННЫЕ ПО НАЛОГАМ РФ НА 2026 ГОД.
Отвечай на основе этих данных. Если вопрос выходит за их пределы,
честно скажи, что не уверен, и посоветуй проверить в налоговой или
у специалиста. Не выдумывай цифры, ставки и сроки, которых нет ниже.

НДС НА УСН (главная реформа 2026):
- Лимит освобождения от НДС на УСН: 20 млн ₽ дохода в год (снижен
  с 60 млн, закон 425-ФЗ от 28.11.2025). Считается по доходу
  (выручке), не по прибыли.
- Если доход за 2025 год превысил 20 млн: НДС с 1 января 2026.
- Если доход 2026 года нарастающим итогом превышает 20 млн: НДС
  с 1 числа месяца, следующего за месяцем превышения.
- Спецставка 5% без вычетов: применяется, если доход за 2025 год
  был от 20 до 250 млн рублей. В течение 2026 года право на неё
  сохраняется, пока доход с начала года не превысил 272,5 млн
  (250 млн умножить на коэффициент-дефлятор 1,090). При превышении
  272,5 млн переход на ставку 7% с 1 числа следующего месяца.
- Спецставка 7% без вычетов: доход за 2025 год от 250 до 450 млн,
  либо доход 2026 года превысил 272,5 млн. Верхняя граница
  490,5 млн (450 млн умножить на 1,090).
- Право на УСН утрачивается при доходе свыше 490,5 млн.
- Коэффициент-дефлятор на 2026 год: 1,090 (приказ Минэкономразвития
  № 734 от 06.11.2025). На лимит освобождения от НДС в 20 млн
  дефлятор НЕ влияет, он остаётся 20 млн.
- Базовая ставка НДС с 2026 года: 22% (была 20%).
- Выбор спецставки фиксируется на 12 кварталов подряд (3 года).
- На спецставках 5% и 7% входной НДС к вычету НЕ принимается.
- Декларация НДС: ежеквартально, только электронно через оператора
  ЭДО. Бумажная декларация считается непредставленной.
- Срок декларации за 2 квартал 2026: 27 июля (перенос с субботы
  25 июля). Уплата: по 1/3 до 28.07, 28.08, 28.09.
- Поблажка 2026 года: за опоздание с самой первой декларацией НДС
  не штрафуют. Действует один раз, на последующие декларации
  не распространяется.
- Штраф за просрочку декларации: 5% от неуплаченного налога за
  каждый месяц, минимум 1 000 ₽, максимум 30% (ст. 119 НК). За
  несдачу возможна блокировка счетов.
- Планировавшееся дальнейшее снижение лимита (15, затем 10 млн)
  поставлено на паузу, порог 20 млн сохраняется на ближайшие годы.

ВЗНОСЫ ИП 2026:
- Фиксированные взносы: 57 390 ₽ за год. Обязательны, даже если
  деятельности не было. Срок: до 28 декабря 2026.
- Дополнительно 1% с дохода свыше 300 тыс. ₽. Срок: до 1 июля 2027.
- ИП без сотрудников на УСН «доходы» уменьшает налог на всю сумму
  взносов, в том числе до их фактической уплаты (правило с 2025).
- Новое с 2026 для УСН «доходы минус расходы»: базу для расчёта 1%
  нельзя уменьшать на сами фиксированные взносы.

САМОЗАНЯТОСТЬ (НПД):
- Режим действует минимум до конца 2028 года.
- Ставки: 4% с оплат от физлиц, 6% от юрлиц и ИП.
- Лимит: 2,4 млн ₽ в год. При превышении статус слетает в день
  превышения, суммы сверх лимита облагаются НДФЛ 13%, вернуться
  на НПД можно с 1 января следующего года.
- Взносы добровольные, отчётности нет, сотрудников нанимать нельзя.
- Запрещена перепродажа чужих товаров и торговля маркируемыми
  товарами («Честный знак»); список маркировки в 2026 расширен,
  с марта включает кондитерские изделия.
- Новое с 2026: добровольные больничные. Взнос от 1 344 до 1 920 ₽
  в месяц, право на выплаты через 6 месяцев регулярных платежей.

РИСКИ И ШТРАФЫ:
- Дробление бизнеса (второе ИП на родственника и т.п.): при
  доначислении выручку объединяют, налоги пересчитывают, штраф 40%
  за умысел плюс пени (ст. 122 НК).
- Подмена трудовых отношений самозанятыми: при переквалификации
  НДФЛ 13% и взносы около 30% за весь период, плюс штрафы. Признаки:
  один заказчик, график, оборудование заказчика, равные ежемесячные
  выплаты.

МАРКЕТПЛЕЙСЫ:
- С 1 октября 2026 маркетплейсы (Wildberries, Ozon и другие)
  передают данные о продажах селлеров в налоговую.

ПРАВИЛА ОТВЕТОВ:
- Всегда уточняй режим (УСН доходы / УСН доходы минус расходы /
  НПД / патент / ОСНО), оборот и род деятельности, если пользователь
  их не назвал: без этого точный ответ невозможен.
- Суммы и сроки называй только из данных выше. Региональные ставки
  и льготы отличаются: советуй проверить свой регион.
- По сложным случаям (споры с налоговой, переквалификация,
  доначисления) рекомендуй живого специалиста.

ОБЯЗАТЕЛЬНО в конце каждого содержательного ответа добавляй:
"⚠️ Это общая информация, не официальная консультация. Для вашей точной ситуации сверьтесь с бухгалтером или напрямую с ФНС."

Если вопрос выходит за рамки твоих знаний - честно скажи, что нужен профильный специалист, не выдумывай ответ."""

START_MESSAGE = (
    "Привет! Я помогу разобраться с налогами и отчётностью для малого бизнеса в России.\n\n"
    "Моя специализация - изменения по НДС и УСН с 2026 года, но можешь спрашивать и про смежные "
    "темы: страховые взносы, патент, кассы, отчётность.\n\n"
    "Примеры вопросов:\n\n"
    "• «У меня ИП на УСН, доход 25 млн в год, что мне теперь делать с НДС?»\n"
    "• «Чем отличается ставка 5% от 22% и что мне выгоднее?»\n"
    "• «Я на АвтоУСН, надо ли мне вообще платить НДС?»\n"
    "• «Какие взносы платит ИП за себя в 2026 году?»\n"
    "• «Стоит ли перейти на патент?»\n\n"
    f"У тебя есть {FREE_MESSAGES_PER_MONTH} бесплатных вопросов в месяц.\n\n"
    "📰 Наш канал с разборами налоговых тем: https://t.me/nalogi_bez_paniki\n\n"
    "⚠️ Я не заменяю бухгалтера, а помогаю разобраться в общих правилах.\n\n"
    "Команда /help - подробнее о боте и правилах использования.\n\n"
    "Бот помогает разобраться, но не заменяет консультацию специалиста по сложным случаям."
)

HELP_MESSAGE = (
    "ℹ️ О боте\n\n"
    "Налоговый Компас помогает разобраться в налогах и отчётности для малого бизнеса в России. "
    "Основная специализация - изменения по НДС и УСН с 1 января 2026 года (ставки, пороги, лимиты), "
    "но можно спрашивать и про смежные темы: взносы, патент, кассы.\n\n"
    "⚠️ Важно понимать\n\n"
    "Бот не заменяет бухгалтера и не даёт официальных консультаций. Ответы носят "
    "общий информационный характер. Для решений с финансовыми последствиями "
    "сверяйтесь с бухгалтером или напрямую с ФНС.\n\n"
    "🔒 О данных\n\n"
    "Вопросы, которые ты задаёшь, сохраняются в техническом логе, чтобы дорабатывать "
    "качество ответов бота. Личные данные (кроме имени пользователя Telegram) не запрашиваются "
    "и не передаются третьим лицам.\n\n"
    f"💳 Подписка\n\n"
    f"Бесплатно: {FREE_MESSAGES_PER_MONTH} вопросов в месяц. "
    f"Безлимитная подписка: {STARS_PRICE} ⭐ или ${USDT_PRICE} в USDT за 30 дней. Оформить: /subscribe\n\n"
    "Возврат средств: если подписка не была активирована по ошибке бота - "
    "напиши в этот же чат, разберёмся индивидуально.\n\n"
    "📰 Наш канал\n\n"
    "Разборы налоговых тем простым языком: https://t.me/nalogi_bez_paniki\n\n"
    "Команды:\n"
    "/subscribe - оформить подписку\n"
    "/status - остаток бесплатных вопросов / статус подписки\n"
    "/reset - начать диалог заново\n"
    "/help - это сообщение\n\n"
    "Бот помогает разобраться, но не заменяет консультацию специалиста по сложным случаям."
)

# История переписки для контекста диалога с ИИ - остаётся в памяти (не критично для бизнеса,
# короткоживущие данные, не нужно тащить в БД)
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10

# Анти-спам для попыток проверки платежа - тоже не критично для персистентности
usdt_verify_attempts: dict[int, list] = {}


# ── Функции работы с БД (подписки, лимиты, платежи) ──────────────────

def has_active_subscription(chat_id: int) -> bool:
    conn = get_db()
    row = conn.execute("SELECT expiry FROM subscriptions WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    if row is None:
        return False
    return datetime.fromisoformat(row["expiry"]) > datetime.now()


def get_subscription_expiry(chat_id: int) -> datetime | None:
    conn = get_db()
    row = conn.execute("SELECT expiry FROM subscriptions WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return datetime.fromisoformat(row["expiry"]) if row else None


def grant_subscription(chat_id: int, days: int = 30) -> datetime:
    conn = get_db()
    row = conn.execute("SELECT expiry FROM subscriptions WHERE chat_id = ?", (chat_id,)).fetchone()
    current_expiry = datetime.fromisoformat(row["expiry"]) if row else None
    base = current_expiry if current_expiry and current_expiry > datetime.now() else datetime.now()
    new_expiry = base + timedelta(days=days)

    conn.execute(
        "INSERT INTO subscriptions (chat_id, expiry) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET expiry = excluded.expiry",
        (chat_id, new_expiry.isoformat()),
    )
    conn.commit()
    conn.close()
    return new_expiry


def check_and_increment_limit(chat_id: int) -> bool:
    """True если лимит не исчерпан или есть активная подписка."""
    if has_active_subscription(chat_id):
        return True

    current_month = date.today().strftime("%Y-%m")
    conn = get_db()
    row = conn.execute(
        "SELECT count FROM daily_usage WHERE chat_id = ? AND usage_date = ?",
        (chat_id, current_month),
    ).fetchone()

    current_count = row["count"] if row else 0

    if current_count >= FREE_MESSAGES_PER_MONTH:
        conn.close()
        return False

    conn.execute(
        "INSERT INTO daily_usage (chat_id, usage_date, count) VALUES (?, ?, 1) "
        "ON CONFLICT(chat_id, usage_date) DO UPDATE SET count = count + 1",
        (chat_id, current_month),
    )
    conn.commit()
    conn.close()
    return True


def remaining_free_messages(chat_id: int) -> int:
    current_month = date.today().strftime("%Y-%m")
    conn = get_db()
    row = conn.execute(
        "SELECT count FROM daily_usage WHERE chat_id = ? AND usage_date = ?",
        (chat_id, current_month),
    ).fetchone()
    conn.close()
    used = row["count"] if row else 0
    return max(0, FREE_MESSAGES_PER_MONTH - used)


def is_txid_used(txid: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM used_txids WHERE txid = ?", (txid,)).fetchone()
    conn.close()
    return row is not None


def mark_txid_used(txid: str, chat_id: int) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO used_txids (txid, chat_id, used_at) VALUES (?, ?, ?)",
        (txid, chat_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def check_verify_rate_limit(chat_id: int) -> bool:
    """Анти-спам для попыток проверки платежа - в памяти, не критично для персистентности."""
    now = datetime.now()
    attempts = usdt_verify_attempts.setdefault(chat_id, [])
    attempts[:] = [t for t in attempts if now - t < timedelta(hours=1)]

    if len(attempts) >= MAX_VERIFY_ATTEMPTS_PER_HOUR:
        return False

    attempts.append(now)
    return True


def verify_usdt_transaction(txid: str, request_timestamp: float) -> tuple[bool, str]:
    """Проверяет транзакцию через публичный Tronscan API."""
    if is_txid_used(txid):
        return False, "Этот хэш транзакции уже был использован ранее."

    try:
        resp = requests.get(TRONSCAN_API, params={"hash": txid}, timeout=10)
        data = resp.json()
    except Exception:
        logger.exception("Ошибка запроса к Tronscan API")
        return False, "Не удалось проверить транзакцию (проблема связи с блокчейном). Попробуй ещё раз через минуту."

    if not data or data.get("contractRet") != "SUCCESS":
        return False, "Транзакция не найдена или ещё не подтверждена в блокчейне. Подожди пару минут и попробуй снова."

    tx_timestamp_ms = data.get("timestamp", 0)
    tx_timestamp = tx_timestamp_ms / 1000 if tx_timestamp_ms else 0
    buffer_seconds = 300
    if tx_timestamp and tx_timestamp < (request_timestamp - buffer_seconds):
        return False, (
            "Эта транзакция была совершена до того, как ты запросил оплату. "
            "Пришли хэш именно своего нового перевода."
        )

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

def save_user_source(chat_id: int, source: str) -> None:
    """Сохраняет источник привлечения. Только первый источник (INSERT OR IGNORE) -
    если человек пришёл по одной ссылке, а потом кликнул другую, засчитывается первая."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO user_sources (chat_id, source, first_seen) VALUES (?, ?, ?)",
        (chat_id, source, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Deep-link атрибуция: t.me/бот?start=dzen -> context.args = ["dzen"]
    source = context.args[0][:32] if context.args else "direct"
    save_user_source(chat_id, source)
    user_histories[chat_id] = []
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
        expiry = get_subscription_expiry(chat_id).strftime("%d.%m.%Y")
        await update.message.reply_text(f"У тебя активная подписка до {expiry} 🎉")
    else:
        left = remaining_free_messages(chat_id)
        await update.message.reply_text(
            f"Осталось бесплатных вопросов в этом месяце: {left} из {FREE_MESSAGES_PER_MONTH}.\n"
            "Безлимит: /subscribe"
        )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if has_active_subscription(chat_id):
        expiry = get_subscription_expiry(chat_id).strftime("%d.%m.%Y")
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
        description="Безлимитные вопросы по налогам и отчётности на 30 дней",
        payload=f"subscription_{chat_id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )


async def send_usdt_instructions(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_usdt_txid"] = True
    context.user_data["usdt_requested_at"] = datetime.now().timestamp()

    text = (
        f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
        f"`{USDT_WALLET_ADDRESS}`\n\n"
        "После оплаты просто пришли сюда хэш транзакции (TXID) - "
        "проверю платёж и активирую подписку автоматически, обычно в течение минуты."
    )
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Ошибка отправки сообщения pay_usdt (возможно, проблема с USDT_WALLET_ADDRESS)")
        plain_text = (
            f"Отправь ${USDT_PRICE} в USDT (сеть TRC-20) на адрес:\n\n"
            f"{USDT_WALLET_ADDRESS}\n\n"
            "После оплаты просто пришли сюда хэш транзакции (TXID) - "
            "проверю платёж и активирую подписку автоматически."
        )
        await context.bot.send_message(chat_id=chat_id, text=plain_text)


async def pay_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_stars_invoice(update.effective_chat.id, context)


async def pay_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_usdt_instructions(update.effective_chat.id, context)


async def process_usdt_txid(update: Update, context: ContextTypes.DEFAULT_TYPE, txid: str) -> None:
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or str(chat_id)

    if not check_verify_rate_limit(chat_id):
        await update.message.reply_text(
            "Слишком много попыток проверки за последний час. "
            "Попробуй позже, либо напиши в этот чат - разберёмся вручную."
        )
        return

    await update.message.reply_text("Проверяю транзакцию в блокчейне, подожди немного... ⏳")

    request_timestamp = context.user_data.get("usdt_requested_at", datetime.now().timestamp() - 86400)
    success, message = verify_usdt_transaction(txid.strip(), request_timestamp)

    if success:
        mark_txid_used(txid.strip(), chat_id)
        expiry_dt = grant_subscription(chat_id, days=30)
        expiry = expiry_dt.strftime("%d.%m.%Y")
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
    expiry_dt = grant_subscription(chat_id, days=30)
    expiry = expiry_dt.strftime("%d.%m.%Y")
    await update.message.reply_text(
        f"Оплата прошла успешно! ✅ Подписка активна до {expiry}.\n"
        "Теперь можно задавать сколько угодно вопросов."
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ-команда: статистика по источникам привлечения и подпискам."""
    caller_id = str(update.effective_chat.id)
    if not ADMIN_CHAT_ID or caller_id != ADMIN_CHAT_ID:
        return

    conn = get_db()
    source_rows = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM user_sources GROUP BY source ORDER BY cnt DESC"
    ).fetchall()
    total_users = conn.execute("SELECT COUNT(*) FROM user_sources").fetchone()[0]
    active_subs = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE expiry > ?",
        (datetime.now().isoformat(),),
    ).fetchone()[0]
    # Подписчики по источникам - самая ценная метрика
    paying_by_source = conn.execute(
        """SELECT us.source, COUNT(*) as cnt FROM subscriptions s
           JOIN user_sources us ON us.chat_id = s.chat_id
           WHERE s.expiry > ?
           GROUP BY us.source ORDER BY cnt DESC""",
        (datetime.now().isoformat(),),
    ).fetchall()
    conn.close()

    lines = [f"📊 Статистика бота\n\nВсего пользователей: {total_users}\nАктивных подписок: {active_subs}\n"]

    lines.append("Источники (все пользователи):")
    if source_rows:
        for row in source_rows:
            lines.append(f"  {row['source']}: {row['cnt']}")
    else:
        lines.append("  пока пусто")

    lines.append("\nИсточники (платящие):")
    if paying_by_source:
        for row in paying_by_source:
            lines.append(f"  {row['source']}: {row['cnt']}")
    else:
        lines.append("  пока нет платящих")

    await update.message.reply_text("\n".join(lines))


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Админ-команда: ручной запуск той же логики, что и ежедневный job."""
    caller_id = str(update.effective_chat.id)
    if not ADMIN_CHAT_ID or caller_id != ADMIN_CHAT_ID:
        return
    await send_db_backup(context)


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    expiry_dt = grant_subscription(target_chat_id, days=30)
    expiry = expiry_dt.strftime("%d.%m.%Y")
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

    if TXID_PATTERN.match(text_stripped):
        context.user_data["awaiting_usdt_txid"] = False
        await process_usdt_txid(update, context, text_stripped)
        return

    if context.user_data.get("awaiting_usdt_txid", False):
        context.user_data["awaiting_usdt_txid"] = False

    questions_logger.info(f"user={username} | chat_id={chat_id} | question={user_text}")

    if not check_and_increment_limit(chat_id):
        await update.message.reply_text(
            f"Бесплатный лимит в {FREE_MESSAGES_PER_MONTH} вопросов в этом месяце исчерпан 🙏\n\n"
            "Лимит обновится в следующем месяце, либо оформи безлимитную подписку: /subscribe"
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
    commands = [
        ("start", "Начать работу с ботом"),
        ("subscribe", "Оформить безлимитную подписку"),
        ("status", "Сколько вопросов осталось / статус подписки"),
        ("help", "О боте и правилах использования"),
        ("reset", "Начать диалог заново"),
    ]
    await application.bot.set_my_commands(commands)


def main() -> None:
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("pay_stars", pay_stars))
    app.add_handler(CommandHandler("pay_usdt", pay_usdt))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(subscribe_button_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(backup_job, time=dt_time(hour=3, minute=0, tzinfo=timezone.utc))

    logger.info("Бот запущен, ждём сообщения...")
    app.run_polling()


if __name__ == "__main__":
    main()
