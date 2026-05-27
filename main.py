import re
import logging
import sqlite3
import aiohttp
import json
import asyncio
from datetime import datetime
from contextlib import contextmanager
from typing import Tuple, Optional, List, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, MessageHandler, filters, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

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
    """Экранирование спецсимволов для MarkdownV2"""
    chars = r'_*[]()~`>#+-=|{}.!'
    for ch in chars:
        text = text.replace(ch, f'\\{ch}')
    return text

async def safe_send(msg, text: str, **kwargs):
    """Безопасная отправка с MarkdownV2"""
    parse_mode = kwargs.pop('parse_mode', None)
    if parse_mode == ParseMode.MARKDOWN_V2:
        text = escape_md(text)
    await msg.reply_text(text, parse_mode=parse_mode, **kwargs)

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
            'rules': '📜 **ПРАВИЛА РАБОТЫ**\\n\\n• Минимальная сумма: 1000 ₽\\n• Комиссия: 10%\\n• Работаем 24/7\\n• Чек PDF обязателен\\n• Неоплата счёта влечёт блокировку',
            'schedule': '⏰ **ГРАФИК РАБОТЫ**\\n\\n• Пн–Вс: 24/7\\n• Без выходных',
            'links': '🔗 **ПОЛЕЗНЫЕ ССЫЛКИ**\\n\\n• 📢 Канал: https://t.me/svenobmen\\n• 📊 Bitpapa: https://bitpapa.com\\n• 💬 Поддержка: @svenobmen'
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

    @contextmanager
    def _get_cursor(self):
        with sqlite3.connect(self.db_file, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            yield conn.cursor()
            conn.commit()

    async def _run_query(self, query: str, params: tuple = ()):
        async with self._lock:
            with self._get_cursor() as cur:
                cur.execute(query, params)
                return cur.fetchall() if query.strip().upper().startswith("SELECT") else None

    async def _run_insert(self, query: str, params: tuple = ()):
        async with self._lock:
            with self._get_cursor() as cur:
                cur.execute(query, params)
                return cur.lastrowid

    # --------------------------------------------------
    # Клиенты
    async def add_client(self, user_id: int, username: str):
        await self._run_insert(
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

    # --------------------------------------------------
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

    # --------------------------------------------------
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
        if rows and rows[0][0]:
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
        rows = await self._run_query("SELECT COUNT(*) FROM feedback WHERE is_displayed = 1")
        return rows[0][0] if rows else 0

    async def get_avg_rating(self) -> float:
        rows = await self._run_query("SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL")
        return rows[0][0] if rows and rows[0][0] else 0.0

    # --------------------------------------------------
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
                    data = json.loads(await resp.text())
                    cached_rate = float(data['price'])
                    cached_time = now
                    return cached_rate
    except:
        pass
    return 92.5

def calculate_client_total(amount: float) -> float:
    return amount * 1.169 + 285

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

def get_progress_bar(current: int, target: int) -> str:
    if target <= 0:
        return "▰" * 10
    filled = int(10 * current / target)
    filled = min(filled, 10)
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
        [InlineKeyboardButton("🏪 ОТПРАВИТЬ НА КОШЕЛЁК МАГАЗИНА", callback_data="type_shop")],
        [InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main")]
    ])

def get_back_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main")]])

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

def get_rating_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5⭐", callback_data="rate_5"),
         InlineKeyboardButton("4⭐", callback_data="rate_4"),
         InlineKeyboardButton("3⭐", callback_data="rate_3")],
        [InlineKeyboardButton("2⭐", callback_data="rate_2"),
         InlineKeyboardButton("1⭐", callback_data="rate_1")]
    ])

# ==================================================
# ================== СБРОС СОСТОЯНИЙ ===============
# ==================================================

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in ['awaiting_amount', 'temp_amount', 'operation_type', 'editing', 'editing_setting']:
        if key in context.user_data:
            del context.user_data[key]

# ==================================================
# ================== ОСНОВНЫЕ ФУНКЦИИ ===============
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.add_client(user.id, user.username or str(user.id))
    banned, reason = await db.is_banned(user.id)
    if banned:
        await safe_send(update.message, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}", reply_markup=get_main_keyboard())
        return
    await safe_send(update.message,
                    f"👋 ПРИВЕТСТВУЮ, {user.first_name}!\n\nSVEN OBMEN — помощь с криптовалютными задачами.\n\n➡️ НАЧНИТЕ С КНОПКИ НИЖЕ ⬇️",
                    reply_markup=get_main_keyboard())

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    banned, _ = await db.is_banned(user_id)
    if banned:
        await safe_send(update.message, "⛔ ДОСТУП ЗАБЛОКИРОВАН", reply_markup=get_main_keyboard())
        return

    if text == "🔥 НОВЫЙ ЗАПРОС":
        active = await db.get_user_active_request(user_id)
        if active:
            await safe_send(update.message, f"⚠️ У вас уже есть активная заявка #{active['id']}.", reply_markup=get_main_keyboard())
            return
        reset_request_flow(context)
        await safe_send(update.message, "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
        return

    if text == "⭐ ОТЗЫВЫ":
        await show_reviews(update, context)
        return
    if text == "📜 ПРАВИЛА":
        rules = await db.get_setting('rules')
        await safe_send(update.message, rules, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=get_main_keyboard())
        return
    if text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
        return

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
        if text == "/admin":
            await safe_send(update.message, "🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())
            return

    await safe_send(update.message, "Используйте кнопки меню.", reply_markup=get_main_keyboard())

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    if not user_id:
        user_id = update.effective_user.id
    stats = await db.get_client_stats(user_id)
    user = update.effective_user
    if not stats or stats['total_deals'] == 0:
        await safe_send(update.message, f"👤 ПРОФИЛЬ | @{user.username or user_id}\n\n📊 У вас пока нет сделок.",
                        reply_markup=get_main_keyboard())
        return
    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    rank_name, rank_emoji, discount, next_rank = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals - (next_rank if next_rank > 0 else deals % 15), 10)
    text = (f"👤 ПРОФИЛЬ | @{user.username or user_id}\n\n"
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
        await safe_send(update.message, "⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", reply_markup=get_main_keyboard())
        return
    text = f"⭐ ОТЗЫВЫ КЛИЕНТОВ\n\nВсего отзывов: {total}\nСредний рейтинг: {avg_rating:.1f} ⭐\n━━━━━━━━━━━━━\n\n"
    for r in reviews:
        stars = "⭐" * (r['rating'] if r['rating'] else 0) if r['rating'] else "📝"
        text += f"👤 @{r['username']}\n📅 {r['created_at'][:10]}\n"
        if r['comment']:
            text += f"💬 \"{r['comment']}\"\n"
        if r['rating']:
            text += f"Оценка: {stars}\n"
        text += "━━━━━━━━━━━━━\n"
    kb = []
    if total > (page + 1) * limit:
        kb.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb.append(InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main"))
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([kb]))

async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    banned = await db.get_banned_users()
    if not banned:
        await safe_send(update.message, "🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", reply_markup=get_admin_keyboard())
        return
    text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
    for u in banned:
        text += f"👤 @{u['username'] or u['user_id']} (ID: {u['user_id']})\n📅 Забанен: {u['banned_at'][:10]}\n📝 Причина: {u['ban_reason']}\n━━━━━━━━━━━━━\n"
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    pending = await db.get_all_pending_requests()
    processing = await db.get_all_processing_requests()
    if not pending and not processing:
        await safe_send(update.message, "📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return
    text = "📋 ЗАЯВКИ\n\n"
    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            text += f"  #{req['id']} | @user_{req['user_id']} | {req['amount']:.0f} ₽ | {req['created_at'][:16]}\n"
        text += "\n"
    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            text += f"  #{req['id']} | @user_{req['user_id']} | {req['amount']:.0f} ₽\n"
    text += "\n➡️ /take <id> — взять в работу\n➡️ /send <id> <текст>\n➡️ /reject <id>\n➡️ /ban @username <причина>"
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await safe_send(update.message,
                    "⚙️ НАСТРОЙКИ\n\n/edit_rules\n/edit_schedule\n/edit_links\n/afk on/off",
                    reply_markup=get_admin_keyboard())

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    clients = await db.get_all_clients()
    total_clients = len(clients)
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)
    avg_rating = await db.get_avg_rating()
    text = (f"📊 СТАТИСТИКА\n\n• Клиентов: {total_clients}\n• Сделок: {total_deals}\n"
            f"• Объём: {total_volume:.0f} ₽\n• Рейтинг: ⭐ {avg_rating:.1f}\n• Прибыль (10%): {total_volume * 0.1:.0f} ₽")
    await safe_send(update.message, text, reply_markup=get_admin_keyboard())

# ==================================================
# ================== CALLBACK =======================
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    banned, _ = await db.is_banned(user_id)
    if banned and data not in ("back_to_main", "reviews_next"):
        await query.edit_message_text("⛔ ДОСТУП ЗАБЛОКИРОВАН")
        return

    if data == "back_to_main":
        await query.edit_message_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
        return
    if data == "reviews_next":
        context.user_data['reviews_page'] = context.user_data.get('reviews_page', 0) + 1
        await show_reviews(update, context)
        return
    if data.startswith("type_"):
        op_type = data[5:]
        mapping = {"oxapay": OPERATION_OXAPAY, "bitpapa": OPERATION_BITPAPA, "crypto": OPERATION_CRYPTO, "shop": OPERATION_SHOP}
        context.user_data['operation_type'] = mapping.get(op_type, OPERATION_OXAPAY)
        await query.edit_message_text("💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\n(Напишите число, например: 3000)", reply_markup=get_back_inline())
        context.user_data['awaiting_amount'] = True
        return
    if data == "edit_amount":
        await query.edit_message_text("💰 ВВЕДИТЕ НОВУЮ СУММУ", reply_markup=get_back_inline())
        context.user_data['awaiting_amount'] = True
        return
    if data == "get_requisites":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type', OPERATION_OXAPAY)
        if not amount:
            await query.edit_message_text("❌ Ошибка: сумма не найдена.", reply_markup=get_operation_keyboard())
            return
        client_total = calculate_client_total(amount)
        req_id = await db.add_request(user_id, op_type, amount, client_total)
        await query.edit_message_text(
            f"✅ ЗАЯВКА #{req_id} ПРИНЯТА!\n\nТип: {op_type}\nСумма: {amount:.0f} ₽\nК оплате: {client_total:.0f} ₽\n\nСтатус: ожидает обработки\n\nОператор скоро предоставит реквизиты.",
            reply_markup=get_cancel_keyboard(req_id))
        await context.bot.send_message(ADMIN_ID,
                                       f"🔔 НОВАЯ ЗАЯВКА #{req_id}\n👤 @{query.from_user.username or user_id} (ID:{user_id})\n💰 {amount:.0f} ₽\n💸 {client_total:.0f} ₽\n✅ /take {req_id}")
        reset_request_flow(context)
        return
    if data.startswith("cancel_"):
        req_id = int(data.split("_")[1])
        await db.cancel_request(req_id, "user")
        await query.edit_message_text(f"✅ ЗАЯВКА #{req_id} ОТМЕНЕНА", reply_markup=get_operation_keyboard())
        return
    if data.startswith("rate_"):
        rating = int(data.split("_")[1])
        rid = context.user_data.get('rating_request_id')
        if rid:
            await db.add_feedback(user_id, rid, rating, None)
            await query.edit_message_text(f"✅ Спасибо за оценку {rating}⭐!", reply_markup=get_main_keyboard())
            context.user_data.pop('rating_request_id', None)

# ==================================================
# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
# ==================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return
    user_id = update.effective_user.id

    banned, reason = await db.is_banned(user_id)
    if banned:
        await safe_send(update.message, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n{reason}", reply_markup=get_main_keyboard())
        return

    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active:
                await safe_send(update.message, "❌ Нет активной заявки.", reply_markup=get_main_keyboard())
                return
            await db.mark_paid(active['id'], update.message.document.file_id)
            await safe_send(update.message, "✅ ЧЕК ПОЛУЧЕН!\nСтатус: 🔍 проверка", reply_markup=get_main_keyboard())
            await context.bot.send_message(ADMIN_ID,
                                           f"🔔 PDF ЧЕК\n👤 @{update.effective_user.username or user_id}\n📋 Заявка #{active['id']}\n✅ /confirm {active['id']}")
        else:
            await safe_send(update.message, "❌ Отправьте чек в формате PDF.", reply_markup=get_main_keyboard())
        return

    if context.user_data.get('awaiting_amount'):
        amount = extract_amount(update.message.text)
        if not amount or amount < 1000:
            await safe_send(update.message, "❌ Введите сумму (минимум 1000 ₽).", reply_markup=get_main_keyboard())
            return
        client_total = calculate_client_total(amount)
        context.user_data['temp_amount'] = amount
        context.user_data['awaiting_amount'] = False
        await safe_send(update.message,
                        f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\nТип: {context.user_data.get('operation_type', OPERATION_OXAPAY)}\nСумма: {amount:.0f} ₽\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n\n⚠️ После получения реквизитов вы обязуетесь оплатить счёт. Неоплата = БАН.",
                        reply_markup=get_confirm_keyboard())
        return

    if context.user_data.get('awaiting_feedback'):
        comment = update.message.text
        rid = context.user_data.get('rating_request_id')
        if rid:
            await db.add_feedback(user_id, rid, None, comment)
            await safe_send(update.message, f"✅ Спасибо за отзыв!\n\"{comment}\"", reply_markup=get_main_keyboard())
            context.user_data.pop('awaiting_feedback', None)
            context.user_data.pop('rating_request_id', None)
        return

    await handle_menu(update, context)

# ==================================================
# ================== КОМАНДЫ =======================
# ==================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await safe_send(update.message, "⛔ Только админ", reply_markup=get_main_keyboard())
        return
    await safe_send(update.message, "🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ /take <id>")
        return
    try:
        req_id = int(context.args[0])
    except:
        await update.message.reply_text("❌ id должно быть числом")
        return
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PENDING:
        await update.message.reply_text("❌ Заявка не найдена или уже обработана")
        return
    await db.take_request(req_id)
    await update.message.reply_text(f"✅ Заявка #{req_id} взята\nТеперь /send {req_id} <реквизиты>")
    await context.bot.send_message(req['user_id'], f"✅ ЗАЯВКА #{req_id} ПРИНЯТА В РАБОТУ!\nСкоро получите реквизиты.")

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ /send <id> <текст реквизитов>")
        return
    req_id = int(context.args[0])
    text = " ".join(context.args[1:])
    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена")
        return
    await db.send_requisites(req_id, text)
    await context.bot.send_message(req['user_id'],
                                   f"✅ ЗАЯВКА #{req_id} | РЕКВИЗИТЫ\n💸 К ОПЛАТЕ: {req['client_total']:.0f} ₽\n📋 {text}\n\n⚠️ Неоплата = БАН\n📎 После оплаты пришлите PDF чек.", reply_markup=get_cancel_keyboard(req_id))
    await update.message.reply_text(f"✅ Реквизиты отправлены по заявке #{req_id}")

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ /confirm <id>")
        return
    req_id = int(context.args[0])
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PAID:
        await update.message.reply_text("❌ Заявка не в статусе PAID")
        return
    await db.complete_request(req_id, req['user_id'], req['amount'])
    await update.message.reply_text(f"✅ Заявка #{req_id} завершена")
    context.user_data['rating_request_id'] = req_id
    context.user_data['awaiting_feedback'] = True
    await context.bot.send_message(req['user_id'],
                                   f"✅ ЗАЯВКА #{req_id} ЗАВЕРШЕНА!\nСумма: {req['amount']:.0f} ₽\n\n⭐ Оцените нашу работу:\n\n✏️ Напишите отзыв или /skip",
                                   reply_markup=get_rating_keyboard())

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ /reject <id>")
        return
    req_id = int(context.args[0])
    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена")
        return
    await db.cancel_request(req_id, "admin")
    await update.message.reply_text(f"✅ Заявка #{req_id} отклонена")
    await context.bot.send_message(req['user_id'], f"❌ ЗАЯВКА #{req_id} ОТКЛОНЕНА\nПричина: чек не соответствует требованиям.")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ /ban @username <причина>")
        return
    username = context.args[0].replace("@", "")
    reason = " ".join(context.args[1:])
    uid = await db.find_user_id_by_username(username)
    if not uid:
        await update.message.reply_text("❌ Пользователь не найден")
        return
    await db.ban_user(uid, reason)
    await update.message.reply_text(f"✅ @{username} заблокирован")
    await context.bot.send_message(uid, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}\nПо вопросам: @svenobmen")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ /unban @username")
        return
    username = context.args[0].replace("@", "")
    uid = await db.find_user_id_by_username(username)
    if not uid:
        await update.message.reply_text("❌ Пользователь не найден")
        return
    await db.unban_user(uid)
    await update.message.reply_text(f"✅ @{username} разблокирован")
    await context.bot.send_message(uid, "✅ ДОСТУП ВОССТАНОВЛЕН\n/start")

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('awaiting_feedback', None)
    context.user_data.pop('rating_request_id', None)
    await safe_send(update.message, "✅ Отзыв пропущен.", reply_markup=get_main_keyboard())

async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing_setting'] = 'rules'
    await update.message.reply_text("📝 Введите новый текст правил (Markdown):")

async def edit_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing_setting'] = 'schedule'
    await update.message.reply_text("📝 Введите новый текст графика:")

async def edit_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing_setting'] = 'links'
    await update.message.reply_text("📝 Введите новый текст ссылок:")

async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if 'editing_setting' in context.user_data:
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, update.message.text)
        await update.message.reply_text(f"✅ {key} обновлены!", reply_markup=get_admin_keyboard())

async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ /afk on/off")
        return
    afk_mode = context.args[0].lower() == "on"
    await update.message.reply_text(f"✅ Режим «Не работаю» {'ВКЛЮЧЁН' if afk_mode else 'ВЫКЛЮЧЁН'}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID and context.args:
        username = context.args[0].replace("@", "")
        uid = await db.find_user_id_by_username(username)
        if uid:
            await show_profile(update, context, uid)
        else:
            await update.message.reply_text("❌ Клиент не найден")
        return
    await show_profile(update, context, user_id)

# ==================================================
# ================== ERROR HANDLER ==================
# ==================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(msg="Exception:", exc_info=context.error)
    try:
        if update and update.effective_message:
            await safe_send(update.effective_message, "⚠️ Внутренняя ошибка. Попробуйте позже.")
    except:
        pass

# ==================================================
# ================== ЗАПУСК ========================
# ==================================================

def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    for cmd in [start, stats_command, skip_command]:
        app.add_handler(CommandHandler(cmd.__name__.split('_')[0] if cmd.__name__ != 'start' else 'start', cmd))
    for cmd in [admin_command, take_command, send_command, confirm_command, reject_command, ban_command, unban_command, afk_command,
                edit_rules_command, edit_schedule_command, edit_links_command]:
        app.add_handler(CommandHandler(cmd.__name__.split('_')[0], cmd))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit))

    print("✅ БОТ ЗАПУЩЕН (FIXED version)")
    app.run_polling()

if __name__ == "__main__":
    main()