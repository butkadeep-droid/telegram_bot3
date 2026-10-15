"""Гоша: private-chat MVP. Python 3.10+, aiogram 3, OpenRouter, SQLite."""
import asyncio
import logging
import os
import re
import secrets
import math
import billing
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, FSInputFile, BotCommand, MenuButtonCommands,
                           PreCheckoutQuery, LabeledPrice)
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
# ================================================

# Для сохранения истории при перезапусках DB_PATH должен указывать
# на файл в подключённом постоянном диске хостинга.
DB = Path(os.getenv("DB_PATH", "data/gosha.sqlite3"))
MODEL = os.getenv("MODEL", "deepseek/deepseek-chat")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PAYMENTS_ENABLED = os.getenv("ENABLE_PAYMENTS", "false").lower() == "true"
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

SYSTEM = """Ты — Гоша, вымышленный дерзкий ИИ-собеседник. По-русски, на «ты».
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


def db_run(action, uid=0, value=None):
    """Short transactions; executed off the event loop, never across API waits."""
    day = datetime.now(timezone.utc).date().isoformat()
    now = time.time()
    with closing(sqlite3.connect(DB, timeout=10)) as con, con:
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


router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
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


def payments_ready():
    if not (PAYMENTS_ENABLED and OWNER_ID > 0 and DB.is_absolute() and VOLUME_PATH):
        return False
    # Railway supplies this mount path only for an attached volume.
    return DB.resolve().is_relative_to(Path(VOLUME_PATH).resolve())


def utc_date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def consent_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("✅ Мне 16+ · принимаю дерзкий стиль", "ui:agree")],
        [button("🔒 О данных", "ui:privacy"), button("📜 Условия", "ui:terms")],
    ])


def menu_keyboard(mode="hard", pro=False):
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(("✓ " if key == mode else "") + label, "mode:" + key)
         for key, label in MODE_NAMES.items()],
        [button("⭐ Мой Pro · продлить" if pro else "⭐ Pro · 7 дней за 5 звёзд", "ui:pro")],
        [button("❔ Как общаться", "ui:help"), button("🔒 О данных", "ui:privacy")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Главное меню", "ui:menu")]])


WELCOME = (
    "<b>ГОША · коротко и в лицо</b>\n\n"
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
    "Для квоты сохраняются отметки бесплатных запросов за последний час. "
    "Для Pro — заказы, подтверждения платежей, сроки доступа и возвраты. "
    "Финансовый журнал не удаляется автоматически командой /delete.\n\n"
    "/reset — стереть переписку. /delete — также настройки и согласие. "
    "Активный оплаченный доступ, записи платежей и временные антиспам-счётчики сохраняются. "
    "Старый дневной счётчик предыдущей версии очищается после смены дня UTC.\n\n"
    "Это не удаляет копии у Telegram и провайдеров. Самоподтверждение 16+ не заменяет "
    "проверку документов. По данным и платежам: /support текст или /paysupport текст."
)
TERMS = (
    "<b>📜 Условия Pro</b>\n\n"
    "⭐ Цена: <b>5 Telegram Stars</b>.\n"
    "🗓 Срок: <b>7 × 24 часа</b> после подтверждения оплаты. "
    "Повторная покупка добавляет 7 дней к действующему сроку.\n"
    "∞ На этот срок снимается бесплатная квота 10 запросов/час. "
    f"Пауза {COOLDOWN} с, ограничения длины и защита от перегрузки остаются.\n"
    "🔁 Разовый платёж, <b>без автопродления</b>.\n\n"
    "Оплачивается доступ к ИИ-боту, а не гарантированная формулировка каждого ответа. "
    "Сервисы Telegram и модели могут быть временно недоступны. "
    "Угрозы, дискриминация, травля третьих лиц и опасные инструкции не входят в услугу.\n\n"
    "Помощь и запрос возврата: /paysupport и описание проблемы. "
    "Покупку обслуживает владелец этого бота, не поддержка Telegram. "
    "При возврате оплаченный срок пересчитывается. Возвраты и обязательные права "
    "покупателя определяются применимым законодательством.\n\n"
    "Кнопка «Согласен · оплатить» подтверждает принятие этих условий."
)
HELP = (
    "<b>❔ Как общаться</b>\n\n"
    "Просто пиши. Гоша отвечает на твою реплику, не рассказывает анекдоты.\n"
    "😌 Мягко — спокойно и без мата.\n🔥 Жёстко — ругань и личные подколы.\n"
    "💀 Без фильтров — самая грубая добровольная перепалка. "
    "При реальном кризисе — поддержка, не добивание.\n\n"
    "🆓 Бесплатно: 10 успешных ответов за скользящие 60 минут. "
    "Освободившийся запрос возвращается через час после его использования.\n"
    "⭐ Pro: 5 звёзд → 7 дней без почасовой квоты.\n\n"
    "/menu — меню · /pro — доступ\n/soft · /hard · /raw — режимы\n"
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
        quota = f"⭐ <b>Pro до {utc_date(state['until'])}</b>" if state['pro'] else f"🆓 <b>{state['remaining']}/10</b> запросов за последний час"
        caption = (
            "<b>ГОША · коротко и в лицо</b>\n\n"
            f"🎛 Твой режим: <b>{MODE_NAMES[mode]}</b>\n{quota}\n\n"
            "Пиши прямо в чат. Меньше церемоний — больше дела."
        )
        kb = menu_keyboard(mode, state['pro'])
    else:
        caption, kb = WELCOME, consent_keyboard()
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


async def show_pro(message, uid):
    state = await bill("status", uid)
    current = f"\n✅ <b>Уже активен до {utc_date(state['until'])}</b>\n" if state['pro'] else ""
    text = (
        "<b>⭐ ГОША PRO</b>\n\n"
        "∞ Без почасовой квоты\n🗓 7 дней доступа\n💫 Всего 5 Telegram Stars\n"
        "🔁 Без автосписаний\n" + current +
        f"\nПауза {COOLDOWN} с и защита от перегрузки остаются. "
        "Повторная покупка продлевает срок на 7 дней.\n\n"
        "Прочитай условия ниже. Нажимая «Согласен · оплатить», принимаешь /terms. "
        "Вопросы по покупке: /paysupport."
    )
    rows = []
    if payments_ready():
        rows.append([button("✅ Согласен · оплатить 5 ⭐", "pro:buy")])
    else:
        text += "\n\n⚙️ Продажи пока не включены владельцем."
    rows += [[button("📜 Условия", "ui:terms"), button("🛟 Помощь", "ui:support")],
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
            (query.invoice_payload, query.total_amount, query.currency)))
    except Exception:
        logging.error("Checkout validation failed")
        ok = False
    await query.answer(ok=ok, error_message=None if ok else "Счёт недоступен. Открой /pro и создай новый, либо обратись в /paysupport.")


@router.message(F.successful_payment)
async def payment_received(message: Message):
    payment = message.successful_payment
    result = await bill("payment", message.from_user.id,
                        (payment.telegram_payment_charge_id, payment.invoice_payload,
                         payment.total_amount, payment.currency))
    if result['state'] == 'duplicate':
        return
    if result['state'] == 'granted':
        await message.answer(f"⭐ Pro включён до {utc_date(result['until'])}.\nПочасовая квота снята. Пиши.", reply_markup=back_keyboard())
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
    until = await bill("refund", payer, refund.telegram_payment_charge_id)
    suffix = f"Pro остаётся до {utc_date(until)}." if until > time.time() else "Доступны бесплатные 10 запросов/час."
    await message.answer("Возврат учтён. " + suffix)


@router.message(CommandStart())
@router.message(Command("menu", "mode"))
async def start(message: Message):
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
    await message.answer(f"{MODE_NAMES[mode]} — включён.", reply_markup=menu_keyboard(mode, (await bill('status', uid))['pro']))


@router.message(Command("reset", "delete"))
async def clear_handler(message: Message):
    action = message.text.split()[0].split("@")[0][1:]
    await store(action, message.from_user.id)
    text = "🧹 Переписка забыта. Режим и лимит не изменились."
    if action == "delete":
        text = "Переписка, настройки и согласие удалены. Оплаченный срок, платёжный журнал и временные счётчики сохраняются. Подробнее: /privacy."
    await message.answer(text, reply_markup=back_keyboard())


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
        balance = await message.bot.get_my_star_balance()
        await message.answer(f"Баланс бота: {balance.amount} ⭐")
    elif command == "payments":
        rows = await bill("payments")
        lines = [f"{r['user_id']} · {r['amount']} {r['currency']} · {r['state']}\n{r['charge_id']}" for r in rows]
        await message.answer("Последние платежи:\n\n" + ("\n\n".join(lines) or "Пока нет."), parse_mode=None)
    elif command == "refund":
        if len(parts) < 2:
            await message.answer("/refund CHARGE_ID — полный возврат 5 Stars. Для несопоставленного платежа: /refund CHARGE_ID TELEGRAM_ID")
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
        refunded = await message.bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=parts[1])
        if not refunded:
            await message.answer(ERROR_MESSAGE)
            return
        await bill("refund", uid, parts[1])
        await message.answer("Возврат выполнен, срок Pro пересчитан.")


async def create_invoice(message, uid):
    if not payments_ready():
        await message.answer("Продажи ещё не включены владельцем.")
        return
    if not await consent(message, uid):
        return
    payload = "gosha3:" + secrets.token_urlsafe(24)
    await bill("create_order", uid, payload)
    try:
        await message.bot.send_invoice(
            chat_id=uid, title="Гоша Pro · 7 дней",
            description="7 дней без почасовой квоты. Разовая оплата 5 Stars, без автопродления. Защита от спама остаётся. Условия: /terms. Поддержка: /paysupport.",
            payload=payload, provider_token="", currency="XTR",
            prices=[LabeledPrice(label="Pro · 7 дней", amount=billing.PRICE)],
            start_parameter="gosha-pro",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Оплатить 5 ⭐", pay=True)]]),
        )
    except TelegramAPIError:
        logging.error("Invoice creation failed")
        await message.answer(ERROR_MESSAGE)


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
        permit = await bill("reserve", uid, COOLDOWN)
        if not permit['ok']:
            if permit['reason'] == 'quota':
                minutes = max(1, math.ceil((permit['next_slot'] - time.time()) / 60))
                await message.answer(f"🆓 10/10 за последний час. Следующий запрос примерно через {minutes} мин.\n⭐ Pro: 7 дней без этой квоты за 5 звёзд.",
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


@router.message(F.text.startswith("/"))
async def unknown(message: Message):
    await message.answer("Такой команды нет. Жми /menu.")


@router.message(F.text)
async def chat(message: Message):
    await generate(message, message.from_user.id, message.text)


@router.message()
async def unsupported(message: Message):
    await message.answer("Пока только текст.")


@router.callback_query(F.data.startswith("mode:"))
async def mode_callback(query: CallbackQuery):
    mode = query.data.split(":", 1)[1]
    if mode not in MODES or not await consent(query.message, query.from_user.id):
        return
    await store("mode", query.from_user.id, mode)
    await show_menu(query.message, query.from_user.id, edit=True)


@router.callback_query(F.data == "pro:buy")
async def buy_callback(query: CallbackQuery):
    await create_invoice(query.message, query.from_user.id)


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


async def main():
    global client
    if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN и OPENROUTER_API_KEY в Railway Variables")
    await asyncio.to_thread(db_init)
    await asyncio.to_thread(billing.init, DB)
    await store("cleanup")
    await bill("cleanup")
    client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1", timeout=45, max_retries=0)
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    # No FSM global isolation: checkout must never queue behind a slow AI generation.
    dp = Dispatcher(disable_fsm=True)
    dp.include_router(router)
    dp.errors.register(errors)
    task = asyncio.create_task(housekeeping())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands([
            BotCommand(command="menu", description="🎛 Главное меню"),
            BotCommand(command="pro", description="⭐ Pro · 7 дней за 5 Stars"),
            BotCommand(command="hard", description="🔥 Жёстко"),
            BotCommand(command="soft", description="😌 Без мата"),
            BotCommand(command="raw", description="💀 Без фильтров"),
            BotCommand(command="help", description="❔ Как общаться"),
            BotCommand(command="terms", description="📜 Условия Pro"),
            BotCommand(command="paysupport", description="🛟 Помощь с платежом"),
            BotCommand(command="privacy", description="🔒 О данных"),
        ])
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.warning("Gosha v3 started; rolling quota and 7-day Stars Pro enabled; payments=%s", payments_ready())
        await dp.start_polling(bot, allowed_updates=["message", "callback_query", "pre_checkout_query"])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    asyncio.run(main())
