import re
import logging
import sqlite3
import aiohttp
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, MessageHandler, filters, CommandHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler
)

# ==================================================
# ================== НАСТРОЙКИ ====================
# ==================================================

BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039  # Замените на свой Telegram ID

CACHE_TIME_SECONDS = 3600

# Статусы заявок
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REQUISITES_SENT = "requisites_sent"
STATUS_PAID = "paid"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED_BY_USER = "cancelled_by_user"
STATUS_CANCELLED_BY_ADMIN = "cancelled_by_admin"

# Типы операций
OPERATION_OXAPAY = "Оплата счёта OxaPay"
OPERATION_BITPAPA = "Создание чека Bitpapa"
OPERATION_CRYPTO = "Покупка крипты на кошелёк"
OPERATION_SHOP = "Отправка на кошелёк магазина"

# Режим AFK
afk_mode = False

# ==================================================
# ================== БАЗА ДАННЫХ ===================
# ==================================================

class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()
        self._init_settings()

    def _create_tables(self):
        self.cursor.execute("""
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
        self.cursor.execute("""
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
                pdf_file_id TEXT,
                FOREIGN KEY (user_id) REFERENCES clients(user_id)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                request_id INTEGER,
                rating INTEGER,
                comment TEXT,
                created_at TEXT,
                is_displayed INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES clients(user_id),
                FOREIGN KEY (request_id) REFERENCES requests(id)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def _init_settings(self):
        defaults = {
            'rules': '📜 **ПРАВИЛА РАБОТЫ**\n\n• Минимальная сумма: 1000 ₽\n• Комиссия: 10%\n• Работаем 24/7\n• Чек PDF обязателен\n• Неоплата счёта влечёт блокировку',
            'schedule': '⏰ **ГРАФИК РАБОТЫ**\n\n• Пн–Вс: 24/7\n• Без выходных',
            'links': '🔗 **ПОЛЕЗНЫЕ ССЫЛКИ**\n\n• 📢 Канал: https://t.me/svenobmen\n• 📊 Bitpapa: https://bitpapa.com\n• 💬 Поддержка: @svenobmen'
        }
        for key, value in defaults.items():
            self.cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # Клиенты
    def add_client(self, user_id, username):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)",
            (user_id, username, now)
        )
        self.conn.commit()
    
    def is_banned(self, user_id):
        self.cursor.execute("SELECT is_banned, ban_reason FROM clients WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if row and row[0] == 1:
            return True, row[1]
        return False, None
    
    def ban_user(self, user_id, reason):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE clients SET is_banned = 1, ban_reason = ?, banned_at = ? WHERE user_id = ?",
            (reason, now, user_id)
        )
        self.conn.commit()
    
    def unban_user(self, user_id):
        self.cursor.execute(
            "UPDATE clients SET is_banned = 0, ban_reason = NULL, banned_at = NULL WHERE user_id = ?",
            (user_id,)
        )
        self.conn.commit()
    
    def get_banned_users(self):
        self.cursor.execute("SELECT user_id, username, ban_reason, banned_at FROM clients WHERE is_banned = 1")
        return self.cursor.fetchall()
    
    def update_client_after_deal(self, user_id, amount):
        self.cursor.execute(
            "UPDATE clients SET total_deals = total_deals + 1, total_volume = total_volume + ? WHERE user_id = ?",
            (amount, user_id)
        )
        self.conn.commit()
    
    def get_client_stats(self, user_id):
        self.cursor.execute(
            "SELECT total_deals, total_volume, avg_rating, ratings_count FROM clients WHERE user_id = ?",
            (user_id,)
        )
        return self.cursor.fetchone()
    
    def get_all_clients(self):
        self.cursor.execute(
            "SELECT user_id, username, total_deals, total_volume FROM clients WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20"
        )
        return self.cursor.fetchall()
    
    def find_user_by_username(self, username):
        self.cursor.execute("SELECT user_id, username FROM clients WHERE username = ?", (username,))
        return self.cursor.fetchone()
    
    def find_user_id_by_username(self, username):
        self.cursor.execute("SELECT user_id FROM clients WHERE username = ?", (username,))
        row = self.cursor.fetchone()
        return row[0] if row else None
    
    # Заявки
    def add_request(self, user_id, operation_type, amount, client_total):
        now = datetime.now().isoformat()
        self.cursor.execute("""
            INSERT INTO requests (user_id, operation_type, amount, client_total, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, operation_type, amount, client_total, STATUS_PENDING, now))
        self.conn.commit()
        return self.cursor.lastrowid
    
    def get_request(self, request_id):
        self.cursor.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        return self.cursor.fetchone()
    
    def get_user_active_request(self, user_id):
        self.cursor.execute(
            "SELECT id, operation_type, amount, client_total, status FROM requests WHERE user_id = ? AND status IN (?, ?, ?, ?) ORDER BY id DESC LIMIT 1",
            (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID)
        )
        return self.cursor.fetchone()
    
    def get_all_requests_by_status(self, status=None):
        if status:
            self.cursor.execute("SELECT id, user_id, amount, client_total, status, created_at FROM requests WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            self.cursor.execute("SELECT id, user_id, amount, client_total, status, created_at FROM requests ORDER BY created_at DESC")
        return self.cursor.fetchall()
    
    def get_all_active_requests(self):
        return self.get_all_requests_by_status(STATUS_PENDING)
    
    def update_request_status(self, request_id, status, extra_field=None, extra_value=None):
        if extra_field and extra_value:
            self.cursor.execute(
                f"UPDATE requests SET status = ?, {extra_field} = ? WHERE id = ?",
                (status, extra_value, request_id)
            )
        else:
            self.cursor.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
        self.conn.commit()
    
    def take_request(self, request_id):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE requests SET status = ?, taken_at = ? WHERE id = ?",
            (STATUS_PROCESSING, now, request_id)
        )
        self.conn.commit()
    
    def send_requisites(self, request_id, requisites_text):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE requests SET status = ?, requisites_sent_at = ?, requisites_text = ? WHERE id = ?",
            (STATUS_REQUISITES_SENT, now, requisites_text, request_id)
        )
        self.conn.commit()
    
    def mark_paid(self, request_id, pdf_file_id):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE requests SET status = ?, paid_at = ?, pdf_file_id = ? WHERE id = ?",
            (STATUS_PAID, now, pdf_file_id, request_id)
        )
        self.conn.commit()
    
    def complete_request(self, request_id, user_id, amount):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE requests SET status = ?, completed_at = ? WHERE id = ?",
            (STATUS_COMPLETED, now, request_id)
        )
        self.update_client_after_deal(user_id, amount)
        self.conn.commit()
    
    def cancel_request(self, request_id, cancelled_by):
        now = datetime.now().isoformat()
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        self.cursor.execute(
            "UPDATE requests SET status = ?, cancelled_at = ?, cancelled_by = ? WHERE id = ?",
            (status, now, cancelled_by, request_id)
        )
        self.conn.commit()
    
    # Отзывы
    def add_feedback(self, user_id, request_id, rating=None, comment=None):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO feedback (user_id, request_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, request_id, rating, comment, now)
        )
        self.cursor.execute("SELECT AVG(rating), COUNT(*) FROM feedback WHERE user_id = ? AND rating IS NOT NULL", (user_id,))
        avg, count = self.cursor.fetchone()
        if avg:
            self.cursor.execute(
                "UPDATE clients SET avg_rating = ?, ratings_count = ? WHERE user_id = ?",
                (avg, count, user_id)
            )
        self.conn.commit()
    
    def get_feedback_for_display(self, limit=5, offset=0):
        self.cursor.execute("""
            SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at
            FROM feedback f
            JOIN clients c ON f.user_id = c.user_id
            WHERE f.is_displayed = 1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL)
            ORDER BY f.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        return self.cursor.fetchall()
    
    def get_feedback_count(self):
        self.cursor.execute("SELECT COUNT(*) FROM feedback WHERE is_displayed = 1")
        return self.cursor.fetchone()[0]
    
    def get_avg_rating(self):
        self.cursor.execute("SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL")
        avg = self.cursor.fetchone()[0]
        return avg if avg else 0
    
    # Настройки
    def get_setting(self, key):
        self.cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = self.cursor.fetchone()
        return row[0] if row else None
    
    def update_setting(self, key, value):
        self.cursor.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))
        self.conn.commit()


db = Database()

# ==================================================
# ================== КУРС ВАЛЮТ ====================
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

# ==================================================
# ================== ЛОГИКА РАСЧЁТА ================
# ==================================================

def calculate_client_total(amount: float) -> float:
    return amount * 1.169 + 285

# ==================================================
# ================== РАНГИ =========================
# ==================================================

def get_rank_and_discount(deals: int):
    if deals < 3:
        return ("Новичок", "🟢", 0, 3 - deals)
    elif deals < 7:
        return ("Ходок", "🔵", 0, 7 - deals)
    elif deals < 10:
        return ("Опытный", "🟠", 0, 10 - deals)
    elif deals < 15:
        return ("Мастер", "🟣", 0, 15 - deals)
    else:
        return ("Легенда", "🔥", 1, 0)

def get_progress_bar(current: int, target: int) -> str:
    if target <= 0:
        return "▰" * 10
    filled = int(10 * current / target)
    if filled > 10:
        filled = 10
    return "▰" * filled + "▱" * (10 - filled)

# ==================================================
# ================== КЛАВИАТУРЫ ====================
# ==================================================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 НОВЫЙ ЗАПРОС")],
        [KeyboardButton("⭐ ОТЗЫВЫ КЛИЕНТОВ")],
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
        [InlineKeyboardButton("💰 КУПИТЬ КРИПТУ НА КОШЕЛЁК", callback_data="type_crypto")],
        [InlineKeyboardButton("🏪 ОТПРАВИТЬ НА КОШЕЛЁК МАГАЗИНА", callback_data="type_shop")],
        [InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main")]
    ])

def get_back_keyboard(callback_data="back_to_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ НАЗАД", callback_data=callback_data)]])

def get_confirm_with_warning_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ПОЛУЧИТЬ РЕКВИЗИТЫ", callback_data="get_requisites")],
        [InlineKeyboardButton("✏️ ИЗМЕНИТЬ", callback_data="edit_amount")],
        [InlineKeyboardButton("◀️ ОТМЕНА", callback_data="back_to_main")]
    ])

def get_cancel_keyboard(request_id):
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
# ================== ОСНОВНЫЕ ФУНКЦИИ ==============
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or str(user.id)
    db.add_client(user.id, username)
    
    banned, reason = db.is_banned(user.id)
    if banned:
        await update.message.reply_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\n"
            f"Причина: {reason}\n\n"
            f"По вопросам разблокировки: @svenobmen",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text(
        f"👋 ПРИВЕТСТВУЮ, {user.first_name}!\n\n"
        f"SVEN OBMEN — помощь с криптовалютными задачами.\n\n"
        f"➡️ НАЧНИТЕ С КНОПКИ НИЖЕ ⬇️",
        reply_markup=get_main_keyboard()
    )

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    banned, reason = db.is_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {reason}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return
    
    if text == "🔥 НОВЫЙ ЗАПРОС":
        active = db.get_user_active_request(user_id)
        if active:
            await update.message.reply_text(
                f"⚠️ У вас уже есть активная заявка #{active[0]}.\n"
                f"Дождитесь её обработки или отмените через кнопку.",
                reply_markup=get_main_keyboard()
            )
            return
        await update.message.reply_text(
            "💰 ЧТО ВАМ НУЖНО?",
            reply_markup=get_operation_keyboard()
        )
    elif text == "⭐ ОТЗЫВЫ КЛИЕНТОВ":
        await show_reviews(update, context)
    elif text == "📜 ПРАВИЛА":
        rules = db.get_setting('rules')
        await update.message.reply_text(rules, parse_mode="Markdown", reply_markup=get_main_keyboard())
    elif text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
    elif text == "📋 ЗАЯВКИ" and update.effective_user.id == ADMIN_ID:
        await show_requests_list(update, context)
    elif text == "⚙️ НАСТРОЙКИ" and update.effective_user.id == ADMIN_ID:
        await show_settings(update, context)
    elif text == "📊 СТАТИСТИКА" and update.effective_user.id == ADMIN_ID:
        await show_admin_stats(update, context)
    elif text == "🚫 ЗАБАНЕННЫЕ" and update.effective_user.id == ADMIN_ID:
        await show_banned_users(update, context)
    elif text == "◀️ ВЫЙТИ" and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔐 Выход из админ-панели.", reply_markup=get_main_keyboard())
    elif text == "/admin" and update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    if user_id is None:
        user_id = update.effective_user.id
    stats = db.get_client_stats(user_id)
    user = await context.bot.get_chat(user_id)
    if not stats or stats[0] == 0:
        await update.message.reply_text(
            f"👤 **ПРОФИЛЬ** | @{user.username or user_id}\n\n"
            f"📊 У вас пока нет сделок.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return
    deals = stats[0]
    volume = stats[1]
    rating = stats[2] if stats[2] else 0
    rating_count = stats[3] if stats[3] else 0
    rank_name, rank_emoji, discount, next_rank = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals - (next_rank if next_rank > 0 else deals % 15), 10)
    text = (
        f"👤 **ПРОФИЛЬ** | @{user.username or user_id}\n\n"
        f"🏆 РАНГ: {rank_emoji} {rank_name}\n"
        f"📊 ПРОГРЕСС: {progress_bar}\n"
        f"💰 СКИДКА: {discount}%\n\n"
        f"📈 **СТАТИСТИКА:**\n"
        f"• Сделок: {deals}\n"
        f"• Объём: {volume:.0f} ₽\n"
        f"• Рейтинг: ⭐ {rating:.1f} ({rating_count} отзывов)\n\n"
        f"📎 АКТИВНАЯ ЗАЯВКА: Нет"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = context.user_data.get('reviews_page', 0)
    limit = 5
    reviews = db.get_feedback_for_display(limit, page * limit)
    total = db.get_feedback_count()
    avg_rating = db.get_avg_rating()
    if not reviews:
        await update.message.reply_text("⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", reply_markup=get_main_keyboard())
        return
    text = f"⭐ **ОТЗЫВЫ КЛИЕНТОВ**\n\n"
    text += f"Всего отзывов: {total}\n"
    text += f"Средний рейтинг: {avg_rating:.1f} ⭐\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for review in reviews:
        rating_stars = "⭐" * (review[3] if review[3] else 0) if review[3] else "📝"
        text += f"👤 @{review[2]}\n"
        text += f"📅 {review[5][:10]}\n"
        if review[4]:
            text += f"💬 \"{review[4]}\"\n"
        if review[3]:
            text += f"Оценка: {rating_stars}\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    kb = []
    if total > (page + 1) * limit:
        kb.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb.append(InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main"))
    
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([kb])
    )

async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    banned = db.get_banned_users()
    if not banned:
        await update.message.reply_text("🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", reply_markup=get_admin_keyboard())
        return
    text = "🚫 **ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ**\n\n"
    for user in banned:
        text += f"👤 @{user[1] or user[0]}\n"
        text += f"📅 Забанен: {user[3][:10]}\n"
        text += f"📝 Причина: {user[2]}\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())

# ==================================================
# ================== АДМИН-ФУНКЦИИ =================
# ==================================================

async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    requests = db.get_all_active_requests()
    if not requests:
        await update.message.reply_text("📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return
    text = "📋 **ЗАЯВКИ В ОЖИДАНИИ:**\n\n"
    for req in requests:
        user = await context.bot.get_chat(req[1])
        username = user.username or str(req[1])
        text += f"#{req[0]} | @{username} | {req[2]:.0f} ₽ | {req[5][:16]}\n"
    text += "\n➡️ /take <id> — взять в работу\n➡️ /send <id> <текст> — отправить реквизиты\n➡️ /cancel <id> — отклонить\n➡️ /ban <id> <причина> — заблокировать"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "⚙️ **НАСТРОЙКИ**\n\n"
        "/edit_rules — редактировать правила\n"
        "/edit_schedule — редактировать график\n"
        "/edit_links — редактировать ссылки\n"
        "/afk on/off — режим не работаю",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    clients = db.get_all_clients()
    total_clients = len(clients)
    total_deals = sum(c[2] for c in clients)
    total_volume = sum(c[3] for c in clients)
    avg_rating = db.get_avg_rating()
    text = (
        f"📊 **СТАТИСТИКА**\n\n"
        f"• Клиентов: {total_clients}\n"
        f"• Сделок: {total_deals}\n"
        f"• Объём: {total_volume:.0f} ₽\n"
        f"• Рейтинг: ⭐ {avg_rating:.1f}\n"
        f"• Твоя прибыль (10%): {total_volume * 0.1:.0f} ₽"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())

# ==================================================
# ================== ОБРАБОТКА CALLBACK ============
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    banned, reason = db.is_banned(user_id)
    if banned and data not in ["back_to_main", "reviews_next"]:
        await query.edit_message_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {reason}",
            parse_mode="Markdown"
        )
        return
    
    if data == "back_to_main":
        await query.edit_message_text(
            "🏠 ГЛАВНОЕ МЕНЮ",
            reply_markup=get_operation_keyboard()
        )
    
    elif data == "reviews_next":
        page = context.user_data.get('reviews_page', 0) + 1
        context.user_data['reviews_page'] = page
        await query.edit_message_text("Загрузка...")
        await show_reviews(update, context)
    
    elif data.startswith("type_"):
        op_type = data[5:]
        if op_type == "oxapay":
            context.user_data['operation_type'] = OPERATION_OXAPAY
        elif op_type == "bitpapa":
            context.user_data['operation_type'] = OPERATION_BITPAPA
        elif op_type == "crypto":
            context.user_data['operation_type'] = OPERATION_CRYPTO
        elif op_type == "shop":
            context.user_data['operation_type'] = OPERATION_SHOP
        
        await query.edit_message_text(
            f"💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\n"
            f"(Напишите число, например: 3000)\n\n"
            f"◀️ Нажмите «НАЗАД» для отмены",
            reply_markup=get_back_keyboard("back_to_main")
        )
        context.user_data['awaiting_amount'] = True
    
    elif data == "edit_amount":
        await query.edit_message_text(
            f"💰 ВВЕДИТЕ НОВУЮ СУММУ В РУБЛЯХ\n\n"
            f"(Напишите число, например: 3500)\n\n"
            f"◀️ Нажмите «НАЗАД» для отмены",
            reply_markup=get_back_keyboard("back_to_main")
        )
        context.user_data['awaiting_amount'] = True
        context.user_data['editing'] = True
    
    elif data == "get_requisites":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type', OPERATION_OXAPAY)
        client_total = calculate_client_total(amount)
        
        request_id = db.add_request(user_id, op_type, amount, client_total)
        
        await query.edit_message_text(
            f"✅ **ЗАЯВКА #{request_id} ПРИНЯТА!**\n\n"
            f"Тип: {op_type}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"К оплате: {client_total:.0f} ₽\n\n"
            f"Статус: ⏳ ожидает обработки\n\n"
            f"Оператор скоро предоставит реквизиты.\n\n"
            f"🚫 ОТМЕНИТЬ ЗАЯВКУ — нажмите кнопку ниже",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard(request_id)
        )
        
        user = await context.bot.get_chat(user_id)
        username = user.username or str(user_id)
        
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 **НОВАЯ ЗАЯВКА #{request_id}**\n\n"
            f"Тип: {op_type}\n"
            f"Клиент: @{username}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"К оплате: {client_total:.0f} ₽\n\n"
            f"✅ /take {request_id} — взять в работу\n"
            f"📤 /send {request_id} <текст> — отправить реквизиты"
        )
        
        context.user_data['awaiting_amount'] = False
        context.user_data['temp_amount'] = None
        context.user_data['operation_type'] = None
    
    elif data.startswith("cancel_"):
        request_id = int(data.split("_")[1])
        request = db.get_request(request_id)
        if not request:
            await query.edit_message_text("❌ Заявка не найдена.", reply_markup=get_operation_keyboard())
            return
        db.cancel_request(request_id, "user")
        await query.edit_message_text(
            f"✅ **ЗАЯВКА #{request_id} ОТМЕНЕНА**\n\n"
            f"Вы можете создать новую заявку.",
            reply_markup=get_operation_keyboard()
        )
        user = await context.bot.get_chat(user_id)
        username = user.username or str(user_id)
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 Пользователь @{username} отменил заявку #{request_id}"
        )
    
    elif data.startswith("rate_"):
        rating = int(data.split("_")[1])
        request_id = context.user_data.get('rating_request_id')
        if request_id:
            db.add_feedback(user_id, request_id, rating, None)
            await query.edit_message_text(
                f"✅ Спасибо за оценку {rating}⭐!",
                reply_markup=get_main_keyboard()
            )
            context.user_data['rating_request_id'] = None

# ==================================================
# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
# ==================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    if update.message is None:
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Пропускаем команды
    if text.startswith('/'):
        return
    
    banned, reason = db.is_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {reason}\n\nПо вопросам разблокировки: @svenobmen",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return
    
    if user_id != ADMIN_ID and afk_mode:
        await update.message.reply_text(
            "⚠️ ОПЕРАТОР ВРЕМЕННО НЕДОСТУПЕН.\n"
            "Ваша заявка будет обработана позже.",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Обработка ввода суммы
    if context.user_data.get('awaiting_amount'):
        match = re.search(r"(\d+)", text)
        if not match:
            await update.message.reply_text(
                "❌ Введите число (сумму в рублях).",
                reply_markup=get_main_keyboard()
            )
            return
        amount = float(match.group(1))
        if amount < 1000:
            await update.message.reply_text(
                "❌ Минимальная сумма: 1000 ₽",
                reply_markup=get_main_keyboard()
            )
            return
        
        client_total = calculate_client_total(amount)
        op_type = context.user_data.get('operation_type', OPERATION_OXAPAY)
        
        warning_text = (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ **ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ!**\n\n"
            "После нажатия кнопки «✅ ПОЛУЧИТЬ РЕКВИЗИТЫ»:\n\n"
            "• Вы обязуетесь оплатить выставленный счёт\n"
            "• Неоплата влечёт блокировку аккаунта (БАН)\n"
            "• Вы больше не сможете пользоваться сервисом\n\n"
            "Нажимая «✅ ПОЛУЧИТЬ РЕКВИЗИТЫ», вы подтверждаете, \n"
            "что готовы оплатить счёт в полном объёме.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        
        context.user_data['temp_amount'] = amount
        context.user_data['temp_client_total'] = client_total
        context.user_data['awaiting_amount'] = False
        
        await update.message.reply_text(
            f"📝 **ПРОВЕРЬТЕ ДАННЫЕ**\n\n"
            f"Тип: {op_type}\n"
            f"Сумма: {amount:.0f} ₽\n"
            f"💸 К ОПЛАТЕ: {client_total:.0f} ₽\n\n"
            f"{warning_text}",
            parse_mode="Markdown",
            reply_markup=get_confirm_with_warning_keyboard()
        )
        return
    
    # Обработка PDF файла
    if update.message.document and update.message.document.mime_type == 'application/pdf':
        active = db.get_user_active_request(user_id)
        if not active:
            await update.message.reply_text(
                "❌ У вас нет активной заявки.",
                reply_markup=get_main_keyboard()
            )
            return
        request_id = active[0]
        db.mark_paid(request_id, update.message.document.file_id)
        await update.message.reply_text(
            "✅ **ЧЕК ПОЛУЧЕН!**\n\n"
            "Спасибо! Оператор проверит его в ближайшее время.\n\n"
            "Статус: 🔍 чек на проверке",
            reply_markup=get_main_keyboard()
        )
        user = await context.bot.get_chat(user_id)
        username = user.username or str(user_id)
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 **ПОЛУЧЕН PDF ЧЕК**\n\n"
            f"Заявка #{request_id}\n"
            f"Клиент: @{username}\n\n"
            f"✅ /confirm {request_id} — подтвердить\n"
            f"❌ /reject {request_id} — отклонить"
        )
        return
    
    # Текстовый отзыв
    if context.user_data.get('awaiting_feedback'):
        comment = text
        request_id = context.user_data.get('rating_request_id')
        if request_id:
            db.add_feedback(user_id, request_id, None, comment)
            await update.message.reply_text(
                f"✅ Спасибо за ваш отзыв!\n\n\"{comment}\"\n\nЭто поможет нам стать лучше. 🔥",
                reply_markup=get_main_keyboard()
            )
            context.user_data['awaiting_feedback'] = False
            context.user_data['rating_request_id'] = None
        else:
            await update.message.reply_text(
                "❌ Не удалось сохранить отзыв. Попробуйте позже.",
                reply_markup=get_main_keyboard()
            )
        return
    
    # Если ничего не подошло
    await update.message.reply_text(
        "Используйте кнопки меню для навигации.",
        reply_markup=get_main_keyboard()
    )

# ==================================================
# ================== КОМАНДЫ АДМИНА ================
# ==================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ /take <id>")
        return
    try:
        request_id = int(args[0])
    except:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    if request[5] != STATUS_PENDING:
        await update.message.reply_text(f"❌ Заявка #{request_id} уже обработана")
        return
    db.take_request(request_id)
    await update.message.reply_text(f"✅ Заявка #{request_id} взята в работу\n\nТеперь отправьте реквизиты: /send {request_id} <текст реквизитов>")
    await context.bot.send_message(
        request[1],
        f"✅ ЗАЯВКА #{request_id} ПРИНЯТА В РАБОТУ!\n\n"
        f"Статус: ⏳ оператор готовит реквизиты\n\n"
        f"Ожидайте, скоро они появятся в этом чате.",
        reply_markup=get_main_keyboard()
    )

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ /send <id> <текст реквизитов>")
        return
    try:
        request_id = int(args[0])
    except:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    requisites_text = " ".join(args[1:])
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    db.send_requisites(request_id, requisites_text)
    
    warning = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ **НАПОМИНАНИЕ!**\n\n"
        "Вы подтвердили готовность оплатить счёт.\n\n"
        "Неоплата влечёт **БЛОКИРОВКУ аккаунта**.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await context.bot.send_message(
        request[1],
        f"✅ **ЗАЯВКА #{request_id} | РЕКВИЗИТЫ ПОЛУЧЕНЫ**\n\n"
        f"💸 СУММА К ОПЛАТЕ: {request[4]:.0f} ₽\n\n"
        f"📋 **РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:**\n{requisites_text}\n\n"
        f"{warning}\n\n"
        f"📎 **ПОСЛЕ ОПЛАТЫ ПРИШЛИТЕ ЧЕК PDF.**",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard(request_id)
    )
    await update.message.reply_text(f"✅ Реквизиты отправлены клиенту по заявке #{request_id}")

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ /confirm <id>")
        return
    try:
        request_id = int(args[0])
    except:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    db.complete_request(request_id, request[1], request[3])
    await update.message.reply_text(f"✅ Заявка #{request_id} завершена")
    context.user_data['rating_request_id'] = request_id
    context.user_data['awaiting_feedback'] = True
    await context.bot.send_message(
        request[1],
        f"✅ **ЗАЯВКА #{request_id} ЗАВЕРШЕНА!**\n\n"
        f"Сумма: {request[3]:.0f} ₽ → оплачено {request[4]:.0f} ₽\n\n"
        f"⭐ **Оцените нашу работу:**\n\n"
        f"✏️ Напишите отзыв текстом\n"
        f"Или нажмите /skip чтобы пропустить",
        parse_mode="Markdown",
        reply_markup=get_rating_keyboard()
    )

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ /reject <id>")
        return
    try:
        request_id = int(args[0])
    except:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    db.cancel_request(request_id, "admin")
    await update.message.reply_text(f"✅ Заявка #{request_id} отклонена")
    await context.bot.send_message(
        request[1],
        f"❌ **ЗАЯВКА #{request_id} ОТКЛОНЕНА.**\n\n"
        f"Причина: чек не соответствует требованиям.\n\n"
        f"Вы можете создать новую заявку: /start",
        reply_markup=get_main_keyboard()
    )

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ /ban @username <причина>")
        return
    username = args[0].replace("@", "")
    reason = " ".join(args[1:])
    user_id = db.find_user_id_by_username(username)
    if not user_id:
        await update.message.reply_text(f"❌ Пользователь @{username} не найден")
        return
    db.ban_user(user_id, reason)
    await update.message.reply_text(f"✅ Пользователь @{username} заблокирован\nПричина: {reason}")
    await context.bot.send_message(
        user_id,
        f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\n"
        f"Причина: {reason}\n\n"
        f"По вопросам разблокировки: @svenobmen",
        reply_markup=get_main_keyboard()
    )

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ /unban @username")
        return
    username = args[0].replace("@", "")
    user_id = db.find_user_id_by_username(username)
    if not user_id:
        await update.message.reply_text(f"❌ Пользователь @{username} не найден")
        return
    db.unban_user(user_id)
    await update.message.reply_text(f"✅ Пользователь @{username} разблокирован")
    await context.bot.send_message(
        user_id,
        f"✅ **ДОСТУП ВОССТАНОВЛЕН**\n\n"
        f"Вы можете снова пользоваться сервисом.\n\n"
        f"/start",
        reply_markup=get_main_keyboard()
    )

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['awaiting_feedback'] = False
    context.user_data['rating_request_id'] = None
    await update.message.reply_text(
        "✅ Отзыв пропущен. Спасибо за обращение!",
        reply_markup=get_main_keyboard()
    )

async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'rules'
    await update.message.reply_text(
        "📝 Введите новый текст правил (можно с Markdown):\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{db.get_setting('rules')}"
    )

async def edit_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'schedule'
    await update.message.reply_text(
        "📝 Введите новый текст графика работы:\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{db.get_setting('schedule')}"
    )

async def edit_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'links'
    await update.message.reply_text(
        "📝 Введите новый текст полезных ссылок:\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{db.get_setting('links')}"
    )

async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    editing = context.user_data.get('editing')
    if editing:
        db.update_setting(editing, update.message.text)
        await update.message.reply_text(f"✅ {editing} обновлены!", reply_markup=get_admin_keyboard())
        context.user_data['editing'] = None

async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ /afk on  или  /afk off")
        return
    if args[0].lower() == "on":
        afk_mode = True
        await update.message.reply_text("✅ Режим «Не работаю» ВКЛЮЧЁН")
    elif args[0].lower() == "off":
        afk_mode = False
        await update.message.reply_text("✅ Режим «Не работаю» ВЫКЛЮЧЁН")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID and context.args:
        username = context.args[0].replace("@", "")
        user_data = db.find_user_by_username(username)
        if user_data:
            await show_profile(update, context, user_data[0])
        else:
            await update.message.reply_text(f"❌ Клиент @{username} не найден")
        return
    await show_profile(update, context, user_id)

# ==================================================
# ==================== ЗАПУСК ======================
# ==================================================

def main():
    logging.basicConfig(level=logging.INFO)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Команды для клиентов
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("skip", skip_command))
    
    # Команды для админа
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("take", take_command))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("confirm", confirm_command))
    app.add_handler(CommandHandler("reject", reject_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("afk", afk_command))
    app.add_handler(CommandHandler("edit_rules", edit_rules_command))
    app.add_handler(CommandHandler("edit_schedule", edit_schedule_command))
    app.add_handler(CommandHandler("edit_links", edit_links_command))
    
    # Обработчики
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit))
    
    print("✅ БОТ ЗАПУЩЕН. SVEN OBMEN")
    app.run_polling()


if __name__ == "__main__":
    main()
