import re
import logging
import sqlite3
import aiohttp
import asyncio
from datetime import datetime
from functools import wraps
from typing import Tuple, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, MessageHandler, filters, CommandHandler,
    CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

# ==================================================
# ================== НАСТРОЙКИ ====================
# ==================================================

BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039

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

# Шаги диалога
ASKING_AMOUNT = 1
ASKING_LINK = 2
ASKING_FEEDBACK_COMMENT = 3

# Кнопки меню (для детектирования нажатий в состоянии ввода)
MENU_BUTTONS = {
    "🔥 НОВЫЙ ЗАПРОС", "⭐ ОТЗЫВЫ", "📜 ПРАВИЛА", "👤 ПРОФИЛЬ",
    "📋 ЗАЯВКИ", "⚙️ НАСТРОЙКИ", "📊 СТАТИСТИКА",
    "🚫 ЗАБАНЕННЫЕ", "◀️ ВЫЙТИ"
}

# ==================================================
# ================== УТИЛИТЫ ======================
# ==================================================

def format_user(username: Optional[str], user_id: int) -> str:
    return f"@{username}" if username else f"ID:{user_id}"

# ==================================================
# ================== БД ============================
# ==================================================

class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.db_file = db_file
        self._lock = asyncio.Lock()
        self._init_db()

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
                invoice_link TEXT,
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
            'rules': '📜 ПРАВИЛА РАБОТЫ\n\n• Минимальная сумма: 1000 ₽\n• Комиссия: 10%\n• Работаем 24/7',
            'schedule': '⏰ ГРАФИК РАБОТЫ\n\n• Пн–Вс: 24/7',
            'links': '🔗 ПОЛЕЗНЫЕ ССЫЛКИ\n\n• Канал: https://t.me/svenobmen',
            'afk_mode': '0',
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
        conn.commit()

    async def _run_query(self, query: str, params: tuple = ()):
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(query, params)
                return cur.fetchall()

    async def _run_execute(self, query: str, params: tuple = ()):
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.execute(query, params)
                conn.commit()

    async def _run_insert(self, query: str, params: tuple = ()):
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                return cur.lastrowid

    # --- Клиенты ---
    async def add_client(self, user_id: int, username: Optional[str]):
        await self._run_execute(
            "INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)",
            (user_id, username, datetime.now().isoformat())
        )

    async def is_banned(self, user_id: int) -> Tuple[bool, Optional[str]]:
        rows = await self._run_query(
            "SELECT is_banned, ban_reason FROM clients WHERE user_id = ?", (user_id,)
        )
        if rows and rows[0]['is_banned'] == 1:
            return True, rows[0]['ban_reason']
        return False, None

    async def ban_user(self, user_id: int, reason: str):
        await self._run_execute(
            "UPDATE clients SET is_banned=1, ban_reason=?, banned_at=? WHERE user_id=?",
            (reason, datetime.now().isoformat(), user_id)
        )

    async def unban_user(self, user_id: int):
        await self._run_execute(
            "UPDATE clients SET is_banned=0, ban_reason=NULL, banned_at=NULL WHERE user_id=?",
            (user_id,)
        )

    async def get_banned_users(self):
        return await self._run_query(
            "SELECT user_id, username, ban_reason, banned_at FROM clients WHERE is_banned=1"
        )

    async def update_client_after_deal(self, user_id: int, amount: float):
        await self._run_execute(
            "UPDATE clients SET total_deals=total_deals+1, total_volume=total_volume+? WHERE user_id=?",
            (amount, user_id)
        )

    async def get_client_stats(self, user_id: int):
        rows = await self._run_query(
            "SELECT total_deals, total_volume, avg_rating, ratings_count FROM clients WHERE user_id=?",
            (user_id,)
        )
        return rows[0] if rows else None

    async def get_all_clients(self):
        return await self._run_query(
            "SELECT user_id, username, total_deals, total_volume FROM clients "
            "WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20"
        )

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        rows = await self._run_query(
            "SELECT user_id FROM clients WHERE username=?", (username,)
        )
        return rows[0]['user_id'] if rows else None

    # --- Заявки ---
    async def add_request(
        self, user_id: int, operation_type: str,
        amount: float, client_total: float,
        invoice_link: str = None
    ) -> int:
        return await self._run_insert("""
            INSERT INTO requests
                (user_id, operation_type, amount, client_total, status, created_at, invoice_link)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, operation_type, amount, client_total,
              STATUS_PENDING, datetime.now().isoformat(), invoice_link))

    async def get_request(self, request_id: int):
        rows = await self._run_query("SELECT * FROM requests WHERE id=?", (request_id,))
        return rows[0] if rows else None

    async def get_user_active_request(self, user_id: int):
        # FIX #4: проверяем, что заявка принадлежит пользователю
        rows = await self._run_query("""
            SELECT id, operation_type, amount, client_total, status
            FROM requests
            WHERE user_id=? AND status IN (?,?,?,?)
            ORDER BY id DESC LIMIT 1
        """, (user_id, STATUS_PENDING, STATUS_PROCESSING,
              STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0] if rows else None

    async def get_all_pending_requests(self):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at, invoice_link "
            "FROM requests WHERE status=? ORDER BY created_at DESC",
            (STATUS_PENDING,)
        )

    async def get_all_processing_requests(self):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at, invoice_link "
            "FROM requests WHERE status IN (?,?) ORDER BY created_at DESC",
            (STATUS_PROCESSING, STATUS_REQUISITES_SENT)
        )

    async def take_request(self, request_id: int):
        await self._run_execute(
            "UPDATE requests SET status=?, taken_at=? WHERE id=?",
            (STATUS_PROCESSING, datetime.now().isoformat(), request_id)
        )

    async def send_requisites(self, request_id: int, requisites_text: str):
        await self._run_execute(
            "UPDATE requests SET status=?, requisites_sent_at=?, requisites_text=? WHERE id=?",
            (STATUS_REQUISITES_SENT, datetime.now().isoformat(), requisites_text, request_id)
        )

    async def mark_paid(self, request_id: int, pdf_file_id: str):
        await self._run_execute(
            "UPDATE requests SET status=?, paid_at=?, pdf_file_id=? WHERE id=?",
            (STATUS_PAID, datetime.now().isoformat(), pdf_file_id, request_id)
        )

    async def complete_request(self, request_id: int, user_id: int, amount: float):
        await self._run_execute(
            "UPDATE requests SET status=?, completed_at=? WHERE id=?",
            (STATUS_COMPLETED, datetime.now().isoformat(), request_id)
        )
        await self.update_client_after_deal(user_id, amount)

    async def cancel_request(self, request_id: int, cancelled_by: str, user_id: Optional[int] = None):
        # FIX #5: проверяем, что заявка принадлежит пользователю
        if cancelled_by == "user" and user_id is not None:
            req = await self.get_request(request_id)
            if not req or req['user_id'] != user_id:
                return False
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        await self._run_execute(
            "UPDATE requests SET status=?, cancelled_at=?, cancelled_by=? WHERE id=?",
            (status, datetime.now().isoformat(), cancelled_by, request_id)
        )
        return True

    # --- Отзывы ---
    async def add_feedback(
        self, user_id: int, request_id: int,
        rating: Optional[int] = None, comment: Optional[str] = None
    ):
        await self._run_insert(
            "INSERT INTO feedback (user_id, request_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, request_id, rating, comment, datetime.now().isoformat())
        )
        if rating is not None:
            rows = await self._run_query(
                "SELECT AVG(rating), COUNT(*) FROM feedback "
                "WHERE user_id=? AND rating IS NOT NULL",
                (user_id,)
            )
            if rows and rows[0][0] is not None:
                await self._run_execute(
                    "UPDATE clients SET avg_rating=?, ratings_count=? WHERE user_id=?",
                    (rows[0][0], rows[0][1], user_id)
                )

    # FIX #2: показываем ВСЕ отзывы (и с комментариями, и с рейтингом)
    async def get_feedback_for_display(self, limit: int = 5, offset: int = 0):
        return await self._run_query("""
            SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at
            FROM feedback f
            JOIN clients c ON f.user_id = c.user_id
            WHERE f.is_displayed=1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL)
            ORDER BY f.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

    # FIX #1: считаем ВСЕ отзывы (не только с рейтингом)
    async def get_feedback_count(self) -> int:
        rows = await self._run_query(
            "SELECT COUNT(*) as cnt FROM feedback WHERE is_displayed=1"
        )
        return rows[0]['cnt'] if rows else 0

    async def get_avg_rating(self) -> float:
        rows = await self._run_query(
            "SELECT AVG(rating) as avg FROM feedback WHERE rating IS NOT NULL"
        )
        return rows[0]['avg'] if rows and rows[0]['avg'] is not None else 0.0

    # --- Настройки ---
    async def get_setting(self, key: str) -> Optional[str]:
        rows = await self._run_query("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]['value'] if rows else None

    async def update_setting(self, key: str, value: str):
        await self._run_execute("UPDATE settings SET value=? WHERE key=?", (value, key))


db = Database()

# ==================================================
# ================== КУРС ==========================
# ==================================================

_rate_lock = asyncio.Lock()
_cached_rate: Optional[float] = None
_cached_time: float = 0.0

async def get_usdt_rate() -> float:
    global _cached_rate, _cached_time
    now = datetime.now().timestamp()
    if _cached_rate is not None and (now - _cached_time) < CACHE_TIME_SECONDS:
        return _cached_rate
    async with _rate_lock:
        if _cached_rate is not None and (now - _cached_time) < CACHE_TIME_SECONDS:
            return _cached_rate
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        _cached_rate = float(data['price'])
                        _cached_time = now
                        return _cached_rate
        except Exception as e:
            logging.error(f"Error fetching USDT rate: {e}")
        return _cached_rate if _cached_rate is not None else 92.5

def calculate_client_total(amount: float, discount_percent: float = 0) -> float:
    # FIX #3: скидка применяется к расчёту
    # Базовая комиссия 16.9% + 285 ₽
    # Скидка уменьшает комиссию на discount_percent%
    commission_rate = 1.169 - (discount_percent / 100)
    return (amount * commission_rate) + 285

# FIX #10: правильный парсинг суммы (поддержка 1000.00 и 1,000.00)
def extract_amount(text: str) -> Optional[float]:
    cleaned = text.strip().replace(" ", "")
    # Если есть запятая и точка — определяем формат
    if "," in cleaned and "." in cleaned:
        # Формат 1,000.00 -> убираем запятые
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned and "." not in cleaned:
        # Формат 1000,00 -> меняем запятую на точку
        cleaned = cleaned.replace(",", ".")
    match = re.fullmatch(r"(\d{1,10}(\.\d{1,2})?)", cleaned)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (ValueError, TypeError):
        return None

def is_valid_oxapay_link(link: str) -> bool:
    return (link.startswith("https://pay.oxapay.com/invoice/") or
            link.startswith("https://oxapay.com/invoice/"))

# ==================================================
# ================== AFK ===========================
# ==================================================

async def is_afk_mode() -> bool:
    val = await db.get_setting('afk_mode')
    return val == '1'

# ==================================================
# ================== РАНГИ =========================
# ==================================================

def get_rank_and_discount(deals: int):
    if deals < 3:  return ("Новичок", "🟢", 0,   3 - deals)
    if deals < 7:  return ("Ходок",   "🔵", 0.5, 7 - deals)
    if deals < 10: return ("Опытный", "🟠", 1,   10 - deals)
    if deals < 15: return ("Мастер",  "🟣", 1.5, 15 - deals)
    return ("Легенда", "🔥", 2, 0)

def get_progress_bar(current: int, needed: int) -> str:
    if needed <= 0:
        return "▰" * 10
    filled = min(max(int(10 * current / max(current + needed, 1)), 0), 10)
    return "▰" * filled + "▱" * (10 - filled)

# ==================================================
# ================== ДЕКОРАТОР ADMIN ===============
# ==================================================

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            if update.message:
                await update.message.reply_text("⛔ Недостаточно прав.")
            return
        return await func(update, context)
    return wrapper

# ==================================================
# ================== КЛАВИАТУРЫ ===================
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
    ])

def get_back_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ К ВЫБОРУ ТИПА", callback_data="back_to_type_select")],
        [InlineKeyboardButton("❌ ОТМЕНИТЬ И В МЕНЮ", callback_data="cancel_to_main")],
    ])

def get_confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ПОДТВЕРДИТЬ", callback_data="confirm_request")],
        [InlineKeyboardButton("✏️ ИЗМЕНИТЬ СУММУ", callback_data="edit_amount")],
        [InlineKeyboardButton("❌ ОТМЕНИТЬ", callback_data="cancel_to_main")],
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
         InlineKeyboardButton("1⭐", callback_data=f"rate_{request_id}_1")],
    ])

def get_help_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ КАК РАБОТАЕТ БОТ", callback_data="help_info")],
        [InlineKeyboardButton("🛠️ ДЛЯ АДМИНИСТРАТОРА", callback_data="help_admin")],
    ])

# ==================================================
# ================== СБРОС СОСТОЯНИЙ ===============
# ==================================================

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in ['step', 'temp_amount', 'operation_type', 'invoice_link',
                'feedback_req_id', 'feedback_rating']:
        context.user_data.pop(key, None)

# ==================================================
# ================== HANDLERS =====================
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await db.add_client(user.id, user.username)
    banned, reason = await db.is_banned(user.id)
    if banned:
        await update.message.reply_text(
            f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}",
            reply_markup=get_main_keyboard()
        )
        return
    reset_request_flow(context)
    await update.message.reply_text(
        f"👋 ПРИВЕТСТВУЮ, {user.first_name}!\n\n"
        "SVEN OBMEN — помощь с криптовалютными задачами.\n\n"
        "➡️ НАЧНИТЕ С КНОПКИ НИЖЕ ⬇️",
        reply_markup=get_main_keyboard()
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    banned, reason = await db.is_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}",
            reply_markup=get_main_keyboard()
        )
        return

    if text == "🔥 НОВЫЙ ЗАПРОС":
        if await is_afk_mode() and user_id != ADMIN_ID:
            await update.message.reply_text(
                "😴 Бот временно не принимает новые заявки.",
                reply_markup=get_main_keyboard()
            )
            return
        active = await db.get_user_active_request(user_id)
        if active:
            await update.message.reply_text(
                f"⚠️ У вас уже есть активная заявка #{active['id']}. "
                "Сначала завершите или отмените её.",
                reply_markup=get_main_keyboard()
            )
            return
        reset_request_flow(context)
        await update.message.reply_text(
            "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:",
            reply_markup=get_operation_keyboard()
        )
        return

    if text == "⭐ ОТЗЫВЫ":
        context.user_data['reviews_page'] = 0
        await show_reviews(update, context)
        return

    if text == "📜 ПРАВИЛА":
        rules = await db.get_setting('rules') or "Правила не заданы."
        await update.message.reply_text(rules, reply_markup=get_main_keyboard())
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
            await update.message.reply_text(
                "🔐 Выход из админ-панели.",
                reply_markup=get_main_keyboard()
            )
            return

    await update.message.reply_text(
        "Пожалуйста, используйте кнопки меню.",
        reply_markup=get_main_keyboard()
    )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    stats = await db.get_client_stats(user_id)
    if not stats or (stats['total_deals'] == 0 and stats['ratings_count'] == 0):
        await update.message.reply_text(
            "👤 ПРОФИЛЬ\n\n📊 У вас пока нет завершённых сделок.",
            reply_markup=get_main_keyboard()
        )
        return
    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    rank_name, rank_emoji, discount, next_rank_deals = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals, next_rank_deals)
    text = (
        f"👤 ПРОФИЛЬ\n\n"
        f"🏆 РАНГ: {rank_emoji} {rank_name}\n"
        f"📊 ПРОГРЕСС: {progress_bar}\n"
        f"💰 СКИДКА: {discount}%\n\n"
        f"📈 СТАТИСТИКА:\n"
        f"• Сделок: {deals}\n"
        f"• Объём: {volume:.0f} ₽\n"
        f"• Рейтинг: ⭐ {rating:.1f} ({rating_count} отзывов)"
    )
    await update.message.reply_text(text, reply_markup=get_main_keyboard())

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
            await update.message.reply_text(
                "⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!",
                reply_markup=get_main_keyboard()
            )
        return

    text = (f"⭐ ОТЗЫВЫ КЛИЕНТОВ\n\n"
            f"Всего отзывов: {total} | Средний рейтинг: {avg_rating:.1f} ⭐\n"
            f"━━━━━━━━━━━━━\n\n")
    for r in reviews:
        stars = "⭐" * int(r['rating']) if r['rating'] else "📝"
        username = r['username'] or "User"
        text += f"👤 @{username}\n📅 {r['created_at'][:10]}\n"
        if r['comment']:
            text += f'💬 "{r["comment"]}"\n'
        text += f"Оценка: {stars}\n━━━━━━━━━━━━━\n"

    kb_row = []
    if total > (page + 1) * limit:
        kb_row.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb_row.append(InlineKeyboardButton("◀️ В МЕНЮ", callback_data="back_to_menu_ui"))
    reply_markup = InlineKeyboardMarkup([kb_row])

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            user_id = update.callback_query.from_user.id
            await context.bot.send_message(user_id, text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

@admin_only
async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    banned = await db.get_banned_users()
    if not banned:
        await update.message.reply_text(
            "🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.",
            reply_markup=get_admin_keyboard()
        )
        return
    text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
    for u in banned:
        text += (f"👤 {format_user(u['username'], u['user_id'])} (ID: {u['user_id']})\n"
                 f"📅 {u['banned_at'][:10]}\n"
                 f"📝 {u['ban_reason']}\n━━━━━━━━━━━━━\n")
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await db.get_all_pending_requests()
    processing = await db.get_all_processing_requests()
    if not pending and not processing:
        await update.message.reply_text("📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return
    text = "📋 АКТИВНЫЕ ЗАЯВКИ\n\n"
    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            # FIX #11: проверка на None перед срезом
            link_info = f"\n    🔗 {req['invoice_link'][:50]}..." if req.get('invoice_link') else ""
            text += f"  #{req['id']} | ID:{req['user_id']} | {req['amount']:.0f} ₽ | {req['created_at'][11:16]}{link_info}\n"
        text += "\n"
    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            ico = "⏳" if req['status'] == STATUS_PROCESSING else "💳"
            link_info = f"\n    🔗 {req['invoice_link'][:50]}..." if req.get('invoice_link') else ""
            text += f"  #{req['id']} | ID:{req['user_id']} | {req['amount']:.0f} ₽ | {ico}{link_info}\n"
    text += "\n🔧 /take <id> | /send <id> <реквизиты> | /confirm <id> | /reject <id> | /getpdf <id>"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ НАСТРОЙКИ\n\n/edit_rules - Правила\n/edit_schedule - График\n"
        "/edit_links - Ссылки\n/afk on/off - Режим AFK",
        reply_markup=get_admin_keyboard()
    )

@admin_only
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clients = await db.get_all_clients()
    avg_rating = await db.get_avg_rating()
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)
    text = (
        f"📊 ОБЩАЯ СТАТИСТИКА\n\n"
        f"• Клиентов (активных): {len(clients)}\n"
        f"• Всего сделок: {total_deals}\n"
        f"• Общий объём: {total_volume:.0f} ₽\n"
        f"• Ср. рейтинг: ⭐ {avg_rating:.1f}\n"
        f"• Расч. прибыль (~10%): {total_volume * 0.1:.0f} ₽"
    )
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

# ==================================================
# ================== CALLBACK =====================
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    banned, _ = await db.is_banned(user_id)
    if banned:
        await query.edit_message_text("⛔ ДОСТУП ЗАБЛОКИРОВАН")
        return

    if data == "back_to_type_select":
        reset_request_flow(context)
        await query.edit_message_text(
            "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:",
            reply_markup=get_operation_keyboard()
        )
        return

    if data == "cancel_to_main":
        reset_request_flow(context)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
        return

    if data == "back_to_menu_ui":
        reset_request_flow(context)
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
        return

    if data == "reviews_next":
        context.user_data['reviews_page'] = context.user_data.get('reviews_page', 0) + 1
        await show_reviews(update, context)
        return

    if data.startswith("type_"):
        op_key = data[5:]
        mapping = {
            "oxapay": OPERATION_OXAPAY,
            "bitpapa": OPERATION_BITPAPA,
            "crypto": OPERATION_CRYPTO,
            "shop": OPERATION_SHOP,
        }
        op_type = mapping.get(op_key, OPERATION_OXAPAY)
        context.user_data['operation_type'] = op_type
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text(
            "💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\nМинимум: 1000 ₽.",
            reply_markup=get_back_inline()
        )
        return

    if data == "edit_amount":
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text(
            "💰 ВВЕДИТЕ НОВУЮ СУММУ:",
            reply_markup=get_back_inline()
        )
        return

    if data == "confirm_request":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        invoice_link = context.user_data.get('invoice_link')
        if not amount or not op_type:
            await query.edit_message_text(
                "❌ Ошибка данных. Начните заново.",
                reply_markup=get_operation_keyboard()
            )
            return
        # FIX #3: получаем скидку пользователя и применяем к расчёту
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount_percent, _ = get_rank_and_discount(deals)
        client_total = calculate_client_total(amount, discount_percent)
        req_id = await db.add_request(user_id, op_type, amount, client_total, invoice_link)
        msg_text = (
            f"✅ ЗАЯВКА #{req_id} СОЗДАНА!\n\n"
            f"Тип: {op_type}\nСумма: {amount:.0f} ₽\n"
            f"К оплате: {client_total:.0f} ₽\n"
            + (f"🔗 {invoice_link}\n" if invoice_link else "")
            + "\nСтатус: ОЖИДАЕТ ОБРАБОТКИ\n"
            "Оператор скоро пришлёт реквизиты."
        )
        await query.edit_message_text(msg_text, reply_markup=get_cancel_keyboard(req_id))
        admin_msg = (
            f"🔔 НОВАЯ ЗАЯВКА #{req_id}\n"
            f"👤 {format_user(query.from_user.username, user_id)}\n"
            f"💰 {amount:.0f} ₽ | 💸 {client_total:.0f} ₽\n"
            f"✅ /take {req_id}"
        )
        if invoice_link:
            admin_msg += f"\n🔗 {invoice_link}"
        await context.bot.send_message(ADMIN_ID, admin_msg)
        reset_request_flow(context)
        return

    # FIX #5: проверяем владельца заявки при отмене
    if data.startswith("cancel_"):
        try:
            req_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            await query.answer("Некорректный запрос.")
            return
        success = await db.cancel_request(req_id, "user", user_id)
        if success:
            await query.edit_message_text(f"✅ ЗАЯВКА #{req_id} ОТМЕНЕНА.")
            await context.bot.send_message(
                ADMIN_ID, f"⚠️ Пользователь отменил заявку #{req_id}"
            )
        else:
            await query.answer("Нельзя отменить чужую заявку или заявка не найдена.")
        return

    # FIX #8: запрашиваем комментарий после оценки
    if data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                req_id, rating = int(parts[1]), int(parts[2])
            except ValueError:
                await query.answer("Некорректные данные.")
                return
            context.user_data['feedback_req_id'] = req_id
            context.user_data['feedback_rating'] = rating
            context.user_data['step'] = ASKING_FEEDBACK_COMMENT
            await query.edit_message_text(
                f"Вы поставили {rating}⭐\n\n"
                "Оставьте комментарий или отправьте /skip для пропуска:"
            )
        return

    # FIX #13: помощь
    if data == "help_info":
        help_text = (
            "ℹ️ **КАК РАБОТАЕТ БОТ**\n\n"
            "1. Нажмите «НОВЫЙ ЗАПРОС»\n"
            "2. Выберите тип операции\n"
            "3. Введите сумму\n"
            "4. Для OxaPay дополнительно введите ссылку\n"
            "5. Подтвердите данные\n"
            "6. Оператор вышлет реквизиты\n"
            "7. Оплатите и пришлите PDF-чек\n"
            "8. После подтверждения получите USDT"
        )
        await query.edit_message_text(help_text, parse_mode="Markdown", reply_markup=get_help_keyboard())
        return

    if data == "help_admin":
        if user_id == ADMIN_ID:
            help_admin_text = (
                "🛠️ **КОМАНДЫ АДМИНИСТРАТОРА**\n\n"
                "/take <id> – взять заявку\n"
                "/send <id> <реквизиты> – отправить реквизиты\n"
                "/confirm <id> – подтвердить оплату\n"
                "/reject <id> – отклонить заявку\n"
                "/getpdf <id> – скачать чек\n"
                "/getlink <id> – посмотреть ссылку OxaPay\n"
                "/ban @username <причина> – заблокировать\n"
                "/unban @username – разблокировать\n"
                "/afk on/off – режим ожидания\n"
                "/edit_rules – изменить правила\n"
                "/edit_schedule – изменить график\n"
                "/edit_links – изменить ссылки"
            )
            await query.edit_message_text(help_admin_text, parse_mode="Markdown", reply_markup=get_help_keyboard())
        else:
            await query.answer("Эта информация только для администратора", show_alert=True)
        return

# ==================================================
# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
# ==================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX #6: защита от None
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    msg_text = update.message.text or ""

    # Редактирование настроек (только для админа)
    if user_id == ADMIN_ID and context.user_data.get('editing_setting'):
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, msg_text)
        await update.message.reply_text(
            f"✅ Настройка '{key}' обновлена!",
            reply_markup=get_admin_keyboard()
        )
        return

    # PDF-чек
    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active or active['status'] != STATUS_REQUISITES_SENT:
                await update.message.reply_text(
                    "❌ У вас нет заявок в статусе ожидания оплаты."
                )
                return
            await db.mark_paid(active['id'], update.message.document.file_id)
            await update.message.reply_text(
                "✅ ЧЕК ПРИНЯТ!\nСтатус: 🔍 ПРОВЕРКА ОПЕРАТОРОМ\n"
                "Обычно это занимает 5–15 минут."
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"💳 ПОЛУЧЕН ЧЕК\n"
                f"👤 {format_user(update.effective_user.username, user_id)}\n"
                f"📋 Заявка #{active['id']}\n"
                f"📄 /getpdf {active['id']}\n"
                f"✅ /confirm {active['id']}"
            )
        else:
            await update.message.reply_text(
                "❌ Пожалуйста, отправьте чек именно в формате PDF."
            )
        return

    # FIX #8: ожидание комментария к отзыву
    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        req_id = context.user_data.pop('feedback_req_id', None)
        rating = context.user_data.pop('feedback_rating', None)
        context.user_data.pop('step', None)
        comment = msg_text if msg_text != '/skip' else None
        if req_id and rating:
            await db.add_feedback(user_id, req_id, rating, comment)
        await update.message.reply_text(
            "✅ Спасибо за отзыв!", reply_markup=get_main_keyboard()
        )
        return

    # FIX #3: сброс при нажатии кнопки меню в состоянии ввода
    if context.user_data.get('step') == ASKING_AMOUNT:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        amount = extract_amount(msg_text)
        if not amount or amount < 1000:
            await update.message.reply_text(
                "❌ Введите корректную сумму (минимум 1000 ₽).",
                reply_markup=get_back_inline()
            )
            return
        context.user_data['temp_amount'] = amount
        op_type = context.user_data.get('operation_type')
        if op_type == OPERATION_OXAPAY:
            context.user_data['step'] = ASKING_LINK
            await update.message.reply_text(
                "🔗 ВВЕДИТЕ ССЫЛКУ НА СЧЁТ OXAPAY\n\n"
                "Пример: https://pay.oxapay.com/invoice/xxxxxxxx",
                reply_markup=get_back_inline()
            )
        else:
            # Получаем скидку пользователя
            stats = await db.get_client_stats(user_id)
            deals = stats['total_deals'] if stats else 0
            _, _, discount_percent, _ = get_rank_and_discount(deals)
            client_total = calculate_client_total(amount, discount_percent)
            await update.message.reply_text(
                f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\n"
                f"Тип: {op_type}\n"
                f"Сумма: {amount:.0f} ₽\n"
                f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n\n"
                "⚠️ После получения реквизитов вы обязуетесь произвести оплату.",
                reply_markup=get_confirm_keyboard()
            )
            context.user_data.pop('step', None)
        return

    # Сброс при нажатии кнопки меню в состоянии ввода ссылки
    if context.user_data.get('step') == ASKING_LINK:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        link = msg_text.strip()
        if not is_valid_oxapay_link(link):
            await update.message.reply_text(
                "❌ Неверный формат ссылки.\n\nСсылка должна начинаться с:\n"
                "https://pay.oxapay.com/invoice/\nили\nhttps://oxapay.com/invoice/",
                reply_markup=get_back_inline()
            )
            return
        context.user_data['invoice_link'] = link
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount_percent, _ = get_rank_and_discount(deals)
        client_total = calculate_client_total(amount, discount_percent)
        await update.message.reply_text(
            f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\n"
            f"Тип: {op_type}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n"
            f"🔗 Ссылка: {link}\n\n"
            "⚠️ После подтверждения вы обязуетесь оплатить счёт.",
            reply_markup=get_confirm_keyboard()
        )
        context.user_data.pop('step', None)
        return

    await handle_menu(update, context)

# ==================================================
# ================== КОМАНДЫ АДМИНА ================
# ==================================================

@admin_only
async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Использование: /take <id>")
    try:
        req_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID должен быть числом.")
    try:
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PENDING:
            return await update.message.reply_text("Заявка не найдена или уже в работе.")
        await db.take_request(req_id)
        await update.message.reply_text(
            f"✅ Заявка #{req_id} взята. /send {req_id} <реквизиты>"
        )
        msg = f"✅ ЗАЯВКА #{req_id} ПРИНЯТА В РАБОТУ.\nОператор готовит реквизиты..."
        if req.get('invoice_link'):
            msg += f"\n\n🔗 Счёт: {req['invoice_link']}"
        await context.bot.send_message(req['user_id'], msg)
    except Exception as e:
        logging.exception(f"take_command error: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка.")

@admin_only
async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /send <id> <реквизиты>")
    try:
        req_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID должен быть числом.")
    try:
        # FIX #8: проверяем статус заявки
        req = await db.get_request(req_id)
        if not req:
            return await update.message.reply_text("Заявка не найдена.")
        if req['status'] not in (STATUS_PROCESSING, STATUS_PENDING):
            return await update.message.reply_text(f"Заявка #{req_id} уже в статусе {req['status']}")
        text = " ".join(context.args[1:])
        await db.send_requisites(req_id, text)
        msg = (f"💳 ВАШИ РЕКВИЗИТЫ ПО ЗАЯВКЕ #{req_id}\n\n"
               f"💸 К ОПЛАТЕ: {req['client_total']:.0f} ₽\n"
               f"📋 РЕКВИЗИТЫ: {text}\n\n"
               "⚠️ После оплаты пришлите PDF чек.")
        if req.get('invoice_link'):
            msg += f"\n\n🔗 Счёт: {req['invoice_link']}"
        await context.bot.send_message(
            req['user_id'], msg, reply_markup=get_cancel_keyboard(req_id)
        )
        await update.message.reply_text(f"✅ Реквизиты для #{req_id} отправлены.")
    except Exception as e:
        logging.exception(f"send_command error: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка.")

@admin_only
async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Использование: /confirm <id>")
    try:
        req_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID должен быть числом.")
    try:
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PAID:
            return await update.message.reply_text("Заявка не в статусе PAID.")
        await db.complete_request(req_id, req['user_id'], req['amount'])
        await update.message.reply_text(f"✅ Заявка #{req_id} ЗАВЕРШЕНА.")
        await context.bot.send_message(
            req['user_id'],
            f"🎉 ЗАЯВКА #{req_id} УСПЕШНО ВЫПОЛНЕНА!\n"
            f"Сумма: {req['amount']:.0f} ₽ зачислена.\n\nОцените нашу работу:",
            reply_markup=get_rating_keyboard(req_id)
        )
    except Exception as e:
        logging.exception(f"confirm_command error: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка.")

@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Использование: /reject <id>")
    try:
        req_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID должен быть числом.")
    try:
        req = await db.get_request(req_id)
        if not req:
            return await update.message.reply_text("Заявка не найдена.")
        await db.cancel_request(req_id, "admin")
        await update.message.reply_text(f"❌ Заявка #{req_id} ОТКЛОНЕНА.")
        await context.bot.send_message(
            req['user_id'],
            f"❌ ЗАЯВКА #{req_id} ОТКЛОНЕНА.\n"
            "Причина: Чек не прошёл проверку. Свяжитесь с поддержкой."
        )
    except Exception as e:
        logging.exception(f"reject_command error: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка.")

@admin_only
async def getpdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/getpdf <id>")
    try:
        req_id = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("❌ ID должен быть числом.")
    try:
        req = await db.get_request(req_id)
        if not req or not req['pdf_file_id']:
            return await update.message.reply_text("PDF не найден.")
        await context.bot.send_document(
            ADMIN_ID, req['pdf_file_id'],
            caption=f"Чек к заявке #{req_id}"
        )
    except Exception as e:
        logging.exception(f"getpdf_command error: {e}")
        await update.message.reply_text("❌ Ошибка при получении PDF.")

@admin_only
async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/getlink <id>")
    try:
        req_id = int(context.args[0])
        req = await db.get_request(req_id)
        if not req or not req.get('invoice_link'):
            return await update.message.reply_text("Ссылка не найдена.")
        await update.message.reply_text(
            f"🔗 Ссылка по заявке #{req_id}:\n{req['invoice_link']}"
        )
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
    except Exception as e:
        logging.exception(f"getlink_command error: {e}")
        await update.message.reply_text("❌ Внутренняя ошибка.")

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("/ban @username <причина>")
    username = context.args[0].replace("@", "")
    reason = " ".join(context.args[1:])
    uid = await db.find_user_id_by_username(username)
    if not uid:
        return await update.message.reply_text("Пользователь не найден.")
    await db.ban_user(uid, reason)
    await update.message.reply_text(f"🚫 @{username} забанен.")
    try:
        await context.bot.send_message(uid, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}")
    except Exception:
        pass

@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/unban @username")
    username = context.args[0].replace("@", "")
    uid = await db.find_user_id_by_username(username)
    if uid:
        await db.unban_user(uid)
        await update.message.reply_text(f"✅ @{username} разбанен.")
    else:
        await update.message.reply_text("Пользователь не найден.")

@admin_only
async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/afk on/off")
    enabled = context.args[0].lower() == "on"
    await db.update_setting('afk_mode', '1' if enabled else '0')
    status = "ВКЛЮЧЕН (приём заявок закрыт)" if enabled else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"✅ Режим AFK: {status}")

@admin_only
async def edit_setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.split()[0].replace('/', '')
    setting_map = {
        'edit_rules': 'rules',
        'edit_schedule': 'schedule',
        'edit_links': 'links'
    }
    key = setting_map.get(cmd)
    if key:
        context.user_data['editing_setting'] = key
        current = await db.get_setting(key) or ""
        await update.message.reply_text(
            f"📝 ТЕКУЩЕЕ ЗНАЧЕНИЕ '{key}':\n\n{current}\n\nВведите новое значение:"
        )

@admin_only
async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """FIX #13: базовая справка для пользователей"""
    help_text = (
        "ℹ️ **СПРАВКА**\n\n"
        "Основные команды:\n"
        "/start – запуск бота\n"
        "/stats – профиль и статистика\n"
        "/cancel – отмена активной заявки\n"
        "/skip – пропустить отзыв\n\n"
        "Используйте кнопки меню для навигации."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропуск отзыва"""
    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        context.user_data.pop('step', None)
        context.user_data.pop('feedback_req_id', None)
        context.user_data.pop('feedback_rating', None)
        await update.message.reply_text("✅ Отзыв пропущен.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Нет активного запроса отзыва.", reply_markup=get_main_keyboard())

# ==================================================
# ================== ERROR HANDLER =================
# ==================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.exception(f"Update {update} caused error", exc_info=context.error)

# ==================================================
# ================== ЗАПУСК =======================
# ==================================================

def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ]
    )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("admin", admin_panel_cmd))
    app.add_handler(CommandHandler("take", take_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("confirm", confirm_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("getpdf", getpdf_command))
    app.add_handler(CommandHandler("getlink", getlink_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("afk", afk_command))
    app.add_handler(CommandHandler(
        ["edit_rules", "edit_schedule", "edit_links"], edit_setting_start
    ))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.TEXT & ~filters.COMMAND,
        handle_message
    ))
    app.add_error_handler(error_handler)

    logging.info("✅ БОТ ЗАПУЩЕН (FULLY FIXED VERSION)")
    app.run_polling()

if __name__ == "__main__":
    main()