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
from telegram.error import Forbidden

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

ACTIVE_REQUEST_STATUSES = (
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_REQUISITES_SENT,
    STATUS_PAID
)

OPERATION_OXAPAY = "Оплата счёта OxaPay"
OPERATION_BITPAPA = "Создание чека Bitpapa"
OPERATION_CRYPTO = "Покупка крипты на кошелёк"
OPERATION_SHOP = "Отправка на кошелёк магазина"

# Шаги диалога
ASKING_AMOUNT = 1
ASKING_LINK = 2
ASKING_FEEDBACK_COMMENT = 3

# ==================================================
# ================== УТИЛИТЫ ======================
# ==================================================

def format_user(username: Optional[str], user_id: int) -> str:
    return f"@{username}" if username else f"ID:{user_id}"

def extract_amount(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = text.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if match:
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            pass
    return None

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
            self._migrate_db(conn)
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

    def _migrate_db(self, conn):
        cursor = conn.execute("PRAGMA table_info(requests)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'invoice_link' not in columns:
            conn.execute("ALTER TABLE requests ADD COLUMN invoice_link TEXT")
            conn.commit()

    def _init_settings(self, conn):
        defaults = {
            'rules': '📜 ПРАВИЛА РАБОТЫ\n\n• Минимальная сумма: 1000 ₽\n• Работаем 24/7',
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
            "SELECT is_banned, ban_reason FROM clients WHERE user_id = ?",
            (user_id,)
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
            "SELECT user_id, username, total_deals, total_volume, avg_rating, ratings_count "
            "FROM clients WHERE user_id=?",
            (user_id,)
        )
        return rows[0] if rows else None

    async def get_all_clients(self):
        return await self._run_query(
            "SELECT user_id, username, total_deals, total_volume FROM clients "
            "WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20"
        )

    async def get_top_clients(self, limit: int = 10):
        return await self._run_query(
            "SELECT user_id, username, total_deals, total_volume, avg_rating, ratings_count "
            "FROM clients WHERE total_deals > 0 "
            "ORDER BY total_deals DESC, total_volume DESC LIMIT ?",
            (limit,)
        )

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        rows = await self._run_query(
            "SELECT user_id FROM clients WHERE username=?",
            (username,)
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
        """, (
            user_id, operation_type, amount, client_total,
            STATUS_PENDING, datetime.now().isoformat(), invoice_link
        ))

    async def create_request_if_no_active(
        self, user_id: int, operation_type: str, amount: float,
        client_total: float, invoice_link: Optional[str] = None
    ) -> Optional[int]:
        async with self._lock:
            with sqlite3.connect(self.db_file, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT id FROM requests
                    WHERE user_id=? AND status IN (?,?,?,?)
                    ORDER BY id DESC LIMIT 1
                """, (
                    user_id, STATUS_PENDING, STATUS_PROCESSING,
                    STATUS_REQUISITES_SENT, STATUS_PAID
                ))
                active = cur.fetchone()
                if active:
                    return None

                cur.execute("""
                    INSERT INTO requests
                        (user_id, operation_type, amount, client_total, status, created_at, invoice_link)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id, operation_type, amount, client_total,
                    STATUS_PENDING, datetime.now().isoformat(), invoice_link
                ))
                conn.commit()
                return cur.lastrowid

    async def get_request(self, request_id: int):
        rows = await self._run_query("SELECT * FROM requests WHERE id=?", (request_id,))
        return rows[0] if rows else None

    async def get_user_active_request(self, user_id: int):
        rows = await self._run_query("""
            SELECT id, operation_type, amount, client_total, status
            FROM requests
            WHERE user_id=? AND status IN (?,?,?,?)
            ORDER BY id DESC LIMIT 1
        """, (
            user_id, STATUS_PENDING, STATUS_PROCESSING,
            STATUS_REQUISITES_SENT, STATUS_PAID
        ))
        return rows[0] if rows else None

    async def get_all_pending_requests(self, limit: int = 20):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at, invoice_link "
            "FROM requests WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (STATUS_PENDING, limit)
        )

    async def get_all_processing_requests(self, limit: int = 20):
        return await self._run_query(
            "SELECT id, user_id, amount, client_total, status, created_at, invoice_link "
            "FROM requests WHERE status IN (?,?) ORDER BY created_at DESC LIMIT ?",
            (STATUS_PROCESSING, STATUS_REQUISITES_SENT, limit)
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
        req = await self.get_request(request_id)
        if not req:
            return False
        if cancelled_by == "user" and user_id is not None and req['user_id'] != user_id:
            return False
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        await self._run_execute(
            "UPDATE requests SET status=?, cancelled_at=?, cancelled_by=? WHERE id=?",
            (status, datetime.now().isoformat(), cancelled_by, request_id)
        )
        return True

    # --- Отзывы ---
    async def recalculate_client_rating(self, user_id: int):
        rows = await self._run_query(
            "SELECT COALESCE(AVG(rating), 0) as avg_rating, COUNT(rating) as ratings_count "
            "FROM feedback WHERE user_id=? AND rating IS NOT NULL",
            (user_id,)
        )
        avg_rating = rows[0]['avg_rating'] if rows else 0
        ratings_count = rows[0]['ratings_count'] if rows else 0
        await self._run_execute(
            "UPDATE clients SET avg_rating=?, ratings_count=? WHERE user_id=?",
            (avg_rating, ratings_count, user_id)
        )

    async def add_feedback(
        self, user_id: int, request_id: int,
        rating: Optional[int] = None, comment: Optional[str] = None
    ) -> bool:
        exists = await self._run_query(
            "SELECT id FROM feedback WHERE user_id=? AND request_id=? LIMIT 1",
            (user_id, request_id)
        )
        if exists:
            return False

        await self._run_insert(
            "INSERT INTO feedback (user_id, request_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, request_id, rating, comment, datetime.now().isoformat())
        )
        if rating is not None:
            await self.recalculate_client_rating(user_id)
        return True

    async def get_feedback_for_display(self, limit: int = 5, offset: int = 0):
        return await self._run_query("""
            SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at
            FROM feedback f
            JOIN clients c ON f.user_id = c.user_id
            WHERE f.is_displayed=1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL)
            ORDER BY f.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))

    async def get_feedback_count(self) -> int:
        rows = await self._run_query(
            "SELECT COUNT(*) as cnt FROM feedback WHERE is_displayed=1"
        )
        return rows[0]['cnt'] if rows else 0

    async def get_avg_rating(self) -> float:
        rows = await self._run_query(
            "SELECT AVG(rating) as avg FROM feedback WHERE rating IS NOT NULL AND is_displayed=1"
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

def get_rank_and_discount(deals: int):
    if deals < 3:
        return ("Новичок", "🟢", 0.0, 3 - deals)
    if deals < 7:
        return ("Ходок", "🔵", 0.5, 7 - deals)
    if deals < 10:
        return ("Опытный", "🟠", 1.0, 10 - deals)
    if deals < 15:
        return ("Мастер", "🟣", 1.5, 15 - deals)
    return ("Легенда", "🔥", 2.0, 0)

def calculate_client_total(amount: float, discount_percent: float = 0.0) -> float:
    base_commission_rate = 0.169
    actual_rate = max(0, base_commission_rate - (discount_percent / 100))
    return (amount * (1 + actual_rate)) + 285

def get_progress_bar(current: int, needed: int) -> str:
    if needed <= 0:
        return "▰" * 10
    total = current + needed
    filled = min(max(int(10 * current / total), 0), 10)
    return "▰" * filled + "▱" * (10 - filled)

# ==================================================
# ================== AFK ===========================
# ==================================================

async def is_afk_mode() -> bool:
    val = await db.get_setting('afk_mode')
    return val == '1'

# ==================================================
# ================== ДЕКОРАТОРЫ ===================
# ==================================================

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != ADMIN_ID:
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

# ==================================================
# ================== ВСПОМОГАТЕЛЬНЫЕ ===============
# ==================================================

async def safe_send(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Forbidden:
        logging.warning(f"User {user_id} blocked the bot.")
    except Exception as e:
        logging.error(f"Error sending message to {user_id}: {e}")
    return None

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in ['step', 'temp_amount', 'operation_type', 'invoice_link',
                'feedback_req_id', 'feedback_rating']:
        context.user_data.pop(key, None)

async def show_settings_menu(update: Update):
    await update.message.reply_text(
        "⚙️ НАСТРОЙКИ\n\n"
        "/edit_rules - изменить правила\n"
        "/edit_schedule - изменить график\n"
        "/edit_links - изменить ссылки\n"
        "/afk on|off - режим ожидания",
        reply_markup=get_admin_keyboard()
    )

# ==================================================
# ================== ОСНОВНЫЕ HANDLERS =============
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    await db.add_client(user.id, user.username)
    banned, reason = await db.is_banned(user.id)
    if banned:
        await update.message.reply_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}")
        return

    reset_request_flow(context)
    await update.message.reply_text(
        f"👋 ПРИВЕТСТВУЮ, {user.first_name}!\n\n"
        "SVEN OBMEN - быстрый и надежный обмен.\n\n"
        "Используйте кнопки меню ниже",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (
        "📖 СПРАВОЧНИК\n\n"
        "• Новый запрос - начать обмен\n"
        "• Профиль - ваша статистика и ранг\n"
        "• Отзывы - почитать, что пишут другие\n\n"
        "Команды:\n"
        "/start - перезапуск бота\n"
        "/help - это сообщение\n"
        "/stats - профиль или админ-статистика\n"
        "/skip - пропустить ввод комментария к отзыву"
    )
    await update.message.reply_text(text)

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        req_id = context.user_data.pop('feedback_req_id', None)
        rating = context.user_data.pop('feedback_rating', None)
        context.user_data.pop('step', None)
        if req_id and rating is not None:
            await db.add_feedback(user_id, req_id, rating, None)
        await update.message.reply_text("✅ Отзыв сохранен.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Нет активного запроса отзыва.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id

    if user_id == ADMIN_ID and not context.args:
        top = await db.get_top_clients(limit=10)
        if not top:
            await update.message.reply_text("📊 Пока нет данных по завершенным сделкам.", reply_markup=get_admin_keyboard())
            return

        lines = ["📊 ТОП-10 КЛИЕНТОВ ПО СДЕЛКАМ\n"]
        for i, c in enumerate(top, start=1):
            uname = f"@{c['username']}" if c['username'] else f"ID:{c['user_id']}"
            lines.append(
                f"{i}. {uname} | Сделок: {c['total_deals']} | Объем: {c['total_volume']:.0f} ₽"
            )
        await update.message.reply_text("\n".join(lines), reply_markup=get_admin_keyboard())
        return

    if user_id == ADMIN_ID and context.args:
        raw = context.args[0].strip()
        target_id = None
        if raw.startswith("@"):
            target_id = await db.find_user_id_by_username(raw[1:])
        else:
            try:
                target_id = int(raw)
            except ValueError:
                pass

        if not target_id:
            await update.message.reply_text("❌ Пользователь не найден. Используйте /stats <id|@username>")
            return

        await show_profile(update, context, target_id)
        return

    await show_profile(update, context, user_id)

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    user_id = update.effective_user.id
    text = update.message.text or ""

    banned, reason = await db.is_banned(user_id)
    if banned:
        await update.message.reply_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}")
        return

    if text == "🔥 НОВЫЙ ЗАПРОС":
        if await is_afk_mode() and user_id != ADMIN_ID:
            await update.message.reply_text("😴 Бот временно не принимает новые заявки.")
            return

        active = await db.get_user_active_request(user_id)
        if active:
            await update.message.reply_text(f"⚠️ У вас уже есть активная заявка #{active['id']}.")
            return

        reset_request_flow(context)
        await update.message.reply_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
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
        if "НАСТРОЙКИ" in text:
            await show_settings_menu(update)
            return
        if text == "📊 СТАТИСТИКА":
            await show_admin_stats(update, context)
            return
        if text == "🚫 ЗАБАНЕННЫЕ":
            await show_banned_users(update, context)
            return
        if text == "◀️ ВЫЙТИ":
            await update.message.reply_text("🔐 Выход из админ-панели.", reply_markup=get_main_keyboard())
            return

    await update.message.reply_text("Пожалуйста, используйте кнопки меню.", reply_markup=get_main_keyboard())

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if not update.message:
        return
    stats = await db.get_client_stats(user_id)
    if not stats or (stats['total_deals'] == 0 and stats['ratings_count'] == 0):
        await update.message.reply_text(
            "👤 ПРОФИЛЬ\n\n📊 У пользователя пока нет данных.",
            reply_markup=get_main_keyboard()
        )
        return

    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    rank_name, rank_emoji, discount, next_rank_deals = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals, next_rank_deals)
    username = f"@{stats['username']}" if stats['username'] else f"ID:{stats['user_id']}"

    text = (
        f"👤 ПРОФИЛЬ {username}\n\n"
        f"🏆 РАНГ: {rank_emoji} {rank_name}\n"
        f"📊 ПРОГРЕСС: {progress_bar}\n"
        f"💰 СКИДКА: {discount}%\n\n"
        f"📈 СТАТИСТИКА:\n"
        f"• Сделок: {deals}\n"
        f"• Объем: {volume:.0f} ₽\n"
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
        elif update.message:
            await update.message.reply_text("⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", reply_markup=get_main_keyboard())
        return

    text = f"⭐ ОТЗЫВЫ КЛИЕНТОВ\n\nВсего отзывов: {total} | Средний рейтинг: {avg_rating:.1f} ⭐\n━━━━━━━━━━━━━\n\n"
    for r in reviews:
        stars = "⭐" * int(r['rating']) if r['rating'] else "📝"
        username = r['username'] or "User"
        text += f"👤 @{username}\n📅 {r['created_at'][:10]}\n"
        if r['comment']:
            text += f'💬 "{r["comment"]}"\n'
        text += f"Оценка: {stars}\n━━━━━━━━━━━━━\n"

    kb_row = []
    if total > (page + 1) * limit:
        kb_row.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЕ", callback_data="reviews_next"))
    kb_row.append(InlineKeyboardButton("◀️ В МЕНЮ", callback_data="back_to_menu_ui"))
    reply_markup = InlineKeyboardMarkup([kb_row])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)

# ==================================================
# ================== АДМИН-ФУНКЦИИ ================
# ==================================================

@admin_only
async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    pending = await db.get_all_pending_requests(limit=15)
    processing = await db.get_all_processing_requests(limit=15)

    if not pending and not processing:
        await update.message.reply_text("📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return

    text = "📋 АКТИВНЫЕ ЗАЯВКИ\n\n"

    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | ID:{req['user_id']}\n"
        text += "\n"

    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            ico = "⏳" if req['status'] == STATUS_PROCESSING else "💳"
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | {ico}\n"

    text += "\n🔧 КОМАНДЫ:\n"
    text += "/take <id> - взять заявку\n"
    text += "/send <id> <текст> - отправить реквизиты\n"
    text += "/confirm <id> - подтвердить оплату\n"
    text += "/reject <id> - отклонить\n"
    text += "/getpdf <id> - скачать чек\n"
    text += "/getlink <id> - посмотреть ссылку"

    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    clients = await db.get_all_clients()
    avg_rating = await db.get_avg_rating()
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)

    text = (
        f"📊 ОБЩАЯ СТАТИСТИКА\n\n"
        f"• Клиентов (активных): {len(clients)}\n"
        f"• Всего сделок: {total_deals}\n"
        f"• Общий объем: {total_volume:.0f} ₽\n"
        f"• Ср. рейтинг: ⭐ {avg_rating:.1f}\n"
        f"• Расч. прибыль (~10%): {total_volume * 0.1:.0f} ₽"
    )
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    banned = await db.get_banned_users()
    if not banned:
        await update.message.reply_text("🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", reply_markup=get_admin_keyboard())
        return

    text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
    for u in banned:
        text += f"👤 {format_user(u['username'], u['user_id'])} (ID: {u['user_id']})\n"
        text += f"📅 Забанен: {u['banned_at'][:10]}\n"
        text += f"📝 Причина: {u['ban_reason']}\n━━━━━━━━━━━━━\n"

    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

# ==================================================
# ================== CALLBACK ======================
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "back_to_type_select":
        reset_request_flow(context)
        await query.edit_message_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
        return

    if data == "cancel_to_main":
        reset_request_flow(context)
        await query.message.delete()
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
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

    if data == "edit_amount":
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text("💰 ВВЕДИТЕ НОВУЮ СУММУ:", reply_markup=get_back_inline())
        return

    if data.startswith("type_"):
        if await is_afk_mode() and user_id != ADMIN_ID:
            await query.edit_message_text("😴 Бот временно не принимает новые заявки.")
            return

        active = await db.get_user_active_request(user_id)
        if active:
            await query.edit_message_text(
                f"⚠️ У вас уже есть активная заявка #{active['id']}.",
                reply_markup=get_cancel_keyboard(active['id'])
            )
            return

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

    if data == "confirm_request":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        invoice_link = context.user_data.get('invoice_link')

        if not amount or not op_type:
            await query.edit_message_text("❌ Ошибка данных. Начните заново.", reply_markup=get_operation_keyboard())
            return

        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        client_total = calculate_client_total(amount, discount)

        req_id = await db.create_request_if_no_active(
            user_id=user_id,
            operation_type=op_type,
            amount=amount,
            client_total=client_total,
            invoice_link=invoice_link
        )

        if req_id is None:
            active = await db.get_user_active_request(user_id)
            active_id = active['id'] if active else "?"
            await query.edit_message_text(
                f"⚠️ У вас уже есть активная заявка #{active_id}.",
                reply_markup=get_cancel_keyboard(active['id']) if active else None
            )
            reset_request_flow(context)
            return

        await query.edit_message_text(
            f"✅ ЗАЯВКА #{req_id} СОЗДАНА!\n\n"
            f"Тип: {op_type}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"К оплате: {client_total:.0f} ₽\n\n"
            f"Статус: ⏳ ОЖИДАЕТ ОБРАБОТКИ\n\n"
            f"Оператор скоро свяжется с вами.",
            reply_markup=get_cancel_keyboard(req_id)
        )

        admin_msg = (
            f"🔔 НОВАЯ ЗАЯВКА #{req_id}\n"
            f"👤 {format_user(query.from_user.username, user_id)}\n"
            f"💰 Сумма: {amount:.0f} ₽\n"
            f"💸 К оплате: {client_total:.0f} ₽\n"
        )
        if invoice_link:
            admin_msg += f"🔗 Ссылка: {invoice_link}\n"
        admin_msg += f"\n✅ /take {req_id} - взять в работу"

        await context.bot.send_message(ADMIN_ID, admin_msg)
        reset_request_flow(context)
        return

    if data.startswith("cancel_"):
        try:
            req_id = int(data.split("_")[1])
            if await db.cancel_request(req_id, "user", user_id):
                await query.edit_message_text(f"✅ ЗАЯВКА #{req_id} ОТМЕНЕНА.", reply_markup=get_operation_keyboard())
                await context.bot.send_message(ADMIN_ID, f"⚠️ Пользователь отменил заявку #{req_id}")
            else:
                await query.answer("Нельзя отменить чужую заявку.")
        except Exception as e:
            logging.error(f"Cancel error: {e}")
        return

    if data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                req_id, rating = int(parts[1]), int(parts[2])
                context.user_data['feedback_req_id'] = req_id
                context.user_data['feedback_rating'] = rating
                context.user_data['step'] = ASKING_FEEDBACK_COMMENT
                await query.edit_message_text(
                    f"Вы поставили {rating}⭐\n\n"
                    "✏️ Оставьте комментарий или отправьте /skip для пропуска:"
                )
            except Exception as e:
                logging.error(f"Rate callback parse error: {e}")
        return

# ==================================================
# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
# ==================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return

    user_id = update.effective_user.id
    msg_text = update.message.text or ""

    if user_id == ADMIN_ID and context.user_data.get('editing_setting'):
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, msg_text)
        await update.message.reply_text(f"✅ Настройка '{key}' обновлена!", reply_markup=get_admin_keyboard())
        return

    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active or active['status'] != STATUS_REQUISITES_SENT:
                await update.message.reply_text("❌ У вас нет заявок в статусе ожидания оплаты.")
                return

            await db.mark_paid(active['id'], update.message.document.file_id)
            await update.message.reply_text(
                "✅ ЧЕК ПРИНЯТ!\n\n"
                "Статус: 🔍 ПРОВЕРКА ОПЕРАТОРОМ\n"
                "Обычно это занимает 5-15 минут."
            )
            await context.bot.send_message(
                ADMIN_ID,
                f"💳 ПОЛУЧЕН ЧЕК\n"
                f"👤 {format_user(update.effective_user.username, user_id)}\n"
                f"📋 Заявка #{active['id']}\n"
                f"✅ /confirm {active['id']} - подтвердить\n"
                f"❌ /reject {active['id']} - отклонить"
            )
        else:
            await update.message.reply_text("❌ Пожалуйста, отправьте чек именно в формате PDF.")
        return

    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        req_id = context.user_data.pop('feedback_req_id', None)
        rating = context.user_data.pop('feedback_rating', None)
        context.user_data.pop('step', None)

        comment = msg_text if msg_text != '/skip' else None
        if req_id and rating is not None:
            saved = await db.add_feedback(user_id, req_id, rating, comment)
            if saved:
                await update.message.reply_text("✅ Спасибо за отзыв!", reply_markup=get_main_keyboard())
            else:
                await update.message.reply_text("ℹ️ Отзыв по этой заявке уже был сохранен.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("❌ Не удалось сохранить отзыв.", reply_markup=get_main_keyboard())
        return

    if context.user_data.get('step') == ASKING_AMOUNT:
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
                "🔗 ВВЕДИТЕ ССЫЛКУ НА СЧЕТ OXAPAY\n\n"
                "Пример: https://pay.oxapay.com/invoice/xxxxxxxx",
                reply_markup=get_back_inline()
            )
        else:
            stats = await db.get_client_stats(user_id)
            deals = stats['total_deals'] if stats else 0
            _, _, discount, _ = get_rank_and_discount(deals)
            client_total = calculate_client_total(amount, discount)
            await update.message.reply_text(
                f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\n"
                f"Тип: {op_type}\n"
                f"Сумма: {amount:.0f} ₽\n"
                f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n\n"
                f"⚠️ После получения реквизитов вы обязуетесь произвести оплату.",
                reply_markup=get_confirm_keyboard()
            )
            context.user_data.pop('step', None)
        return

    if context.user_data.get('step') == ASKING_LINK:
        link = msg_text.strip()
        if not (link.startswith("https://pay.oxapay.com/invoice/") or link.startswith("https://oxapay.com/invoice/")):
            await update.message.reply_text(
                "❌ Неверный формат ссылки.\n\n"
                "Ссылка должна начинаться с:\n"
                "https://pay.oxapay.com/invoice/\n"
                "или\nhttps://oxapay.com/invoice/",
                reply_markup=get_back_inline()
            )
            return

        context.user_data['invoice_link'] = link
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        client_total = calculate_client_total(amount, discount)
        await update.message.reply_text(
            f"📝 ПРОВЕРЬТЕ ДАННЫЕ\n\n"
            f"Тип: {op_type}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n"
            f"🔗 Ссылка: {link}\n\n"
            f"⚠️ После подтверждения вы обязуетесь оплатить счет.",
            reply_markup=get_confirm_keyboard()
        )
        context.user_data.pop('step', None)
        return

    await handle_menu(update, context)

# ==================================================
# ================== КОМАНДЫ АДМИНА ================
# ==================================================

@admin_only
async def admin_panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

@admin_only
async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /take <id>")
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена.")
        return
    if req['status'] != STATUS_PENDING:
        await update.message.reply_text(f"❌ Заявка в статусе {req['status']}, взять в работу нельзя.")
        return

    await db.take_request(req_id)

    await update.message.reply_text(
        f"✅ Заявка #{req_id} взята в работу.\n\n"
        f"📤 Отправьте реквизиты:\n"
        f"/send {req_id} <текст реквизитов>"
    )

    await safe_send(
        context, req['user_id'],
        f"✅ ЗАЯВКА #{req_id} ПРИНЯТА В РАБОТУ!\n\n"
        "Оператор скоро пришлет реквизиты для оплаты."
    )

@admin_only
async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Использование: /send <id> <текст реквизитов>\n\n"
            "Пример: /send 123 Карта 2200 1234 5678 9012"
        )
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    requisites = " ".join(context.args[1:])
    req = await db.get_request(req_id)

    if not req:
        await update.message.reply_text(f"❌ Заявка #{req_id} не найдена.")
        return

    if req['status'] not in (STATUS_PROCESSING, STATUS_PENDING):
        await update.message.reply_text(
            f"❌ Заявка #{req_id} в статусе {req['status']}, отправка реквизитов недоступна."
        )
        return

    await db.send_requisites(req_id, requisites)
    await update.message.reply_text(f"✅ Реквизиты для заявки #{req_id} отправлены!")

    msg = (
        f"💳 РЕКВИЗИТЫ ПО ЗАЯВКЕ #{req_id}\n\n"
        f"💸 Сумма к оплате: {req['client_total']:.0f} ₽\n\n"
        f"📋 Реквизиты:\n{requisites}\n\n"
        f"⚠️ После оплаты ОБЯЗАТЕЛЬНО пришлите PDF-чек в этот чат.\n\n"
        f"🚫 Для отмены заявки нажмите кнопку ниже."
    )
    if req['invoice_link']:
        msg += f"\n\n🔗 Счет: {req['invoice_link']}"

    sent = await safe_send(
        context,
        req['user_id'],
        msg,
        reply_markup=get_cancel_keyboard(req_id)
    )
    if not sent:
        await safe_send(context, req['user_id'], msg)

@admin_only
async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /confirm <id>")
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена.")
        return
    if req['status'] != STATUS_PAID:
        await update.message.reply_text(f"❌ Заявка в статусе {req['status']}. Нужен статус {STATUS_PAID}.")
        return

    await db.complete_request(req_id, req['user_id'], req['amount'])
    await update.message.reply_text(f"✅ Заявка #{req_id} ЗАВЕРШЕНА!")

    await safe_send(
        context, req['user_id'],
        f"🎉 ЗАЯВКА #{req_id} УСПЕШНО ВЫПОЛНЕНА!\n\n"
        f"Сумма: {req['amount']:.0f} ₽\n\n"
        f"⭐ Оцените нашу работу:",
        reply_markup=get_rating_keyboard(req_id)
    )

@admin_only
async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /reject <id>")
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена.")
        return

    if req['status'] not in ACTIVE_REQUEST_STATUSES:
        await update.message.reply_text(f"❌ Заявка в статусе {req['status']}, отклонение недоступно.")
        return

    await db.cancel_request(req_id, "admin")
    await update.message.reply_text(f"❌ Заявка #{req_id} ОТКЛОНЕНА.")

    await safe_send(
        context, req['user_id'],
        f"❌ ЗАЯВКА #{req_id} ОТКЛОНЕНА.\n\n"
        f"Причина: чек не прошел проверку.\n\n"
        f"Свяжитесь с поддержкой: @svenobmen"
    )

@admin_only
async def getpdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /getpdf <id>")
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    req = await db.get_request(req_id)
    if not req or not req['pdf_file_id']:
        await update.message.reply_text("❌ PDF не найден.")
        return

    await context.bot.send_document(ADMIN_ID, req['pdf_file_id'], caption=f"📎 Чек к заявке #{req_id}")

@admin_only
async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /getlink <id>")
        return

    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return

    req = await db.get_request(req_id)
    if not req or not req['invoice_link']:
        await update.message.reply_text("❌ Ссылка не найдена.")
        return

    await update.message.reply_text(f"🔗 Ссылка по заявке #{req_id}:\n{req['invoice_link']}")

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /ban @username <причина>")
        return

    username = context.args[0].replace("@", "")
    reason = " ".join(context.args[1:])
    uid = await db.find_user_id_by_username(username)

    if not uid:
        await update.message.reply_text("❌ Пользователь не найден.")
        return

    await db.ban_user(uid, reason)
    await update.message.reply_text(f"🚫 Пользователь @{username} заблокирован.\nПричина: {reason}")
    await safe_send(context, uid, f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}\n\nПо вопросам: @svenobmen")

@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /unban @username")
        return

    username = context.args[0].replace("@", "")
    uid = await db.find_user_id_by_username(username)

    if uid:
        await db.unban_user(uid)
        await update.message.reply_text(f"✅ Пользователь @{username} разблокирован.")
    else:
        await update.message.reply_text("❌ Пользователь не найден.")

@admin_only
async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text("❌ Использование: /afk on/off")
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await update.message.reply_text("❌ Использование: /afk on/off")
        return

    enabled = arg == "on"
    await db.update_setting('afk_mode', '1' if enabled else '0')
    status = "ВКЛЮЧЕН (новые заявки не принимаются)" if enabled else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"✅ Режим AFK: {status}")

@admin_only
async def edit_setting_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    cmd = update.message.text.split()[0].replace('/', '')
    setting_map = {'edit_rules': 'rules', 'edit_schedule': 'schedule', 'edit_links': 'links'}
    key = setting_map.get(cmd)
    if key:
        context.user_data['editing_setting'] = key
        current = await db.get_setting(key) or ""
        await update.message.reply_text(
            f"📝 ТЕКУЩЕЕ ЗНАЧЕНИЕ '{key}':\n\n{current}\n\n"
            f"✏️ Введите новое значение:"
        )

@admin_only
async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_setting_start(update, context)

@admin_only
async def edit_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_setting_start(update, context)

@admin_only
async def edit_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_setting_start(update, context)

# ==================================================
# ================== ERROR HANDLER =================
# ==================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.exception(f"Update {update} caused error", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")
    except Exception:
        pass

# ==================================================
# ================== ЗАПУСК ========================
# ==================================================

def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO,
        handlers=[logging.StreamHandler(), logging.FileHandler("bot.log", encoding="utf-8")]
    )

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды для всех
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("skip", skip_command))

    # Команды админа
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
    app.add_handler(CommandHandler("edit_rules", edit_rules_command))
    app.add_handler(CommandHandler("edit_schedule", edit_schedule_command))
    app.add_handler(CommandHandler("edit_links", edit_links_command))

    # Обработчики
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
            handle_message
        )
    )

    app.add_error_handler(error_handler)

    logging.info("✅ БОТ ЗАПУЩЕН")
    app.run_polling()

if __name__ == "__main__":
    main()