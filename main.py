import re
import logging
import sqlite3
import asyncio
import html
import hashlib
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
from telegram.error import Forbidden

# ================== НАСТРОЙКИ ====================
BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039
SUPPORT_CONTACT = "@tripo3"

SESSION_TIMEOUT_SECONDS = 300
MAX_ACTIVE_REQUESTS = 2
COOLDOWN_SECONDS = 300
CHAT_ANTISPAM_SECONDS = 180

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_REQUISITES_SENT = "requisites_sent"
STATUS_PAID = "paid"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED_BY_USER = "cancelled_by_user"
STATUS_CANCELLED_BY_ADMIN = "cancelled_by_admin"

STATUS_EMOJI = {
    STATUS_PENDING: "⏳", STATUS_PROCESSING: "🔄",
    STATUS_REQUISITES_SENT: "💳", STATUS_PAID: "📎",
    STATUS_COMPLETED: "✅", STATUS_CANCELLED_BY_USER: "❌",
    STATUS_CANCELLED_BY_ADMIN: "❌",
}

STATUS_TEXT = {
    STATUS_PENDING: "Ожидает обработки", STATUS_PROCESSING: "В работе",
    STATUS_REQUISITES_SENT: "Реквизиты отправлены", STATUS_PAID: "Чек на проверке",
    STATUS_COMPLETED: "Завершена", STATUS_CANCELLED_BY_USER: "Отменена",
    STATUS_CANCELLED_BY_ADMIN: "Отклонена",
}

OPERATION_OXAPAY_BTC = "Оплата счёта OxaPay (Биткоин)"
OPERATION_OXAPAY_USDT = "Оплата счёта OxaPay (Tether)"
OPERATION_BITPAPA_BTC = "Создание чека Bitpapa (Биткоин)"
OPERATION_BITPAPA_USDT = "Создание чека Bitpapa (Tether)"
OPERATION_CRYPTO_BTC = "Покупка Биткоина на кошелёк"
OPERATION_CRYPTO_USDT = "Покупка Tether на кошелёк"
OPERATION_SHOP_BTC = "Отправка Биткоина на кошелёк магазина"
OPERATION_SHOP_USDT = "Отправка Tether на кошелёк магазина"

ASKING_AMOUNT = 1
ASKING_LINK = 2
ASKING_FEEDBACK_COMMENT = 3
ASKING_COIN = 4
ASKING_ADMIN_MESSAGE = 5

MENU_BUTTONS = {
    "🔥 НОВЫЙ ЗАПРОС", "⭐ ОТЗЫВЫ", "📜 ПРАВИЛА", "👤 ПРОФИЛЬ",
    "📞 ПОДДЕРЖКА", "❓ КАК ОПЛАТИТЬ", "🎁 РЕФЕРАЛЫ", "🔗 ССЫЛКИ",
    "📝 НАПИСАТЬ ОПЕРАТОРУ",
    "📋 ЗАЯВКИ", "⚙️ НАСТРОЙКИ", "📊 СТАТИСТИКА",
    "🚫 ЗАБАНЕННЫЕ", "💱 КУРС", "◀️ ВЫЙТИ"
}

# ================== ДЕКОРАТОРЫ ====================
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper

def not_banned(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
        user_id = update.effective_user.id
        banned, reason = await db.is_banned(user_id)
        if banned:
            if update.message:
                await update.message.reply_text(
                    f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}",
                    reply_markup=get_main_keyboard()
                )
            return
        return await func(update, context)
    return wrapper

def idempotent_callback(timeout: int = 5):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            if not query:
                return await func(update, context)
            key = f"idempotent_{query.message.message_id}_{query.data}"
            now = datetime.now().timestamp()
            lock_data = context.user_data.get(key)
            if lock_data:
                locked_until = lock_data.get('locked_until', 0)
                if now < locked_until:
                    await query.answer("⏳ Запрос обрабатывается...", show_alert=True)
                    return
            context.user_data[key] = {'locked_until': now + timeout}
            try:
                return await func(update, context)
            except Exception as e:
                logging.error(f"Callback error: {e}", exc_info=True)
                raise
        return wrapper
    return decorator

# ================== КОМИССИИ ====================
def _get_btc_network_fee(amount_usd: float, btc_rate: float) -> float:
    if amount_usd <= 61: btc_fee = 0.00032
    elif amount_usd <= 183: btc_fee = 0.00063
    elif amount_usd <= 1218: btc_fee = 0.00079
    else: btc_fee = 0.0012
    return btc_fee * btc_rate

def _get_trc20_fee(amount_usd: float, usdt_rate: float) -> float:
    if amount_usd <= 100: return 5 * usdt_rate
    elif amount_usd <= 999: return 7 * usdt_rate
    else: return 11 * usdt_rate

def _get_ton_fee(amount_usd: float, usdt_rate: float) -> float:
    if amount_usd <= 100: return 2 * usdt_rate
    elif amount_usd <= 999: return 4 * usdt_rate
    elif amount_usd <= 4997: return 8 * usdt_rate
    else: return 15 * usdt_rate

def get_network_fee(deal_amount_rub: float, coin: str, operation_type: str,
                    usdt_rate: float, btc_rate: float) -> float:
    if "Bitpapa" in operation_type:
        return 0
    amount_usd = deal_amount_rub / usdt_rate
    if coin == "BTC":
        return _get_btc_network_fee(amount_usd, btc_rate)
    elif coin == "USDT":
        if "OxaPay" in operation_type:
            return _get_ton_fee(amount_usd, usdt_rate)
        else:
            return _get_trc20_fee(amount_usd, usdt_rate)
    return 0

def calculate_client_total(amount: float, discount_percent: float = 0.0,
                           use_free_deal: bool = False, coin: str = "USDT",
                           operation_type: str = "", usdt_rate: float = 92.5,
                           btc_rate: float = 5500000) -> Tuple[float, float, float]:
    rate = usdt_rate if coin == "USDT" else btc_rate
    network_fee = get_network_fee(amount, coin, operation_type, usdt_rate, btc_rate)
    
    if use_free_deal:
        service_commission = 0
        client_total = amount + network_fee
        crypto_amount = amount / rate
        return client_total, crypto_amount, service_commission
    
    base_commission_rate = 0.169
    actual_rate = max(0, base_commission_rate - (discount_percent / 100))
    service_commission = max(285, amount * actual_rate)
    client_total = amount + network_fee + service_commission
    crypto_amount = amount / rate
    return client_total, crypto_amount, service_commission

def calculate_operator_info(client_total: float, service_commission: float,
                             coin: str, operation_type: str,
                             usdt_rate: float, btc_rate: float) -> Tuple[float, float, float]:
    rate = usdt_rate if coin == "USDT" else btc_rate
    operator_crypto_amount = client_total / rate
    operator_profit_crypto = service_commission / rate
    network_fee_rub = get_network_fee(client_total - service_commission - get_network_fee(client_total - service_commission, coin, operation_type, usdt_rate, btc_rate), coin, operation_type, usdt_rate, btc_rate)
    network_fee_rub = get_network_fee(client_total - service_commission, coin, operation_type, usdt_rate, btc_rate) if abs(client_total - service_commission - get_network_fee(client_total - service_commission, coin, operation_type, usdt_rate, btc_rate)) < 1 else 0
    network_fee_rub = get_network_fee(client_total, coin, operation_type, usdt_rate, btc_rate)
    network_fee_crypto = network_fee_rub / rate
    return operator_crypto_amount, network_fee_crypto, operator_profit_crypto

# ================== БД ============================
class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.db_file = db_file
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            self._create_tables(conn)
            self._migrate_db(conn)
            self._init_settings(conn)

    def _create_tables(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY, username TEXT,
                total_deals INTEGER DEFAULT 0, total_volume REAL DEFAULT 0,
                avg_rating REAL DEFAULT 0, ratings_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0, ban_reason TEXT, banned_at TEXT,
                created_at TEXT, referral_code TEXT UNIQUE, referred_by INTEGER,
                referral_completed_count INTEGER DEFAULT 0, free_deals_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                operation_type TEXT, amount REAL, client_total REAL,
                crypto_amount REAL, operator_crypto_amount REAL,
                network_fee_crypto REAL, service_commission REAL,
                operator_profit_crypto REAL,
                status TEXT, requisites_text TEXT, invoice_link TEXT,
                created_at TEXT, taken_at TEXT, requisites_sent_at TEXT,
                paid_at TEXT, completed_at TEXT, cancelled_at TEXT,
                cancelled_by TEXT, pdf_file_id TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                request_id INTEGER, rating INTEGER, comment TEXT,
                created_at TEXT, is_displayed INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY, last_active TEXT
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER,
                referred_id INTEGER UNIQUE, created_at TEXT,
                first_completed_at TEXT, status TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS free_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                granted_at TEXT, used_at TEXT, source TEXT
            );
            CREATE TABLE IF NOT EXISTS payment_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER UNIQUE,
                reminders_sent INTEGER DEFAULT 0, last_reminder_at TEXT,
                next_reminder_at TEXT
            );
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER,
                old_status TEXT, new_status TEXT, changed_by TEXT,
                changed_at TEXT, comment TEXT
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, request_id INTEGER,
                message_text TEXT, direction TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_requests_user_id ON requests(user_id);
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_request_events_request_id ON request_events(request_id);
            CREATE INDEX IF NOT EXISTS idx_chat_messages_user_time ON chat_messages(user_id, created_at);
        """)
        conn.commit()

    def _migrate_db(self, conn):
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("PRAGMA table_info(requests)")
            existing_columns = {column[1] for column in cursor.fetchall()}
            new_cols = {
                'invoice_link': 'TEXT', 'crypto_amount': 'REAL',
                'operator_crypto_amount': 'REAL', 'network_fee_crypto': 'REAL',
                'service_commission': 'REAL', 'operator_profit_crypto': 'REAL'
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_columns:
                    try:
                        conn.execute(f"ALTER TABLE requests ADD COLUMN {col_name} {col_type}")
                        logging.info(f"✅ Добавлена колонка requests.{col_name}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" not in str(e).lower():
                            raise
            cursor = conn.execute("PRAGMA table_info(clients)")
            existing_client_columns = {column[1] for column in cursor.fetchall()}
            for col_name, col_type in [('referral_code', 'TEXT'), ('referred_by', 'INTEGER'),
                                         ('referral_completed_count', 'INTEGER DEFAULT 0'),
                                         ('free_deals_count', 'INTEGER DEFAULT 0')]:
                if col_name not in existing_client_columns:
                    try:
                        conn.execute(f"ALTER TABLE clients ADD COLUMN {col_name} {col_type}")
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" not in str(e).lower():
                            raise
            conn.commit()
            logging.info("✅ Миграция БД завершена")
        except Exception as e:
            conn.rollback()
            logging.error(f"❌ Ошибка миграции: {e}", exc_info=True)
            raise

    def _init_settings(self, conn):
        defaults = {
            'rules': '📜 ПРАВИЛА РАБОТЫ\n\n• Минимальная сумма: 1000 ₽\n• Работаем 24/7',
            'usdt_rate': '92.5',
            'btc_rate': '5500000',
            'links': '🔗 ПОЛЕЗНЫЕ ССЫЛКИ\n\n• Канал: https://t.me/svenobmen',
            'afk_mode': '0',
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

    async def get_rates(self) -> Tuple[float, float]:
        usdt = await self.get_setting('usdt_rate')
        btc = await self.get_setting('btc_rate')
        return float(usdt or 92.5), float(btc or 5500000)

    async def _log_event(self, request_id: int, old_status: str, new_status: str, changed_by: str, comment: str = ""):
        try:
            await self._run_execute(
                "INSERT INTO request_events (request_id, old_status, new_status, changed_by, changed_at, comment) VALUES (?, ?, ?, ?, ?, ?)",
                (request_id, old_status, new_status, changed_by, datetime.now().isoformat(), comment)
            )
        except Exception as e:
            logging.error(f"❌ Ошибка логирования: {e}", exc_info=True)

    async def _run_query(self, query: str, params: tuple = ()):
        async with self._lock:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                return cur.fetchall()

    async def _run_execute(self, query: str, params: tuple = ()):
        async with self._lock:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                return cur.rowcount

    async def _run_insert(self, query: str, params: tuple = ()):
        async with self._lock:
            with self._get_connection() as conn:
                cur = conn.cursor()
                cur.execute(query, params)
                conn.commit()
                return cur.lastrowid

    async def _run_execute_without_lock(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            conn.commit()
            return cur.rowcount

    async def _run_query_without_lock(self, query: str, params: tuple = ()):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, params)
            return cur.fetchall()

    async def update_last_activity(self, user_id: int):
        await self._run_execute("INSERT OR REPLACE INTO user_activity (user_id, last_active) VALUES (?, ?)", (user_id, datetime.now().isoformat()))

    async def get_last_activity(self, user_id: int) -> Optional[float]:
        rows = await self._run_query("SELECT last_active FROM user_activity WHERE user_id = ?", (user_id,))
        if rows and rows[0]['last_active']:
            return datetime.fromisoformat(rows[0]['last_active']).timestamp()
        return None

    async def add_client(self, user_id: int, username: Optional[str]):
        await self._run_execute("INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)", (user_id, username, datetime.now().isoformat()))
        await self._run_execute("UPDATE clients SET username=? WHERE user_id=?", (username, user_id))
        rows = await self._run_query("SELECT referral_code FROM clients WHERE user_id=?", (user_id,))
        if rows and not rows[0]['referral_code']:
            code = hashlib.md5(f"{user_id}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
            await self._run_execute("UPDATE clients SET referral_code=? WHERE user_id=?", (code, user_id))

    async def get_referral_code(self, user_id: int) -> Optional[str]:
        rows = await self._run_query("SELECT referral_code FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['referral_code'] if rows else None

    async def get_user_by_referral_code(self, code: str):
        rows = await self._run_query("SELECT user_id, username FROM clients WHERE referral_code=?", (code,))
        return rows[0] if rows else None

    async def add_referral(self, referrer_id: int, referred_id: int) -> bool:
        try:
            await self._run_insert("INSERT INTO referrals (referrer_id, referred_id, created_at, status) VALUES (?, ?, ?, ?)", (referrer_id, referred_id, datetime.now().isoformat(), "pending"))
            await self._run_execute("UPDATE clients SET referred_by=? WHERE user_id=?", (referrer_id, referred_id))
            return True
        except sqlite3.IntegrityError:
            return False

    async def complete_referral(self, referred_id: int) -> Optional[int]:
        async with self._lock:
            rows = await self._run_query_without_lock("SELECT id, referrer_id, status FROM referrals WHERE referred_id=?", (referred_id,))
            if not rows or rows[0]['status'] != 'pending':
                return None
            referrer_id = rows[0]['referrer_id']
            await self._run_execute_without_lock("UPDATE referrals SET status='completed', first_completed_at=? WHERE id=?", (datetime.now().isoformat(), rows[0]['id']))
            return referrer_id

    async def increment_referral_completed(self, referrer_id: int) -> int:
        await self._run_execute("UPDATE clients SET referral_completed_count = referral_completed_count + 1 WHERE user_id=?", (referrer_id,))
        rows = await self._run_query("SELECT referral_completed_count FROM clients WHERE user_id=?", (referrer_id,))
        return rows[0]['referral_completed_count'] if rows else 0

    async def add_free_deal(self, user_id: int, source: str = 'referral_3'):
        await self._run_insert("INSERT INTO free_deals (user_id, granted_at, source) VALUES (?, ?, ?)", (user_id, datetime.now().isoformat(), source))
        await self._run_execute("UPDATE clients SET free_deals_count = free_deals_count + 1 WHERE user_id=?", (user_id,))

    async def get_free_deals_count(self, user_id: int) -> int:
        rows = await self._run_query("SELECT free_deals_count FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['free_deals_count'] if rows else 0

    async def is_banned(self, user_id: int) -> Tuple[bool, Optional[str]]:
        rows = await self._run_query("SELECT is_banned, ban_reason FROM clients WHERE user_id = ?", (user_id,))
        if rows and rows[0]['is_banned'] == 1:
            return True, rows[0]['ban_reason']
        return False, None

    async def ban_user(self, user_id: int, reason: str):
        await self._run_execute("UPDATE clients SET is_banned=1, ban_reason=?, banned_at=? WHERE user_id=?", (reason, datetime.now().isoformat(), user_id))

    async def unban_user(self, user_id: int):
        await self._run_execute("UPDATE clients SET is_banned=0, ban_reason=NULL, banned_at=NULL WHERE user_id=?", (user_id,))

    async def get_banned_users(self):
        return await self._run_query("SELECT user_id, username, ban_reason, banned_at FROM clients WHERE is_banned=1")

    async def update_client_after_deal(self, user_id: int, amount: float):
        await self._run_execute("UPDATE clients SET total_deals=total_deals+1, total_volume=total_volume+? WHERE user_id=?", (amount, user_id))

    async def get_client_stats(self, user_id: int):
        rows = await self._run_query("SELECT total_deals, total_volume, avg_rating, ratings_count, free_deals_count, referral_completed_count, username FROM clients WHERE user_id=?", (user_id,))
        return rows[0] if rows else None

    async def get_all_clients(self):
        return await self._run_query("SELECT user_id, username, total_deals, total_volume FROM clients WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20")

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        rows = await self._run_query("SELECT user_id FROM clients WHERE username=?", (username,))
        return rows[0]['user_id'] if rows else None

    async def save_chat_message_atomic(self, user_id: int, request_id: int, message_text: str, direction: str) -> Tuple[bool, str]:
        async with self._lock:
            with self._get_connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("SELECT created_at FROM chat_messages WHERE user_id=? AND direction='to_admin' ORDER BY created_at DESC LIMIT 1", (user_id,))
                row = cur.fetchone()
                if row and row['created_at']:
                    last_time = datetime.fromisoformat(row['created_at']).timestamp()
                    elapsed = datetime.now().timestamp() - last_time
                    if elapsed < CHAT_ANTISPAM_SECONDS:
                        conn.rollback()
                        remaining = int(CHAT_ANTISPAM_SECONDS - elapsed)
                        return False, f"⏳ Подождите {remaining // 60} мин {remaining % 60} сек"
                conn.execute("INSERT INTO chat_messages (user_id, request_id, message_text, direction, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, request_id, message_text, direction, datetime.now().isoformat()))
                conn.commit()
                return True, ""

    async def create_request_atomic(self, user_id: int, operation_type: str, amount: float, 
                                     client_total: float, crypto_amount: float,
                                     operator_crypto_amount: float, network_fee_crypto: float,
                                     service_commission: float, operator_profit_crypto: float,
                                     use_free_deal: bool = False,
                                     invoice_link: str = None) -> Tuple[Optional[int], str]:
        async with self._lock:
            with self._get_connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute("SELECT COUNT(*) as cnt FROM requests WHERE user_id=? AND status IN (?,?,?,?)", 
                                       (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
                    if cur.fetchone()['cnt'] >= MAX_ACTIVE_REQUESTS:
                        conn.rollback()
                        return None, f"⚠️ Максимум {MAX_ACTIVE_REQUESTS} активных заявок."
                    cur = conn.execute("SELECT MAX(COALESCE(completed_at, cancelled_at)) as last_action FROM requests WHERE user_id=? AND (status = ? OR status IN (?, ?))", 
                                       (user_id, STATUS_COMPLETED, STATUS_CANCELLED_BY_USER, STATUS_CANCELLED_BY_ADMIN))
                    row = cur.fetchone()
                    if row and row['last_action']:
                        last_action_time = datetime.fromisoformat(row['last_action']).timestamp()
                        time_diff = datetime.now().timestamp() - last_action_time
                        if time_diff < COOLDOWN_SECONDS:
                            conn.rollback()
                            remaining = int(COOLDOWN_SECONDS - time_diff)
                            return None, f"⏳ Подождите {remaining // 60} мин {remaining % 60} сек"
                    if use_free_deal:
                        cur = conn.execute("SELECT id FROM free_deals WHERE user_id=? AND used_at IS NULL LIMIT 1", (user_id,))
                        free_deal_row = cur.fetchone()
                        if not free_deal_row:
                            conn.rollback()
                            return None, "❌ Бесплатная сделка недоступна."
                        cur = conn.execute("UPDATE free_deals SET used_at=? WHERE id=? AND used_at IS NULL", 
                                          (datetime.now().isoformat(), free_deal_row['id']))
                        if cur.rowcount != 1:
                            conn.rollback()
                            return None, "❌ Не удалось списать бесплатную сделку."
                        conn.execute("UPDATE clients SET free_deals_count = MAX(0, free_deals_count - 1) WHERE user_id=?", (user_id,))
                    cur = conn.execute("""
                        INSERT INTO requests (user_id, operation_type, amount, client_total, 
                            crypto_amount, operator_crypto_amount, network_fee_crypto,
                            service_commission, operator_profit_crypto,
                            status, created_at, invoice_link)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, operation_type, amount, client_total, crypto_amount,
                          operator_crypto_amount, network_fee_crypto, service_commission,
                          operator_profit_crypto,
                          STATUS_PENDING, datetime.now().isoformat(), invoice_link))
                    request_id = cur.lastrowid
                    conn.execute("INSERT INTO request_events (request_id, old_status, new_status, changed_by, changed_at, comment) VALUES (?, NULL, ?, 'system', ?, 'Заявка создана')", 
                                (request_id, STATUS_PENDING, datetime.now().isoformat()))
                    conn.commit()
                    logging.info(f"✅ Заявка #{request_id} создана пользователем {user_id}")
                    return request_id, None
                except Exception as e:
                    conn.rollback()
                    logging.error(f"❌ Ошибка создания заявки: {e}", exc_info=True)
                    return None, f"❌ Внутренняя ошибка. Попробуйте позже.\n{e}"

    async def get_request(self, request_id: int):
        rows = await self._run_query("SELECT * FROM requests WHERE id=?", (request_id,))
        return rows[0] if rows else None

    async def get_user_active_request(self, user_id: int):
        rows = await self._run_query("SELECT id, operation_type, amount, client_total, crypto_amount, status FROM requests WHERE user_id=? AND status IN (?,?,?,?) ORDER BY id DESC LIMIT 1", 
                                     (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0] if rows else None

    async def get_active_requests_count(self, user_id: int) -> int:
        rows = await self._run_query("SELECT COUNT(*) as cnt FROM requests WHERE user_id=? AND status IN (?, ?, ?, ?)", 
                                     (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0]['cnt'] if rows else 0

    async def get_all_pending_requests(self, limit: int = 20):
        return await self._run_query("SELECT * FROM requests WHERE status=? ORDER BY created_at DESC LIMIT ?", (STATUS_PENDING, limit))

    async def get_all_processing_requests(self, limit: int = 20):
        return await self._run_query("SELECT * FROM requests WHERE status IN (?,?) ORDER BY created_at DESC LIMIT ?", (STATUS_PROCESSING, STATUS_REQUISITES_SENT, limit))

    async def take_request(self, request_id: int):
        await self._run_execute("UPDATE requests SET status=?, taken_at=? WHERE id=? AND status=?", (STATUS_PROCESSING, datetime.now().isoformat(), request_id, STATUS_PENDING))
        await self._log_event(request_id, STATUS_PENDING, STATUS_PROCESSING, 'admin')

    async def send_requisites(self, request_id: int, requisites_text: str):
        await self._run_execute("UPDATE requests SET status=?, requisites_sent_at=?, requisites_text=? WHERE id=? AND status=?", (STATUS_REQUISITES_SENT, datetime.now().isoformat(), requisites_text, request_id, STATUS_PROCESSING))
        await self._log_event(request_id, STATUS_PROCESSING, STATUS_REQUISITES_SENT, 'admin')
        await self.create_reminder_record(request_id)

    async def mark_paid(self, request_id: int, pdf_file_id: str):
        await self._run_execute("UPDATE requests SET status=?, paid_at=?, pdf_file_id=? WHERE id=? AND status=?", (STATUS_PAID, datetime.now().isoformat(), pdf_file_id, request_id, STATUS_REQUISITES_SENT))
        await self._log_event(request_id, STATUS_REQUISITES_SENT, STATUS_PAID, 'user')
        await self.delete_reminder_record(request_id)

    async def complete_request(self, request_id: int, user_id: int, amount: float):
        now = datetime.now().isoformat()
        await self._run_execute("UPDATE requests SET status=?, completed_at=? WHERE id=? AND status=?", (STATUS_COMPLETED, now, request_id, STATUS_PAID))
        await self._log_event(request_id, STATUS_PAID, STATUS_COMPLETED, 'admin')
        await self.update_client_after_deal(user_id, amount)
        await self.delete_reminder_record(request_id)
        referrer_id = await self.complete_referral(user_id)
        if referrer_id:
            completed_count = await self.increment_referral_completed(referrer_id)
            if completed_count % 3 == 0:
                await self.add_free_deal(referrer_id, 'referral_3')

    async def cancel_request(self, request_id: int, cancelled_by: str, user_id: Optional[int] = None) -> bool:
        req = await self.get_request(request_id)
        if not req or req['status'] in (STATUS_COMPLETED, STATUS_CANCELLED_BY_USER, STATUS_CANCELLED_BY_ADMIN):
            return False
        if cancelled_by == "user":
            if user_id is None or req['user_id'] != user_id:
                return False
            if req['status'] in (STATUS_REQUISITES_SENT, STATUS_PAID):
                return False
        elif cancelled_by != "admin":
            return False
        status = STATUS_CANCELLED_BY_USER if cancelled_by == "user" else STATUS_CANCELLED_BY_ADMIN
        old_status = req['status']
        await self._run_execute("UPDATE requests SET status=?, cancelled_at=?, cancelled_by=? WHERE id=?", (status, datetime.now().isoformat(), cancelled_by, request_id))
        await self._log_event(request_id, old_status, status, cancelled_by)
        await self.delete_reminder_record(request_id)
        return True

    async def create_reminder_record(self, request_id: int):
        now = datetime.now()
        next_reminder = now.timestamp() + 900
        await self._run_execute("INSERT OR IGNORE INTO payment_reminders (request_id, reminders_sent, last_reminder_at, next_reminder_at) VALUES (?, 0, NULL, ?)", (request_id, datetime.fromtimestamp(next_reminder).isoformat()))

    async def get_due_reminders(self) -> list:
        now = datetime.now().isoformat()
        return await self._run_query("SELECT pr.request_id, pr.reminders_sent, r.user_id, r.amount, r.client_total FROM payment_reminders pr JOIN requests r ON pr.request_id = r.id WHERE r.status = ? AND pr.reminders_sent < 2 AND pr.next_reminder_at <= ?", (STATUS_REQUISITES_SENT, now))

    async def update_reminder_sent(self, request_id: int):
        now = datetime.now()
        next_reminder = now.timestamp() + 900
        await self._run_execute("UPDATE payment_reminders SET reminders_sent = reminders_sent + 1, last_reminder_at = ?, next_reminder_at = ? WHERE request_id = ?", (now.isoformat(), datetime.fromtimestamp(next_reminder).isoformat(), request_id))

    async def delete_reminder_record(self, request_id: int):
        await self._run_execute("DELETE FROM payment_reminders WHERE request_id = ?", (request_id,))

    async def add_feedback(self, user_id: int, request_id: int, rating: Optional[int] = None, comment: Optional[str] = None):
        await self._run_insert("INSERT INTO feedback (user_id, request_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, request_id, rating, comment, datetime.now().isoformat()))
        if rating is not None:
            rows = await self._run_query("SELECT AVG(rating) as avg_r, COUNT(*) as cnt FROM feedback WHERE user_id=? AND rating IS NOT NULL", (user_id,))
            if rows and rows[0]['avg_r'] is not None:
                await self._run_execute("UPDATE clients SET avg_rating=?, ratings_count=? WHERE user_id=?", (rows[0]['avg_r'], rows[0]['cnt'], user_id))

    async def get_feedback_for_display(self, limit: int = 5, offset: int = 0):
        return await self._run_query("SELECT f.id, f.user_id, c.username, f.rating, f.comment, f.created_at FROM feedback f JOIN clients c ON f.user_id = c.user_id WHERE f.is_displayed=1 AND (f.comment IS NOT NULL OR f.rating IS NOT NULL) ORDER BY f.created_at DESC LIMIT ? OFFSET ?", (limit, offset))

    async def get_feedback_count(self) -> int:
        rows = await self._run_query("SELECT COUNT(*) as cnt FROM feedback WHERE is_displayed=1")
        return rows[0]['cnt'] if rows else 0

    async def get_avg_rating(self) -> float:
        rows = await self._run_query("SELECT AVG(rating) as avg FROM feedback WHERE rating IS NOT NULL AND is_displayed=1")
        return rows[0]['avg'] if rows and rows[0]['avg'] is not None else 0.0

    async def get_setting(self, key: str) -> Optional[str]:
        rows = await self._run_query("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]['value'] if rows else None

    async def update_setting(self, key: str, value: str):
        await self._run_execute("UPDATE settings SET value=? WHERE key=?", (value, key))

db = Database()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_rank_and_discount(deals: int):
    if deals < 3:  return ("Новичок", "🟢", 0.0, 3 - deals)
    if deals < 7:  return ("Ходок",   "🔵", 0.5, 7 - deals)
    if deals < 10: return ("Опытный", "🟠", 1.0, 10 - deals)
    if deals < 15: return ("Мастер",  "🟣", 1.5, 15 - deals)
    return ("Легенда", "🔥", 2.0, 0)

def get_progress_bar(current: int, needed: int) -> str:
    if needed <= 0: return "▰" * 10
    total = current + needed
    filled = min(max(int(10 * current / total), 0), 10)
    return "▰" * filled + "▱" * (10 - filled)

def format_status(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '📋')} {STATUS_TEXT.get(status, status)}"

def get_status_progress_bar(status: str) -> tuple:
    progress_map = {
        STATUS_PENDING: (0, "⏳ Ожидает обработки"),
        STATUS_PROCESSING: (2, "🔄 В работе"),
        STATUS_REQUISITES_SENT: (4, "💳 Реквизиты получены"),
        STATUS_PAID: (8, "📎 Чек на проверке"),
        STATUS_COMPLETED: (10, "✅ Завершена"),
    }
    if status in progress_map:
        percent, text = progress_map[status]
        return f"[{'🟩' * percent}{'⬜' * (10 - percent)}] {text}", percent * 10
    return "[⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜] ❓ Статус неизвестен", 0

def format_user(username: Optional[str], user_id: int) -> str:
    return f"@{username}" if username else f"ID:{user_id}"

def extract_amount(text: str) -> Optional[float]:
    if not text: return None
    cleaned = text.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned: cleaned = cleaned.replace(",", "")
    elif "," in cleaned: cleaned = cleaned.replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if match:
        try: return float(match.group(1))
        except (ValueError, TypeError): pass
    return None

def format_requisites(requisites: str, total: float) -> str:
    requisites_lower = requisites.lower()
    if "сбп" in requisites_lower or "телефон" in requisites_lower:
        phone_match = re.search(r"(\+?7[\d\s]{10,15})", requisites)
        phone = phone_match.group(1) if phone_match else "указан выше"
        return (f"💳 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ</b>\n\n📱 <b>ОПЛАТА ПО СБП</b>\n\n📞 <b>НОМЕР ТЕЛЕФОНА:</b>\n<code>{html.escape(phone)}</code>\n\n━━━━━━━━━━━━━━━━━━━━━\n💡 <b>ИНСТРУКЦИЯ:</b>\n1️⃣ Откройте приложение банка с СБП\n2️⃣ Выберите \"Оплата по номеру телефона\"\n3️⃣ Введите номер телефона\n4️⃣ Укажите сумму: {total:.0f} ₽\n5️⃣ Подтвердите платеж\n\n✅ <b>После оплаты пришлите чек!</b>")
    if "карт" in requisites_lower:
        card_match = re.search(r"(\d[\d\s]{15,19})", requisites)
        card = card_match.group(1) if card_match else "указана выше"
        holder_match = re.search(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)", requisites)
        holder = holder_match.group(1) if holder_match else "не указан"
        return (f"💳 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ</b>\n\n💳 <b>ОПЛАТА ПО КАРТЕ</b>\n\n💳 <b>НОМЕР КАРТЫ:</b>\n<code>{html.escape(card)}</code>\n\n👤 <b>ПОЛУЧАТЕЛЬ:</b>\n<code>{html.escape(holder)}</code>\n\n━━━━━━━━━━━━━━━━━━━━━\n💡 <b>ИНСТРУКЦИЯ:</b>\n1️⃣ Откройте приложение банка\n2️⃣ Выберите \"Перевод по номеру карты\"\n3️⃣ Введите номер карты\n4️⃣ Укажите сумму: {total:.0f} ₽\n5️⃣ Назначьте платеж: «Оплата обмена USDT»\n6️⃣ Подтвердите платеж\n\n✅ <b>После оплаты пришлите чек!</b>")
    return (f"💳 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ</b>\n\n{html.escape(requisites)}\n\n✅ <b>После оплаты пришлите чек!</b>")

async def is_afk_mode() -> bool:
    val = await db.get_setting('afk_mode')
    return val == '1'

async def safe_send(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, **kwargs):
    try: return await context.bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Forbidden: logging.warning(f"User {user_id} blocked the bot.")
    except Exception as e: logging.error(f"Error sending message to {user_id}: {e}", exc_info=True)
    return None

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in ['step', 'temp_amount', 'operation_type', 'base_operation', 'selected_coin',
                'invoice_link', 'feedback_req_id', 'feedback_rating', 'pending_free_deal',
                'temp_client_total_with_commission', 'temp_client_total_without_commission',
                'temp_op_type', 'temp_coin_data', 'temp_invoice_link',
                'chat_request_id', 'admin_send_req_id', 'admin_msg_req_id']:
        context.user_data.pop(key, None)

async def get_context_keyboard(user_id: int):
    active = await db.get_user_active_request(user_id)
    if active and active['status'] in (STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID):
        return get_main_keyboard_with_chat()
    return get_main_keyboard()

# ================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 НОВЫЙ ЗАПРОС")],
        [KeyboardButton("⭐ ОТЗЫВЫ")],
        [KeyboardButton("📜 ПРАВИЛА"), KeyboardButton("👤 ПРОФИЛЬ")],
        [KeyboardButton("📞 ПОДДЕРЖКА"), KeyboardButton("❓ КАК ОПЛАТИТЬ")],
        [KeyboardButton("🎁 РЕФЕРАЛЫ"), KeyboardButton("🔗 ССЫЛКИ")],
    ], resize_keyboard=True)

def get_main_keyboard_with_chat():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 НОВЫЙ ЗАПРОС")],
        [KeyboardButton("⭐ ОТЗЫВЫ")],
        [KeyboardButton("📜 ПРАВИЛА"), KeyboardButton("👤 ПРОФИЛЬ")],
        [KeyboardButton("📞 ПОДДЕРЖКА"), KeyboardButton("❓ КАК ОПЛАТИТЬ")],
        [KeyboardButton("🎁 РЕФЕРАЛЫ"), KeyboardButton("🔗 ССЫЛКИ")],
        [KeyboardButton("📝 НАПИСАТЬ ОПЕРАТОРУ")],
    ], resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📋 ЗАЯВКИ")],
        [KeyboardButton("💱 КУРС"), KeyboardButton("⚙️ НАСТРОЙКИ")],
        [KeyboardButton("📊 СТАТИСТИКА"), KeyboardButton("🚫 ЗАБАНЕННЫЕ")],
        [KeyboardButton("◀️ ВЫЙТИ")]
    ], resize_keyboard=True)

def get_operation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 ОПЛАТИТЬ СЧЁТ OXAPAY", callback_data="type_oxapay")],
        [InlineKeyboardButton("🏷️ СОЗДАТЬ ЧЕК BITPAPA", callback_data="type_bitpapa")],
        [InlineKeyboardButton("💰 КУПИТЬ КРИПТУ", callback_data="type_crypto")],
        [InlineKeyboardButton("🏪 ОТПРАВИТЬ НА КОШЕЛЁК МАГАЗИНА", callback_data="type_shop")],
    ])

def get_coin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("₿ Биткоин (BTC)", callback_data="coin_btc")],
        [InlineKeyboardButton("🪙 Tether (USDT)", callback_data="coin_usdt")],
        [InlineKeyboardButton("◀️ НАЗАД", callback_data="back_to_type_select")],
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

def get_free_deal_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 ИСПОЛЬЗОВАТЬ БЕСПЛАТНУЮ СДЕЛКУ", callback_data="use_free_deal")],
        [InlineKeyboardButton("💰 ОБЫЧНАЯ ОПЛАТА", callback_data="skip_free_deal")],
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
        [InlineKeyboardButton("⏭️ ПРОПУСТИТЬ", callback_data=f"rate_{request_id}_skip")]
    ])

# ================== HANDLERS ====================
@not_banned
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return
    args = context.args
    ref_code = args[0] if args else None
    if ref_code:
        if ref_code.startswith("ref_"): ref_code = ref_code[4:]
        referrer = await db.get_user_by_referral_code(ref_code)
        if referrer and referrer['user_id'] != user.id:
            await db.add_client(user.id, user.username)
            if await db.add_referral(referrer['user_id'], user.id):
                await update.message.reply_text(f"🎉 Вас пригласил @{referrer['username']}!\n\n🎁 Скидка 3% на первый обмен!\n\n➡️ Нажмите «НОВЫЙ ЗАПРОС».", reply_markup=get_main_keyboard())
                return
    await db.add_client(user.id, user.username)
    await db.update_last_activity(user.id)
    reset_request_flow(context)
    usdt_rate, btc_rate = await db.get_rates()
    stats = await db.get_client_stats(user.id)
    deals = stats['total_deals'] if stats else 0
    rank_name, rank_emoji, discount, _ = get_rank_and_discount(deals)
    free_deals = stats['free_deals_count'] if stats else 0
    safe_name = html.escape(user.first_name)
    if user.id == ADMIN_ID:
        await update.message.reply_text(f"🔐 ДОБРО ПОЖАЛОВАТЬ, АДМИНИСТРАТОР!\n\n💱 Курсы:\n• USDT: {usdt_rate} ₽\n• BTC: {btc_rate} ₽\n\n/admin — панель управления.", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text(f"🔥 <b>ДОБРО ПОЖАЛОВАТЬ, {safe_name}!</b> 🔥\n\nSVEN OBMEN — быстрый обмен криптовалюты\n\n📊 Курс USDT: {usdt_rate} ₽\n🏆 Ранг: {rank_emoji} {rank_name}\n💰 Скидка: {discount}%\n🎁 Бесплатных сделок: {free_deals}\n\n▶️ Нажмите «🔥 НОВЫЙ ЗАПРОС»", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_last_activity(update.effective_user.id)
    links = await db.get_setting('links') or ""
    await update.message.reply_text(f"📖 <b>СПРАВОЧНИК</b>\n\n<b>Команды:</b>\n/start /help /stats /status /cancel /referral\n\n<b>Обмен:</b>\n1️⃣ «🔥 НОВЫЙ ЗАПРОС»\n2️⃣ Выберите тип\n3️⃣ Выберите крипту\n4️⃣ Введите сумму\n5️⃣ Подтвердите\n\n{links}\n\n📞 Поддержка: {SUPPORT_CONTACT}", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    stats = await db.get_client_stats(user_id)
    if not stats or stats['total_deals'] == 0:
        await update.message.reply_text("🔒 Реферальная программа доступна после первой сделки.", reply_markup=get_main_keyboard())
        return
    ref_code = await db.get_referral_code(user_id)
    if not ref_code:
        await update.message.reply_text("❌ Реферальный код не найден.", reply_markup=get_main_keyboard())
        return
    ref_link = f"https://t.me/{context.bot.username}?start=ref_{ref_code}"
    referrals_count = stats['referral_completed_count'] or 0
    free_deals = stats['free_deals_count'] or 0
    needed = (3 - (referrals_count % 3)) % 3
    await update.message.reply_text(f"🔗 <b>РЕФЕРАЛЬНАЯ ССЫЛКА</b>\n\n<code>{ref_link}</code>\n\n📊 Приглашено: {referrals_count}\n🎁 Бесплатных сделок: {free_deals}\n📌 До следующей: {needed} чел.", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    active = await db.get_user_active_request(user_id)
    if not active:
        await update.message.reply_text("❌ Нет активных заявок.", reply_markup=await get_context_keyboard(user_id))
        return
    if await db.cancel_request(active['id'], "user", user_id):
        await update.message.reply_text(f"✅ Заявка #{active['id']} отменена.", reply_markup=get_main_keyboard())
        await context.bot.send_message(ADMIN_ID, f"⚠️ {format_user(update.effective_user.username, user_id)} отменил заявку #{active['id']}")
    else:
        await update.message.reply_text("❌ Нельзя отменить (реквизиты уже отправлены).", reply_markup=await get_context_keyboard(user_id))

@not_banned
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    active = await db.get_user_active_request(user_id)
    if not active:
        await update.message.reply_text("📋 Нет активных заявок.", reply_markup=get_main_keyboard())
        return
    req_id = active['id']
    amount = active['amount']
    client_total = active['client_total']
    crypto_amount = active['crypto_amount'] if active['crypto_amount'] else 0
    status = active['status']
    operation_type = active['operation_type']
    progress_bar, _ = get_status_progress_bar(status)
    if "Биткоин" in operation_type or "BTC" in operation_type:
        coin_code = "BTC"
    else:
        coin_code = "USDT"
    text = (f"📋 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n"
            f"📊 {format_status(status)}\n\n"
            f"💰 <b>СУММА СДЕЛКИ:</b> {amount:.0f} ₽\n"
            f"🪙 <b>ВЫ ПОЛУЧИТЕ:</b> {crypto_amount:.6f} {coin_code}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💸 <b>К ОПЛАТЕ:</b> {client_total:.0f} ₽")
    if status == STATUS_REQUISITES_SENT:
        text += "\n\n📎 <b>Отправьте PDF-чек!</b>\n⚠️ Неоплата = БАН"
    elif status == STATUS_PAID:
        text += "\n\n🔍 Чек на проверке."
    elif status == STATUS_COMPLETED:
        text += "\n\n✅ Сделка завершена!"
    text += "\n\n🚫 /cancel — отменить"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=await get_context_keyboard(user_id))

@not_banned
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        req_id = context.user_data.pop('feedback_req_id', None)
        rating = context.user_data.pop('feedback_rating', None)
        context.user_data.pop('step', None)
        if req_id and rating is not None:
            await db.add_feedback(user_id, req_id, rating, None)
        await update.message.reply_text("✅ Отзыв сохранен.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Нет активного запроса отзыва.")

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    await db.update_last_activity(user_id)
    banned, reason = await db.is_banned(user_id)
    if banned:
        await update.message.reply_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\nПричина: {reason}", reply_markup=get_main_keyboard())
        return

    if text == "🔥 НОВЫЙ ЗАПРОС":
        if await is_afk_mode() and user_id != ADMIN_ID:
            await update.message.reply_text("😴 Бот не принимает заявки.")
            return
        if await db.get_active_requests_count(user_id) >= MAX_ACTIVE_REQUESTS:
            await update.message.reply_text(f"⚠️ Максимум {MAX_ACTIVE_REQUESTS} активных заявок.")
            return
        reset_request_flow(context)
        await update.message.reply_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
    elif text == "⭐ ОТЗЫВЫ":
        context.user_data['reviews_page'] = 0
        await show_reviews(update, context)
    elif text == "📜 ПРАВИЛА":
        rules = await db.get_setting('rules') or "Правила не заданы."
        await update.message.reply_text(rules, reply_markup=await get_context_keyboard(user_id))
    elif text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
    elif text == "📞 ПОДДЕРЖКА":
        await update.message.reply_text(f"📞 Поддержка: {SUPPORT_CONTACT}\n\n⚠️ Описывайте вопрос в ОДНОМ сообщении\n⚠️ Указывайте номер заявки\n⚠️ Неоплата = БАН", reply_markup=await get_context_keyboard(user_id))
    elif text == "❓ КАК ОПЛАТИТЬ":
        await update.message.reply_text("📎 <b>ИНСТРУКЦИЯ</b>\n\n<b>СБП:</b>\n1️⃣ Приложение банка\n2️⃣ «Оплата по номеру телефона»\n3️⃣ Номер из реквизитов\n4️⃣ Точная сумма\n5️⃣ Подтвердить\n\n<b>Карта:</b>\n1️⃣ Приложение банка\n2️⃣ «Перевод по номеру карты»\n3️⃣ Номер карты\n4️⃣ Сумма\n5️⃣ Платёж: «Оплата обмена»\n6️⃣ Подтвердить\n\n📎 Отправьте PDF-чек в чат!", parse_mode="HTML", reply_markup=await get_context_keyboard(user_id))
    elif text == "🎁 РЕФЕРАЛЫ":
        await referral_command(update, context)
    elif text == "🔗 ССЫЛКИ":
        links = await db.get_setting('links') or "Ссылки не заданы."
        await update.message.reply_text(links, reply_markup=await get_context_keyboard(user_id))
    elif text == "📝 НАПИСАТЬ ОПЕРАТОРУ":
        active = await db.get_user_active_request(user_id)
        if not active:
            await update.message.reply_text("❌ Нет активных заявок.", reply_markup=get_main_keyboard())
            return
        context.user_data['chat_request_id'] = active['id']
        context.user_data['step'] = ASKING_ADMIN_MESSAGE
        await update.message.reply_text(f"📝 <b>НАПИСАТЬ ОПЕРАТОРУ</b>\n\n📋 Заявка #{active['id']}\n\nВведите сообщение.\n⚠️ Не более 1 сообщения в 3 минуты\n\n❌ Кнопка меню — отмена.", parse_mode="HTML")
    elif user_id == ADMIN_ID:
        if text == "📋 ЗАЯВКИ":
            await show_requests_list(update, context)
        elif text == "💱 КУРС":
            usdt_rate, btc_rate = await db.get_rates()
            await update.message.reply_text(
                f"💱 <b>ТЕКУЩИЕ КУРСЫ</b>\n\n"
                f"• USDT: <b>{usdt_rate} ₽</b>\n"
                f"• BTC: <b>{btc_rate} ₽</b>\n\n"
                f"<b>Установить новые:</b>\n"
                f"/set_usdt [курс] — курс USDT\n"
                f"/set_btc [курс] — курс BTC",
                parse_mode="HTML",
                reply_markup=get_admin_keyboard()
            )
        elif text == "⚙️ НАСТРОЙКИ":
            await update.message.reply_text("⚙️ /edit_rules | /edit_links | /afk on|off", reply_markup=get_admin_keyboard())
        elif text == "📊 СТАТИСТИКА":
            await show_admin_stats(update, context)
        elif text == "🚫 ЗАБАНЕННЫЕ":
            await show_banned_users(update, context)
        elif text == "◀️ ВЫЙТИ":
            await update.message.reply_text("🔐 Выход из админ-панели.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Используйте кнопки меню.", reply_markup=get_admin_keyboard())
    else:
        await update.message.reply_text("Используйте кнопки меню.", reply_markup=await get_context_keyboard(user_id))

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    stats = await db.get_client_stats(user_id)
    if not stats or stats['total_deals'] == 0:
        await update.message.reply_text("👤 <b>ПРОФИЛЬ</b>\n\n📊 Нет завершённых сделок.\n🔥 Нажмите «НОВЫЙ ЗАПРОС»!", parse_mode="HTML", reply_markup=get_main_keyboard())
        return
    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    free_deals = stats['free_deals_count'] or 0
    referrals_count = stats['referral_completed_count'] or 0
    rank_name, rank_emoji, discount, next_rank_deals = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals, next_rank_deals)
    next_rank_name = ""
    if deals < 3: next_rank_name = "Ходок"
    elif deals < 7: next_rank_name = "Опытный"
    elif deals < 10: next_rank_name = "Мастер"
    elif deals < 15: next_rank_name = "Легенда"
    next_rank_text = f"📌 До ранга <b>{next_rank_name}</b>: {next_rank_deals} сделок" if next_rank_deals > 0 and next_rank_name else "🏆 Максимальный ранг!"
    await update.message.reply_text(f"👤 <b>ПРОФИЛЬ</b> | @{stats['username'] or user_id}\n\n🏆 <b>{rank_emoji} {rank_name}</b>\n📊 <code>{progress_bar}</code>\n{next_rank_text}\n\n🎁 Рефералы: {referrals_count}\n🎁 Бесплатных сделок: {free_deals}\n\n📈 Сделок: <b>{deals}</b>\n📈 Объём: <b>{volume:.0f} ₽</b>\n⭐ Рейтинг: <b>{rating:.1f}</b>", parse_mode="HTML", reply_markup=await get_context_keyboard(user_id))

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
            await update.message.reply_text("⭐ ПОКА НЕТ ОТЗЫВОВ.", reply_markup=get_main_keyboard())
        return
    text = f"⭐ ОТЗЫВЫ\n\nВсего: {total} | Ср.: {avg_rating:.1f} ⭐\n━━━━━━━━━━━━━\n\n"
    for r in reviews:
        stars = "⭐" * r['rating'] if r['rating'] is not None else "📝"
        username = r['username'] or "User"
        text += f"👤 @{username}\n📅 {(r['created_at'] or '')[:10]}\n"
        if r['comment']: text += f'💬 "{r["comment"]}"\n'
        text += f"Оценка: {stars}\n━━━━━━━━━━━━━\n"
    kb_row = []
    if total > (page + 1) * limit:
        kb_row.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb_row.append(InlineKeyboardButton("◀️ В МЕНЮ", callback_data="back_to_menu_ui"))
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([kb_row]))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([kb_row]))

@admin_only
async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    banned = await db.get_banned_users()
    if not banned:
        await update.message.reply_text("🚫 НЕТ ЗАБАНЕННЫХ.", reply_markup=get_admin_keyboard())
        return
    text = "🚫 ЗАБАНЕННЫЕ\n\n"
    for u in banned:
        text += f"👤 {format_user(u['username'], u['user_id'])}\n📅 {(u['banned_at'] or '')[:10]}\n📝 {u['ban_reason']}\n━━━━━━━━━━━━━\n"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await db.get_all_pending_requests(limit=10)
    processing = await db.get_all_processing_requests(limit=10)
    if not pending and not processing:
        await update.message.reply_text("📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return
    text = "📋 АКТИВНЫЕ ЗАЯВКИ\n\n"
    keyboard_rows = []
    
    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            coin = "BTC" if "BTC" in req['operation_type'] else "USDT"
            coin_symbol = "₿" if coin == "BTC" else "🪙"
            crypto_amount = req['crypto_amount'] if req['crypto_amount'] else 0
            operator_crypto = req['operator_crypto_amount'] if req['operator_crypto_amount'] else 0
            service_comm = req['service_commission'] if req['service_commission'] else 0
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | {coin_symbol} {crypto_amount:.6f} {coin} | ID:{req['user_id']}\n"
            text += f"  💸 К оплате: {req['client_total']:.0f} ₽\n"
            text += f"  👨‍💼 Купить: {operator_crypto:.6f} {coin} | Комиссия: {service_comm:.0f} ₽\n"
            keyboard_rows.append([
                InlineKeyboardButton(f"▶️ Взять #{req['id']}", callback_data=f"admin_take_{req['id']}"),
                InlineKeyboardButton(f"❌ #{req['id']}", callback_data=f"admin_reject_{req['id']}")
            ])
            text += "\n"
    
    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            ico = "⏳" if req['status'] == STATUS_PROCESSING else "💳"
            coin = "BTC" if "BTC" in req['operation_type'] else "USDT"
            coin_symbol = "₿" if coin == "BTC" else "🪙"
            crypto_amount = req['crypto_amount'] if req['crypto_amount'] else 0
            operator_crypto = req['operator_crypto_amount'] if req['operator_crypto_amount'] else 0
            service_comm = req['service_commission'] if req['service_commission'] else 0
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | {coin_symbol} {crypto_amount:.6f} {coin} | {ico}\n"
            text += f"  💸 К оплате: {req['client_total']:.0f} ₽\n"
            text += f"  👨‍💼 Купить: {operator_crypto:.6f} {coin} | Комиссия: {service_comm:.0f} ₽\n"
            
            if req['status'] == STATUS_PROCESSING:
                keyboard_rows.append([
                    InlineKeyboardButton(f"💳 #{req['id']}", callback_data=f"admin_send_{req['id']}"),
                    InlineKeyboardButton(f"💬 #{req['id']}", callback_data=f"admin_msg_{req['id']}"),
                    InlineKeyboardButton(f"❌ #{req['id']}", callback_data=f"admin_reject_{req['id']}")
                ])
            elif req['status'] == STATUS_REQUISITES_SENT:
                keyboard_rows.append([
                    InlineKeyboardButton(f"📄 Чек #{req['id']}", callback_data=f"admin_getpdf_{req['id']}"),
                    InlineKeyboardButton(f"💬 #{req['id']}", callback_data=f"admin_msg_{req['id']}"),
                    InlineKeyboardButton(f"❌ #{req['id']}", callback_data=f"admin_reject_{req['id']}")
                ])
            text += "\n"
    
    if keyboard_rows:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows))
    else:
        await update.message.reply_text(text)

@admin_only
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clients = await db.get_all_clients()
    avg_rating = await db.get_avg_rating()
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)
    text = f"📊 СТАТИСТИКА\n\n• Клиентов: {len(clients)}\n• Сделок: {total_deals}\n• Объём: {total_volume:.0f} ₽\n• Рейтинг: ⭐ {avg_rating:.1f}\n"
    if clients:
        text += "\n🏆 ТОП:\n"
        for i, c in enumerate(clients[:10], 1):
            text += f"{i}. {format_user(c['username'], c['user_id'])} — {c['total_deals']} сделок\n"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

# ================== CALLBACK =====================
@idempotent_callback(timeout=5)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    banned, reason = await db.is_banned(user_id)
    if banned:
        await query.edit_message_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}")
        return
    
    await db.update_last_activity(user_id)

    if data == "back_to_type_select":
        reset_request_flow(context)
        await query.edit_message_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
    elif data == "cancel_to_main":
        reset_request_flow(context)
        try: await query.message.delete()
        except Exception: pass
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
    elif data == "back_to_menu_ui":
        reset_request_flow(context)
        try: await query.message.delete()
        except Exception: pass
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
    elif data == "reviews_next":
        context.user_data['reviews_page'] = context.user_data.get('reviews_page', 0) + 1
        await show_reviews(update, context)
    elif data == "edit_amount":
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text("💰 ВВЕДИТЕ НОВУЮ СУММУ:", reply_markup=get_back_inline())
    elif data.startswith("type_"):
        op_key = data[5:]
        mapping = {"oxapay": "OXAPAY", "bitpapa": "BITPAPA", "crypto": "CRYPTO", "shop": "SHOP"}
        context.user_data['base_operation'] = mapping.get(op_key)
        context.user_data['step'] = ASKING_COIN
        await query.edit_message_text("💎 ВЫБЕРИТЕ КРИПТОВАЛЮТУ:", reply_markup=get_coin_keyboard())
    elif data.startswith("coin_"):
        coin = data[5:]
        base_op = context.user_data.get('base_operation')
        if coin == "btc":
            coin_data = {'name': "Биткоин (BTC)", 'symbol': "₿", 'code': "BTC"}
        else:
            coin_data = {'name': "Tether (USDT)", 'symbol': "🪙", 'code': "USDT"}
        context.user_data['selected_coin'] = coin_data
        op_map = {
            ("OXAPAY", "btc"): OPERATION_OXAPAY_BTC, ("OXAPAY", "usdt"): OPERATION_OXAPAY_USDT,
            ("BITPAPA", "btc"): OPERATION_BITPAPA_BTC, ("BITPAPA", "usdt"): OPERATION_BITPAPA_USDT,
            ("CRYPTO", "btc"): OPERATION_CRYPTO_BTC, ("CRYPTO", "usdt"): OPERATION_CRYPTO_USDT,
            ("SHOP", "btc"): OPERATION_SHOP_BTC, ("SHOP", "usdt"): OPERATION_SHOP_USDT,
        }
        context.user_data['operation_type'] = op_map.get((base_op, coin), OPERATION_CRYPTO_USDT)
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text(f"💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\nВы выбрали: {coin_data['name']}\nМинимум: 1000 ₽.", reply_markup=get_back_inline())
    elif data == "confirm_request":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        coin_data = context.user_data.get('selected_coin')
        invoice_link = context.user_data.get('invoice_link')
        if not amount or not op_type or not coin_data:
            await query.edit_message_text("❌ Ошибка данных.", reply_markup=get_operation_keyboard())
            return
        if await is_afk_mode() and user_id != ADMIN_ID:
            await query.edit_message_text("😴 Бот не принимает заявки.", reply_markup=get_operation_keyboard())
            return
        
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        free_deals_count = stats['free_deals_count'] if stats else 0
        
        usdt_rate, btc_rate = await db.get_rates()
        coin = coin_data['code']
        rate = usdt_rate if coin == "USDT" else btc_rate
        
        if free_deals_count > 0:
            client_total_free, crypto_free, _ = calculate_client_total(amount, discount, True, coin, op_type, usdt_rate, btc_rate)
            client_total_paid, crypto_paid, _ = calculate_client_total(amount, discount, False, coin, op_type, usdt_rate, btc_rate)
            context.user_data.update({
                'temp_amount': amount, 'temp_op_type': op_type, 'temp_coin_data': coin_data,
                'temp_invoice_link': invoice_link,
                'temp_client_total_with_commission': client_total_paid,
                'temp_client_total_without_commission': client_total_free,
                'pending_free_deal': True
            })
            await query.edit_message_text(f"🎁 <b>БЕСПЛАТНАЯ СДЕЛКА!</b>\n\n💰 Обычная: {client_total_paid:.0f} ₽\n🎁 Бесплатная: {client_total_free:.0f} ₽", parse_mode="HTML", reply_markup=get_free_deal_keyboard())
            return
        
        client_total, crypto_amount, service_commission = calculate_client_total(amount, discount, False, coin, op_type, usdt_rate, btc_rate)
        operator_crypto, network_fee_crypto, operator_profit = calculate_operator_info(client_total, service_commission, coin, op_type, usdt_rate, btc_rate)
        
        req_id, error = await db.create_request_atomic(user_id, op_type, amount, client_total, crypto_amount, operator_crypto, network_fee_crypto, service_commission, operator_profit, False, invoice_link)
        if req_id is None:
            await query.edit_message_text(error, reply_markup=get_operation_keyboard())
            return
        
        progress_bar, _ = get_status_progress_bar(STATUS_PENDING)
        msg = f"✅ <b>ЗАЯВКА #{req_id} СОЗДАНА!</b>\n\n📋 {op_type}\n💰 Сумма сделки: {amount:.0f} ₽\n{coin_data['symbol']} Вы получите: {crypto_amount:.6f} {coin}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n📌 Курс: ≈{rate:.2f} ₽\n\n{progress_bar}\n\n⚠️ После получения реквизитов — ОБЯЗАТЕЛЬНАЯ оплата.\n⛔ Неоплата = БАН.\n\n📞 {SUPPORT_CONTACT}"
        if invoice_link: msg += f"\n🔗 {html.escape(invoice_link)}"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=get_cancel_keyboard(req_id))
        
        admin_msg = (
            f"🔔 <b>НОВАЯ ЗАЯВКА #{req_id}</b>\n\n"
            f"👤 Клиент: {format_user(query.from_user.username, user_id)}\n"
            f"📋 Тип: {op_type}\n\n"
            f"════════════════════════════\n"
            f"👤 <b>ДЛЯ КЛИЕНТА:</b>\n"
            f"════════════════════════════\n"
            f"💰 Сумма сделки: {amount:,.0f} ₽\n"
            f"{coin_data['symbol']} Получит: {crypto_amount:.6f} {coin}\n"
            f"💸 К оплате: {client_total:,.0f} ₽ ({operator_crypto:.6f} {coin})\n"
            f"📌 Курс: {rate:.2f} ₽\n\n"
            f"════════════════════════════\n"
            f"👨‍💼 <b>ДЛЯ ОПЕРАТОРА:</b>\n"
            f"════════════════════════════\n"
            f"💳 Купить на бирже: {client_total:,.0f} ₽ → {operator_crypto:.6f} {coin}\n\n"
            f"📊 <b>Распределение:</b>\n"
            f"   • Клиенту: {crypto_amount:.6f} {coin}\n"
        )
        if "Bitpapa" not in op_type:
            admin_msg += f"   • Комиссия сети: {network_fee_crypto:.6f} {coin}\n"
        admin_msg += (
            f"   • Комиссия сервиса: {service_commission:,.0f} ₽ ({operator_profit:.6f} {coin})\n\n"
            f"💵 <b>ВЫРУЧКА: {operator_profit:.6f} {coin} ({service_commission:,.0f} ₽)</b>\n"
            f"════════════════════════════"
        )
        if invoice_link: admin_msg += f"\n🔗 {invoice_link}"
        
        await context.bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ ВЗЯТЬ", callback_data=f"admin_take_{req_id}")], [InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"admin_reject_{req_id}")]]))
        reset_request_flow(context)
    elif data in ("use_free_deal", "skip_free_deal"):
        if not context.user_data.get('pending_free_deal'):
            await query.edit_message_text("❌ Ошибка.", reply_markup=get_operation_keyboard())
            return
        use_free = (data == "use_free_deal")
        amount = context.user_data['temp_amount']
        op_type = context.user_data['temp_op_type']
        coin_data = context.user_data['temp_coin_data']
        invoice_link = context.user_data['temp_invoice_link']
        client_total = context.user_data['temp_client_total_without_commission'] if use_free else context.user_data['temp_client_total_with_commission']
        
        usdt_rate, btc_rate = await db.get_rates()
        coin = coin_data['code']
        rate = usdt_rate if coin == "USDT" else btc_rate
        
        _, crypto_amount, service_commission = calculate_client_total(amount, 0, use_free, coin, op_type, usdt_rate, btc_rate)
        operator_crypto, network_fee_crypto, operator_profit = calculate_operator_info(client_total, service_commission, coin, op_type, usdt_rate, btc_rate)
        
        req_id, error = await db.create_request_atomic(user_id, op_type, amount, client_total, crypto_amount, operator_crypto, network_fee_crypto, service_commission, operator_profit, use_free, invoice_link)
        if req_id is None:
            await query.edit_message_text(error, reply_markup=get_operation_keyboard())
            return
        
        progress_bar, _ = get_status_progress_bar(STATUS_PENDING)
        free_text = " (БЕСПЛАТНАЯ)" if use_free else ""
        msg = f"✅ <b>ЗАЯВКА #{req_id}{free_text}</b>\n\n📋 {op_type}\n💰 Сумма сделки: {amount:.0f} ₽\n{coin_data['symbol']} Вы получите: {crypto_amount:.6f} {coin}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n📌 Курс: ≈{rate:.2f} ₽\n\n{progress_bar}\n\n⚠️ Неоплата = БАН.\n📞 {SUPPORT_CONTACT}"
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=get_cancel_keyboard(req_id))
        
        admin_msg = f"🔔 <b>ЗАЯВКА #{req_id}{free_text}</b>\n\n👤 {format_user(query.from_user.username, user_id)}\n📋 {op_type}\n\n👤 Клиент: {amount:.0f} ₽ → {crypto_amount:.6f} {coin}\n💸 К оплате: {client_total:.0f} ₽\n\n👨‍💼 Купить: {operator_crypto:.6f} {coin}\n"
        if use_free:
            admin_msg += "🎁 <b>БЕСПЛАТНАЯ СДЕЛКА</b> (комиссия сервиса: 0₽)"
        else:
            admin_msg += f"💰 Комиссия сервиса: {service_commission:.0f} ₽"
        await context.bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ ВЗЯТЬ", callback_data=f"admin_take_{req_id}")], [InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"admin_reject_{req_id}")]]))
        reset_request_flow(context)
    elif data.startswith("admin_take_"):
        if user_id != ADMIN_ID: return
        req_id = int(data.split("_")[2])
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PENDING:
            await query.edit_message_text("❌ Заявка не найдена.")
            return
        await db.take_request(req_id)
        await query.edit_message_text(f"✅ #{req_id} взята!")
        progress_bar, _ = get_status_progress_bar(STATUS_PROCESSING)
        await safe_send(context, req['user_id'], f"🔄 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n👨‍💼 Оператор обрабатывает заявку.\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")
    elif data.startswith("admin_reject_"):
        if user_id != ADMIN_ID: return
        req_id = int(data.split("_")[2])
        req = await db.get_request(req_id)
        if not req:
            await query.edit_message_text("❌ Не найдена.")
            return
        await db.cancel_request(req_id, "admin")
        await query.edit_message_text(f"❌ #{req_id} отклонена!")
        await safe_send(context, req['user_id'], f"❌ <b>ЗАЯВКА #{req_id} ОТКЛОНЕНА</b>\n\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")
    elif data.startswith("admin_send_"):
        if user_id != ADMIN_ID: return
        req_id = int(data.split("_")[2])
        context.user_data['admin_send_req_id'] = req_id
        await query.edit_message_text(f"📝 Введите реквизиты для заявки #{req_id}:\n\n/send {req_id} [реквизиты]")
    elif data.startswith("admin_msg_"):
        if user_id != ADMIN_ID: return
        req_id = int(data.split("_")[2])
        context.user_data['admin_msg_req_id'] = req_id
        await query.edit_message_text(f"💬 Введите сообщение для заявки #{req_id}:\n\n/msg {req_id} [текст]")
    elif data.startswith("admin_getpdf_"):
        if user_id != ADMIN_ID: return
        req_id = int(data.split("_")[2])
        req = await db.get_request(req_id)
        if req and req['pdf_file_id']:
            await context.bot.send_document(ADMIN_ID, req['pdf_file_id'], caption=f"Чек #{req_id}")
            await query.answer("Чек отправлен.", show_alert=True)
        else:
            await query.answer("Чек отсутствует.", show_alert=True)
    elif data.startswith("cancel_"):
        try:
            req_id = int(data.split("_")[1])
            if await db.cancel_request(req_id, "user", user_id):
                await query.edit_message_text(f"✅ #{req_id} отменена.", reply_markup=get_operation_keyboard())
                await context.bot.send_message(ADMIN_ID, f"⚠️ {format_user(query.from_user.username, user_id)} отменил #{req_id}")
            else:
                await query.answer("Нельзя отменить.", show_alert=True)
        except Exception as e:
            logging.error(f"cancel error: {e}", exc_info=True)
    elif data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                req_id = int(parts[1])
                rating_str = parts[2]
                if rating_str == "skip":
                    await db.add_feedback(user_id, req_id, None, None)
                    await query.edit_message_text("✅ Пропущено.", reply_markup=get_main_keyboard())
                else:
                    rating = int(rating_str)
                    context.user_data.update({'feedback_req_id': req_id, 'feedback_rating': rating, 'step': ASKING_FEEDBACK_COMMENT})
                    await query.edit_message_text(f"{rating}⭐\n\n✏️ Комментарий или /skip:")
            except Exception as e:
                logging.error(f"rate error: {e}", exc_info=True)

# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message: return
    user_id = update.effective_user.id
    
    banned, reason = await db.is_banned(user_id)
    if banned:
        reset_request_flow(context)
        await update.message.reply_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}", reply_markup=get_main_keyboard())
        return
    
    last_active = await db.get_last_activity(user_id)
    if last_active and (datetime.now().timestamp() - last_active) > SESSION_TIMEOUT_SECONDS and context.user_data.get('step'):
        reset_request_flow(context)
        await update.message.reply_text("⏳ Сессия истекла.", parse_mode="HTML", reply_markup=get_main_keyboard())
        return
    
    await db.update_last_activity(user_id)
    
    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active or active['status'] != STATUS_REQUISITES_SENT:
                await update.message.reply_text(f"❌ Нет заявок в статусе ожидания оплаты.\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")
                return
            await db.mark_paid(active['id'], update.message.document.file_id)
            await update.message.reply_text("✅ ЧЕК ПРИНЯТ!\n🔍 Проверка оператором (5–15 минут).")
            await context.bot.send_message(ADMIN_ID, f"💳 ЧЕК\n👤 {format_user(update.effective_user.username, user_id)}\n📋 #{active['id']}\n/getpdf {active['id']}\n/confirm {active['id']}")
        else:
            await update.message.reply_text("❌ Отправьте чек в формате PDF.")
        return
    
    msg_text = update.message.text or ""

    if user_id == ADMIN_ID and context.user_data.get('editing_setting'):
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, msg_text)
        await update.message.reply_text(f"✅ Настройка '{key}' обновлена!", reply_markup=get_admin_keyboard())
        return

    if user_id == ADMIN_ID and context.user_data.get('admin_msg_req_id'):
        req_id = context.user_data.pop('admin_msg_req_id')
        req = await db.get_request(req_id)
        if req:
            await db.save_chat_message_atomic(req['user_id'], req_id, msg_text, 'to_client')
            await safe_send(context, req['user_id'], f"📨 <b>ОПЕРАТОР</b>\n\n📋 Заявка #{req_id}\n\n{html.escape(msg_text)}\n\n✏️ «📝 НАПИСАТЬ ОПЕРАТОРУ» — ответить", parse_mode="HTML", reply_markup=await get_context_keyboard(req['user_id']))
            await update.message.reply_text(f"✅ Отправлено клиенту #{req_id}", reply_markup=get_admin_keyboard())
        else:
            await update.message.reply_text("❌ Заявка не найдена.", reply_markup=get_admin_keyboard())
        return

    if context.user_data.get('step') == ASKING_FEEDBACK_COMMENT:
        req_id = context.user_data.pop('feedback_req_id', None)
        rating = context.user_data.pop('feedback_rating', None)
        context.user_data.pop('step', None)
        comment = msg_text if msg_text != '/skip' else None
        if req_id and rating:
            await db.add_feedback(user_id, req_id, rating, comment)
        await update.message.reply_text("✅ Спасибо за отзыв!", reply_markup=get_main_keyboard())
        return

    if context.user_data.get('step') == ASKING_ADMIN_MESSAGE:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        req_id = context.user_data.get('chat_request_id')
        if not req_id:
            reset_request_flow(context)
            await update.message.reply_text("❌ Ошибка.", reply_markup=get_main_keyboard())
            return
        
        success, error_msg = await db.save_chat_message_atomic(user_id, req_id, msg_text, 'to_admin')
        if not success:
            await update.message.reply_text(error_msg, reply_markup=await get_context_keyboard(user_id))
            context.user_data.pop('step', None)
            return
        
        username = format_user(update.effective_user.username, user_id)
        await context.bot.send_message(ADMIN_ID, f"📨 <b>ОТ КЛИЕНТА</b>\n\n👤 {username}\n📋 #{req_id}\n💬 {html.escape(msg_text)}\n\n✏️ /msg {req_id} [текст]", parse_mode="HTML")
        await update.message.reply_text("✅ <b>ОТПРАВЛЕНО!</b>\n\nОператор свяжется с вами.\n⏳ Следующее через 3 минуты.", parse_mode="HTML", reply_markup=await get_context_keyboard(user_id))
        context.user_data.pop('step', None)
        return

    if context.user_data.get('step') == ASKING_AMOUNT:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        amount = extract_amount(msg_text)
        if not amount or amount < 1000:
            await update.message.reply_text("❌ Минимум 1000 ₽.", reply_markup=get_back_inline())
            return
        context.user_data['temp_amount'] = amount
        op_type = context.user_data.get('operation_type')
        if "OxaPay" in str(op_type):
            context.user_data['step'] = ASKING_LINK
            await update.message.reply_text("🔗 ВВЕДИТЕ ССЫЛКУ OXAPAY\n\nhttps://pay.oxapay.com/invoice/...", reply_markup=get_back_inline())
        else:
            stats = await db.get_client_stats(user_id)
            deals = stats['total_deals'] if stats else 0
            _, _, discount, _ = get_rank_and_discount(deals)
            coin_data = context.user_data.get('selected_coin')
            coin = coin_data['code'] if coin_data else "USDT"
            coin_symbol = coin_data['symbol'] if coin_data else "🪙"
            usdt_rate, btc_rate = await db.get_rates()
            rate = usdt_rate if coin == "USDT" else btc_rate
            client_total, crypto_amount, _ = calculate_client_total(amount, discount, False, coin, op_type, usdt_rate, btc_rate)
            await update.message.reply_text(f"📝 <b>ПРОВЕРЬТЕ ДАННЫЕ</b>\n\n📋 {op_type}\n💰 Сумма сделки: {amount:.0f} ₽\n{coin_symbol} Вы получите: {crypto_amount:.6f} {coin}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n📌 Курс: ≈{rate:.2f} ₽\n\n⚠️ После реквизитов — ОБЯЗАТЕЛЬНАЯ оплата.\n⛔ Неоплата = БАН.", parse_mode="HTML", reply_markup=get_confirm_keyboard())
            context.user_data.pop('step', None)
        return

    if context.user_data.get('step') == ASKING_LINK:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        link = msg_text.strip()
        if not (link.startswith("https://pay.oxapay.com/invoice/") or link.startswith("https://oxapay.com/invoice/")):
            await update.message.reply_text("❌ Неверный формат ссылки.", reply_markup=get_back_inline())
            return
        context.user_data['invoice_link'] = link
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        coin_data = context.user_data.get('selected_coin')
        coin = coin_data['code'] if coin_data else "USDT"
        coin_symbol = coin_data['symbol'] if coin_data else "🪙"
        usdt_rate, btc_rate = await db.get_rates()
        rate = usdt_rate if coin == "USDT" else btc_rate
        client_total, crypto_amount, _ = calculate_client_total(amount, discount, False, coin, op_type, usdt_rate, btc_rate)
        await update.message.reply_text(f"📝 <b>ПРОВЕРЬТЕ ДАННЫЕ</b>\n\n📋 {op_type}\n💰 Сумма сделки: {amount:.0f} ₽\n{coin_symbol} Вы получите: {crypto_amount:.6f} {coin}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n🔗 {html.escape(link)}\n📌 Курс: ≈{rate:.2f} ₽\n\n⚠️ После реквизитов — ОБЯЗАТЕЛЬНАЯ оплата.\n⛔ Неоплата = БАН.", parse_mode="HTML", reply_markup=get_confirm_keyboard())
        context.user_data.pop('step', None)
        return

    await handle_menu(update, context)

# ================== КОМАНДЫ =======================
@not_banned
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_last_activity(update.effective_user.id)
    await show_profile(update, context, update.effective_user.id)

@not_banned
async def free_deal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = await db.get_client_stats(user_id)
    free_deals = stats['free_deals_count'] if stats else 0
    referrals_count = stats['referral_completed_count'] if stats else 0
    needed = (3 - (referrals_count % 3)) % 3
    await update.message.reply_text(f"🎁 Бесплатных сделок: {free_deals}\n👥 Рефералов: {referrals_count}\n📌 До следующей: {needed}", reply_markup=get_main_keyboard())

@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔐 АДМИН-ПАНЕЛЬ", reply_markup=get_admin_keyboard())

@admin_only
async def set_usdt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /set_usdt [курс]\nПример: /set_usdt 92.5")
        return
    try:
        rate = float(context.args[0].replace(",", "."))
        await db.update_setting('usdt_rate', str(rate))
        await update.message.reply_text(f"✅ Курс USDT обновлен: {rate} ₽")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат числа.")

@admin_only
async def set_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /set_btc [курс]\nПример: /set_btc 5500000")
        return
    try:
        rate = float(context.args[0].replace(",", "."))
        await db.update_setting('btc_rate', str(rate))
        await update.message.reply_text(f"✅ Курс BTC обновлен: {rate} ₽")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат числа.")

@admin_only
async def take_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /take [id]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PENDING:
        await update.message.reply_text("❌ Не найдена или не в ожидании.")
        return
    await db.take_request(req_id)
    await update.message.reply_text(f"✅ #{req_id} взята!")
    progress_bar, _ = get_status_progress_bar(STATUS_PROCESSING)
    await safe_send(context, req['user_id'], f"🔄 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n👨‍💼 Оператор обрабатывает.", parse_mode="HTML")

@admin_only
async def send_requisites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ /send [id] [реквизиты]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    requisites_text = " ".join(context.args[1:])
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PROCESSING:
        await update.message.reply_text("❌ Не найдена или не в работе.")
        return
    await db.send_requisites(req_id, requisites_text)
    coin = "BTC" if "BTC" in req['operation_type'] else "USDT"
    coin_symbol = "₿" if coin == "BTC" else "🪙"
    usdt_rate, btc_rate = await db.get_rates()
    rate = btc_rate if coin == "BTC" else usdt_rate
    formatted_reqs = format_requisites(requisites_text, req['client_total'])
    progress_bar, _ = get_status_progress_bar(STATUS_REQUISITES_SENT)
    await safe_send(context, req['user_id'], f"💳 <b>РЕКВИЗИТЫ #{req_id}</b>\n\n{progress_bar}\n\n{formatted_reqs}\n\n📊 Сумма к оплате: {req['client_total']:.0f} ₽\n{coin_symbol} Вы получите: {(req['crypto_amount'] if req['crypto_amount'] else 0):.6f} {coin}\n📌 Курс: ≈{rate:.2f} ₽\n\n⚠️ Оплатите ТОЧНО указанную сумму.\n📎 Отправьте PDF-чек.\n⛔ Неоплата = БАН.\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")
    await update.message.reply_text(f"✅ Реквизиты отправлены по #{req_id}")

@admin_only
async def msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ /msg [id] [текст]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    message_text = " ".join(context.args[1:])
    req = await db.get_request(req_id)
    if not req:
        await update.message.reply_text("❌ Заявка не найдена.")
        return
    await db.save_chat_message_atomic(req['user_id'], req_id, message_text, 'to_client')
    await safe_send(context, req['user_id'], f"📨 <b>СООБЩЕНИЕ ОТ ОПЕРАТОРА</b>\n\n📋 Заявка #{req_id}\n\n{html.escape(message_text)}\n\n✏️ «📝 НАПИСАТЬ ОПЕРАТОРУ» — ответить", parse_mode="HTML", reply_markup=await get_context_keyboard(req['user_id']))
    await update.message.reply_text(f"✅ Отправлено (заявка #{req_id})")

@admin_only
async def confirm_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /confirm [id]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PAID:
        await update.message.reply_text("❌ Не найдена или не оплачена.")
        return
    await db.complete_request(req_id, req['user_id'], req['amount'])
    coin = "BTC" if "BTC" in req['operation_type'] else "USDT"
    coin_symbol = "₿" if coin == "BTC" else "🪙"
    usdt_rate, btc_rate = await db.get_rates()
    rate = btc_rate if coin == "BTC" else usdt_rate
    progress_bar, _ = get_status_progress_bar(STATUS_COMPLETED)
    await safe_send(context, req['user_id'], f"✅ <b>ЗАЯВКА #{req_id} ЗАВЕРШЕНА!</b>\n\n{progress_bar}\n\n🎉 Сделка завершена!\n\n📊 Сумма: {req['amount']:.0f} ₽\n💸 Оплачено: {req['client_total']:.0f} ₽\n{coin_symbol} Получено: {(req['crypto_amount'] if req['crypto_amount'] else 0):.6f} {coin}\n\n⭐ Оцените сервис!", parse_mode="HTML", reply_markup=get_rating_keyboard(req_id))
    await update.message.reply_text(f"✅ #{req_id} завершена!")

@admin_only
async def reject_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /reject [id]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    if await db.cancel_request(req_id, "admin"):
        req = await db.get_request(req_id)
        await update.message.reply_text(f"❌ #{req_id} отклонена!")
        if req:
            await safe_send(context, req['user_id'], f"❌ <b>ЗАЯВКА #{req_id} ОТКЛОНЕНА</b>\n\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Не удалось отклонить.")

@admin_only
async def get_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /getpdf [id]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    req = await db.get_request(req_id)
    if not req or not req['pdf_file_id']:
        await update.message.reply_text("❌ Чек отсутствует.")
        return
    await update.message.reply_document(document=req['pdf_file_id'], caption=f"Чек #{req_id}")

@admin_only
async def get_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /getlink [id]")
        return
    try: req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    req = await db.get_request(req_id)
    if not req or not req['invoice_link']:
        await update.message.reply_text("❌ Ссылка отсутствует.")
        return
    await update.message.reply_text(f"🔗 #{req_id}:\n{req['invoice_link']}")

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ /ban [id или @username] [причина]")
        return
    identifier = context.args[0]
    reason = " ".join(context.args[1:])
    user_id = None
    if identifier.startswith("@"):
        user_id = await db.find_user_id_by_username(identifier[1:])
    else:
        try: user_id = int(identifier)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат.")
            return
    if not user_id:
        await update.message.reply_text(f"❌ {identifier} не найден.")
        return
    await db.ban_user(user_id, reason)
    await update.message.reply_text(f"🚫 {format_user(None, user_id)} заблокирован.")
    await safe_send(context, user_id, f"⛔ <b>ДОСТУП ЗАБЛОКИРОВАН</b>\n\nПричина: {reason}\n📞 {SUPPORT_CONTACT}", parse_mode="HTML")

@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ /unban [id]")
        return
    try: user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return
    await db.unban_user(user_id)
    await update.message.reply_text(f"✅ {format_user(None, user_id)} разблокирован.")
    await safe_send(context, user_id, "✅ <b>ДОСТУП ВОССТАНОВЛЕН</b>\n\n/start", parse_mode="HTML")

@admin_only
async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📝 Отправьте новый текст правил:")
        context.user_data['editing_setting'] = 'rules'
        return
    await db.update_setting('rules', " ".join(context.args))
    await update.message.reply_text("✅ Правила обновлены!")

@admin_only
async def edit_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📝 Отправьте новые ссылки:")
        context.user_data['editing_setting'] = 'links'
        return
    await db.update_setting('links', " ".join(context.args))
    await update.message.reply_text("✅ Ссылки обновлены!")

@admin_only
async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        status = await db.get_setting('afk_mode')
        current = "включен" if status == '1' else "выключен"
        await update.message.reply_text(f"AFK: {current}\n/afk on|off")
        return
    mode = context.args[0].lower()
    if mode == 'on':
        await db.update_setting('afk_mode', '1')
        await update.message.reply_text("😴 AFK включен.")
    elif mode == 'off':
        await db.update_setting('afk_mode', '0')
        await update.message.reply_text("✅ AFK выключен.")
    else:
        await update.message.reply_text("❌ /afk on или /afk off")

# ================== НАПОМИНАНИЯ ====================
async def reminder_loop(application: Application):
    await asyncio.sleep(10)
    while True:
        try:
            due_reminders = await db.get_due_reminders()
            for reminder in due_reminders:
                request_id = reminder['request_id']
                reminders_sent = reminder['reminders_sent']
                user_id = reminder['user_id']
                client_total = reminder['client_total']
                if reminders_sent == 0:
                    message = f"⏰ <b>НАПОМИНАНИЕ</b>\n\n📋 #{request_id}\n💸 К оплате: {client_total:.0f} ₽\n\n⚠️ Отправьте PDF-чек.\n⏳ 15 минут до предупреждения.\n📞 {SUPPORT_CONTACT}"
                else:
                    message = f"🚨 <b>ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ!</b>\n\n📋 #{request_id}\n💸 К оплате: {client_total:.0f} ₽\n\n⛔ Неоплата = БАН!\n📞 {SUPPORT_CONTACT}"
                await safe_send(application, user_id, message, parse_mode="HTML")
                await db.update_reminder_sent(request_id)
                if reminders_sent == 1:
                    await application.bot.send_message(ADMIN_ID, f"⚠️ Второе предупреждение по #{request_id}")
        except Exception as e:
            logging.error(f"Reminder error: {e}", exc_info=True)
        await asyncio.sleep(60)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Update error:", exc_info=context.error)
    try:
        if update and isinstance(update, Update) and update.effective_user:
            await safe_send(context, update.effective_user.id, "❌ Произошла внутренняя ошибка. Попробуйте позже.")
    except Exception:
        pass

# ================== MAIN ==========================
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("free_deal", free_deal_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("set_usdt", set_usdt_command))
    application.add_handler(CommandHandler("set_btc", set_btc_command))
    application.add_handler(CommandHandler("take", take_request_command))
    application.add_handler(CommandHandler("send", send_requisites_command))
    application.add_handler(CommandHandler("msg", msg_command))
    application.add_handler(CommandHandler("confirm", confirm_payment_command))
    application.add_handler(CommandHandler("reject", reject_request_command))
    application.add_handler(CommandHandler("getpdf", get_pdf_command))
    application.add_handler(CommandHandler("getlink", get_link_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("edit_rules", edit_rules_command))
    application.add_handler(CommandHandler("edit_links", edit_links_command))
    application.add_handler(CommandHandler("afk", afk_command))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_message))

    loop = asyncio.get_event_loop()
    loop.create_task(reminder_loop(application))

    application.add_error_handler(error_handler)

    logging.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    main()