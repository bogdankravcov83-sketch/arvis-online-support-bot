import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from dotenv import load_dotenv
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================
# ARVIS ONLINE configuration
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [
    6099747512,
    987654321,
]

SERVERS = {
    "kyiv": "🌐 Київ",
    "dnipro": "🌐 Дніпро",
    "odesa": "🌐 Одеса",
}

CATEGORIES = {
    "game": "🎮 Проблема в грі",
    "bug": "🐞 Повідомити про помилку",
    "suggestion": "💡 Пропозиція",
    "player_complaint": "👮 Скарга на гравця",
    "admin_question": "👨‍💼 Питання до адміністрації",
    "other": "❓ Інше",
}

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = BASE_DIR / "media"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "arvis_online_support.sqlite3"

for directory in (DATA_DIR, MEDIA_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("arvis_online_support")

router = Router()


# =========================
# Database
# =========================
class Database:
    def __init__(self, path: Path):
        self.path = path
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self):
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admins (
                    telegram_id INTEGER PRIMARY KEY,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_number INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    server TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    account_number TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    admin_id INTEGER,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    rating TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS ticket_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    telegram_file_id TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_name TEXT,
                    mime_type TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_role TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS admin_sessions (
                    admin_id INTEGER PRIMARY KEY,
                    ticket_id INTEGER,
                    updated_at TEXT NOT NULL
                );
                """
            )
            now = utc_now()
            for admin_id in ADMIN_IDS:
                conn.execute(
                    "INSERT OR IGNORE INTO admins (telegram_id, added_at) VALUES (?, ?)",
                    (admin_id, now),
                )
            conn.commit()

    def upsert_user(self, message: Message):
        now = utc_now()
        user = message.from_user
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    updated_at=excluded.updated_at
                """,
                (user.id, user.username, user.first_name, user.last_name, now, now),
            )
            conn.commit()

    def next_ticket_number(self) -> int:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT MAX(ticket_number) AS n FROM tickets").fetchone()
            current = row["n"] if row and row["n"] is not None else 10000
            return current + 1

    def create_ticket(self, data: dict) -> sqlite3.Row:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT MAX(ticket_number) AS n FROM tickets").fetchone()
            current = row["n"] if row and row["n"] is not None else 10000
            ticket_number = current + 1
            now = utc_now()
            cur = conn.execute(
                """
                INSERT INTO tickets
                (ticket_number, user_id, server, nickname, account_number, category, description, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)
                """,
                (
                    ticket_number,
                    data["user_id"],
                    data["server"],
                    data["nickname"],
                    data["account_number"],
                    data["category"],
                    data["description"],
                    now,
                ),
            )
            ticket_id = cur.lastrowid
            conn.commit()
            return self.get_ticket(ticket_id)

    def add_file(self, ticket_id: int, file_id: str, file_type: str, file_name: str = "", mime_type: str = ""):
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO ticket_files
                (ticket_id, telegram_file_id, file_type, file_name, mime_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ticket_id, file_id, file_type, file_name, mime_type, utc_now()),
            )
            conn.commit()

    def get_ticket(self, ticket_id: int):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()

    def get_ticket_by_number(self, number: int):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM tickets WHERE ticket_number = ?", (number,)).fetchone()

    def get_files(self, ticket_id: int):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM ticket_files WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()

    def list_tickets(self, status: str, limit: int = 20, offset: int = 0):
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()

    def list_user_tickets(self, user_id: int, limit: int = 20):
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    def assign_ticket(self, ticket_id: int, admin_id: int) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "UPDATE tickets SET status='in_progress', admin_id=? WHERE id=? AND status='new' AND admin_id IS NULL",
                (admin_id, ticket_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def close_ticket(self, ticket_id: int, admin_id: int) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "UPDATE tickets SET status='closed', closed_at=?, admin_id=COALESCE(admin_id, ?) WHERE id=? AND status='in_progress' AND admin_id=?",
                (utc_now(), admin_id, ticket_id, admin_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def set_rating(self, ticket_id: int, user_id: int, rating: str) -> bool:
        with closing(self.connect()) as conn:
            cur = conn.execute(
                "UPDATE tickets SET rating=? WHERE id=? AND user_id=? AND status='closed' AND rating IS NULL",
                (rating, ticket_id, user_id),
            )
            conn.commit()
            return cur.rowcount == 1

    def add_message(self, ticket_id: int, sender_id: int, sender_role: str, message_type: str, text: Optional[str], telegram_message_id: int):
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO messages (ticket_id, sender_id, sender_role, message_type, text, telegram_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ticket_id, sender_id, sender_role, message_type, text, telegram_message_id, utc_now()),
            )
            conn.commit()

    def set_admin_session(self, admin_id: int, ticket_id: Optional[int]):
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO admin_sessions (admin_id, ticket_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(admin_id) DO UPDATE SET ticket_id=excluded.ticket_id, updated_at=excluded.updated_at
                """,
                (admin_id, ticket_id, utc_now()),
            )
            conn.commit()

    def get_admin_session(self, admin_id: int):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM admin_sessions WHERE admin_id=?", (admin_id,)).fetchone()

    def stats(self, admin_id: int):
        with closing(self.connect()) as conn:
            processed = conn.execute("SELECT COUNT(*) c FROM tickets WHERE admin_id=?", (admin_id,)).fetchone()["c"]
            closed = conn.execute("SELECT COUNT(*) c FROM tickets WHERE admin_id=? AND status='closed'", (admin_id,)).fetchone()["c"]
            positive = conn.execute("SELECT COUNT(*) c FROM tickets WHERE admin_id=? AND rating='positive'", (admin_id,)).fetchone()["c"]
            negative = conn.execute("SELECT COUNT(*) c FROM tickets WHERE admin_id=? AND rating='negative'", (admin_id,)).fetchone()["c"]
            return processed, closed, positive, negative

    def global_counts(self):
        with closing(self.connect()) as conn:
            rows = {}
            for status in ("new", "in_progress", "closed"):
                rows[status] = conn.execute("SELECT COUNT(*) c FROM tickets WHERE status=?", (status,)).fetchone()["c"]
            return rows


db = Database(DB_PATH)


# =========================
# FSM
# =========================
class TicketForm(StatesGroup):
    server = State()
    nickname = State()
    account_number = State()
    confirm_player = State()
    category = State()
    description = State()
    attachments = State()
    final_confirm = State()


class AdminReply(StatesGroup):
    waiting = State()


# =========================
# Helpers / keyboards
# =========================
def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_dt(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard(user_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="🎫 Створити звернення", callback_data="ticket:create")
    b.button(text="📂 Мої звернення", callback_data="tickets:mine")
    if is_admin(user_id):
        b.button(text="📩 Нові звернення", callback_data="admin:list:new")
        b.button(text="🛠️ В роботі", callback_data="admin:list:in_progress")
        b.button(text="🔒 Закриті", callback_data="admin:list:closed")
        b.button(text="📊 Статистика", callback_data="admin:stats")
    b.adjust(1, 1, 2, 1)
    return b.as_markup()


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Скасувати", callback_data="ticket:cancel")]])


def servers_keyboard():
    b = InlineKeyboardBuilder()
    for key, name in SERVERS.items():
        b.button(text=name, callback_data=f"ticket:server:{key}")
    b.button(text="❌ Скасувати", callback_data="ticket:cancel")
    b.adjust(1)
    return b.as_markup()


def categories_keyboard():
    b = InlineKeyboardBuilder()
    for key, name in CATEGORIES.items():
        b.button(text=name, callback_data=f"ticket:category:{key}")
    b.button(text="❌ Скасувати", callback_data="ticket:cancel")
    b.adjust(1)
    return b.as_markup()


def confirm_keyboard(prefix="ticket:confirm"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"{prefix}:yes")],
        [InlineKeyboardButton(text="✏️ Змінити", callback_data=f"{prefix}:edit")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="ticket:cancel")],
    ])


def attachment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Перейти далі", callback_data="ticket:attachments_done")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="ticket:cancel")],
    ])


def ticket_actions(ticket_id: int, status: str, admin_id: Optional[int], requester_admin: int):
    rows = []
    if status == "new":
        rows.append([InlineKeyboardButton(text="🛠️ Взяти в роботу", callback_data=f"admin:take:{ticket_id}")])
    elif status == "in_progress" and admin_id == requester_admin:
        rows.append([
            InlineKeyboardButton(text="✉️ Відповісти", callback_data=f"admin:reply:{ticket_id}"),
            InlineKeyboardButton(text="🔒 Закрити тікет", callback_data=f"admin:close:{ticket_id}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rating_keyboard(ticket_number: int):
    return InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="🟢 Позитивний відгук", callback_data=f"rating:positive:{ticket_number}"),
        InlineKeyboardButton(text="🔴 Негативний відгук", callback_data=f"rating:negative:{ticket_number}"),
    ]])


def status_text(status: str) -> str:
    return {"new": "📌 Новий", "in_progress": "🛠️ В роботі", "closed": "🔒 Закритий"}.get(status, status)


def ticket_summary(ticket) -> str:
    return (
        f"🎫 Тікет #{ticket['ticket_number']}\n"
        f"👤 Нікнейм: {ticket['nickname']}\n"
        f"🌐 Сервер: {ticket['server']}\n"
        f"📂 Категорія: {ticket['category']}\n"
        f"📅 Створено: {display_dt(ticket['created_at'])}\n"
        f"📌 Статус: {status_text(ticket['status'])}"
    )


def full_ticket_text(ticket) -> str:
    admin = str(ticket["admin_id"]) if ticket["admin_id"] else "Не призначений"
    return (
        f"🎫 Тікет #{ticket['ticket_number']}\n\n"
        f"👤 Telegram ID: {ticket['user_id']}\n"
        f"👤 Нікнейм: {ticket['nickname']}\n"
        f"🌐 Сервер: {ticket['server']}\n"
        f"🔢 Номер акаунта: {ticket['account_number']}\n"
        f"📂 Категорія: {ticket['category']}\n"
        f"📅 Створено: {display_dt(ticket['created_at'])}\n"
        f"📌 Статус: {status_text(ticket['status'])}\n"
        f"👨‍💼 Адміністратор: {admin}\n\n"
        f"📝 Опис проблеми:\n{ticket['description']}"
    )


def media_type_of(message: Message):
    if message.photo:
        return "photo", message.photo[-1].file_id, "", "image/jpeg"
    if message.video:
        return "video", message.video.file_id, "", message.video.mime_type or "video/mp4"
    if message.document:
        return "document", message.document.file_id, message.document.file_name or "", message.document.mime_type or ""
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_name or "", message.audio.mime_type or ""
    if message.voice:
        return "voice", message.voice.file_id, "", "audio/ogg"
    if message.animation:
        return "animation", message.animation.file_id, message.animation.file_name or "", message.animation.mime_type or ""
    return None


async def safe_edit(callback: CallbackQuery, text: str, markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        try:
            await callback.message.answer(text, reply_markup=markup)
        except Exception:
            pass


async def send_media_copy(bot: Bot, chat_id: int, source: Message):
    if source.photo:
        return await bot.send_photo(chat_id, source.photo[-1].file_id, caption=source.caption or None)
    if source.video:
        return await bot.send_video(chat_id, source.video.file_id, caption=source.caption or None)
    if source.document:
        return await bot.send_document(chat_id, source.document.file_id, caption=source.caption or None)
    if source.audio:
        return await bot.send_audio(chat_id, source.audio.file_id, caption=source.caption or None)
    if source.voice:
        return await bot.send_voice(chat_id, source.voice.file_id, caption=source.caption or None)
    if source.animation:
        return await bot.send_animation(chat_id, source.animation.file_id, caption=source.caption or None)
    return None


# =========================
# General commands
# =========================
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    db.upsert_user(message)
    text = (
        "Вітаємо у службі підтримки ARVIS ONLINE!\n\n"
        "Тут ви можете створити звернення, переглянути свої тікети та отримати допомогу."
    )
    await message.answer(text, reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("menu"))
async def menu(message: Message, state: FSMContext):
    await state.clear()
    db.upsert_user(message)
    await message.answer("Головне меню ARVIS ONLINE:", reply_markup=main_keyboard(message.from_user.id))


# =========================
# Ticket creation
# =========================
@router.callback_query(F.data == "ticket:create")
async def ticket_create(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TicketForm.server)
    await callback.answer()
    await safe_edit(callback, "Оберіть сервер:", servers_keyboard())


@router.callback_query(F.data == "ticket:cancel")
async def ticket_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Створення звернення скасовано")
    await safe_edit(callback, "Створення звернення скасовано.", main_keyboard(callback.from_user.id))


@router.callback_query(TicketForm.server, F.data.startswith("ticket:server:"))
async def choose_server(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    if key not in SERVERS:
        await callback.answer("Невідомий сервер", show_alert=True)
        return
    await state.update_data(server=SERVERS[key])
    await state.set_state(TicketForm.nickname)
    await callback.answer()
    await safe_edit(callback, "Введіть ваш нікнейм у грі:", cancel_keyboard())


@router.message(TicketForm.nickname)
async def enter_nickname(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нікнейм не може бути порожнім.", reply_markup=cancel_keyboard())
        return
    if len(text) > 64:
        await message.answer("Нікнейм занадто довгий. Максимум — 64 символи.", reply_markup=cancel_keyboard())
        return
    await state.update_data(nickname=text)
    await state.set_state(TicketForm.account_number)
    await message.answer("Введіть номер вашого ігрового акаунта:", reply_markup=cancel_keyboard())


@router.message(TicketForm.account_number)
async def enter_account(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Номер акаунта не може бути порожнім.", reply_markup=cancel_keyboard())
        return
    if len(text) > 32 or not re.fullmatch(r"[A-Za-zА-Яа-яІіЇїЄє0-9_-]+", text):
        await message.answer("Введіть коректний номер акаунта.", reply_markup=cancel_keyboard())
        return
    await state.update_data(account_number=text)
    data = await state.get_data()
    await state.set_state(TicketForm.confirm_player)
    await message.answer(
        f"Перевірте введені дані:\n\n"
        f"🌐 Сервер: {data['server']}\n"
        f"👤 Нікнейм: {data['nickname']}\n"
        f"🔢 Номер акаунта: {data['account_number']}\n\n"
        f"Підтвердити дані?",
        reply_markup=confirm_keyboard("ticket:player"),
    )


@router.callback_query(TicketForm.confirm_player, F.data == "ticket:player:yes")
async def player_confirm(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TicketForm.category)
    await callback.answer()
    await safe_edit(callback, "Оберіть категорію звернення:", categories_keyboard())


@router.callback_query(TicketForm.confirm_player, F.data == "ticket:player:edit")
async def player_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TicketForm.nickname)
    await callback.answer()
    await safe_edit(callback, "Введіть ваш нікнейм у грі:", cancel_keyboard())


@router.callback_query(TicketForm.category, F.data.startswith("ticket:category:"))
async def choose_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 2)[2]
    if key not in CATEGORIES:
        await callback.answer("Невідома категорія", show_alert=True)
        return
    await state.update_data(category=CATEGORIES[key])
    await state.set_state(TicketForm.description)
    await callback.answer()
    await safe_edit(callback, "Опишіть вашу проблему або звернення одним повідомленням:", cancel_keyboard())


@router.message(TicketForm.description)
async def enter_description(message: Message, state: FSMContext):
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Опис звернення не може бути порожнім.", reply_markup=cancel_keyboard())
        return
    await state.update_data(description=text)
    await state.set_state(TicketForm.attachments)
    await message.answer(
        "Додайте фотографії, відео, документи або інші дозволені файли.\n\n"
        "Якщо файлів немає — натисніть «Перейти далі».",
        reply_markup=attachment_keyboard(),
    )


@router.message(TicketForm.attachments)
async def collect_attachments(message: Message, state: FSMContext):
    media = media_type_of(message)
    if not media:
        await message.answer("Надішліть фото, відео, документ або інший дозволений файл.", reply_markup=attachment_keyboard())
        return
    data = await state.get_data()
    attachments = data.get("attachments", [])
    attachments.append({
        "file_id": media[1],
        "file_type": media[0],
        "file_name": media[2],
        "mime_type": media[3],
    })
    await state.update_data(attachments=attachments)
    await message.answer("Файл додано до звернення. Можете додати ще або перейти далі.", reply_markup=attachment_keyboard())


@router.callback_query(TicketForm.attachments, F.data == "ticket:attachments_done")
async def attachments_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files_count = len(data.get("attachments", []))
    await state.set_state(TicketForm.final_confirm)
    await callback.answer()
    await safe_edit(
        callback,
        f"Фінальна перевірка звернення:\n\n"
        f"🌐 Сервер: {data['server']}\n"
        f"👤 Нікнейм: {data['nickname']}\n"
        f"🔢 Номер акаунта: {data['account_number']}\n"
        f"📂 Категорія: {data['category']}\n"
        f"📎 Файлів: {files_count}\n\n"
        f"📝 Опис:\n{data['description']}\n\n"
        f"Створити тікет?",
        confirm_keyboard("ticket:final"),
    )


@router.callback_query(TicketForm.final_confirm, F.data == "ticket:final:edit")
async def final_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(TicketForm.description)
    await callback.answer()
    await safe_edit(callback, "Введіть опис звернення заново:", cancel_keyboard())


@router.callback_query(TicketForm.final_confirm, F.data == "ticket:final:yes")
async def final_create(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    ticket = db.create_ticket({
        "user_id": callback.from_user.id,
        "server": data["server"],
        "nickname": data["nickname"],
        "account_number": data["account_number"],
        "category": data["category"],
        "description": data["description"],
    })
    for item in data.get("attachments", []):
        db.add_file(ticket["id"], item["file_id"], item["file_type"], item["file_name"], item["mime_type"])

    await state.clear()
    await callback.answer()
    await safe_edit(
        callback,
        f"Звернення створено успішно.\n\n🎫 Номер тікета: #{ticket['ticket_number']}\n📌 Статус: Новий",
        main_keyboard(callback.from_user.id),
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "📩 Нове звернення ARVIS ONLINE:\n\n" + full_ticket_text(ticket),
                reply_markup=ticket_actions(ticket["id"], ticket["status"], ticket["admin_id"], admin_id),
            )
            for f in db.get_files(ticket["id"]):
                if f["file_type"] == "photo":
                    await bot.send_photo(admin_id, f["telegram_file_id"], caption=f"📎 Доказ до тікета #{ticket['ticket_number']}")
                elif f["file_type"] == "video":
                    await bot.send_video(admin_id, f["telegram_file_id"], caption=f"📎 Доказ до тікета #{ticket['ticket_number']}")
                else:
                    await bot.send_document(admin_id, f["telegram_file_id"], caption=f"📎 Доказ до тікета #{ticket['ticket_number']}")
        except Exception:
            logger.exception("Не вдалося повідомити адміністратора %s про тікет %s", admin_id, ticket["ticket_number"])


# =========================
# Player tickets
# =========================
@router.callback_query(F.data == "tickets:mine")
async def my_tickets(callback: CallbackQuery):
    tickets = db.list_user_tickets(callback.from_user.id)
    if not tickets:
        await callback.answer()
        await safe_edit(callback, "У вас ще немає звернень.", main_keyboard(callback.from_user.id))
        return
    b = InlineKeyboardBuilder()
    for ticket in tickets:
        b.button(text=f"#{ticket['ticket_number']} — {status_text(ticket['status'])}", callback_data=f"ticket:view:{ticket['id']}")
    b.button(text="⬅️ Назад", callback_data="ticket:back")
    b.adjust(1)
    await callback.answer()
    await safe_edit(callback, "Ваші звернення:", b.as_markup())


@router.callback_query(F.data == "ticket:back")
async def ticket_back(callback: CallbackQuery):
    await callback.answer()
    await safe_edit(callback, "Головне меню ARVIS ONLINE:", main_keyboard(callback.from_user.id))


@router.callback_query(F.data.startswith("ticket:view:"))
async def view_my_ticket(callback: CallbackQuery):
    try:
        ticket_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неправильний ID", show_alert=True)
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer("Цей тікет вам недоступний", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, full_ticket_text(ticket), main_keyboard(callback.from_user.id))


# =========================
# Admin panel
# =========================
@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    await callback.answer()
    await safe_edit(callback, "Панель адміністратора ARVIS ONLINE:", main_keyboard(callback.from_user.id))


@router.callback_query(F.data.startswith("admin:list:"))
async def admin_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    status = callback.data.split(":", 2)[2]
    if status not in {"new", "in_progress", "closed"}:
        await callback.answer("Невідомий статус", show_alert=True)
        return
    tickets = db.list_tickets(status)
    b = InlineKeyboardBuilder()
    for ticket in tickets:
        b.button(text=f"#{ticket['ticket_number']} • {ticket['nickname']}", callback_data=f"admin:view:{ticket['id']}")
    b.button(text="⬅️ Назад", callback_data="admin:home")
    b.adjust(1)
    title = {"new": "📩 Нові звернення", "in_progress": "🛠️ Тікети в роботі", "closed": "🔒 Закриті тікети"}[status]
    await callback.answer()
    await safe_edit(callback, f"{title}:\n\nКількість: {len(tickets)}", b.as_markup())


@router.callback_query(F.data.startswith("admin:view:"))
async def admin_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    try:
        ticket_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неправильний ID", show_alert=True)
        return
    ticket = db.get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Тікет не знайдено", show_alert=True)
        return
    # Admin may inspect any ticket, but actions are protected by ownership.
    await callback.answer()
    await safe_edit(callback, full_ticket_text(ticket), ticket_actions(ticket_id, ticket["status"], ticket["admin_id"], callback.from_user.id))
    for f in db.get_files(ticket_id):
        try:
            if f["file_type"] == "photo":
                await callback.message.answer_photo(f["telegram_file_id"], caption=f"📎 Файл до тікета #{ticket['ticket_number']}")
            elif f["file_type"] == "video":
                await callback.message.answer_video(f["telegram_file_id"], caption=f"📎 Файл до тікета #{ticket['ticket_number']}")
            else:
                await callback.message.answer_document(f["telegram_file_id"], caption=f"📎 Файл до тікета #{ticket['ticket_number']}")
        except Exception:
            logger.exception("Не вдалося показати файл тікета %s", ticket["ticket_number"])


@router.callback_query(F.data.startswith("admin:take:"))
async def admin_take(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    try:
        ticket_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Неправильний ID", show_alert=True)
        return
    if db.assign_ticket(ticket_id, callback.from_user.id):
        ticket = db.get_ticket(ticket_id)
        db.set_admin_session(callback.from_user.id, ticket_id)
        await callback.answer("Тікет взято в роботу")
        await safe_edit(callback, full_ticket_text(ticket), ticket_actions(ticket_id, ticket["status"], ticket["admin_id"], callback.from_user.id))
        try:
            await bot.send_message(ticket["user_id"], f"🛠️ Ваш тікет #{ticket['ticket_number']} взяв у роботу адміністратор ARVIS ONLINE.\n\nВи можете продовжувати спілкування через цей бот.")
        except (TelegramForbiddenError, Exception):
            logger.exception("Не вдалося повідомити користувача про взяття тікета")
    else:
        ticket = db.get_ticket(ticket_id)
        if not ticket:
            await callback.answer("Тікет не знайдено", show_alert=True)
        elif ticket["status"] == "closed":
            await callback.answer("Тікет уже закритий", show_alert=True)
        else:
            await callback.answer("Тікет уже взятий іншим адміністратором", show_alert=True)


@router.callback_query(F.data.startswith("admin:reply:"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    ticket = db.get_ticket(ticket_id)
    if not ticket or ticket["status"] != "in_progress" or ticket["admin_id"] != callback.from_user.id:
        await callback.answer("Цей тікет не закріплений за вами", show_alert=True)
        return
    db.set_admin_session(callback.from_user.id, ticket_id)
    await state.set_state(AdminReply.waiting)
    await state.update_data(ticket_id=ticket_id)
    await callback.answer()
    await safe_edit(callback, f"✉️ Введіть повідомлення для гравця у тікеті #{ticket['ticket_number']}.\n\nМожна надіслати текст, фото, відео, документ або файл.")


@router.message(AdminReply.waiting)
async def admin_reply_send(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    ticket = db.get_ticket(ticket_id) if ticket_id else None
    if not ticket or ticket["status"] != "in_progress" or ticket["admin_id"] != message.from_user.id:
        await state.clear()
        await message.answer("Тікет недоступний для відповіді.", reply_markup=main_keyboard(message.from_user.id))
        return

    if message.text:
        if not message.text.strip():
            await message.answer("Порожнє повідомлення не може бути надіслане.")
            return
        sent = await bot.send_message(ticket["user_id"], f"👨‍💼 Адміністратор ARVIS ONLINE:\n\n{message.text}")
        db.add_message(ticket_id, message.from_user.id, "admin", "text", message.text, sent.message_id)
    elif media_type_of(message):
        sent = await send_media_copy(bot, ticket["user_id"], message)
        db.add_message(ticket_id, message.from_user.id, "admin", media_type_of(message)[0], message.caption, sent.message_id if sent else message.message_id)
    else:
        await message.answer("Цей тип повідомлення не підтримується.")
        return

    await state.clear()
    await message.answer(f"Повідомлення надіслано гравцю у тікеті #{ticket['ticket_number']}.", reply_markup=ticket_actions(ticket_id, ticket["status"], ticket["admin_id"], message.from_user.id))


@router.callback_query(F.data.startswith("admin:close:"))
async def admin_close(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    ticket_id = int(callback.data.rsplit(":", 1)[1])
    ticket = db.get_ticket(ticket_id)
    if not ticket or ticket["status"] != "in_progress" or ticket["admin_id"] != callback.from_user.id:
        await callback.answer("Тікет не можна закрити цим адміністратором", show_alert=True)
        return
    if not db.close_ticket(ticket_id, callback.from_user.id):
        await callback.answer("Тікет уже закритий або змінив статус", show_alert=True)
        return
    ticket = db.get_ticket(ticket_id)
    await callback.answer("Тікет закрито")
    await safe_edit(callback, full_ticket_text(ticket), main_keyboard(callback.from_user.id))
    try:
        await bot.send_message(
            ticket["user_id"],
            f"🔒 Ваш тікет #{ticket['ticket_number']} закрито.\n\nОцініть роботу адміністратора:",
            reply_markup=rating_keyboard(ticket["ticket_number"]),
        )
    except Exception:
        logger.exception("Не вдалося надіслати запит оцінки")


# =========================
# Rating
# =========================
@router.callback_query(F.data.startswith("rating:"))
async def rate_ticket(callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неправильна оцінка", show_alert=True)
        return
    rating, number_text = parts[1], parts[2]
    if rating not in {"positive", "negative"}:
        await callback.answer("Неправильна оцінка", show_alert=True)
        return
    try:
        number = int(number_text)
    except ValueError:
        await callback.answer("Неправильний номер тікета", show_alert=True)
        return
    ticket = db.get_ticket_by_number(number)
    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer("Тікет вам недоступний", show_alert=True)
        return
    if ticket["status"] != "closed":
        await callback.answer("Тікет ще не закритий", show_alert=True)
        return
    if ticket["rating"] is not None:
        await callback.answer("Ви вже оцінили цей тікет", show_alert=True)
        return
    if db.set_rating(ticket["id"], callback.from_user.id, rating):
        await callback.answer("Дякуємо за оцінку")
        await safe_edit(callback, f"Дякуємо! Оцінку для тікета #{number} збережено.", main_keyboard(callback.from_user.id))
    else:
        await callback.answer("Не вдалося зберегти оцінку", show_alert=True)


# =========================
# Statistics
# =========================
@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return
    processed, closed, positive, negative = db.stats(callback.from_user.id)
    counts = db.global_counts()
    text = (
        "📊 Статистика адміністратора ARVIS ONLINE\n\n"
        f"🎫 Кількість оброблених тікетів: {processed}\n"
        f"🔒 Кількість закритих тікетів: {closed}\n"
        f"🟢 Кількість позитивних відгуків: {positive}\n"
        f"🔴 Кількість негативних відгуків: {negative}\n"
        f"📈 Загальна кількість опрацьованих звернень: {processed}\n\n"
        "Загальна статистика:\n"
        f"📩 Нових: {counts['new']}\n"
        f"🛠️ В роботі: {counts['in_progress']}\n"
        f"🔒 Закритих: {counts['closed']}"
    )
    await callback.answer()
    await safe_edit(callback, text, main_keyboard(callback.from_user.id))


# =========================
# Player replies to assigned ticket
# =========================
@router.message()
async def player_message_router(message: Message, bot: Bot):
    if is_admin(message.from_user.id):
        # Admins without an active FSM state can use /menu.
        return
    db.upsert_user(message)
    tickets = db.list_user_tickets(message.from_user.id)
    active = next((t for t in tickets if t["status"] == "in_progress"), None)
    if not active:
        return
    if message.text and message.text.startswith("/"):
        return
    if message.text:
        sent = await bot.send_message(active["admin_id"], f"👤 Гравець у тікеті #{active['ticket_number']}:\n\n{message.text}")
        db.add_message(active["id"], message.from_user.id, "player", "text", message.text, sent.message_id)
        await message.answer(f"Ваше повідомлення додано до тікета #{active['ticket_number']}.")
    elif media_type_of(message):
        sent = await send_media_copy(bot, active["admin_id"], message)
        db.add_message(active["id"], message.from_user.id, "player", media_type_of(message)[0], message.caption, sent.message_id if sent else message.message_id)
        await message.answer(f"Файл додано до тікета #{active['ticket_number']}.")


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не заданий. Вкажіть токен у змінній середовища BOT_TOKEN або у файлі .env.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("ARVIS ONLINE support bot запускається")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("ARVIS ONLINE support bot зупинено")
