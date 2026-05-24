import re
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Tuple, List

import aiohttp
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
ADMIN_ID = 5243173039  # Замените на свой Telegram ID

CACHE_TIME_SECONDS = 3600
MIN_AMOUNT = 1000
MAX_AMOUNT = 1000000

# Статусы заявок
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REQUISITES_SENT = "requisites_sent"
STATUS_PAID = "paid"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED_BY_USER = "cancelled_by_user"
STATUS_CANCELLED_BY_ADMIN = "cancelled_by_admin"

CANCELLABLE_STATUSES = [STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT]

# Типы операций
OPERATION_OXAPAY = "Оплата счёта OxaPay"
OPERATION_BITPAPA = "Создание чека Bitpapa"
OPERATION_CRYPTO = "Покупка крипты на кошелёк"
OPERATION_SHOP = "Отправка на кошелёк магазина"

# Режим AFK
afk_lock = threading.Lock()
afk_mode = False

# ==================================================
# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
# ==================================================

def escape_markdown(text: str) -> str:
    """Экранирует специальные символы MarkdownV2"""
    if not text:
        return ""
    escape_chars = r'_[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in escape_chars else c for c in str(text))

def get_username_or_id(user) -> str:
    """Безопасно получает username или ID"""
    if hasattr(user, 'username') and user.username:
        return f"@{user.username}"
    return str(user.id) if hasattr(user, 'id') else str(user)

# ==================================================
# ================== БАЗА ДАННЫХ ===================
# ==================================================

class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.cursor = self.conn.cursor()
        self._create_tables()
        self._init_settings()

    def _create_tables(self):
        with self.lock:
            self.cursor.executescript("""
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
                    created_at TEXT,
                    last_request_at TEXT
                );
                
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
                );
                
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    request_id INTEGER,
                    rating INTEGER CHECK(rating BETWEEN 1 AND 5),
                    comment TEXT,
                    created_at TEXT,
                    is_displayed INTEGER DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES clients(user_id),
                    FOREIGN KEY (request_id) REFERENCES requests(id)
                );
                
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                
                CREATE INDEX IF NOT EXISTS idx_requests_user_status 
                    ON requests(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_requests_status 
                    ON requests(status);
                CREATE INDEX IF NOT EXISTS idx_clients_username 
                    ON clients(username);
            """)
            self.conn.commit()

    def _init_settings(self):
        defaults = {
            'rules': ('📜 **ПРАВИЛА РАБОТЫ**\n\n'
                     '• Минимальная сумма: 1000 ₽\n'
                     '• Комиссия: 10%\n'
                     '• Работаем 24/7\n'
                     '• Чек PDF обязателен\n'
                     '• Неоплата счёта влечёт блокировку'),
            'schedule': ('⏰ **ГРАФИК РАБОТЫ**\n\n'
                        '• Пн–Вс: 24/7\n'
                        '• Без выходных'),
            'links': ('🔗 **ПОЛЕЗНЫЕ ССЫЛКИ**\n\n'
                     '• 📢 Канал: https://t.me/svenobmen\n'
                     '• 📊 Bitpapa: https://bitpapa.com\n'
                     '• 💬 Поддержка: @svenobmen')
        }
        with self.lock:
            for key, value in defaults.items():
                self.cursor.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value)
                )
            self.conn.commit()

    def execute_query(self, query: str, params: tuple = None, fetchone: bool = False, fetchall: bool = False):
        """Потокобезопасное выполнение запросов"""
        with self.lock:
            if params:
                self.cursor.execute(query, params)
            else:
                self.cursor.execute(query)
            
            if fetchone:
                result = self.cursor.fetchone()
                return dict(result) if result else None
            elif fetchall:
                return [dict(row) for row in self.cursor.fetchall()]
            
            self.conn.commit()
            return self.cursor.lastrowid

    def add_client(self, user_id: int, username: str):
        now = datetime.now().isoformat()
        self.execute_query(
            "INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)",
            (user_id, username, now)
        )

    def is_banned(self, user_id: int) -> Tuple[bool, Optional[str]]:
        row = self.execute_query(
            "SELECT is_banned, ban_reason FROM clients WHERE user_id = ?",
            (user_id,), fetchone=True
        )
        if row and row['is_banned'] == 1:
            return True, row['ban_reason']
        return False, None

    def ban_user(self, user_id: int, reason: str):
        now = datetime.now().isoformat()
        self.execute_query(
            "UPDATE clients SET is_banned = 1, ban_reason = ?, banned_at = ? WHERE user_id = ?",
            (reason, now, user_id)
        )

    def unban_user(self, user_id: int):
        self.execute_query(
            "UPDATE clients SET is_banned = 0, ban_reason = NULL, banned_at = NULL WHERE user_id = ?",
            (user_id,)
        )

    def get_banned_users(self) -> List[dict]:
        return self.execute_query(
            "SELECT user_id, username, ban_reason, banned_at FROM clients WHERE is_banned = 1",
            fetchall=True
        )

    def update_client_after_deal(self, user_id: int, amount: float):
        self.execute_query(
            "UPDATE clients SET total_deals = total_deals + 1, total_volume = total_volume + ? WHERE user_id = ?",
            (amount, user_id)
        )

    def get_client_stats(self, user_id: int) -> Optional[dict]:
        return self.execute_query(
            "SELECT total_deals, total_volume, avg_rating, ratings_count FROM clients WHERE user_id = ?",
            (user_id,), fetchone=True
        )

    def get_all_clients(self) -> List[dict]:
        return self.execute_query(
            "SELECT user_id, username, total_deals, total_volume FROM clients WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20",
            fetchall=True
        )

    def find_user_by_username(self, username: str) -> Optional[dict]:
        return self.execute_query(
            "SELECT user_id, username FROM clients WHERE username = ?",
            (username,), fetchone=True
        )

    def find_user_id_by_username(self, username: str) -> Optional[int]:
        row = self.execute_query(
            "SELECT user_id FROM clients WHERE username = ?",
            (username,), fetchone=True
        )
        return row['user_id'] if row else None

    def add_request(self, user_id: int, operation_type: str, amount: float, client_total: float) -> int:
        now = datetime.now().isoformat()
        request_id = self.execute_query(
            "INSERT INTO requests (user_id, operation_type, amount, client_total, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, operation_type, amount, client_total, STATUS_PENDING, now)
        )
        self.execute_query(
            "UPDATE clients SET last_request_at = ? WHERE user_id = ?",
            (now, user_id)
        )
        return request_id

    def get_request(self, request_id: int) -> Optional[dict]:
        return self.execute_query(
            "SELECT * FROM requests WHERE id = ?",
            (request_id,), fetchone=True
        )

    def get_user_active_request(self, user_id: int) -> Optional[dict]:
        return self.execute_query(
            """SELECT id, operation_type, amount, client_total, status 
               FROM requests 
               WHERE user_id = ? AND status IN (?, ?, ?, ?) 
               ORDER BY id DESC LIMIT 1""",
            (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID),
            fetchone=True
        )

    def get_all_pending_requests(self) -> List[dict]:
        return self.execute_query(
            "SELECT id, user_id, amount, client_total, status, created_at FROM requests WHERE status = ? ORDER BY created_at DESC",
            (STATUS_PENDING,), fetchall=True
        )

    def get_all_processing_requests(self) -> List[dict]:
        return self.execute_query(
            "SELECT id, user_id, amount, client_total, status, created_at FROM requests WHERE status IN (?, ?) ORDER BY created_at DESC",
            (STATUS_PROCESSING, STATUS_REQUISITES_SENT), fetchall=True
        )

    def take_request(self, request_id: int) -> bool:
        """Возвращает True если заявка успешно взята"""
        now = datetime.now().isoformat()
        request = self.get_request(request_id)
        if not request or request['status'] != STATUS_PENDING:
            return False
        self.execute_query(
            "UPDATE requests SET status = ?, taken_at = ? WHERE id = ? AND status = ?",
            (STATUS_PROCESSING, now, request_id, STATUS_PENDING)
        )
        return True

    def send_requisites(self, request_id: int, requisites_text: str) -> bool:
        now = datetime.now().isoformat()
        request = self.get_request(request_id)
        if not request or request['status'] != STATUS_PROCESSING:
            return False
        self.execute_query(
            "UPDATE requests SET status = ?, requisites_sent_at = ?, requisites_text = ? WHERE id = ? AND status = ?",
            (STATUS_REQUISITES_SENT, now, requisites_text, request_id, STATUS_PROCESSING)
        )
        return True

    def mark_paid(self, request_id: int, pdf_file_id: str) -> bool:
        now = datetime.now().isoformat()
        request = self.get_request(request_id)
        if not request or request['status'] != STATUS_REQUISITES_SENT:
            return False
        self.execute_query(
            "UPDATE requests SET status = ?, paid_at = ?, pdf_file_id = ? WHERE id = ? AND status = ?",
            (STATUS_PAID, now, pdf_file_id, request_id, STATUS_REQUISITES_SENT)
        )
        return True

    def complete_request(self, request_id: int) -> bool:
        now = datetime.now().isoformat()
        request = self.get_request(request_id)
        if not request or request['status'] != STATUS_PAID:
            return False
        self.execute_query(
            "UPDATE requests SET status = ?, completed_at = ? WHERE id = ? AND status = ?",
            (STATUS_COMPLETED, now, request_id, STATUS_PAID)
        )
        self.update_client_after_deal(request['user_id'], request['amount'])
        return True

    def cancel_request(self, request_id: int, cancelled_by: str) -> bool:
        now = datetime.now().isoformat()
        request = self.get_request(request_id)
        if not request or request['status'] not in CANCELLABLE_STATUSES:
            return False
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        self.execute_query(
            "UPDATE requests SET status = ?, cancelled_at = ?, cancelled_by = ? WHERE id = ?",
            (status, now, cancelled_by, request_id)
        )
        return True

    def add_feedback(self, user_id: int, request_id: int, rating: int = None, comment: str = None):
        now = datetime.now().isoformat()
        self.execute_query(
            "INSERT INTO feedback (user_id, request_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, request_id, rating, comment, now)
        )
        if rating is not None:
            self._update_user_rating(user_id)

    def _update_user_rating(self, user_id: int):
        row = self.execute_query(
            "SELECT AVG(rating) as avg, COUNT(*) as count FROM feedback WHERE user_id = ? AND rating IS NOT NULL",
            (user_id,), fetchone=True
        )
        if row['avg'] is not None:
            self.execute_query(
                "UPDATE clients SET avg_rating = ?, ratings_count = ? WHERE user_id = ?",
                (row['avg'], row['count'], user_id)
            )

    def get_feedback_for_display(self, limit: int = 5, offset: int = 0) -> List[dict]:
        return self.execute_query(
            """SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at
               FROM feedback f
               JOIN clients c ON f.user_id = c.user_id
               WHERE f.is_displayed = 1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL)
               ORDER BY f.created_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset), fetchall=True
        )

    def get_feedback_count(self) -> int:
        row = self.execute_query("SELECT COUNT(*) as count FROM feedback WHERE is_displayed = 1", fetchone=True)
        return row['count']

    def get_avg_rating(self) -> float:
        row = self.execute_query("SELECT AVG(rating) as avg FROM feedback WHERE rating IS NOT NULL", fetchone=True)
        return row['avg'] if row['avg'] else 0

    def get_setting(self, key: str) -> Optional[str]:
        row = self.execute_query("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)
        return row['value'] if row else None

    def update_setting(self, key: str, value: str):
        self.execute_query("UPDATE settings SET value = ? WHERE key = ?", (value, key))

    def close(self):
        self.conn.close()


db = Database()

# ==================================================
# ================== КУРС ВАЛЮТ ====================
# ==================================================

cached_rate = None
cached_time = 0
rate_lock = threading.Lock()

async def get_usdt_rate():
    global cached_rate, cached_time
    now = datetime.now().timestamp()
    
    with rate_lock:
        if cached_rate and (now - cached_time) < CACHE_TIME_SECONDS:
            return cached_rate
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    new_rate = float(data['price'])
                    with rate_lock:
                        cached_rate = new_rate
                        cached_time = now
                    return new_rate
    except Exception as e:
        logging.error(f"Error fetching USDT rate: {e}")
    
    return 92.5

# ==================================================
# ================== ЛОГИКА РАСЧЁТА ================
# ==================================================

def calculate_client_total(amount: float) -> float:
    return amount * 1.169 + 285

# ==================================================
# ================== РАНГИ =========================
# ==================================================

RANKS = [
    {"name": "Новичок", "emoji": "🟢", "min_deals": 0, "max_deals": 3},
    {"name": "Ходок", "emoji": "🔵", "min_deals": 3, "max_deals": 7},
    {"name": "Опытный", "emoji": "🟠", "min_deals": 7, "max_deals": 10},
    {"name": "Мастер", "emoji": "🟣", "min_deals": 10, "max_deals": 15},
    {"name": "Легенда", "emoji": "🔥", "min_deals": 15, "max_deals": float('inf')},
]

def get_rank_info(deals: int) -> dict:
    for rank in RANKS:
        if deals < rank['max_deals']:
            progress_current = deals - rank['min_deals']
            progress_total = rank['max_deals'] - rank['min_deals']
            deals_to_next = rank['max_deals'] - deals
            return {
                "name": rank['name'],
                "emoji": rank['emoji'],
                "discount": 1 if rank['name'] == "Легенда" else 0,
                "deals_to_next": deals_to_next,
                "progress_percent": int((progress_current / progress_total * 100)) if progress_total > 0 else 100
            }
    # Если не нашли (маловероятно), возвращаем последний ранг
    last_rank = RANKS[-1]
    return {
        "name": last_rank['name'],
        "emoji": last_rank['emoji'],
        "discount": 1,
        "deals_to_next": 0,
        "progress_percent": 100
    }

def get_progress_bar(percent: int, length: int = 10) -> str:
    filled = max(0, min(length, int(length * percent / 100)))
    return "▰" * filled + "▱" * (length - filled)

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
        [InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{i}") for i in range(5, 2, -1)],
        [InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{i}") for i in range(2, 0, -1)]
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
            f"Причина: {escape_markdown(reason)}\n\n"
            f"По вопросам разблокировки: @svenobmen",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
        return
    
    await update.message.reply_text(
        f"👋 ПРИВЕТСТВУЮ, {escape_markdown(user.first_name)}!\n\n"
        f"SVEN OBMEN — помощь с криптовалютными задачами.\n\n"
        f"➡️ НАЧНИТЕ С КНОПКИ НИЖЕ ⬇️",
        reply_markup=get_main_keyboard()
    )

async def handle_all_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый обработчик всех текстовых сообщений"""
    user_id = update.effective_user.id
    
    # Проверка: не редактирует ли админ настройки
    if user_id == ADMIN_ID and context.user_data.get('editing'):
        await save_edit(update, context)
        return
    
    # Проверка: ожидается ли ввод суммы
    if context.user_data.get('awaiting_amount'):
        await handle_amount_input(update, context)
        return
    
    # Проверка: ожидается ли отзыв
    if context.user_data.get('awaiting_feedback'):
        await handle_feedback_input(update, context)
        return
    
    # Если ничего из вышеперечисленного - обрабатываем как меню
    await handle_menu(update, context)

async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода суммы"""
    text = update.message.text.strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    
    if not match:
        await update.message.reply_text(
            "❌ Введите число (сумму в рублях).\n\n"
            "Пример: 3000",
            reply_markup=get_back_keyboard()
        )
        return
    
    try:
        amount = float(match.group(1))
    except ValueError:
        await update.message.reply_text(
            "❌ Некорректное число. Попробуйте еще раз.",
            reply_markup=get_back_keyboard()
        )
        return
    
    if amount < MIN_AMOUNT:
        await update.message.reply_text(
            f"❌ Минимальная сумма: {MIN_AMOUNT} ₽\n"
            f"Пожалуйста, введите сумму не менее {MIN_AMOUNT} ₽",
            reply_markup=get_back_keyboard()
        )
        return
    
    if amount > MAX_AMOUNT:
        await update.message.reply_text(
            f"❌ Максимальная сумма: {MAX_AMOUNT:,} ₽\n"
            f"Пожалуйста, введите меньшую сумму",
            reply_markup=get_back_keyboard()
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
    context.user_data['awaiting_amount'] = False
    
    await update.message.reply_text(
        f"📝 **ПРОВЕРЬТЕ ДАННЫЕ**\n\n"
        f"Тип: {escape_markdown(op_type)}\n"
        f"Сумма: {amount:,.0f} ₽\n"
        f"💸 К ОПЛАТЕ: {client_total:,.0f} ₽\n\n"
        f"{warning_text}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=get_confirm_with_warning_keyboard()
    )

async def handle_feedback_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового отзыва"""
    comment = update.message.text.strip()
    user_id = update.effective_user.id
    
    if len(comment) > 500:
        await update.message.reply_text(
            "❌ Отзыв слишком длинный. Максимум 500 символов.",
            reply_markup=get_main_keyboard()
        )
        return
    
    request_id = context.user_data.get('rating_request_id')
    if request_id:
        db.add_feedback(user_id, request_id, None, comment)
        await update.message.reply_text(
            f"✅ Спасибо за ваш отзыв!\n\n"
            f"\"{escape_markdown(comment)}\"\n\n"
            f"Это поможет нам стать лучше. 🔥",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "✅ Спасибо за ваш отзыв!",
            reply_markup=get_main_keyboard()
        )
    
    context.user_data['awaiting_feedback'] = False
    context.user_data['rating_request_id'] = None

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок меню"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # Проверка бана для всех кроме админа
    if user_id != ADMIN_ID:
        banned, reason = db.is_banned(user_id)
        if banned:
            await update.message.reply_text(
                f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {escape_markdown(reason)}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=get_main_keyboard()
            )
            return
    
    # Обработка клиентских кнопок
    if text == "🔥 НОВЫЙ ЗАПРОС":
        active = db.get_user_active_request(user_id)
        if active:
            await update.message.reply_text(
                f"⚠️ У вас уже есть активная заявка #{active['id']}.\n"
                f"Дождитесь её обработки или отмените через кнопку.",
                reply_markup=get_main_keyboard()
            )
            return
        await update.message.reply_text(
            "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:",
            reply_markup=get_operation_keyboard()
        )
        return
    
    elif text == "⭐ ОТЗЫВЫ КЛИЕНТОВ":
        await show_reviews(update, context)
        return
    
    elif text == "📜 ПРАВИЛА":
        rules = db.get_setting('rules')
        await update.message.reply_text(
            rules, 
            parse_mode=ParseMode.MARKDOWN_V2, 
            reply_markup=get_main_keyboard()
        )
        return
    
    elif text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
        return
    
    # Обработка админских кнопок
    if user_id == ADMIN_ID:
        if text == "📋 ЗАЯВКИ":
            await show_requests_list(update, context)
            return
        elif text == "⚙️ НАСТРОЙКИ":
            await show_settings(update, context)
            return
        elif text == "📊 СТАТИСТИКА":
            await show_admin_stats(update, context)
            return
        elif text == "🚫 ЗАБАНЕННЫЕ":
            await show_banned_users(update, context)
            return
        elif text == "◀️ ВЫЙТИ":
            await update.message.reply_text(
                "🔐 Выход из админ-панели.", 
                reply_markup=get_main_keyboard()
            )
            return
        elif text == "/admin":
            await update.message.reply_text(
                "🔐 АДМИН-ПАНЕЛЬ", 
                reply_markup=get_admin_keyboard()
            )
            return
    
    # Если сообщение не распознано
    await update.message.reply_text(
        "Используйте кнопки меню для навигации.\n"
        "Если вы хотите создать заявку, нажмите «🔥 НОВЫЙ ЗАПРОС»",
        reply_markup=get_main_keyboard()
    )

async def handle_pdf_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка PDF документов"""
    user_id = update.effective_user.id
    
    # Проверка на бан
    banned, reason = db.is_banned(user_id)
    if banned:
        await update.message.reply_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {escape_markdown(reason)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
        return
    
    # Режим AFK
    global afk_mode
    with afk_lock:
        is_afk = afk_mode
    
    if user_id != ADMIN_ID and is_afk:
        await update.message.reply_text(
            "⚠️ ОПЕРАТОР ВРЕМЕННО НЕДОСТУПЕН.\n"
            "Ваша заявка будет обработана позже.",
            reply_markup=get_main_keyboard()
        )
        return
    
    # Обработка PDF чека
    active = db.get_user_active_request(user_id)
    if not active:
        await update.message.reply_text(
            "❌ У вас нет активной заявки.\n\n"
            "Сначала создайте заявку через кнопку «🔥 НОВЫЙ ЗАПРОС»",
            reply_markup=get_main_keyboard()
        )
        return
    
    request_id = active['id']
    if db.mark_paid(request_id, update.message.document.file_id):
        await update.message.reply_text(
            "✅ **ЧЕК ПОЛУЧЕН!**\n\n"
            "Спасибо! Оператор проверит его в ближайшее время.\n\n"
            "Статус: 🔍 чек на проверке",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
        
        try:
            user = await context.bot.get_chat(user_id)
            username = get_username_or_id(user)
        except:
            username = str(user_id)
        
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 **ПОЛУЧЕН PDF ЧЕК**\n\n"
            f"👤 Клиент: {escape_markdown(username)} (ID: {user_id})\n"
            f"📋 Заявка #{request_id}\n\n"
            f"✅ /confirm {request_id} — подтвердить\n"
            f"❌ /reject {request_id} — отклонить",
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        await update.message.reply_text(
            "❌ Не удалось обработать чек. Возможно, заявка не в том статусе.\n"
            "Проверьте статус заявки или создайте новую.",
            reply_markup=get_main_keyboard()
        )

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None):
    """Показывает профиль пользователя"""
    if user_id is None:
        user_id = update.effective_user.id
    
    try:
        user = await context.bot.get_chat(user_id)
        username = get_username_or_id(user)
    except:
        username = str(user_id)
    
    stats = db.get_client_stats(user_id)
    
    if not stats or stats['total_deals'] == 0:
        await update.message.reply_text(
            f"👤 **ПРОФИЛЬ** | {escape_markdown(username)}\n\n"
            f"📊 У вас пока нет сделок.\n\n"
            f"Создайте первую заявку через кнопку «🔥 НОВЫЙ ЗАПРОС»",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
        return
    
    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    
    rank_info = get_rank_info(deals)
    progress_bar = get_progress_bar(rank_info['progress_percent'])
    
    # Проверяем активные заявки
    active_request = db.get_user_active_request(user_id)
    active_text = f"#{active_request['id']} ({active_request['status']})" if active_request else "Нет"
    
    text = (
        f"👤 **ПРОФИЛЬ** | {escape_markdown(username)}\n\n"
        f"{rank_info['emoji']} РАНГ: {escape_markdown(rank_info['name'])}\n"
        f"📊 ПРОГРЕСС: {escape_markdown(progress_bar)} {rank_info['progress_percent']}%\n"
        f"💰 СКИДКА: {rank_info['discount']}%\n\n"
        f"📈 **СТАТИСТИКА:**\n"
        f"• Сделок: {deals}\n"
        f"• Объём: {volume:,.0f} ₽\n"
        f"• Рейтинг: ⭐ {rating:.1f} ({rating_count} отзывов)\n\n"
        f"📎 АКТИВНАЯ ЗАЯВКА: {escape_markdown(active_text)}"
    )
    
    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.MARKDOWN_V2, 
        reply_markup=get_main_keyboard()
    )

async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает отзывы клиентов"""
    page = context.user_data.get('reviews_page', 0)
    limit = 5
    reviews = db.get_feedback_for_display(limit, page * limit)
    total = db.get_feedback_count()
    avg_rating = db.get_avg_rating()
    
    if not reviews:
        await update.message.reply_text(
            "⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", 
            reply_markup=get_main_keyboard()
        )
        return
    
    text = f"⭐ **ОТЗЫВЫ КЛИЕНТОВ**\n\n"
    text += f"Всего отзывов: {total}\n"
    text += f"Средний рейтинг: {avg_rating:.1f} ⭐\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    for review in reviews:
        rating_stars = "⭐" * review['rating'] if review['rating'] else "📝"
        username = escape_markdown(review['username'] or str(review['user_id']))
        text += f"👤 {username}\n"
        text += f"📅 {review['created_at'][:10]}\n"
        if review['comment']:
            text += f"💬 \"{escape_markdown(review['comment'])}\"\n"
        if review['rating']:
            text += f"Оценка: {rating_stars}\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    kb = []
    if total > (page + 1) * limit:
        kb.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb.append(InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_main"))
    
    # Определяем, откуда был вызван показ отзывов
    if hasattr(update, 'callback_query') and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([kb])
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([kb])
        )

async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список забаненных пользователей (только для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    banned = db.get_banned_users()
    if not banned:
        await update.message.reply_text(
            "🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", 
            reply_markup=get_admin_keyboard()
        )
        return
    
    text = "🚫 **ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ**\n\n"
    for user in banned:
        username = escape_markdown(user['username'] or str(user['user_id']))
        text += f"👤 {username} (ID: {user['user_id']})\n"
        text += f"📅 Забанен: {user['banned_at'][:10] if user['banned_at'] else 'Н/Д'}\n"
        text += f"📝 Причина: {escape_markdown(user['ban_reason'])}\n"
        text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.MARKDOWN_V2, 
        reply_markup=get_admin_keyboard()
    )

# ==================================================
# ================== АДМИН-ФУНКЦИИ =================
# ==================================================

async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список активных заявок (для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    pending_requests = db.get_all_pending_requests()
    processing_requests = db.get_all_processing_requests()
    
    if not pending_requests and not processing_requests:
        await update.message.reply_text(
            "📋 НЕТ АКТИВНЫХ ЗАЯВОК.\n\n"
            "Создайте заявку через клиентскую часть.",
            reply_markup=get_admin_keyboard()
        )
        return
    
    text = "📋 **ЗАЯВКИ**\n\n"
    
    if pending_requests:
        text += "🟡 **В ОЖИДАНИИ:**\n"
        for req in pending_requests:
            try:
                user = await context.bot.get_chat(req['user_id'])
                username = get_username_or_id(user)
            except:
                username = str(req['user_id'])
            text += f"  #{req['id']} | {escape_markdown(username)} | {req['amount']:,.0f} ₽\n"
        text += "\n"
    
    if processing_requests:
        text += "🟢 **В РАБОТЕ:**\n"
        for req in processing_requests:
            try:
                user = await context.bot.get_chat(req['user_id'])
                username = get_username_or_id(user)
            except:
                username = str(req['user_id'])
            text += f"  #{req['id']} | {escape_markdown(username)} | {req['amount']:,.0f} ₽\n"
        text += "\n"
    
    text += "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    text += "➡️ /take <id> — взять в работу\n"
    text += "➡️ /send <id> <текст> — отправить реквизиты\n"
    text += "➡️ /reject <id> — отклонить\n"
    text += "➡️ /ban @username <причина> — заблокировать"
    
    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.MARKDOWN_V2, 
        reply_markup=get_admin_keyboard()
    )

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает настройки (для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    await update.message.reply_text(
        "⚙️ **НАСТРОЙКИ**\n\n"
        "/edit\\_rules — редактировать правила\n"
        "/edit\\_schedule — редактировать график\n"
        "/edit\\_links — редактировать ссылки\n"
        "/afk on/off — режим не работаю",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=get_admin_keyboard()
    )

async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику (для админа)"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    clients = db.get_all_clients()
    total_clients = len(clients)
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)
    avg_rating = db.get_avg_rating()
    
    text = (
        f"📊 **СТАТИСТИКА**\n\n"
        f"• Клиентов: {total_clients}\n"
        f"• Сделок: {total_deals}\n"
        f"• Объём: {total_volume:,.0f} ₽\n"
        f"• Рейтинг: ⭐ {avg_rating:.1f}\n"
        f"• Прибыль (10%): {total_volume * 0.1:,.0f} ₽"
    )
    await update.message.reply_text(
        text, 
        parse_mode=ParseMode.MARKDOWN_V2, 
        reply_markup=get_admin_keyboard()
    )

# ==================================================
# ================== ОБРАБОТКА CALLBACK ============
# ==================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка callback-запросов от инлайн-кнопок"""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    # Проверка бана
    banned, reason = db.is_banned(user_id)
    if banned and data not in ["back_to_main", "reviews_next"]:
        await query.edit_message_text(
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\nПричина: {escape_markdown(reason)}",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return
    
    # Навигация
    if data == "back_to_main":
        await query.edit_message_text(
            "💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:",
            reply_markup=get_operation_keyboard()
        )
    
    elif data == "reviews_next":
        page = context.user_data.get('reviews_page', 0) + 1
        context.user_data['reviews_page'] = page
        await show_reviews(update, context)
    
    # Выбор типа операции
    elif data.startswith("type_"):
        operation_mapping = {
            "oxapay": OPERATION_OXAPAY,
            "bitpapa": OPERATION_BITPAPA,
            "crypto": OPERATION_CRYPTO,
            "shop": OPERATION_SHOP
        }
        op_type = data[5:]
        if op_type in operation_mapping:
            context.user_data['operation_type'] = operation_mapping[op_type]
            await query.edit_message_text(
                f"💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\n"
                f"(Напишите число, например: 3000)\n"
                f"Минимальная сумма: {MIN_AMOUNT:,} ₽\n\n"
                f"◀️ Нажмите «НАЗАД» для отмены",
                reply_markup=get_back_keyboard("back_to_main")
            )
            context.user_data['awaiting_amount'] = True
    
    elif data == "edit_amount":
        context.user_data['awaiting_amount'] = True
        context.user_data['editing'] = True
        await query.edit_message_text(
            f"💰 ВВЕДИТЕ НОВУЮ СУММУ В РУБЛЯХ\n\n"
            f"(Напишите число, например: 3500)\n"
            f"Минимальная сумма: {MIN_AMOUNT:,} ₽\n\n"
            f"◀️ Нажмите «НАЗАД» для отмены",
            reply_markup=get_back_keyboard("back_to_main")
        )
    
    elif data == "get_requisites":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type', OPERATION_OXAPAY)
        
        if not amount:
            await query.edit_message_text(
                "❌ Ошибка: сумма не найдена. Начните заново.",
                reply_markup=get_operation_keyboard()
            )
            return
        
        client_total = calculate_client_total(amount)
        request_id = db.add_request(user_id, op_type, amount, client_total)
        
        await query.edit_message_text(
            f"✅ **ЗАЯВКА #{request_id} ПРИНЯТА!**\n\n"
            f"📋 Тип: {escape_markdown(op_type)}\n"
            f"💰 Сумма: {amount:,.0f} ₽\n"
            f"💸 К оплате: {client_total:,.0f} ₽\n\n"
            f"👤 Ваш ID: {user_id}\n"
            f"📅 Создана: {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Статус: ⏳ ожидает обработки\n\n"
            f"Оператор скоро предоставит реквизиты.\n\n"
            f"🚫 ОТМЕНИТЬ ЗАЯВКУ — нажмите кнопку ниже",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_cancel_keyboard(request_id)
        )
        
        # Уведомление админу
        try:
            user = await context.bot.get_chat(user_id)
            username = get_username_or_id(user)
        except:
            username = str(user_id)
        
        await context.bot.send_message(
            ADMIN_ID,
            f"🔔 **НОВАЯ ЗАЯВКА #{request_id}**\n\n"
            f"👤 Клиент: {escape_markdown(username)} (ID: {user_id})\n"
            f"📋 Тип: {escape_markdown(op_type)}\n"
            f"💰 Сумма: {amount:,.0f} ₽\n"
            f"💸 К оплате: {client_total:,.0f} ₽\n\n"
            f"✅ /take {request_id} — взять в работу\n"
            f"📤 /send {request_id} <текст> — отправить реквизиты",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        # Очистка временных данных
        context.user_data['awaiting_amount'] = False
        context.user_data['temp_amount'] = None
        context.user_data['operation_type'] = None
        context.user_data['editing'] = False
    
    elif data.startswith("cancel_"):
        try:
            request_id = int(data.split("_")[1])
        except (IndexError, ValueError):
            await query.edit_message_text(
                "❌ Некорректный ID заявки.",
                reply_markup=get_operation_keyboard()
            )
            return
        
        request = db.get_request(request_id)
        if not request:
            await query.edit_message_text(
                "❌ Заявка не найдена.",
                reply_markup=get_operation_keyboard()
            )
            return
        
        # Проверяем, что это заявка текущего пользователя
        if request['user_id'] != user_id:
            await query.answer("Это не ваша заявка!", show_alert=True)
            return
        
        if request['status'] not in CANCELLABLE_STATUSES:
            await query.edit_message_text(
                "❌ Заявку нельзя отменить в текущем статусе.",
                reply_markup=get_main_keyboard()
            )
            return
        
        if db.cancel_request(request_id, "user"):
            await query.edit_message_text(
                f"✅ **ЗАЯВКА #{request_id} ОТМЕНЕНА**\n\n"
                f"Вы можете создать новую заявку.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=get_operation_keyboard()
            )
            
            # Уведомление админу
            try:
                user = await context.bot.get_chat(user_id)
                username = get_username_or_id(user)
            except:
                username = str(user_id)
            
            await context.bot.send_message(
                ADMIN_ID,
                f"🔔 Пользователь {escape_markdown(username)} (ID: {user_id}) отменил заявку #{request_id}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось отменить заявку.",
                reply_markup=get_main_keyboard()
            )
    
    elif data.startswith("rate_"):
        try:
            rating = int(data.split("_")[1])
            if rating < 1 or rating > 5:
                raise ValueError
        except (IndexError, ValueError):
            await query.answer("Некорректный рейтинг", show_alert=True)
            return
        
        request_id = context.user_data.get('rating_request_id')
        if request_id:
            db.add_feedback(user_id, request_id, rating, None)
            await query.edit_message_text(
                f"✅ Спасибо за оценку {rating}⭐!",
                reply_markup=get_main_keyboard()
            )
            context.user_data['rating_request_id'] = None
            context.user_data['awaiting_feedback'] = False

# ==================================================
# ================== КОМАНДЫ =======================
# ==================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Доступ к админ-панели"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "⛔ Только для администратора.", 
            reply_markup=get_main_keyboard()
        )
        return
    await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

async def take_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Взять заявку в работу"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /take <id>")
        return
    
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    
    if not db.take_request(request_id):
        await update.message.reply_text(
            f"❌ Не удалось взять заявку #{request_id}. "
            f"Возможно, она уже обрабатывается."
        )
        return
    
    await update.message.reply_text(
        f"✅ Заявка #{request_id} взята в работу\n\n"
        f"Отправьте реквизиты: /send {request_id} <текст>"
    )
    
    # Уведомление клиенту
    try:
        await context.bot.send_message(
            request['user_id'],
            f"✅ **ЗАЯВКА #{request_id} ПРИНЯТА В РАБОТУ!**\n\n"
            f"📋 Тип: {escape_markdown(request['operation_type'])}\n"
            f"💰 Сумма: {request['amount']:,.0f} ₽\n"
            f"💸 К оплате: {request['client_total']:,.0f} ₽\n\n"
            f"Статус: ⏳ оператор готовит реквизиты\n\n"
            f"Ожидайте, скоро они появятся в этом чате.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.error(f"Failed to notify user {request['user_id']}: {e}")

async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить реквизиты клиенту"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /send <id> <текст реквизитов>")
        return
    
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    requisites_text = " ".join(context.args[1:])
    request = db.get_request(request_id)
    
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    
    if not db.send_requisites(request_id, requisites_text):
        await update.message.reply_text(
            f"❌ Не удалось отправить реквизиты. Проверьте статус заявки."
        )
        return
    
    # Отправка клиенту
    warning = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ **НАПОМИНАНИЕ!**\n\n"
        "Вы подтвердили готовность оплатить счёт.\n\n"
        "Неоплата влечёт **БЛОКИРОВКУ аккаунта**.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    
    try:
        await context.bot.send_message(
            request['user_id'],
            f"✅ **ЗАЯВКА #{request_id} | РЕКВИЗИТЫ ПОЛУЧЕНЫ**\n\n"
            f"💸 СУММА К ОПЛАТЕ: {request['client_total']:,.0f} ₽\n\n"
            f"📋 **РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:**\n{escape_markdown(requisites_text)}\n\n"
            f"{warning}\n\n"
            f"📎 **ПОСЛЕ ОПЛАТЫ ПРИШЛИТЕ ЧЕК PDF.**",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_cancel_keyboard(request_id)
        )
        await update.message.reply_text(
            f"✅ Реквизиты отправлены клиенту по заявке #{request_id}"
        )
    except Exception as e:
        logging.error(f"Failed to send requisites to user {request['user_id']}: {e}")
        await update.message.reply_text(
            f"❌ Не удалось отправить сообщение клиенту. "
            f"Возможно, он заблокировал бота."
        )

async def confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтвердить выполнение заявки"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /confirm <id>")
        return
    
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    request = db.get_request(request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    
    if not db.complete_request(request_id):
        await update.message.reply_text(
            f"❌ Не удалось завершить заявку #{request_id}. Проверьте статус."
        )
        return
    
    await update.message.reply_text(f"✅ Заявка #{request_id} завершена")
    
    # Предлагаем пользователю оставить отзыв
    try:
        await context.bot.send_message(
            request['user_id'],
            f"✅ **ЗАЯВКА #{request_id} ЗАВЕРШЕНА!**\n\n"
            f"Сумма: {request['amount']:,.0f} ₽ → оплачено {request['client_total']:,.0f} ₽\n\n"
            f"⭐ **Оцените нашу работу:**\n\n"
            f"✏️ Напишите отзыв текстом\n"
            f"Или нажмите /skip чтобы пропустить",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_rating_keyboard()
        )
        
        # Сохраняем context для возможности оставить отзыв
        context.user_data['rating_request_id'] = request_id
        context.user_data['awaiting_feedback'] = True
    except Exception as e:
        logging.error(f"Failed to notify user {request['user_id']}: {e}")

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отклонить заявку"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /reject <id> [причина]")
        return
    
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "чек не соответствует требованиям"
    request = db.get_request(request_id)
    
    if not request:
        await update.message.reply_text(f"❌ Заявка #{request_id} не найдена")
        return
    
    if not db.cancel_request(request_id, "admin"):
        await update.message.reply_text(f"❌ Не удалось отклонить заявку #{request_id}")
        return
    
    await update.message.reply_text(f"✅ Заявка #{request_id} отклонена")
    
    try:
        await context.bot.send_message(
            request['user_id'],
            f"❌ **ЗАЯВКА #{request_id} ОТКЛОНЕНА.**\n\n"
            f"Причина: {escape_markdown(reason)}\n\n"
            f"Вы можете создать новую заявку: /start",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.error(f"Failed to notify user {request['user_id']}: {e}")

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Забанить пользователя"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /ban @username <причина>")
        return
    
    username = context.args[0].replace("@", "")
    reason = " ".join(context.args[1:])
    
    user_id = db.find_user_id_by_username(username)
    if not user_id:
        await update.message.reply_text(f"❌ Пользователь @{username} не найден")
        return
    
    if user_id == ADMIN_ID:
        await update.message.reply_text("❌ Нельзя забанить администратора!")
        return
    
    is_banned, _ = db.is_banned(user_id)
    if is_banned:
        await update.message.reply_text(f"❌ Пользователь @{username} уже забанен")
        return
    
    db.ban_user(user_id, reason)
    
    await update.message.reply_text(
        f"✅ Пользователь @{username} (ID: {user_id}) заблокирован\n"
        f"Причина: {reason}"
    )
    
    try:
        await context.bot.send_message(
            user_id,
            f"⛔ **ДОСТУП ЗАБЛОКИРОВАН**\n\n"
            f"Причина: {escape_markdown(reason)}\n\n"
            f"По вопросам разблокировки: @svenobmen",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.warning(f"Could not send ban notification to user {user_id}: {e}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Разбанить пользователя"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /unban @username")
        return
    
    username = context.args[0].replace("@", "")
    user_id = db.find_user_id_by_username(username)
    
    if not user_id:
        await update.message.reply_text(f"❌ Пользователь @{username} не найден")
        return
    
    is_banned, _ = db.is_banned(user_id)
    if not is_banned:
        await update.message.reply_text(f"❌ Пользователь @{username} не забанен")
        return
    
    db.unban_user(user_id)
    
    await update.message.reply_text(
        f"✅ Пользователь @{username} (ID: {user_id}) разблокирован"
    )
    
    try:
        await context.bot.send_message(
            user_id,
            f"✅ **ДОСТУП ВОССТАНОВЛЕН**\n\n"
            f"Вы можете снова пользоваться сервисом.\n\n"
            f"/start",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logging.warning(f"Could not send unban notification to user {user_id}: {e}")

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пропустить оставление отзыва"""
    context.user_data['awaiting_feedback'] = False
    context.user_data['rating_request_id'] = None
    await update.message.reply_text(
        "✅ Отзыв пропущен. Спасибо за обращение!",
        reply_markup=get_main_keyboard()
    )

async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактировать правила"""
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'rules'
    current_text = db.get_setting('rules')
    await update.message.reply_text(
        f"📝 Введите новый текст правил:\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{current_text}"
    )

async def edit_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактировать график"""
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'schedule'
    current_text = db.get_setting('schedule')
    await update.message.reply_text(
        f"📝 Введите новый текст графика работы:\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{current_text}"
    )

async def edit_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактировать ссылки"""
    if update.effective_user.id != ADMIN_ID:
        return
    context.user_data['editing'] = 'links'
    current_text = db.get_setting('links')
    await update.message.reply_text(
        f"📝 Введите новый текст полезных ссылок:\n\n"
        f"ТЕКУЩИЙ ТЕКСТ:\n{current_text}"
    )

async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранить отредактированные настройки"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    editing = context.user_data.get('editing')
    if editing:
        setting_names = {
            'rules': 'правила',
            'schedule': 'график',
            'links': 'ссылки'
        }
        
        db.update_setting(editing, update.message.text)
        setting_name = setting_names.get(editing, editing)
        await update.message.reply_text(
            f"✅ {setting_name} обновлены!",
            reply_markup=get_admin_keyboard()
        )
        context.user_data['editing'] = None

async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включить/выключить режим AFK"""
    global afk_mode
    
    if update.effective_user.id != ADMIN_ID:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Использование: /afk on  или  /afk off")
        return
    
    with afk_lock:
        if context.args[0].lower() == "on":
            afk_mode = True
            await update.message.reply_text("✅ Режим «Не работаю» ВКЛЮЧЁН")
        elif context.args[0].lower() == "off":
            afk_mode = False
            await update.message.reply_text("✅ Режим «Не работаю» ВЫКЛЮЧЁН")
        else:
            await update.message.reply_text("❌ Используйте on или off")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику пользователя"""
    user_id = update.effective_user.id
    
    # Админ может смотреть статистику других пользователей
    if user_id == ADMIN_ID and context.args:
        username = context.args[0].replace("@", "")
        user_data = db.find_user_by_username(username)
        if user_data:
            await show_profile(update, context, user_data['user_id'])
        else:
            await update.message.reply_text(f"❌ Клиент @{username} не найден")
        return
    
    await show_profile(update, context, user_id)

# ==================================================
# ==================== ЗАПУСК ======================
# ==================================================

def main():
    """Главная функция запуска бота"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Проверка токена
    if BOT_TOKEN == "ВАШ_ТОКЕН_ОТ_BOTFATHER":
        print("❌ ОШИБКА: Укажите токен бота в переменной BOT_TOKEN!")
        return
    
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
    
    # Callback обработчик
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Обработчики сообщений (важен порядок!)
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf_message))  # Только PDF
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_messages))  # Текст
    
    print("✅ БОТ ЗАПУЩЕН. SVEN OBMEN")
    print(f"✅ ADMIN_ID: {ADMIN_ID}")
    print(f"✅ Минимальная сумма: {MIN_AMOUNT:,} ₽")
    
    try:
        app.run_polling()
    except KeyboardInterrupt:
        print("\n⏹️ Бот остановлен")
    finally:
        db.close()
        print("✅ Соединение с БД закрыто")


if __name__ == "__main__":
    main()
