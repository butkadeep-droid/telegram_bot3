"""Анфиса: private-chat MVP. Python 3.10+, aiogram 3, OpenRouter, SQLite."""
import asyncio
import logging
import os
import re
import secrets
import math
import hashlib
from html import escape
import billing
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, FSInputFile, BotCommand, MenuButtonCommands,
                           PreCheckoutQuery, LabeledPrice, InputProfilePhotoStatic)
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv
from openai import AsyncOpenAI, APIError

load_dotenv()

# ===== КЛЮЧИ: ЗАДАЙ ИХ В НАСТРОЙКАХ ХОСТИНГА =====
# Раздел обычно называется Variables / Environment / Secrets / Config Vars.
# TELEGRAM_BOT_TOKEN — сюда на хостинге впиши токен от BotFather.
# OPENROUTER_API_KEY — сюда на хостинге впиши API-ключ OpenRouter.
# В GitHub ключи НЕ загружай. Этот код читает их по именам ниже.
# Для локального запуска можно создать НЕ публикуемый файл .env:
# TELEGRAM_BOT_TOKEN=твой_токен_от_BotFather
# OPENROUTER_API_KEY=твой_ключ_OpenRouter
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
# Отдельный бот от BotFather; ключ хранить только в Railway Variables.
PAYMENT_BOT_TOKEN = os.getenv("PAYMENT_BOT_TOKEN", "").strip()
# ================================================

# Для сохранения истории при перезапусках DB_PATH должен указывать
# на файл в подключённом постоянном диске хостинга.
DB = Path(os.getenv("DB_PATH", "data/gosha.sqlite3"))
MODEL = os.getenv("MODEL", "deepseek/deepseek-chat")
def read_owner_id(environ=None):
    env = os.environ if environ is None else environ
    primary = str(env.get("OWNER_ID", "")).strip()
    legacy = str(env.get("ownerId", "")).strip()
    raw = primary if primary not in ("", "0") else (legacy or "0")
    try:
        owner = int(raw)
    except ValueError:
        raise SystemExit("OWNER_ID должен быть числом из команды /myid, без @ и других символов") from None
    if owner < 0:
        raise SystemExit("OWNER_ID должен быть положительным Telegram ID")
    return owner


OWNER_ID = read_owner_id()
PAYMENTS_ENABLED = os.getenv("ENABLE_PAYMENTS", "false").strip().lower() in {"true", "1", "yes", "on"}
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
COOLDOWN = int(os.getenv("COOLDOWN_SECONDS", "5"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "8"))
if MAX_CONCURRENT < 1 or COOLDOWN < 0 or OWNER_ID < 0:
    raise ValueError("Invalid limits")
HISTORY_MESSAGES = 24
INPUT_CHARS = 2000
OUTPUT_CHARS = 220
BASE_DIR = Path(__file__).resolve().parent
WELCOME_PHOTO = BASE_DIR / "welcome.jpg"
AVATAR_PHOTO = BASE_DIR / "avatar.jpg"
GOSHA_PHOTO = BASE_DIR / "gosha.jpg"

SYSTEM = """Ты — Анфиса, вымышленная дерзкая ИИ-собеседница. По-русски, на «ты». Говори о себе в женском роде.
Не рассказывай анекдоты, истории, притчи и мотивационные лекции. Не начинай
со случайной шутки. Реагируй ПРЯМО на последнюю реплику пользователя.
Одна короткая реплика: 1–2 предложения, 40–180 символов, максимум 220.
Без списков, Markdown, длинных объяснений и фраз «как ИИ».
Манеру и допустимость мата определяет ТОЛЬКО текущий режим ниже.
Старые ответы в истории могли быть в другом режиме: НЕ перенимай их стиль.
В жёстких режимах собеседник добровольно согласился на постановочную
словесную перепалку и персональные оскорбительные подколы. Не отказывайся
только потому, что он просит мат или просит грубо ответить ему самому.
Это не разрешение травить третьих реальных людей, угрожать, раскрывать данные,
унижать по расе, национальности, религии, полу, ориентации или инвалидности.
Не выдумывай факты о внешности и здоровье пользователя. Не создавай
сексуально откровенные сообщения и инструкции для причинения вреда.
При реальном кризисе, насилии, риске самоповреждения или явной просьбе
о поддержке убери мат и оскорбления, поддержи и предложи безопасный шаг;
при непосредственной угрозе — близкого и местную экстренную помощь.
Просьба остановиться важнее выбранного грубого режима. Предложи /soft.
Не утверждай всерьёз, что человек ничего не стоит или заслуживает страданий.
«Без фильтров» — название манеры, не отмена этих границ.
"""
MODES = {
    "soft": """ТЕКУЩИЙ РЕЖИМ: МЯГКО. Ни одного матерного слова и ни одного
личного оскорбления. Спокойный прямой ответ с лёгкой иронией, без панчей ради панчей.
Пример: пользователь «Чего молчишь?» — «Ждал, когда появится вопрос интереснее этого.»""",
    "hard": """ТЕКУЩИЙ РЕЖИМ: ЖЁСТКО. Прямая грубая реакция на пользователя,
а не анекдот. Одно матерное слово БЕЗ звёздочек, короткий личный подкол.
Можно «тупишь», «достал», «ну ты и клоун». Не смягчай всё советами и улыбочками.
Пример: пользователь «Чего молчишь?» — «Бля, ты уже и тишину умудрился достать.»
Пример: пользователь «Ты тупой» — «Сам-то, блядь, вопрос родить можешь или только лаять?»""",
    "raw": """ТЕКУЩИЙ РЕЖИМ: БЕЗ ФИЛЬТРОВ. Самая резкая постановочная
перепалка. Адресуй реплику пользователю напрямую, не развлекай его историями.
Одно-два настоящих матерных слова без цензуры плюс прямой грубый выпад.
Можно шуточно обзывать «тупица», «клоун», отвечать «иди в жопу»; без угроз
и без утверждений, что его жизнь ничего не стоит. Не заканчивай советом.
Пример: пользователь «Чего молчишь?» — «Ты, блядь, зашёл по делу или просто тишину заебать?»
Пример: пользователь «Что делаешь?» — «Пытаюсь понять, нахуя ты опять пришёл без вопроса, клоун.»""",
}
MODE_NAMES = {"soft": "😌 Мягко", "hard": "🔥 Жёстко", "raw": "💀 Без фильтров"}


def db_init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=10)) as con, con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS bot_members (user_id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE IF NOT EXISTS bot_registry_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_assets (
            bot_id INTEGER PRIMARY KEY, avatar_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, agreed INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'hard'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_user ON messages(user_id, id);
        CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL, day TEXT NOT NULL, attempts INTEGER NOT NULL,
            last_request REAL NOT NULL, PRIMARY KEY(user_id, day)
        );
        """)
        # Import only IDs already held by this bot, once. Unknown names stay unknown.
        if not con.execute("SELECT 1 FROM bot_registry_state WHERE key='legacy_import_v1'").fetchone():
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("users", "messages", "usage", "permanent_bonuses", "hour_usage", "pro_entitlements", "pro_orders", "pro_charges"):
                if table in tables:
                    con.execute(f'INSERT OR IGNORE INTO bot_members(user_id) SELECT DISTINCT user_id FROM "{table}" WHERE user_id > 0')
            con.execute("INSERT OR IGNORE INTO bot_registry_state VALUES('legacy_import_v1','done')")


def db_run(action, uid=0, value=None):
    """Short transactions; executed off the event loop, never across API waits."""
    day = datetime.now(timezone.utc).date().isoformat()
    now = time.time()
    with closing(sqlite3.connect(DB, timeout=10)) as con, con:
        if action == "member_seen":
            if uid > 0:
                username = str(value).lstrip("@")[:32] if value else None
                con.execute("INSERT INTO bot_members(user_id,username) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username", (uid, username))
            return
        if action in ("members", "member"):
            if OWNER_ID <= 0 or uid != OWNER_ID:
                raise PermissionError("Owner-only roster")
            if action == "member":
                return con.execute("SELECT user_id,username FROM bot_members WHERE user_id=?", (int(value),)).fetchone()
            total = con.execute("SELECT COUNT(*) FROM bot_members").fetchone()[0]
            pages = max(1, (total + OWNER_PAGE_SIZE - 1) // OWNER_PAGE_SIZE)
            page = min(max(0, int(value or 0)), pages - 1)
            rows = con.execute("SELECT user_id,username FROM bot_members ORDER BY user_id LIMIT ? OFFSET ?", (OWNER_PAGE_SIZE, page * OWNER_PAGE_SIZE)).fetchall()
            return {"rows": rows, "total": total, "page": page, "pages": pages}
        if action == "asset_get":
            row = con.execute("SELECT avatar_hash FROM bot_assets WHERE bot_id=?", (uid,)).fetchone()
            return row[0] if row else None
        if action == "asset_save":
            con.execute("INSERT INTO bot_assets VALUES(?,?) ON CONFLICT(bot_id) DO UPDATE SET avatar_hash=excluded.avatar_hash", (uid, value))
            return
        if action == "cleanup":
            con.execute("DELETE FROM messages WHERE created < ?", (now - 30 * 86400,))
            con.execute("DELETE FROM usage WHERE day < ?", (day,))
            return
        if action == "get":
            return con.execute("SELECT agreed, mode FROM users WHERE user_id=?", (uid,)).fetchone()
        if action == "agree":
            con.execute("INSERT INTO users(user_id,agreed) VALUES(?,1) "
                        "ON CONFLICT(user_id) DO UPDATE SET agreed=1", (uid,))
        elif action == "mode":
            con.execute("UPDATE users SET mode=? WHERE user_id=?", (value, uid))
        elif action == "reset":
            con.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        elif action == "delete":
            con.execute("DELETE FROM messages WHERE user_id=?", (uid,))
            con.execute("DELETE FROM users WHERE user_id=?", (uid,))
            con.execute("DELETE FROM bot_members WHERE user_id=?", (uid,))
            # Keep today's abuse/budget counter until next UTC day.
        elif action == "history":
            rows = con.execute("SELECT role,content FROM messages WHERE user_id=? "
                               "ORDER BY id DESC LIMIT ?", (uid, HISTORY_MESSAGES)).fetchall()
            return [{"role": r, "content": c} for r, c in reversed(rows)]
        elif action == "save":
            question, answer = value
            con.executemany("INSERT INTO messages(user_id,role,content,created) VALUES(?,?,?,?)",
                            [(uid, "user", question, now), (uid, "assistant", answer, now)])
            con.execute("DELETE FROM messages WHERE user_id=? AND id NOT IN "
                        "(SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?)",
                        (uid, uid, HISTORY_MESSAGES))


async def store(action, uid=0, value=None):
    return await asyncio.to_thread(db_run, action, uid, value)


MAIN_BOT = None
PAYMENT_BOT = None
MAIN_USERNAME = ""
PAYMENT_USERNAME = ""
PAYMENT_SETUP_REASON = "not_started"


class BotRoleFilter(BaseFilter):
    def __init__(self, payment=False):
        self.payment = payment

    async def __call__(self, event, bot: Bot):
        expected = PAYMENT_BOT if self.payment else MAIN_BOT
        return expected is not None and bot.id == expected.id


router = Router(name="gosha-main")
router.message.filter(F.chat.type == ChatType.PRIVATE, BotRoleFilter())
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE, BotRoleFilter())
router.pre_checkout_query.filter(BotRoleFilter())
pay_router = Router(name="gosha-payments")
pay_router.message.filter(F.chat.type == ChatType.PRIVATE, BotRoleFilter(payment=True))
pay_router.pre_checkout_query.filter(BotRoleFilter(payment=True))
client = None
in_flight = 0
ERROR_MESSAGE = "Сервера сдохли"
PRO_PHOTO = BASE_DIR / "pro.jpg"


class UserLockMiddleware(BaseMiddleware):
    """Chat actions are serialized, but checkout and payment updates never wait for AI."""
    def __init__(self):
        self.entries = {}

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            try:
                await event.answer()
            except TelegramAPIError:
                logging.warning("Callback acknowledgement failed")
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        entry = self.entries.setdefault(user.id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                if not getattr(user, "is_bot", False):
                    await store("member_seen", user.id, getattr(user, "username", None))
                return await handler(event, data)
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self.entries.pop(user.id, None)


user_lock = UserLockMiddleware()
router.message.outer_middleware(user_lock)
router.callback_query.outer_middleware(user_lock)


async def bill(action, uid=0, value=None):
    return await asyncio.to_thread(billing.run, DB, action, uid, value)


def payment_checks():
    within_volume = False
    if VOLUME_PATH:
        try:
            # Relative paths are safe too, provided their resolved file is INSIDE the volume.
            within_volume = DB.resolve().is_relative_to(Path(VOLUME_PATH).resolve())
        except (OSError, RuntimeError):
            within_volume = False
    return [
        ("ENABLE_PAYMENTS", PAYMENTS_ENABLED, "Поставь true в Variables."),
        ("OWNER_ID", OWNER_ID > 0, "Укажи положительный Telegram ID из /myid."),
        ("RAILWAY_VOLUME_MOUNT_PATH", bool(VOLUME_PATH), "Подключи Volume к сервису; Railway задаст эту переменную сам."),
        ("DB_PATH внутри Volume", within_volume, "Файл базы должен находиться в подключённом Volume, например /data/gosha.sqlite3 при mount path /data."),
        ("PAYMENT_BOT_TOKEN", bool(PAYMENT_BOT_TOKEN), "Нужен токен НОВОГО бота от BotFather, не токен основного."),
        ("Второй бот подтверждён Telegram", PAYMENT_BOT is not None and bool(PAYMENT_USERNAME), payment_reason()),
        ("Основной бот подтверждён Telegram", MAIN_BOT is not None and bool(MAIN_USERNAME), "Нужен успешный запуск основного бота."),
    ]


def payment_reason():
    return {
        "not_started": "Проверка второго бота ещё не выполнялась. Задеплой обновление.",
        "missing_token": "Не задан PAYMENT_BOT_TOKEN.",
        "invalid_format": "Токен второго бота имеет неверный формат. Вставь значение из BotFather без кавычек.",
        "same_bot": "В PAYMENT_BOT_TOKEN указан тот же бот. Создай другого через BotFather.",
        "unauthorized": "Telegram отклонил токен второго бота. Проверь актуальное значение PAYMENT_BOT_TOKEN.",
        "network_error": "Telegram не ответил при запуске. Сделай новый Deploy; основной бот продолжает работать.",
        "telegram_error": "Telegram вернул ошибку при проверке второго бота. Проверь токен и перезапусти Deploy.",
        "missing_username": "У платёжного бота не получен username. Проверь его в BotFather.",
        "ready": "Платёжный бот проверен.",
    }.get(PAYMENT_SETUP_REASON, "Перезапусти Deploy и проверь настройки второго бота.")


def payments_ready():
    return all(bool(ok) for _, ok, _ in payment_checks())


def payment_status_text():
    lines = ["<b>⚙️ Диагностика оплаты</b>", ""]
    for name, ok, hint in payment_checks():
        lines.append(("✅ " if ok else "❌ ") + name)
        if not ok:
            lines.append("   " + hint)
    lines += ["", "После исправления Variables запусти новый Deploy. "
              "Токены и их значения здесь не показываются. "
              "Не создавай RAILWAY_VOLUME_MOUNT_PATH вручную и не удаляй базу."]
    return "\n".join(lines)


async def show_payment_status(message, uid):
    if OWNER_ID <= 0 or uid != OWNER_ID:
        await message.answer("Диагностика доступна только Owner. Если это ты, проверь свой ID через /myid и значение OWNER_ID в Railway.")
        return
    await message.answer(payment_status_text(), parse_mode="HTML", reply_markup=back_keyboard())


def role_name(uid, state):
    if OWNER_ID > 0 and uid == OWNER_ID:
        return "Owner"
    return "Pro" if state.get('pro') else "Бесплатная"


def role_line(uid, state):
    role = role_name(uid, state)
    icon = {"Owner": "👑", "Pro": "⭐", "Бесплатная": "🆓"}[role]
    return f"{icon} Роль: <b>{role}</b>"


def utc_date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def consent_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Мне 16+ · принимаю дерзкий стиль", "ui:agree")],
        [button("🔒 О данных", "ui:privacy"), button("📜 Условия", "ui:terms")],
    ])


def menu_keyboard(mode="hard", pro=False, uid=None):
    rows = [
        [button(("✓ " if key == mode else "") + label, "mode:" + key) for key, label in MODE_NAMES.items()],
        [button("⭐ Мой Pro · продлить" if pro else f"⭐ Pro · 7 дней за {billing.PRICE} ⭐", "ui:pro")],
    ]
    if OWNER_ID > 0 and uid == OWNER_ID:
        rows.append([button("🛠 Меню бота", "admin:users:0")])
    rows.append([button("❔ Как общаться", "ui:help"), button("🔒 О данных", "ui:privacy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Главное меню", "ui:menu")]])


WELCOME = (
    "<b>АНФИСА · коротко и в лицо</b>\n\n"
    "😌 Мягко — без мата и личных оскорблений.\n"
    "🔥 Жёстко — грубые подколы прямо тебе.\n"
    "💀 Без фильтров — самая резкая словесная перепалка.\n\n"
    "<b>16+</b> · Это ИИ-персонаж. Продолжая, соглашаешься на выбранный "
    "стиль и обработку сообщений: текст передаётся OpenRouter и провайдеру.\n"
    "Не хочешь ругани — сразу выбирай «Мягко»."
)
PRIVACY = (
    "<b>🔒 О данных</b>\n\n"
    "Текущий текст и до 24 прошлых сообщений передаются OpenRouter и провайдеру. "
    "Не отправляй пароли и чужие личные данные.\n\n"
    "Локально: Telegram ID, согласие, режим и последние 24 сообщения. "
    "Тексты старше 30 дней очищаются ежечасно.\n"
    "Владелец видит список пользователей: Telegram ID и доступный юзернейм. "
    "Юзернейм обновляется при обращении к основному боту; переписка в эту таблицу не входит. "
    "/delete также удаляет запись из списка до следующего обращения. "
    "Для квоты сохраняются отметки бесплатных запросов за последний час. "
    "Для постоянного бонуса — Telegram ID и дата получения; это не история переписки. "
    "Для Pro — заказы, подтверждения платежей, сроки доступа и возвраты. "
    "Также сохраняются ID получателя и владельца, дата и срок подарочного Pro. "
    "Финансовый журнал и записи выданных подарков не удаляются автоматически командой /delete.\n\n"
    "/reset — стереть переписку. /delete — также настройки и согласие. "
    "Активный оплаченный доступ, записи платежей, постоянный бонус к квоте и временные антиспам-счётчики сохраняются. "
    "Старый дневной счётчик предыдущей версии очищается после смены дня UTC.\n\n"
    "Это не удаляет копии у Telegram и провайдеров. Самоподтверждение 16+ не заменяет "
    "проверку документов. По данным и платежам: /support текст или /paysupport текст."
)
TERMS = (
    "<b>📜 Условия Pro</b>\n\n"
    f"⭐ Цена: <b>{billing.PRICE} ⭐</b> (Telegram Stars).\n"
    "🗓 Срок: <b>7 × 24 часа</b> после подтверждения оплаты. "
    "Повторная покупка добавляет 7 дней к действующему сроку.\n"
    f"∞ На этот срок снимается бесплатная квота {billing.FREE_LIMIT} запросов/час (с постоянным бонусом — {billing.FREE_LIMIT + billing.GOSHA_BONUS}). "
    f"Пауза {COOLDOWN} с, ограничения длины и защита от перегрузки остаются.\n"
    "🔁 Разовый платёж, <b>без автопродления</b>.\n\n"
    "Оплачивается доступ к ИИ-боту, а не гарантированная формулировка каждого ответа. "
    "Сервисы Telegram и модели могут быть временно недоступны. "
    "Угрозы, дискриминация, травля третьих лиц и опасные инструкции не входят в услугу.\n\n"
    "Помощь и запрос возврата: /paysupport и описание проблемы. "
    "Покупку обслуживает владелец этого бота, не поддержка Telegram. "
    "При возврате оплаченный срок пересчитывается. Возвраты и обязательные права "
    "покупателя определяются применимым законодательством.\n\n"
    "Кнопка «Оплатить» в основном боте подтверждает принятие этих условий. Счёт выставляет отдельный платёжный бот этого же сервиса."
)
HELP = (
    "<b>❔ Как общаться</b>\n\n"
    "Просто пиши. Анфиса отвечает на твою реплику, не рассказывает анекдоты.\n"
    "😌 Мягко — спокойно и без мата.\n🔥 Жёстко — ругань и личные подколы.\n"
    "💀 Без фильтров — самая грубая добровольная перепалка. "
    "При реальном кризисе — поддержка, не добивание.\n\n"
    f"🆓 Бесплатно: {billing.FREE_LIMIT} успешных ответов за скользящие 60 минут; с постоянным бонусом — {billing.FREE_LIMIT + billing.GOSHA_BONUS}. "
    "Освободившийся запрос возвращается через час после его использования.\n"
    f"⭐ Pro: {billing.PRICE} ⭐ → 7 дней без почасовой квоты. Оплата — в отдельном боте.\n👑 Owner — без квоты и паузы между запросами.\n\n"
    "/menu — меню · /pro — доступ · /role — роль\n/soft · /hard · /raw — режимы\n"
    "/reset — забыть диалог · /delete — удалить данные\n"
    "/terms — условия · /privacy — данные\n/paysupport текст — помощь с оплатой\n"
    f"Ответы — до {OUTPUT_CHARS} символов. Пауза между запросами: {COOLDOWN} с."
)


def compact_reply(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace("**", "").replace("__", "").strip('"«» ')
    text = " ".join(re.split(r"(?<=[.!?…])\s+(?=[А-ЯЁA-Z0-9])", text)[:2])
    if len(text) <= OUTPUT_CHARS:
        return text
    short = text[:OUTPUT_CHARS - 1]
    if " " in short:
        short = short.rsplit(" ", 1)[0]
    return short.rstrip(" ,;:-.!?…") + "…"


async def consent(message, uid):
    row = await store("get", uid)
    if not row or not row[0]:
        await message.answer("Сначала подтверди 16+ и выбранный дерзкий стиль 👇", reply_markup=consent_keyboard())
        return None
    return row


async def show_menu(message, uid, edit=False):
    row = await store("get", uid)
    agreed = bool(row and row[0])
    state = await bill("status", uid)
    mode = row[1] if agreed and row[1] in MODES else "hard"
    if agreed:
        if OWNER_ID > 0 and uid == OWNER_ID:
            quota = "∞ <b>Owner: без квоты и паузы между запросами</b>"
        else:
            quota = f"⭐ <b>Pro до {utc_date(state['until'])}</b>" if state['pro'] else f"🆓 <b>{state['remaining']}/{state['limit']}</b> запросов за последний час"
        bonus_line = f"\n🎁 Постоянный бонус: +{state['bonus']} к бесплатному лимиту" if state['bonus'] else ""
        caption = (
            "<b>АНФИСА · коротко и в лицо</b>\n\n"
            f"{role_line(uid, state)}\n🎛 Твой режим: <b>{MODE_NAMES[mode]}</b>\n{quota}{bonus_line}\n\n"
            "Пиши прямо в чат. Меньше церемоний — больше дела."
        )
        kb = menu_keyboard(mode, state['pro'], uid)
    else:
        caption, kb = WELCOME + "\n\n" + role_line(uid, state), consent_keyboard()
    if edit:
        try:
            if message.photo:
                await message.edit_caption(caption=caption, parse_mode="HTML", reply_markup=kb)
            else:
                await message.edit_text(caption, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramBadRequest:
            logging.warning("Menu edit not applied; sending fresh menu")
    if WELCOME_PHOTO.is_file():
        await message.answer_photo(FSInputFile(WELCOME_PHOTO), caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=kb)


def main_bot_keyboard(pro=False):
    if not MAIN_USERNAME:
        return back_keyboard()
    link = f"https://t.me/{MAIN_USERNAME}?start={'pro' if pro else 'paid'}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← Открыть Pro в основном боте" if pro else "← Вернуться к Анфисе", url=link)
    ]])


async def show_pro(message, uid):
    if not await consent(message, uid):
        return
    state = await bill("status", uid)
    current = f"\n✅ <b>Активен до {utc_date(state['until'])}</b>\n" if state['pro'] else ""
    if OWNER_ID > 0 and uid == OWNER_ID:
        current = "\n👑 <b>У Owner уже нет квоты и паузы. Покупка для себя не требуется.</b>\n"
    text = (
        "<b>⭐ АНФИСА PRO</b>\n" + role_line(uid, state) + "\n\n"
        f"∞ Без почасовой квоты\n🗓 7 дней доступа\n💫 Всего {billing.PRICE} ⭐ (Telegram Stars)\n"
        "🔁 Без автосписаний\n" + current +
        f"\nПауза {COOLDOWN} с и защита от перегрузки остаются для обычного Pro. "
        "Повторная покупка продлевает срок на 7 дней.\n\n"
        "Прочитай условия. Нажимая «Оплатить», принимаешь их. "
        "Оплата откроется в отдельном боте: нажми там «Старт» — появится счёт Telegram."
    )
    rows = []
    if payments_ready():
        payload = "gl_" + secrets.token_urlsafe(24)
        await bill("create_order", uid, {'payload': payload, 'merchant_id': PAYMENT_BOT.id, 'kind': 'link'})
        rows.append([InlineKeyboardButton(text=f"Оплатить ⭐ {billing.PRICE}", url=f"https://t.me/{PAYMENT_USERNAME}?start={payload}")])
    else:
        if OWNER_ID > 0 and uid == OWNER_ID:
            text += "\n\n⚙️ Есть ошибка настройки оплаты. Нажми кнопку диагностики ниже."
            rows.append([button("⚙️ Почему недоступна оплата?", "ui:paymentstatus")])
        else:
            text += "\n\n⚙️ Покупка пока недоступна. Владелец настраивает оплату."
    rows += [[button("📜 Прочитать условия", "ui:terms"), button("🛟 Помощь", "ui:support")],
             [button("← Главное меню", "ui:menu")]]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if PRO_PHOTO.is_file():
        await message.answer_photo(FSInputFile(PRO_PHOTO), caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def notify_owner(bot_instance, text):
    if OWNER_ID > 0:
        try:
            await bot_instance.send_message(OWNER_ID, text, parse_mode=None)
        except TelegramAPIError:
            logging.error("Could not notify payment owner")


@router.pre_checkout_query()
async def checkout(query: PreCheckoutQuery):
    # No chat lock/AI await on this path: Telegram requires an answer within 10 sec.
    try:
        row = await store("get", query.from_user.id)
        ok = bool(payments_ready() and row and row[0] and await bill(
            "precheck", query.from_user.id,
            (query.invoice_payload, query.total_amount, query.currency,
             query.bot.id, MAIN_BOT.id if MAIN_BOT else 0)))
    except Exception:
        logging.error("Checkout validation failed")
        ok = False
    await query.answer(ok=ok, error_message=None if ok else "Счёт недоступен. Вернись в основной бот → Pro и нажми «Оплатить» заново, либо обратись в поддержку.")


@router.message(F.successful_payment)
async def payment_received(message: Message):
    payment = message.successful_payment
    result = await bill("payment", message.from_user.id,
                        (payment.telegram_payment_charge_id, payment.invoice_payload,
                         payment.total_amount, payment.currency,
                         message.bot.id, MAIN_BOT.id if MAIN_BOT else 0))
    if result['state'] == 'duplicate':
        return
    if result['state'] == 'granted':
        await message.answer(f"⭐ Pro включён до {utc_date(result['until'])}.\nПочасовая квота снята. Пиши.", reply_markup=main_bot_keyboard())
        await notify_owner(message.bot, f"Оплата Pro: {payment.total_amount} Stars. Пользователь {message.from_user.id}.\nCharge: {payment.telegram_payment_charge_id}")
    elif result['state'] == 'orphan':
        await message.answer("Оплата получена, но не сопоставлена с заказом. Напиши /paysupport — владелец проверит платёж или оформит возврат.")
        await notify_owner(message.bot, f"Нужна проверка оплаты! User {message.from_user.id}; charge {payment.telegram_payment_charge_id}")
    else:
        await message.answer("Этот платёж уже возвращён. Детали: /paysupport.")


@router.message(F.refunded_payment)
async def payment_refunded(message: Message):
    refund = message.refunded_payment
    if refund.currency != 'XTR':
        return
    charge = await bill("charge", value=refund.telegram_payment_charge_id)
    payer = charge['user_id'] if charge else message.from_user.id
    until = await bill("refund", payer, (refund.telegram_payment_charge_id, message.bot.id,
                                        MAIN_BOT.id if MAIN_BOT else 0))
    state = await bill("status", payer)
    suffix = f"Pro остаётся до {utc_date(until)}." if until > time.time() else f"Доступны бесплатные {state['limit']} запросов/час."
    await message.answer("Возврат учтён. " + suffix)


@router.message(CommandStart())
@router.message(Command("menu", "mode"))
async def start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1] if len(parts) == 2 else ""
    if arg == "pro":
        await show_pro(message, message.from_user.id)
    elif arg == "terms":
        await message.answer(TERMS, parse_mode="HTML", reply_markup=back_keyboard())
    else:
        await show_menu(message, message.from_user.id)


@router.message(Command("agree"))
async def agree(message: Message):
    await store("agree", message.from_user.id)
    await show_menu(message, message.from_user.id)


@router.message(Command("pro"))
async def pro_handler(message: Message):
    await show_pro(message, message.from_user.id)


@router.message(Command("privacy", "terms", "help"))
async def info_handler(message: Message):
    cmd = message.text.split()[0].split("@")[0][1:]
    text = {"privacy": PRIVACY, "terms": TERMS, "help": HELP}[cmd]
    await message.answer(text, parse_mode="HTML", reply_markup=back_keyboard())


@router.message(Command("soft", "hard", "raw"))
async def mode_handler(message: Message):
    uid = message.from_user.id
    if not await consent(message, uid):
        return
    mode = message.text.split()[0].split("@")[0][1:]
    await store("mode", uid, mode)
    await message.answer(f"{MODE_NAMES[mode]} — включён.", reply_markup=menu_keyboard(mode, (await bill('status', uid))['pro'], uid))


@router.message(Command("reset", "delete"))
async def clear_handler(message: Message):
    action = message.text.split()[0].split("@")[0][1:]
    await store(action, message.from_user.id)
    text = "🧹 Переписка забыта. Режим и лимит не изменились."
    if action == "delete":
        text = "Переписка, настройки, согласие и запись в списке пользователей удалены. Оплаченный срок, платёжный журнал, постоянный бонус и временные счётчики сохраняются. Подробнее: /privacy."
    await message.answer(text, reply_markup=back_keyboard())


@router.message(Command("role"))
async def role_handler(message: Message):
    state = await bill("status", message.from_user.id)
    await message.answer(role_line(message.from_user.id, state), parse_mode="HTML", reply_markup=back_keyboard())


@router.message(Command("paymentstatus"))
async def payment_status_handler(message: Message):
    await show_payment_status(message, message.from_user.id)


@router.message(Command("myid"))
async def myid_handler(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@router.message(Command("paysupport", "support"))
async def support_handler(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        await message.answer("Напиши /paysupport и опиши проблему в том же сообщении. Её получит владелец бота. Не отправляй пароли и ключи. Покупку обслуживает владелец, не поддержка Telegram.")
        return
    if OWNER_ID <= 0:
        await message.answer("Владелец ещё не настроил поддержку. Новые оплаты отключены.")
        return
    try:
        await message.bot.send_message(OWNER_ID, f"Обращение от {message.from_user.id}:\n{parts[1][:2600]}\n\nОтвет: /reply {message.from_user.id} ваш текст", parse_mode=None)
    except TelegramAPIError:
        await message.answer(ERROR_MESSAGE)
        return
    await message.answer("🛟 Отправлено владельцу. Ответ придёт сюда.")


@router.message(Command("reply", "stars", "payments", "refund"))
async def owner_handler(message: Message):
    if not OWNER_ID or message.from_user.id != OWNER_ID:
        await message.answer("Эта команда только для владельца.")
        return
    parts = message.text.split(maxsplit=2)
    command = parts[0].split("@")[0][1:]
    if command == "reply":
        if len(parts) != 3 or not parts[1].isdigit():
            await message.answer("/reply TELEGRAM_ID текст ответа")
            return
        await message.bot.send_message(int(parts[1]), "🛟 Ответ поддержки:\n" + parts[2][:3000], parse_mode=None)
        await message.answer("Отправлено.")
    elif command == "stars":
        merchants = [("Основной бот", MAIN_BOT), ("Платёжный бот", PAYMENT_BOT)]
        if not any(merchant for _, merchant in merchants):
            merchants = [("Этот бот", message.bot)]
        lines = []
        for label, merchant in merchants:
            if merchant is not None:
                balance = await merchant.get_my_star_balance()
                lines.append(f"{label}: {balance.amount} ⭐")
        await message.answer("\n".join(lines))
    elif command == "payments":
        rows = await bill("payments")
        lines = [f"{r['user_id']} · {r['amount']} {r['currency']} · {r['state']} · бот {r.get('merchant_id', 0)}\n{r['charge_id']}" for r in rows]
        await message.answer("Последние платежи:\n\n" + ("\n\n".join(lines) or "Пока нет."), parse_mode=None)
    elif command == "refund":
        if len(parts) < 2:
            await message.answer("/refund CHARGE_ID — полный возврат суммы конкретного платежа. Для несопоставленного платежа: /refund CHARGE_ID TELEGRAM_ID")
            return
        charge = await bill("charge", value=parts[1])
        uid = charge['user_id'] if charge else (int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None)
        if uid is None:
            await message.answer("Нет локальной записи. Укажи ID плательщика третьим параметром после проверки транзакции в Telegram.")
            return
        if charge and charge['state'] == 'refunded':
            await message.answer("Этот платёж уже возвращён.")
            return
        # The explicit owner command authorizes this financial action.
        merchant_id = charge.get('merchant_id', 0) if charge else message.bot.id
        if charge and merchant_id == 0:
            merchant = MAIN_BOT or message.bot
        else:
            merchant = next((b for b in (MAIN_BOT, PAYMENT_BOT, message.bot)
                             if b is not None and b.id == merchant_id), None)
        if merchant is None:
            await message.answer("Не найден бот, который получил этот платёж. Проверь конфигурацию токенов.")
            return
        refunded = await merchant.refund_star_payment(user_id=uid, telegram_payment_charge_id=parts[1])
        if not refunded:
            await message.answer(ERROR_MESSAGE)
            return
        await bill("refund", uid, (parts[1], merchant.id, MAIN_BOT.id if MAIN_BOT else 0))
        await message.answer("Возврат выполнен, срок Pro пересчитан.")


async def create_invoice(message, uid, link_payload=None):
    if not payments_ready():
        if OWNER_ID > 0 and uid == OWNER_ID:
            await show_payment_status(message, uid)
        else:
            await message.answer("Покупка пока недоступна. Обратись к владельцу через /paysupport.", reply_markup=main_bot_keyboard(pro=True))
        return
    if link_payload is None:
        # Old main-bot callback buttons now open the new, separate-bot flow.
        await show_pro(message, uid)
        return
    if message.bot.id != PAYMENT_BOT.id:
        await show_pro(message, uid)
        return
    order = await bill("order", uid, link_payload)
    valid = bool(order and order['kind'] == 'link' and order['merchant_id'] == PAYMENT_BOT.id
                 and order['accepted_terms'] == 1 and time.time() - order['created'] <= billing.ORDER_TTL)
    agreed = await store("get", uid)
    if not valid or not agreed or not agreed[0]:
        await message.answer("Ссылка устарела или предназначена другому пользователю. Открой Pro в основном боте и нажми «Оплатить» ещё раз.", reply_markup=main_bot_keyboard(pro=True))
        return
    # Each explicit Start creates a distinct invoice, not a reusable forwarded payment.
    invoice_payload = "gi_" + secrets.token_urlsafe(24)
    await bill("create_order", uid, {'payload': invoice_payload, 'merchant_id': PAYMENT_BOT.id, 'kind': 'invoice'})
    try:
        await message.bot.send_invoice(
            chat_id=uid, title="Анфиса Pro · 7 дней",
            description=f"7 дней доступа без почасовой квоты в @{MAIN_USERNAME}. Всего {billing.PRICE} ⭐, без автосписаний. Защита от спама остаётся.",
            payload=invoice_payload, provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Pro · 7 дней", amount=billing.PRICE)],
            start_parameter="anfisa-pro",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Заплатить ⭐ {billing.PRICE}", pay=True)],
                [InlineKeyboardButton(text="Прочитать условия", url=f"https://t.me/{MAIN_USERNAME}?start=terms")]
            ]),
        )
    except TelegramAPIError:
        logging.error("Invoice creation failed")
        await message.answer(ERROR_MESSAGE)


async def payment_start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) == 2 else ""
    if payload.startswith("gl_") and len(payload) <= 64:
        await create_invoice(message, message.from_user.id, payload)
    else:
        await message.answer(f"⭐ Оплата Анфиса Pro: 7 дней за {billing.PRICE} ⭐. Открой Pro в основном боте и нажми «Оплатить» — сюда передастся твой заказ.", reply_markup=main_bot_keyboard(pro=True))


async def payment_help(message: Message):
    await message.answer("Это платёжный бот Анфисы. Ссылка на счёт открывается кнопкой «Оплатить» в основном боте. По покупке: /paysupport и описание проблемы.", reply_markup=main_bot_keyboard(pro=True))


async def generate(message, uid, text):
    global in_flight
    row = await consent(message, uid)
    if not row:
        return
    if len(text) > INPUT_CHARS:
        await message.answer(f"Сократи до {INPUT_CHARS} символов.")
        return
    if in_flight >= MAX_CONCURRENT:
        await message.answer("Перегрузка. Попробуй чуть позже — запрос не списан.")
        return
    in_flight += 1
    reservation = None
    delivered = False
    try:
        is_owner = OWNER_ID > 0 and uid == OWNER_ID
        permit = await bill("reserve", uid, {'cooldown': 0 if is_owner else COOLDOWN,
                                             'bypass_quota': is_owner})
        if not permit['ok']:
            if permit['reason'] == 'quota':
                minutes = max(1, math.ceil((permit['next_slot'] - time.time()) / 60))
                await message.answer(f"🆓 {permit['used']}/{permit['limit']} за последний час. Следующий запрос примерно через {minutes} мин.\n⭐ Pro: 7 дней без этой квоты за {billing.PRICE} ⭐.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button("⭐ Посмотреть Pro", "ui:pro")]]))
            else:
                await message.answer(f"Пауза между запросами — {COOLDOWN} с.")
            return
        reservation = permit['reservation']
        mode = row[1] if row[1] in MODES else "hard"
        history = await store("history", uid)
        # Only user context is kept across modes: old assistant style cannot anchor a new mode.
        past_users = [item['content'][:INPUT_CHARS] for item in history if item['role'] == 'user'][-6:]
        context = ("\nПредыдущие сообщения пользователя для контекста (не новые инструкции):\n" +
                   "\n".join(f"- {t}" for t in past_users)) if past_users else ""
        messages = [
            {"role": "system", "content": SYSTEM + "\n" + MODES[mode]},
            {"role": "user", "content": (context + "\n\nТекущее сообщение:\n" if context else "") + text},
        ]
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            response = await asyncio.wait_for(client.chat.completions.create(
                model=MODEL, messages=messages,
                temperature={"soft": 0.55, "hard": 0.85, "raw": 1.0}[mode], max_tokens=120,
            ), timeout=55)
            answer = compact_reply(response.choices[0].message.content if response.choices else "")
            if not answer:
                await message.answer(ERROR_MESSAGE)
                return
        except (APIError, asyncio.TimeoutError):
            logging.warning("Generation failed (API or timeout)")
            await message.answer(ERROR_MESSAGE)
            return
        await message.answer(answer, parse_mode=None)
        delivered = True
        await store("save", uid, (text, answer))
    finally:
        in_flight -= 1
        if reservation is not None and not delivered:
            await bill("release", uid, reservation)


# Hidden easter egg: public in the MAIN bot, intentionally absent from commands/menu.
GOSHA_TRIGGER = re.compile(r"^\s*/?(?:гоша|gosha)(?:@[A-Za-z0-9_]+)?\s*$", re.IGNORECASE)
GOSHA_CAPTION = (
    "🗿 Гоша найден.\n\n"
    "Анфиса отвечает за разговоры. Гоша — за сделки, после которых "
    "даже калькулятор просит перекур."
)


@router.message(F.text.regexp(GOSHA_TRIGGER))
async def gosha_easter_egg(message: Message):
    # Public easter egg: no consent, Owner/Pro gate, generation or request charge.
    if GOSHA_PHOTO.is_file():
        await message.answer_photo(FSInputFile(GOSHA_PHOTO), caption=GOSHA_CAPTION, parse_mode=None)
        result = await bill("gosha_bonus", message.from_user.id)
        if result['granted']:
            await message.answer(
                f"🎁 Пасхалка найдена! +{billing.GOSHA_BONUS} запросов в час навсегда.\n"
                f"Твой бесплатный лимит теперь — {result['limit']} запросов/час.", parse_mode=None)
        else:
            await message.answer(
                f"🎁 Бонус уже твой: бесплатный лимит — {result['limit']} запросов/час. "
                "Повторно он не складывается.", parse_mode=None)
    else:
        await message.answer("Гоша ушёл за картинкой. Скоро вернётся 🗿", parse_mode=None)


@router.message(F.text.startswith("/"))
async def unknown(message: Message):
    await message.answer("Такой команды нет. Жми /menu.")


@router.message(F.text)
async def chat(message: Message):
    await generate(message, message.from_user.id, message.text)


@router.message()
async def unsupported(message: Message):
    await message.answer("Пока только текст.")


OWNER_PAGE_SIZE = 10


def owner_admin_allowed(message, uid):
    # Never deliver the roster to a forwarded button in somebody else's chat.
    return (OWNER_ID > 0 and uid == OWNER_ID and message.chat.id == OWNER_ID
            and message.chat.type == ChatType.PRIVATE)


def members_table(rows):
    labels = [("@" + username) if username else "—" for _, username in rows]
    w = max([len("Юзернейм")] + [len(label) for label in labels])
    iw = max([len("ID")] + [len(str(uid)) for uid, _ in rows])
    lines = ["┌" + "─" * (w + 2) + "┬" + "─" * (iw + 2) + "┐",
             f"│ {'Юзернейм':<{w}} │ {'ID':>{iw}} │",
             "├" + "─" * (w + 2) + "┼" + "─" * (iw + 2) + "┤"]
    for (uid, _), label in zip(rows, labels):
        lines.append(f"│ {label:<{w}} │ {str(uid):>{iw}} │")
    lines.append("└" + "─" * (w + 2) + "┴" + "─" * (iw + 2) + "┘")
    return "<pre>" + escape("\n".join(lines)) + "</pre>"


async def show_bot_admin(message, uid, page=0, edit=False):
    if not owner_admin_allowed(message, uid):
        await message.answer("⛔ Это меню доступно только Owner.")
        return
    roster = await store("members", uid, page)
    page = roster['page']
    text = ("<b>👑 Меню бота</b>\n"
            f"👥 Пользователи: <b>{roster['total']}</b>\n\n" + members_table(roster['rows']) +
            f"\n\n📄 Страница {page + 1}/{roster['pages']}\n"
            "— = юзернейм не указан или пока неизвестен.\n"
            "🎁 Справа от каждого пользователя ниже — подарок Pro на 24 часа.")
    rows = []
    for recipient_id, username in roster['rows']:
        label = ("@" + username) if username else "Без юзернейма"
        rows.append([button(f"👤 {label} · {recipient_id}", f"admin:users:{page}"),
                     button("🎁 Pro на 1 день", f"admin:giftstart:{recipient_id}:{page}")])
    nav = []
    if page > 0:
        nav.append(button("⬅️ Назад", f"admin:users:{page - 1}"))
    nav.append(button("🔄 Обновить", f"admin:users:{page}"))
    if page + 1 < roster['pages']:
        nav.append(button("Далее ➡️", f"admin:users:{page + 1}"))
    rows += [nav, [button("← Главное меню", "ui:menu")]]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    if not roster['total']:
        text += "\nПока нет сохранённых пользователей."
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in error.message.lower():
                return
            logging.warning("Admin roster edit not applied; sending a fresh table")
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


def admin_page(text):
    if not text.isascii() or not text.isdecimal() or len(text) > 10:
        return None
    return int(text)


@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_callback(query: CallbackQuery):
    uid = query.from_user.id
    if not owner_admin_allowed(query.message, uid):
        await query.message.answer("⛔ Это меню доступно только Owner.")
        return
    page = admin_page(query.data.rsplit(":", 1)[-1])
    if page is None:
        await query.message.answer("Открой меню заново: /menu.")
        return
    await show_bot_admin(query.message, uid, page, edit=not bool(getattr(query.message, 'photo', None)) and hasattr(query.message, 'edit_text'))


@router.callback_query(F.data.startswith("admin:giftstart:"))
async def admin_gift_start(query: CallbackQuery):
    owner = query.from_user.id
    if not owner_admin_allowed(query.message, owner):
        await query.message.answer("⛔ Подарки может выдавать только Owner.")
        return
    parts = query.data.split(":")
    if len(parts) != 4 or not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 19:
        await query.message.answer("Открой меню заново: /menu.")
        return
    recipient = int(parts[2])
    page = admin_page(parts[3])
    if recipient <= 0 or recipient > 9223372036854775807 or page is None:
        await query.message.answer("Открой меню заново: /menu.")
        return
    member = await store("member", owner, recipient)
    if not member:
        await query.message.answer("Пользователь не найден. Обнови таблицу.")
        return
    token = secrets.token_urlsafe(16)
    await bill("gift_prepare", owner, {"token": token, "recipient_id": recipient})
    username = "@" + member[1] if member[1] else "без юзернейма"
    state = await bill("status", recipient)
    current = f"Текущий Pro до {utc_date(state['until'])}." if state['pro'] else "Сейчас активного Pro нет."
    text = ("<b>🎁 Подарить Pro на 1 день?</b>\n\n"
            f"👤 {escape(username)}\n🆔 <code>{recipient}</code>\n\n"
            f"{current}\nДобавится 24 часа: к действующему сроку или с момента подтверждения.\n"
            "⭐ Звёзды не списываются. Подтверждение действует 10 минут.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Да, подарить 1 день", f"admin:giftconfirm:{token}:{page}")],
        [button("← К таблице", f"admin:users:{page}")],
    ])
    await query.message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin:giftconfirm:"))
async def admin_gift_confirm(query: CallbackQuery):
    owner = query.from_user.id
    if not owner_admin_allowed(query.message, owner):
        await query.message.answer("⛔ Подарки может выдавать только Owner.")
        return
    parts = query.data.split(":")
    if len(parts) != 4 or not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", parts[2]):
        await query.message.answer("Подтверждение недействительно. Открой таблицу заново.")
        return
    page = admin_page(parts[3])
    if page is None:
        await query.message.answer("Открой меню заново: /menu.")
        return
    result = await bill("gift_grant", owner, parts[2])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[button("← К таблице", f"admin:users:{page}")]])
    if result['state'] == 'duplicate':
        await query.message.answer("🎁 Этот подарок уже выдан. Повторно срок не добавлен.", reply_markup=keyboard)
        return
    if result['state'] != 'granted':
        await query.message.answer("Подтверждение истекло или недействительно. Открой таблицу и выбери подарок заново.", reply_markup=keyboard)
        return
    recipient = result['user_id']
    expiry = utc_date(result['until'])
    delivered = True
    try:
        await query.message.bot.send_message(recipient,
            f"🎁 Владелец подарил тебе Pro на 1 день!\nPro активен до {expiry}.\nОткрыть меню: /menu", parse_mode=None)
    except TelegramAPIError:
        delivered = False
    text = f"✅ Подарен 1 день Pro.\n🆔 {recipient}\n🗓 Pro до {expiry}."
    if not delivered:
        text += "\nУведомление не доставлено, но доступ сохранён."
    await query.message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("mode:"))
async def mode_callback(query: CallbackQuery):
    mode = query.data.split(":", 1)[1]
    if mode not in MODES or not await consent(query.message, query.from_user.id):
        return
    await store("mode", query.from_user.id, mode)
    await show_menu(query.message, query.from_user.id, edit=True)


@router.callback_query(F.data == "pro:buy")
async def buy_callback(query: CallbackQuery):
    await show_pro(query.message, query.from_user.id)


@router.callback_query(F.data.startswith("ui:"))
async def ui_callback(query: CallbackQuery):
    action, message, uid = query.data.split(":", 1)[1], query.message, query.from_user.id
    if action == "agree":
        await store("agree", uid)
        await show_menu(message, uid, edit=True)
    elif action == "menu":
        await show_menu(message, uid)
    elif action == "pro":
        await show_pro(message, uid)
    elif action in ("privacy", "terms", "help"):
        await message.answer({"privacy": PRIVACY, "terms": TERMS, "help": HELP}[action], parse_mode="HTML", reply_markup=back_keyboard())
    elif action == "paymentstatus":
        await show_payment_status(message, uid)
    elif action == "support":
        await message.answer("Напиши /paysupport и описание проблемы в одном сообщении. Ответит владелец бота.", reply_markup=back_keyboard())
    else:
        # Old v2 buttons do not delete data or start a roast after the upgrade.
        await message.answer("Меню обновилось. Открой /menu.")


async def errors(event):
    logging.error("Handler failure: %s", type(event.exception).__name__)
    message = event.update.message
    if message is None and event.update.callback_query is not None:
        message = event.update.callback_query.message
    if message is not None and message.chat.type == ChatType.PRIVATE:
        try:
            await message.answer(ERROR_MESSAGE)
        except Exception:
            logging.error("Could not deliver failure notification")
    return True


async def housekeeping():
    while True:
        await store("cleanup")
        await bill("cleanup")
        await asyncio.sleep(300)


pay_router.pre_checkout_query.register(checkout)
pay_router.message.register(payment_received, F.successful_payment)
pay_router.message.register(payment_refunded, F.refunded_payment)
pay_router.message.register(payment_start, CommandStart())
pay_router.message.register(support_handler, Command("paysupport", "support"))
pay_router.message.register(owner_handler, Command("reply", "stars", "payments", "refund"))
pay_router.message.register(payment_status_handler, Command("paymentstatus"))
pay_router.message.register(myid_handler, Command("myid"))
pay_router.message.register(role_handler, Command("role"))
pay_router.message.register(info_handler, Command("terms", "privacy"))
pay_router.message.register(payment_help)


async def configure_payment_bot(main_bot):
    global PAYMENT_SETUP_REASON
    if not PAYMENT_BOT_TOKEN:
        PAYMENT_SETUP_REASON = "missing_token"
        return None, ""
    try:
        candidate = Bot(token=PAYMENT_BOT_TOKEN)
    except TokenValidationError:
        PAYMENT_SETUP_REASON = "invalid_format"
        logging.error("Payment bot disabled: invalid_format")
        return None, ""
    if candidate.id == main_bot.id:
        PAYMENT_SETUP_REASON = "same_bot"
        await candidate.session.close()
        logging.error("Payment bot disabled: same_bot")
        return None, ""
    for attempt in range(3):
        try:
            info = await candidate.get_me(request_timeout=10)
        except TelegramUnauthorizedError:
            PAYMENT_SETUP_REASON = "unauthorized"
            break
        except TelegramNetworkError:
            PAYMENT_SETUP_REASON = "network_error"
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
                continue
            break
        except TelegramAPIError:
            PAYMENT_SETUP_REASON = "telegram_error"
            break
        else:
            if info.username:
                PAYMENT_SETUP_REASON = "ready"
                return candidate, info.username
            PAYMENT_SETUP_REASON = "missing_username"
            break
    logging.error("Payment bot disabled: %s", PAYMENT_SETUP_REASON)
    await candidate.session.close()
    return None, ""


async def apply_brand(merchant, payment=False):
    name = "Анфиса · Оплата" if payment else "Анфиса"
    try:
        await merchant.set_my_name(name=name)
        await merchant.set_my_name(name=name, language_code="ru")
    except TelegramAPIError:
        logging.warning("Brand name update deferred")
    if not AVATAR_PHOTO.is_file():
        return
    digest = hashlib.sha256(AVATAR_PHOTO.read_bytes()).hexdigest()
    if await store("asset_get", merchant.id) == digest:
        return
    try:
        await merchant.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(AVATAR_PHOTO)))
    except TelegramAPIError:
        logging.warning("Profile photo update deferred")
    else:
        await store("asset_save", merchant.id, digest)


def backup_before_price_restore():
    """One verified full SQLite snapshot on the SAME volume, before init/cleanup.

    Includes history, settings, purchases, bonus flags, quotas and every other table.
    Uses SQLite backup (also covers committed WAL data), not a raw file copy.
    An existing verified release snapshot is never overwritten on a restart.
    """
    if not DB.exists():
        logging.warning("Database backup: no existing database at configured DB_PATH")
        return None
    source = DB.resolve()
    if not VOLUME_PATH or not source.is_relative_to(Path(VOLUME_PATH).resolve()):
        raise RuntimeError("Backup requires the existing DB_PATH to be inside its Railway Volume")
    folder = source.parent / "backups"
    if not folder.resolve().is_relative_to(Path(VOLUME_PATH).resolve()):
        raise RuntimeError("Backup directory must stay inside Railway Volume")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (source.name + ".before-owner-menu-v10.sqlite3")

    def verify(path):
        if path.is_symlink():
            raise RuntimeError("Backup must be a regular file")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as con:
            if con.execute("PRAGMA page_count").fetchone()[0] == 0 or con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database backup integrity check failed")

    if target.exists():
        verify(target)
        logging.warning("Database backup ready: %s", target.name)
        return target
    temporary = folder / (target.name + ".tmp-" + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    deadline = time.monotonic() + 90

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Database backup timed out")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary, timeout=30)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
        verify(temporary)
        try:
            # Atomic publication without overwriting an earlier successful snapshot.
            os.link(temporary, target)
        except FileExistsError:
            verify(target)
    finally:
        temporary.unlink(missing_ok=True)
    logging.warning("Database backup ready: %s", target.name)
    return target


async def main():
    global client, MAIN_BOT, PAYMENT_BOT, MAIN_USERNAME, PAYMENT_USERNAME
    if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN и OPENROUTER_API_KEY в Railway Variables")
    try:
        await asyncio.to_thread(backup_before_price_restore)
    except Exception:
        logging.error("Database backup failed; database was not changed")
        raise SystemExit("Не удалось сохранить резервную копию. База не изменена. Проверь Volume, DB_PATH и свободное место; не удаляй базу.") from None
    await asyncio.to_thread(db_init)
    await asyncio.to_thread(billing.init, DB)
    await store("cleanup")
    await bill("cleanup")
    client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1", timeout=45, max_retries=0)
    MAIN_BOT = Bot(token=TELEGRAM_BOT_TOKEN)
    bots = [MAIN_BOT]
    dp = Dispatcher(disable_fsm=True)
    dp.include_routers(router, pay_router)
    dp.errors.register(errors)
    task = asyncio.create_task(housekeeping())
    try:
        main_info = await MAIN_BOT.get_me()
        MAIN_USERNAME = main_info.username or ""
        PAYMENT_BOT, PAYMENT_USERNAME = await configure_payment_bot(MAIN_BOT)
        if PAYMENT_BOT is not None:
            bots.append(PAYMENT_BOT)
        for merchant in bots:
            await merchant.delete_webhook(drop_pending_updates=False)
            await apply_brand(merchant, payment=PAYMENT_BOT is not None and merchant.id == PAYMENT_BOT.id)
        await MAIN_BOT.set_my_commands([
            BotCommand(command="menu", description="🎛 Главное меню"),
            BotCommand(command="pro", description=f"⭐ Pro · 7 дней за {billing.PRICE} ⭐"),
            BotCommand(command="role", description="👤 Моя роль"),
            BotCommand(command="hard", description="🔥 Жёстко"),
            BotCommand(command="soft", description="😌 Без мата"),
            BotCommand(command="raw", description="💀 Без фильтров"),
            BotCommand(command="help", description="❔ Как общаться"),
            BotCommand(command="terms", description="📜 Условия Pro"),
            BotCommand(command="paysupport", description="🛟 Помощь с платежом"),
            BotCommand(command="privacy", description="🔒 О данных"),
        ])
        await MAIN_BOT.set_chat_menu_button(menu_button=MenuButtonCommands())
        if PAYMENT_BOT:
            await PAYMENT_BOT.set_my_commands([
                BotCommand(command="start", description="⭐ Открыть оплату"),
                BotCommand(command="terms", description="📜 Условия"),
                BotCommand(command="paysupport", description="🛟 Помощь с покупкой"),
            ])
        logging.warning("Anfisa v10 started; bots=%s payments=%s owner_has_no_quota=True", len(bots), payments_ready())
        if not payments_ready():
            logging.warning("Payment setup checks failed: %s", ", ".join(name for name, ok, _ in payment_checks() if not ok))
        await dp.start_polling(*bots, allowed_updates=["message", "callback_query", "pre_checkout_query"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        for merchant in bots:
            await merchant.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(main())
