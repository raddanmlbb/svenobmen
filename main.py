import re
import logging
import sqlite3
import aiohttp
import json
import asyncio
from datetime import datetime
from typing import Tuple, Optional, List, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, MessageHandler, filters, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# ==================================================
# ================== НАСТРОЙКИ ====================
# ==================================================

BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039  # замените на свой ID

CACHE_TIME_SECONDS = 3600

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REQUISITES_SENT = "requisites_sent"
STATUS_PAID = "paid"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED_BY_USER = "cancelled_by_user"
STATUS_CANCELLED_BY_ADMIN = "cancelled_by_admin"

OPERATION_OXAPAY = "Оплата счёта OxaPay"
OPERATION_BITPAPA = "Создание чека Bitpapa"
OPERATION_CRYPTO = "Покупка крипты на кошелёк"
OPERATION_SHOP = "Отправка на кошелёк магазина"

afk_mode = False

# ==================================================
# ================== БЕЗОПАСНЫЙ MD =================
# ==================================================

def escape_md(text: str) -> str:
    return escape_markdown(text, version=2)

async def safe_send(msg, text: str, **kwargs):
    parse_mode = kwargs.get('parse_mode')
    if parse_mode == ParseMode.MARKDOWN_V2:
        text = escape_md(text)
    await msg.reply_text(text, **kwargs)

# ==================================================
# ================== БД (WAL + lock) ===============
# ==================================================

class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.db_file = db_file
        self._init_db()
        self._lock = asyncio.Lock()

    def _init_db(self):
        with sqlite3.connect(self.db_file, timeout=10) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            self._create_tables(conn)
            self._init_settings(conn)

    def _create_tables(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_deals INTEGER DEFAULT 0,
                total_volume REAL DEFAULT 0,
                avg_rating REAL DEFAULT 0,
                ratings_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                ban_reason TEXT,
                banned_at TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                operation_type TEXT,
                amount REAL,
                client_total REAL,
                status TEXT,
                requisites_text TEXT,
                created_at TEXT,
                taken_at TEXT,
                requisites_sent_at TEXT,
                paid_at TEXT,
                completed_at TEXT,
                cancelled_at TEXT,
                cancelled_by TEXT,
                pdf_file_id TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                request_id INTEGER,
                rating INTEGER,
                comment TEXT,
                created_at TEXT,
                is_displayed INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

    def _init_settings(self, conn):
        defaults = {
            'rules': '📜 *ПРАВИЛА РАБОТЫ*\n\n• Минимальная сумма: 1000 ₽\n• Комиссия: 10%\n• Работаем 24/7\n• Чек PDF обязателен\n• Неоплата счёта влечёт блокировку',
            'schedule': '⏰ *ГРАФИК РАБОТЫ*\n\n• Пн–Вс: 24/7\n• Без выходных',
            'links': '🔗 *ПОЛЕЗНЫЕ ССЫЛКИ*\n\n• 📢 Канал: https://t.me/svenobmen\n• 📊 Bitpapa: https://bitpapa.com\n• 💬 Поддержка: @svenobmen'
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

    async def _run_query(self, query: str, params: tuple = ()):
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(query, params)
                if query.strip().upper().startswith("SELECT"):
                    return cur.fetchall()
                conn.commit()
                return None

    async def _run_insert(self, query: str, params: tuple = ()):
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                return cur.lastrowid

    # Клиенты
    async def add_client(self, user_id: int, username: str):
        await self._run_query(
            "INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)",
            (user_id, username, datetime.now().isoformat())
        )

    async def is_banned(self, user_id: int) -> Tuple[bool, Optional[str]]:
        rows = await self._run_query("SELECT is_banned, ban_reason FROM clients WHERE user_id = ?", (user_id,))
        if rows and rows[0]['is_banned'] == 1:
            return True, rows[0]['ban_reason']
        return False, None

    async def ban_user(self, user_id: int, reason: str):
        await self._run_query(
            "UPDATE clients SET is_banned = 1, ban_reason = ?, banned_at = ? WHERE user_id = ?",
            (reason, datetime.now().isoformat(), user_id)
        )

    async def unban_user(self, user_id: int):
        await self._run_query(
            "UPDATE clients SET is_banned = 0, ban_reason = NULL, banned_at = NULL WHERE user_id = ?",
            (user_id,)
        )

    async def get_banned_users(self):
        return await self._run_query("SELECT user_id, username, ban_reason, banned_at FROM clients WHERE is_banned = 1")

    async def update_client_after_deal(self, user_id: int, amount: float):
        await self._run_query(
            "UPDATE clients SET total_deals = total_deals + 1, total_volume = total_volume + ? WHERE user_id = ?",
            (amount, user_id)
        )

    async def get_client_stats(self, user_id: int):
        rows = await self._run_query(
            "SELECT total_deals, total_volume, avg_rating, ratings_count FROM clients WHERE user_id = ?",
            (user_id,)
        )
        return rows[0] if rows else None

    async def get_all_clients(self):
        return await self._run_query(
            "SELECT user_id, username, total_deals, total_volume FROM clients WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20"
        )

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        rows = await self._run_query("SELECT user_id FROM clients WHERE username = ?", (username,))
        return rows[0]['user_id'] if rows else None

    # Заявки
    async def add_request(self, user_id: int, operation_type: str, amount: float, client_total: float) -> int:
        return await self._run_insert("""
            INSERT INTO requests (user_id, operation_type, amount, client_total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, operation_type, amount, client_total, STATUS_PENDING, datetime.now().isoformat()))

    async def get_request(self, request_id: int):
        rows = await self._run_query("SELECT * FROM requests WHERE id = ?", (request_id,))
        return rows[0] if rows else None

    async def get_user_active_request(self, user_id: int):
        rows = await self._run_query("""
            SELECT id, operation_type, amount, client_total, status
            FROM requests
            WHERE user_id = ? AND status IN (?, ?, ?, ?)
            ORDER BY id DESC LIMIT 1
        """, (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0] if rows else None

    async def get_all_pending_requests(self):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at FROM requests WHERE status = ? ORDER BY created_at DESC",
            (STATUS_PENDING,)
        )

    async def get_all_processing_requests(self):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at FROM requests WHERE status IN (?, ?) ORDER BY created_at DESC",
            (STATUS_PROCESSING, STATUS_REQUISITES_SENT)
        )

    async def take_request(self, request_id: int):
        await self._run_query(
            "UPDATE requests SET status = ?, taken_at = ? WHERE id = ?",
            (STATUS_PROCESSING, datetime.now().isoformat(), request_id)
        )

    async def send_requisites(self, request_id: int, requisites_text: str):
        await self._run_query(
            "UPDATE requests SET status = ?, requisites_sent_at = ?, requisites_text = ? WHERE id = ?",
            (STATUS_REQUISITES_SENT, datetime.now().isoformat(), requisites_text, request_id)
        )

    async def mark_paid(self, request_id: int, pdf_file_id: str):
        await self._run_query(
            "UPDATE requests SET status = ?, paid_at = ?, pdf_file_id = ? WHERE id = ?",
            (STATUS_PAID, datetime.now().isoformat(), pdf_file_id, request_id)
        )

    async def complete_request(self, request_id: int, user_id: int, amount: float):
        await self._run_query(
            "UPDATE requests SET status = ?, completed_at = ? WHERE id = ?",
            (STATUS_COMPLETED, datetime.now().isoformat(), request_id)
        )
        await self.update_client_after_deal(user_id, amount)

    async def cancel_request(self, request_id: int, cancelled_by: str):
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        await self._run_query(
            "UPDATE requests SET status = ?, cancelled_at = ?, cancelled_by = ? WHERE id = ?",
            (status, datetime.now().isoformat(), cancelled_by, request_id)
        )

    # Отзывы
    async def add_feedback(self, user_id: int, request_id: int, rating: int = None, comment: str = None):
        await self._run_insert(
            "INSERT INTO feedback (user_id, request_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, request_id, rating, comment, datetime.now().isoformat())
        )
        rows = await self._run_query(
            "SELECT AVG(rating), COUNT(*) FROM feedback WHERE user_id = ? AND rating IS NOT NULL",
            (user_id,)
        )
        if rows and rows[0][0] is not None:
            await self._run_query(
                "UPDATE clients SET avg_rating = ?, ratings_count = ? WHERE user_id = ?",
                (rows[0][0], rows[0][1], user_id)
            )

    async def get_feedback_for_display(self, limit: int = 5, offset: int = 0):
        return await self._run_query("""
            SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at
            FROM feedback f
            JOIN clients c ON f.user_id = c.user_id
            WHERE f.is_displayed = 1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL)
            ORDER BY f.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

    async def get_feedback_count(self) -> int:
        rows = await self._run_query("SELECT COUNT(*) as count FROM feedback WHERE is_displayed = 1")
        return rows[0]['count'] if rows else 0

    async def get_avg_rating(self) -> float:
        rows = await self._run_query("SELECT AVG(rating) as avg FROM feedback WHERE rating IS NOT NULL")
        return rows[0]['avg'] if rows and rows[0]['avg'] is not None else 0.0

    # Настройки
    async def get_setting(self, key: str) -> Optional[str]:
        rows = await self._run_query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]['value'] if rows else None

    async def update_setting(self, key: str, value: str):
        await self._run_query("UPDATE settings SET value = ? WHERE key = ?", (value, key))


db = Database()

# ==================================================
# ================== КУРС ==========================
# ==================================================

cached_rate = None
cached_time = 0

async def get_usdt_rate():
    global cached_rate, cached_time
    now = datetime.now().timestamp()
    if cached_rate and (now - cached_time) < CACHE_TIME_SECONDS:
        return cached_rate
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cached_rate = float(data['price'])
                    cached_time = now
                    return cached_rate
    except Exception as e:
        logging.error(f"Error fetching rate: {e}")
    return 92.5

def calculate_client_total(amount: float) -> float:
    return (amount * 1.169) + 285

def extract_amount(text: str) -> Optional[float]:
    match = re.search(r"[\d\s]+(?:[.,]\d+)?", text.replace(" ", ""))
    if not match:
        return None
    amount_str = match.group(0).replace(",", ".")
    try:
        return float(amount_str)
    except:
        return None

# ==================================================
# ================== РАНГИ =========================
# ==================================================

def get_rank_and_discount(deals: int):
    if deals < 3:
        return ("Новичок", "🟢", 0, 3 - deals)
    if deals < 7:
        return ("Ходок", "🔵", 0, 7 - deals)
    if deals < 10:
        return ("Опытный", "🟠", 0, 10 - deals)
    if deals < 15:
        return ("Мастер", "🟣", 0, 15 - deals)
    return ("Легенда", "🔥", 1, 0)

def get_progress_bar(current: int, needed_for_next: int) -> str:
    if needed_for_next <= 0:
        return "▰" * 10
    # Допустим, для перехода к следующему рангу нужно N сделок. Показываем прогресс от 0 до N.
    # current - это общее число сделок. Нужно посчитать, сколько сделано в рамках текущего ранга.
    if needed_for_next < 0:
        needed_for_next = 0
    # Вычисляем процент выполнения: (сколько сделано / сколько нужно до следующего)
    # Так как current включает все предыдущие сделки, то прогресс = (текущие сделки в этом ранге / нужно)
    # Но проще сделать линейную шкалу от 0 до 10
    filled = int(10 * (current / max(current + needed_for_next, 1)))
    filled = min(max(filled, 0), 10)
    return "▰" * filled + "▱" * (10 - filled)

# ==================================================
# ================== КЛАВИАТУРЫ ====================
# ==================================================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 НОВЫЙ ЗАПРОС")],
        [KeyboardButton("⭐ ОТЗЫВЫ")],
        [KeyboardButton("📜 ПРАВИЛА"), KeyboardButton("👤 ПРОФИЛЬ")]
    ], resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 ЗАЯВКИ")],
        [KeyboardButton("⚙️ НАСТРОЙКИ")],
        [KeyboardButton("📊 СТАТИСТИКА")],
        [KeyboardButton("🚫 ЗАБАНЕННЫЕ")],
        [KeyboardButton("◀️ ВЫЙТИ")]
    ], resize_keyboard=True)

def get_operation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 ОПЛАТИТЬ СЧЁТ OXAPAY", callback_data="type_oxapay")],
        [InlineKeyboardButton("🏷️ СОЗДАТЬ ЧЕК BITPAPA", callback_data="type_bitpapa")],
        [InlineKeyboardButton("💰 КУПИТЬ КРИПТУ", callback_data="type_crypto")],
        [InlineKeyboardButton("🏪 ОТПРАВИТЬ НА КОШЕЛЁК МАГАЗИНА", callback_data="type_shop")]
    ])

def get_back_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ ОТМЕНА", callback_data="back_to_main")]])

def get_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ПОЛУЧИТЬ РЕКВИЗИТЫ", callback_data="get_requisites")],
        [InlineKeyboardButton("✏️ ИЗМЕНИТЬ", callback_data="edit_amount")],
        [InlineKeyboardButton("◀️ ОТМЕНА", callback_data="back_to_main")]
    ])

def get_cancel_keyboard(request_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 ОТМЕНИТЬ ЗАЯВКУ", callback_data=f"cancel_{request_id}")]
    ])

def get_rating_keyboard(request_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5⭐", callback_data=f"rate_{request_id}_5"),
         InlineKeyboardButton("4⭐", callback_data=f"rate_{request_id}_4"),
         InlineKeyboardButton("3⭐", callback_data=f"rate_{request_id}_3")],
        [InlineKeyboardButton("2⭐", callback_data=f"rate_{request_id}_2"),
         InlineKeyboardButton("1⭐", callback_data=f"rate_{request_id}_1")]
    ])

# ==================================================
# ================== СБРОС СОСТОЯНИЙ ===============
# ==================================================

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    keys_to_clear = ['awaiting_amount', 'temp_amount', 'operation_type', 'editing', 'editing_setting']
    for key in keys_to_clear:
        if key in context.user_data:
            del context.user_data[key]

# ==================================================
# ================== ОСНОВНЫЕ ФУНКЦИИ ===============
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return
    await db.add_client(user.id, user.username or str(user.id))
    banned, reason = await db.is_banned(user.id)
    if banned:
        await safe_send(update.message, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}", reply_markup=get_main_keyboard())
        return

    reset_request_flow(context)
    await safe_send(update.message,
                    f"👋 ПРИВЕТСТВУЮ, {user.first_name}!\n\nSVEN OBMEN — помощь с криптовалютными задачами.\n\n➡️ НАЧНИТЕ С КНОПКИ НИЖЕ ⬇️",
                    reply_markup=get_main_keyboard())

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    banned, reason = await db.is_banned(user_id)
    if banned:
        await safe_send(update.message, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}", reply_markup=get_main_keyboard())
        return

    if text == "🔥 НОВЫЙ ЗАПРОС":
        if afk_mode and user_id != ADMIN_ID:
            await safe_send(update.message, "😴 Бот временно не принимает новые заявки. Пожалуйста, зайдите позже.", reply_markup=get_main_keyboard())
            return

        active = await db.get_user_active_request(user_id)
        if active:
            await safe_send(update.message, f"⚠️ У вас уже есть активная заявка #{active['id']}. Сначала завершите или отмените её.", reply_markup=get_main_keyboard())
            return
        reset_request_flow(context)
        await safe_send(update.message, "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
        return

    if text == "⭐ ОТЗЫВЫ":
        # Не сбрасываем страницу, если она уже есть
        if 'reviews_page' not in context.user_data:
            context.user_data['reviews_page'] = 0
        await show_reviews(update, context)
        return
    if text == "📜 ПРАВИЛА":
        rules = await db.get_setting('rules')
        await safe_send(update.message, rules, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=get_main_keyboard())
        return
    if text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
        return

    # Admin Menu Check
    if user_id == ADMIN_ID:
        if text == "📋 ЗАЯВКИ":
            await show_requests_list(update, context)
            return
        if text == "⚙️ НАСТРОЙКИ":
            await show_settings(update, context)
            return
        if text == "📊 СТАТИСТИКА":
            await show_admin_stats(update, context)
            return
        if text == "🚫 ЗАБАНЕННЫЕ":
            await show_banned_users(update, context)
            return
        if text == "◀️ ВЫЙТИ":
            await safe_send(update.message, "🔐 Выход из админ-панели.", reply_markup=get_main_keyboard())
            return

    await safe_send(update.message, "Пожалуйста, используйте кнопки меню.", reply_markup=get_main_keyboard())

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    target_user_id = user_id or update.effective_user.id
    stats = await db.get_client_stats(target_user_id)

    if not stats or (stats['total_deals'] == 0 and stats['ratings_count'] == 0):
        await safe_send(update.message, f"👤 ПРОФИЛЬ\n\n📊 У вас пока нет завершённых сделок.",
                        reply_markup=get_main_keyboard())
        return

    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    rank_name, rank_emoji, discount, next_rank_deals = get_rank_and_discount(deals)

    progress_bar = get_progress_bar(deals, next_rank_deals)

    text = (f"👤 ПРОФИЛЬ\n\n"
            f"🏆 РАНГ: {rank_emoji} {rank_name}\n"
            f"📊 ПРОГРЕСС: {progress_bar}\n"
            f"💰 СКИДКА: {discount}%\n\n"
            f"📈 СТАТИСТИКА:\n• Сделок: {deals}\n• Объём: {volume:.0f} ₽\n• Рейтинг: ⭐ {rating:.1f} ({rating_count} отзывов)")
    await safe_send(update.message, text, reply_markup=get_main_keyboard())

async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = context.user_data.get('reviews_page', 0)
    limit = 5
    reviews = await db.get_feedback_for_display(limit, page * limit)
    total = await db.get_feedback_count()
    avg_rating = await db.get_avg_rating()

    if not reviews:
        if update.callback_query:
            await update.callback_query.answer("Больше отзывов нет.")
        else:
            await safe_send(update.message, "⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", reply_markup=get_main_keyboard())
        return

    text = f"⭐ ОТЗЫВЫ КЛИЕНТОВ\n\nВсего отзывов: {total}\nСредний рейтинг: {avg_rating:.1f} ⭐\n━━━━━━━━━━━━━\n\n"
    for r in reviews:
        stars = "⭐" * int(r['rating']) if r['rating'] else "📝"
        username = r['username'] or "User"
        text += f"👤 @{username}\n📅 {r['created_at'][:10]}\n"
        if r['comment']:
            text += f"💬 \"{r['comment']}\"\n"
        text += f"Оценка: {stars}\n━━━━━━━━━━━━━\n"

    kb = []
    if total > (page + 1) * limit:
        kb.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb.append(InlineKeyboardButton("◀️ В МЕНЮ", callback_data="back_to_menu_ui"))

    reply_markup = InlineKeyboardMarkup([kb])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    banned = await db.get_banned_users()
    if not banned:
        await safe_send(update.message, "🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", reply_markup=get_admin_keyboard())
        return
    text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
    for u in banned:
        text += f"👤 @{u['username']} (ID: {u['user_id']})\n📅 Забанен: {u['banned_at'][:10]}\n📝 Причина: {u['ban_reason']}\n━━━━━━━━━━━━━\n"
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    pending = await db.get_all_pending_requests()
    processing = await db.get_all_processing_requests()

    if not pending and not processing:
        await safe_send(update.message, "📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return

    text = "📋 АКТИВНЫЕ ЗАЯВКИ\n\n"
    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            text += f"  #{req['id']} | ID:{req['user_id']} | {req['amount']:.0f} ₽ | {req['created_at'][11:16]}\n"
        text += "\n"
    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            status_ico = "⏳" if req['status'] == STATUS_PROCESSING else "💳"
            text += f"  #{req['id']} | ID:{req['user_id']} | {req['amount']:.0f} ₽ | {status_ico}\n"

    text += "\n🔧 УПРАВЛЕНИЕ:\n/take <id>\n/send <id> <реквизиты>\n/confirm <id>\n/reject <id>\n/getpdf <id>"
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await safe_send(update.message,
                    "⚙️ НАСТРОЙКИ\n\n/edit_rules - Правила\n/edit_schedule - График\n/edit_links - Ссылки\n/afk on/off - Режим AFK",
                    reply_markup=get_admin_keyboard())

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    clients = await db.get_all_clients()
    avg_rating = await db.get_avg_rating()

    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)

    text = (f"📊 ОБЩАЯ СТАТИСТИКА\n\n"
            f"• Клиентов (активных): {len(clients)}\n"
            f"• Всего сделок: {total_deals}\n"
            f"• Общий объём: {total_volume:.0f} ₽\n"
            f"• Ср. рейтинг: ⭐ {avg_rating:.1f}\n"
            f"• Расч. прибыль (10%): {total_volume * 0.1:.0f} ₽")
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

# ==================================================
# ================== CALLBACK =======================
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    banned, _ = await db.is_banned(user_id)
    if banned:
        await query.edit_message_text("⛔ ДОСТУП ЗАБЛОКИРОВАН")
        return

    if data == "back_to_main":
        reset_request_flow(context)
        await query.edit_message_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
        return

    if data == "back_to_menu_ui":
        reset_request_flow(context)
        await query.message.delete()
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
        return

    if data == "reviews_next":
        context.user_data['reviews_page'] = context.user_data.get('reviews_page', 0) + 1
        await show_reviews(update, context)
        return

    if data.startswith("type_"):
        op_type = data[5:]
        mapping = {"oxapay": OPERATION_OXAPAY, "bitpapa": OPERATION_BITPAPA, "crypto": OPERATION_CRYPTO, "shop": OPERATION_SHOP}
        context.user_data['operation_type'] = mapping.get(op_type, OPERATION_OXAPAY)
        context.user_data['awaiting_amount'] = True
        await query.edit_message_text("💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\nМинимум: 1000 ₽. Просто напишите число (например, 5000).", reply_markup=get_back_inline())
        return

    if data == "edit_amount":
        context.user_data['awaiting_amount'] = True
        await query.edit_message_text("💰 ВВЕДИТЕ НОВУЮ СУММУ:", reply_markup=get_back_inline())
        return

    if data == "get_requisites":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        if not amount or not op_type:
            await query.edit_message_text("❌ Ошибка данных. Начните заново.", reply_markup=get_operation_keyboard())
            return

        client_total = calculate_client_total(amount)
        req_id = await db.add_request(user_id, op_type, amount, client_total)

        await query.edit_message_text(
            f"✅ ЗАЯВКА #{req_id} СОЗДАНА!\n\nТип: {op_type}\nСумма: {amount:.0f} ₽\nК оплате: {client_total:.0f} ₽\n\nСтатус: ОЖИДАЕТ ОБРАБОТКИ\n\nОператор скоро пришлёт реквизиты в этот чат.",
            reply_markup=get_cancel_keyboard(req_id))

        await context.bot.send_message(ADMIN_ID,
                                       f"🔔 НОВАЯ ЗАЯВКА #{req_id}\n👤 @{query.from_user.username or user_id}\n💰 {amount:.0f} ₽\n💸 {client_total:.0f} ₽\n✅ /take {req_id}")
        reset_request_flow(context)
        return

    if data.startswith("cancel_"):
        req_id = int(data.split("_")[1])
        req = await db.get_request(req_id)
        if req and req['status'] in [STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT]:
            await db.cancel_request(req_id, "user")
            await query.edit_message_text(f"✅ ЗАЯВКА #{req_id} ОТМЕНЕНА.")
            await context.bot.send_message(ADMIN_ID, f"⚠️ Пользователь отменил заявку #{req_id}")
        else:
            await query.answer("Эту заявку нельзя отменить.")
        return

    if data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) == 3:
            req_id, rating = int(parts[1]), int(parts[2])
            await db.add_feedback(user_id, req_id, rating, None)
            await query.edit_message_text(f"✅ Спасибо за оценку {rating}⭐! Это помогает нам становиться лучше.")
            # Отправляем пользователя в главное меню после оценки
            await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())

# ==================================================
# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
# ==================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id

    # Admin Setting Edit Logic
    if user_id == ADMIN_ID and context.user_data.get('editing_setting'):
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, update.message.text)
        await safe_send(update.message, f"✅ Настройка {key} успешно обновлена!", reply_markup=get_admin_keyboard())
        return

    # PDF Receipt Logic
    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active or active['status'] != STATUS_REQUISITES_SENT:
                await safe_send(update.message, "❌ У вас нет заявок в статусе ожидания оплаты.")
                return
            await db.mark_paid(active['id'], update.message.document.file_id)
            await safe_send(update.message, "✅ ЧЕК ПРИНЯТ!\nСтатус: 🔍 ПРОВЕРКА ОПЕРАТОРОМ\nОбычно это занимает 5-15 минут.")
            await context.bot.send_message(ADMIN_ID,
                                           f"💳 ПОЛУЧЕН ЧЕК\n👤 @{update.effective_user.username or user_id}\n📋 Заявка #{active['id']}\n📄 /getpdf {active['id']}\n✅ /confirm {active['id']}")
        else:
            await safe_send(update.message, "❌ Пожалуйста, отправьте чек именно в формате PDF.")
        return

    # Amount Input Logic
    if context.user_data.get('awaiting_amount'):
        amount = extract_amount(update.message.text)
        if not amount or amount < 1000:
            await safe_send(update.message, "❌ Ошибка. Введите корректную сумму (минимум 1000).")
            return

        client_total = calculate_client_total(amount)
        context.user_data['temp_amount'] = amount
        context.user_data['awaiting_amount'] = False

        await safe_send(update.message,
                        f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\n"
                        f"Тип: {context.user_data.get('operation_type')}\n"
                        f"Сумма: {amount:.0f} ₽\n"
                        f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n\n"
                        f"⚠️ Внимание: после получения реквизитов вы обязуетесь произвести оплату. Намеренный флуд заявками ведёт к блокировке.",
                        reply_markup=get_confirm_keyboard())
        return

    # Default Menu
    await handle_menu(update, context)

# ==================================================
# ================== КОМАНДЫ =======================
# ==================================================

async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Использование: /take <id>")

    try:
        req_id = int(context.args[0])
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PENDING:
            return await update.message.reply_text("Заявка не найдена или уже в работе.")

        await db.take_request(req_id)
        await update.message.reply_text(f"✅ Заявка #{req_id} взята. Теперь отправьте реквизиты: /send {req_id} <текст>")
        await context.bot.send_message(req['user_id'], f"✅ ЗАЯВКА #{req_id} ПРИНЯТА В РАБОТУ.\nОператор готовит реквизиты...")
    except:
        await update.message.reply_text("Ошибка в ID.")

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2: return await update.message.reply_text("Использование: /send <id> <реквизиты>")

    try:
        req_id = int(context.args[0])
        text = " ".join(context.args[1:])
        req = await db.get_request(req_id)
        if not req: return await update.message.reply_text("Заявка не найдена.")

        await db.send_requisites(req_id, text)
        await context.bot.send_message(req['user_id'],
                                       f"💳 ВАШИ РЕКВИЗИТЫ ПО ЗАЯВКЕ #{req_id}\n\n"
                                       f"💸 К ОПЛАТЕ: {req['client_total']:.0f} ₽\n"
                                       f"📋 РЕКВИЗИТЫ: {text}\n\n"
                                       f"⚠️ После оплаты ОБЯЗАТЕЛЬНО пришлите PDF чек в этот чат.",
                                       reply_markup=get_cancel_keyboard(req_id))
        await update.message.reply_text(f"✅ Реквизиты для #{req_id} отправлены.")
    except:
        await update.message.reply_text("Ошибка в команде.")

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Использование: /confirm <id>")

    try:
        req_id = int(context.args[0])
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PAID:
            return await update.message.reply_text("Заявка не в статусе PAID.")

        await db.complete_request(req_id, req['user_id'], req['amount'])
        await update.message.reply_text(f"✅ Заявка #{req_id} ЗАВЕРШЕНА.")
        await context.bot.send_message(
            req['user_id'],
            f"🎉 ЗАЯВКА #{req_id} УСПЕШНО ВЫПОЛНЕНА!\nСумма: {req['amount']:.0f} ₽ зачислена.\n\nБудем благодарны за отзыв:",
            reply_markup=get_rating_keyboard(req_id)
        )
    except:
        await update.message.reply_text("Ошибка в ID.")

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("Использование: /reject <id>")

    try:
        req_id = int(context.args[0])
        req = await db.get_request(req_id)
        if not req: return await update.message.reply_text("Заявка не найдена.")

        await db.cancel_request(req_id, "admin")
        await update.message.reply_text(f"❌ Заявка #{req_id} ОТКЛОНЕНА.")
        await context.bot.send_message(req['user_id'], f"❌ ЗАЯВКА #{req_id} ОТКЛОНЕНА.\nПричина: Чек не прошёл проверку или иная ошибка. Свяжитесь с поддержкой.")
    except:
        await update.message.reply_text("Ошибка.")

async def getpdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("/getpdf <id>")

    req_id = int(context.args[0])
    req = await db.get_request(req_id)
    if not req or not req['pdf_file_id']:
        return await update.message.reply_text("PDF не найден.")

    await context.bot.send_document(ADMIN_ID, req['pdf_file_id'], caption=f"Чек к заявке #{req_id}")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(context.args) < 2: return await update.message.reply_text("/ban @username <причина>")

    username = context.args[0].replace("@", "")
    reason = " ".join(context.args[1:])
    uid = await db.find_user_id_by_username(username)

    if not uid: return await update.message.reply_text("Пользователь не найден.")
    await db.ban_user(uid, reason)
    await update.message.reply_text(f"🚫 @{username} забанен.")
    await context.bot.send_message(uid, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("/unban @username")

    username = context.args[0].replace("@", "")
    uid = await db.find_user_id_by_username(username)
    if uid:
        await db.unban_user(uid)
        await update.message.reply_text(f"✅ @{username} разбанен.")
    else:
        await update.message.reply_text("Не найден.")

async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: return await update.message.reply_text("/afk on/off")

    afk_mode = context.args[0].lower() == "on"
    status = "ВКЛЮЧЕН (приём заявок закрыт)" if afk_mode else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"✅ Режим AFK: {status}")

async def edit_setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    cmd = update.message.text.split()[0].replace('/', '')
    setting_map = {'edit_rules': 'rules', 'edit_schedule': 'schedule', 'edit_links': 'links'}
    key = setting_map.get(cmd)
    if key:
        context.user_data['editing_setting'] = key
        current = await db.get_setting(key)
        await update.message.reply_text(f"📝 ТЕКУЩЕЕ ЗНАЧЕНИЕ '{key}':\n\n{current}\n\nВведите новое значение:")

async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

# ==================================================
# ================== ERROR HANDLER ==================
# ==================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")

# ==================================================
# ================== ЗАПУСК ========================
# ==================================================

def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel_cmd))
    app.add_handler(CommandHandler("take", take_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("confirm", confirm_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("getpdf", getpdf_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("afk", afk_command))
    app.add_handler(CommandHandler(["edit_rules", "edit_schedule", "edit_links"], edit_setting_start))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Message handler handles PDF, Amount Input, and Setting Edits
    app.add_handler(MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    print("✅ БОТ ЗАПУЩЕН (FULLY FIXED VERSION)")
    app.run_polling()

if __name__ == "__main__":
    main()