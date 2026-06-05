import re
import logging
import sqlite3
import asyncio
import aiohttp
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
    CallbackQueryHandler, ContextTypes, JobQueue
)
from telegram.constants import ParseMode
from telegram.error import Forbidden

# ================== НАСТРОЙКИ ====================
BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039
SUPPORT_CONTACT = "@tripo3"

CACHE_TIME_SECONDS = 3600
SESSION_TIMEOUT_SECONDS = 300
MAX_ACTIVE_REQUESTS = 2
COOLDOWN_SECONDS = 300

_cached_usdt_rate: Optional[float] = None
_cached_btc_rate: Optional[float] = None
_cached_usdt_rate_time: float = 0
_cached_btc_rate_time: float = 0

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

MENU_BUTTONS = {
    "🔥 НОВЫЙ ЗАПРОС", "⭐ ОТЗЫВЫ", "📜 ПРАВИЛА", "👤 ПРОФИЛЬ",
    "📞 ПОДДЕРЖКА", "❓ КАК ОПЛАТИТЬ", "🎁 РЕФЕРАЛЫ",
    "📋 ЗАЯВКИ", "⚙️ НАСТРОЙКИ", "📊 СТАТИСТИКА",
    "🚫 ЗАБАНЕННЫЕ", "◀️ ВЫЙТИ"
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
            last_time = context.user_data.get(key, 0)
            if now - last_time < timeout:
                await query.answer("⏳ Запрос обрабатывается...")
                return
            context.user_data[key] = now
            try:
                return await func(update, context)
            finally:
                context.user_data.pop(key, None)
        return wrapper
    return decorator

# ================== BYBIT P2P API =================
async def get_weighted_average_p2p_rate(token: str = "USDT") -> Optional[float]:
    url = "https://api2.bybit.com/fiat/otc/item/online"
    payload = {
        "userId": "", "tokenId": token, "currencyId": "RUB",
        "payment": [], "side": "1", "size": "20", "page": "1",
        "amount": "", "authMaker": False, "canTrade": False
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('retCode') == 0 and data.get('result', {}).get('items'):
                        total_price = 0.0
                        total_quantity = 0.0
                        for item in data['result']['items']:
                            price = float(item['price'])
                            quantity = float(item['quantity'])
                            total_price += price * quantity
                            total_quantity += quantity
                        if total_quantity > 0:
                            return total_price / total_quantity
                logging.warning(f"API Bybit не вернул ордера для {token}")
                return None
    except Exception as e:
        logging.error(f"Ошибка получения курса {token}: {e}")
        return None

async def get_usdt_rate() -> float:
    global _cached_usdt_rate, _cached_usdt_rate_time
    now = datetime.now().timestamp()
    if _cached_usdt_rate is not None and (now - _cached_usdt_rate_time) < CACHE_TIME_SECONDS:
        return _cached_usdt_rate
    rate = await get_weighted_average_p2p_rate("USDT")
    if rate is not None:
        _cached_usdt_rate = rate
        _cached_usdt_rate_time = now
        return rate
    return _cached_usdt_rate if _cached_usdt_rate is not None else 92.5

async def get_btc_rate() -> float:
    global _cached_btc_rate, _cached_btc_rate_time
    now = datetime.now().timestamp()
    if _cached_btc_rate is not None and (now - _cached_btc_rate_time) < CACHE_TIME_SECONDS:
        return _cached_btc_rate
    rate = await get_weighted_average_p2p_rate("BTC")
    if rate is not None:
        _cached_btc_rate = rate
        _cached_btc_rate_time = now
        return rate
    return _cached_btc_rate if _cached_btc_rate is not None else 5500000

# ================== БД ============================
class Database:
    def __init__(self, db_file="sven_bot.db"):
        self.db_file = db_file
        self._lock = asyncio.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
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
                status TEXT, requisites_text TEXT, invoice_link TEXT,
                created_at TEXT, taken_at TEXT, requisites_sent_at TEXT,
                paid_at TEXT, completed_at TEXT, cancelled_at TEXT,
                cancelled_by TEXT, pdf_file_id TEXT,
                FOREIGN KEY (user_id) REFERENCES clients(user_id)
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                request_id INTEGER, rating INTEGER, comment TEXT,
                created_at TEXT, is_displayed INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES clients(user_id),
                FOREIGN KEY (request_id) REFERENCES requests(id)
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
                first_completed_at TEXT, status TEXT DEFAULT 'pending',
                FOREIGN KEY (referrer_id) REFERENCES clients(user_id),
                FOREIGN KEY (referred_id) REFERENCES clients(user_id)
            );
            CREATE TABLE IF NOT EXISTS free_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                granted_at TEXT, used_at TEXT, source TEXT,
                FOREIGN KEY (user_id) REFERENCES clients(user_id)
            );
            CREATE TABLE IF NOT EXISTS payment_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER UNIQUE,
                reminders_sent INTEGER DEFAULT 0, last_reminder_at TEXT,
                next_reminder_at TEXT,
                FOREIGN KEY (request_id) REFERENCES requests(id)
            );
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER,
                old_status TEXT, new_status TEXT, changed_by TEXT,
                changed_at TEXT, comment TEXT,
                FOREIGN KEY (request_id) REFERENCES requests(id)
            );
            CREATE INDEX IF NOT EXISTS idx_requests_user_id ON requests(user_id);
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback(user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_request_events_request_id ON request_events(request_id);
        """)
        conn.commit()

    def _migrate_db(self, conn):
        cursor = conn.execute("PRAGMA table_info(requests)")
        existing_columns = {column[1] for column in cursor.fetchall()}
        if 'invoice_link' not in existing_columns:
            conn.execute("ALTER TABLE requests ADD COLUMN invoice_link TEXT")
        
        cursor = conn.execute("PRAGMA table_info(clients)")
        existing_client_columns = {column[1] for column in cursor.fetchall()}
        for col_name, col_type in [('referral_code', 'TEXT'), ('referred_by', 'INTEGER'),
                                     ('referral_completed_count', 'INTEGER DEFAULT 0'),
                                     ('free_deals_count', 'INTEGER DEFAULT 0')]:
            if col_name not in existing_client_columns:
                try:
                    conn.execute(f"ALTER TABLE clients ADD COLUMN {col_name} {col_type}")
                except sqlite3.OperationalError as e:
                    logging.warning(f"Migration warning for {col_name}: {e}")
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_referral_code ON clients(referral_code)")
        except sqlite3.OperationalError:
            pass
        
        cursor = conn.execute("PRAGMA table_info(referrals)")
        existing_referral_columns = {column[1] for column in cursor.fetchall()}
        if 'first_completed_at' not in existing_referral_columns or 'status' not in existing_referral_columns:
            try:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='referrals_old'")
                if not cursor.fetchone():
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS referrals_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER,
                            referred_id INTEGER UNIQUE, created_at TEXT,
                            first_completed_at TEXT, status TEXT DEFAULT 'pending',
                            FOREIGN KEY (referrer_id) REFERENCES clients(user_id),
                            FOREIGN KEY (referred_id) REFERENCES clients(user_id)
                        )
                    """)
                    conn.execute("INSERT OR IGNORE INTO referrals_new (id, referrer_id, referred_id, created_at) SELECT id, referrer_id, referred_id, created_at FROM referrals")
                    conn.execute("DROP TABLE referrals")
                    conn.execute("ALTER TABLE referrals_new RENAME TO referrals")
                    logging.info("Referrals table migrated successfully")
            except Exception as e:
                logging.error(f"Referrals migration error: {e}")
        conn.commit()

    def _init_settings(self, conn):
        defaults = {
            'rules': '📜 ПРАВИЛА РАБОТЫ\n\n• Минимальная сумма: 1000 ₽\n• Работаем 24/7',
            'schedule': '⏰ ГРАФИК РАБОТЫ\n\n• Пн–Вс: 24/7',
            'links': '🔗 ПОЛЕЗНЫЕ ССЫЛКИ\n\n• Канал: https://t.me/svenobmen',
            'afk_mode': '0',
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

    async def _log_event(self, request_id: int, old_status: str, new_status: str, changed_by: str, comment: str = ""):
        await self._run_execute(
            "INSERT INTO request_events (request_id, old_status, new_status, changed_by, changed_at, comment) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, old_status, new_status, changed_by, datetime.now().isoformat(), comment)
        )

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

    # --- Активность ---
    async def update_last_activity(self, user_id: int):
        await self._run_execute("INSERT OR REPLACE INTO user_activity (user_id, last_active) VALUES (?, ?)", (user_id, datetime.now().isoformat()))

    async def get_last_activity(self, user_id: int) -> Optional[float]:
        rows = await self._run_query("SELECT last_active FROM user_activity WHERE user_id = ?", (user_id,))
        if rows and rows[0]['last_active']:
            return datetime.fromisoformat(rows[0]['last_active']).timestamp()
        return None

    # --- Клиенты ---
    async def add_client(self, user_id: int, username: Optional[str]):
        await self._run_execute("INSERT OR IGNORE INTO clients (user_id, username, created_at) VALUES (?, ?, ?)", (user_id, username, datetime.now().isoformat()))
        await self._run_execute("UPDATE clients SET username=? WHERE user_id=?", (username, user_id))
        rows = await self._run_query("SELECT referral_code FROM clients WHERE user_id=?", (user_id,))
        if rows and not rows[0]['referral_code']:
            code = hashlib.md5(f"{user_id}{datetime.now().isoformat()}".encode()).hexdigest()[:8]
            await self._run_execute("UPDATE clients SET referral_code=? WHERE user_id=?", (code, user_id))

    async def get_client_by_id(self, user_id: int):
        rows = await self._run_query("SELECT * FROM clients WHERE user_id=?", (user_id,))
        return rows[0] if rows else None

    async def get_user_by_referral_code(self, code: str):
        rows = await self._run_query("SELECT user_id, username FROM clients WHERE referral_code=?", (code,))
        return rows[0] if rows else None

    async def get_referral_code(self, user_id: int) -> Optional[str]:
        rows = await self._run_query("SELECT referral_code FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['referral_code'] if rows else None

    async def add_referral(self, referrer_id: int, referred_id: int) -> bool:
        try:
            await self._run_insert("INSERT INTO referrals (referrer_id, referred_id, created_at, status) VALUES (?, ?, ?, ?)", (referrer_id, referred_id, datetime.now().isoformat(), "pending"))
            await self._run_execute("UPDATE clients SET referred_by=? WHERE user_id=?", (referrer_id, referred_id))
            return True
        except sqlite3.IntegrityError:
            logging.warning(f"Referral already exists: {referred_id}")
            return False

    async def get_referred_by(self, user_id: int) -> Optional[int]:
        rows = await self._run_query("SELECT referred_by FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['referred_by'] if rows else None

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

    async def get_referrals_completed_count(self, user_id: int) -> int:
        rows = await self._run_query("SELECT referral_completed_count FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['referral_completed_count'] if rows else 0

    async def add_free_deal(self, user_id: int, source: str = 'referral_3'):
        await self._run_insert("INSERT INTO free_deals (user_id, granted_at, source) VALUES (?, ?, ?)", (user_id, datetime.now().isoformat(), source))
        await self._run_execute("UPDATE clients SET free_deals_count = free_deals_count + 1 WHERE user_id=?", (user_id,))

    async def get_free_deals_count(self, user_id: int) -> int:
        rows = await self._run_query("SELECT free_deals_count FROM clients WHERE user_id=?", (user_id,))
        return rows[0]['free_deals_count'] if rows else 0

    async def use_free_deal(self, user_id: int) -> bool:
        rows = await self._run_query("SELECT id FROM free_deals WHERE user_id=? AND used_at IS NULL LIMIT 1", (user_id,))
        if rows:
            rowcount = await self._run_execute("UPDATE free_deals SET used_at=? WHERE id=? AND used_at IS NULL", (datetime.now().isoformat(), rows[0]['id']))
            if rowcount == 1:
                await self._run_execute("UPDATE clients SET free_deals_count = MAX(0, free_deals_count - 1) WHERE user_id=?", (user_id,))
                return True
        return False

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

    async def create_request_atomic(self, user_id: int, operation_type: str, amount: float, client_total: float, use_free_deal: bool = False, invoice_link: str = None) -> Tuple[Optional[int], str]:
        async with self._lock:
            with self._get_connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute("SELECT COUNT(*) as cnt FROM requests WHERE user_id=? AND status IN (?,?,?,?)", (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
                    active_count = cur.fetchone()['cnt']
                    if active_count >= MAX_ACTIVE_REQUESTS:
                        conn.rollback()
                        return None, f"⚠️ У вас уже {active_count} активных заявок.\nМаксимум: {MAX_ACTIVE_REQUESTS}"
                    
                    cur = conn.execute("SELECT completed_at FROM requests WHERE user_id=? AND status = ? AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1", (user_id, STATUS_COMPLETED))
                    row = cur.fetchone()
                    if row and row['completed_at']:
                        last_completed = datetime.fromisoformat(row['completed_at']).timestamp()
                        time_diff = datetime.now().timestamp() - last_completed
                        if time_diff < COOLDOWN_SECONDS:
                            conn.rollback()
                            remaining = int(COOLDOWN_SECONDS - time_diff)
                            return None, f"⏳ Подождите {remaining // 60} мин {remaining % 60} сек перед созданием новой заявки."
                    
                    if use_free_deal:
                        cur = conn.execute("SELECT id FROM free_deals WHERE user_id=? AND used_at IS NULL LIMIT 1", (user_id,))
                        free_deal_row = cur.fetchone()
                        if not free_deal_row:
                            conn.rollback()
                            return None, "❌ Бесплатная сделка недоступна."
                        cur = conn.execute("UPDATE free_deals SET used_at=? WHERE id=? AND used_at IS NULL", (datetime.now().isoformat(), free_deal_row['id']))
                        if cur.rowcount != 1:
                            conn.rollback()
                            return None, "❌ Не удалось списать бесплатную сделку."
                        conn.execute("UPDATE clients SET free_deals_count = MAX(0, free_deals_count - 1) WHERE user_id=?", (user_id,))
                    
                    cur = conn.execute("INSERT INTO requests (user_id, operation_type, amount, client_total, status, created_at, invoice_link) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, operation_type, amount, client_total, STATUS_PENDING, datetime.now().isoformat(), invoice_link))
                    request_id = cur.lastrowid
                    conn.execute("INSERT INTO request_events (request_id, old_status, new_status, changed_by, changed_at, comment) VALUES (?, NULL, ?, 'system', ?, 'Заявка создана')", (request_id, STATUS_PENDING, datetime.now().isoformat()))
                    conn.commit()
                    return request_id, None
                except Exception as e:
                    conn.rollback()
                    logging.error(f"Atomic request creation failed: {e}")
                    return None, "❌ Внутренняя ошибка. Попробуйте позже."

    # --- Заявки ---
    async def get_request(self, request_id: int):
        rows = await self._run_query("SELECT * FROM requests WHERE id=?", (request_id,))
        return rows[0] if rows else None

    async def get_user_active_request(self, user_id: int):
        rows = await self._run_query("SELECT id, operation_type, amount, client_total, status FROM requests WHERE user_id=? AND status IN (?,?,?,?) ORDER BY id DESC LIMIT 1", (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0] if rows else None

    async def get_active_requests_count(self, user_id: int) -> int:
        rows = await self._run_query("SELECT COUNT(*) as cnt FROM requests WHERE user_id=? AND status IN (?, ?, ?, ?)", (user_id, STATUS_PENDING, STATUS_PROCESSING, STATUS_REQUISITES_SENT, STATUS_PAID))
        return rows[0]['cnt'] if rows else 0

    async def get_all_pending_requests(self, limit: int = 20):
        return await self._run_query("SELECT id, user_id, amount, client_total, status, created_at, invoice_link FROM requests WHERE status=? ORDER BY created_at DESC LIMIT ?", (STATUS_PENDING, limit))

    async def get_all_processing_requests(self, limit: int = 20):
        return await self._run_query("SELECT id, user_id, amount, client_total, status, created_at, invoice_link FROM requests WHERE status IN (?,?) ORDER BY created_at DESC LIMIT ?", (STATUS_PROCESSING, STATUS_REQUISITES_SENT, limit))

    async def take_request(self, request_id: int):
        await self._run_execute("UPDATE requests SET status=?, taken_at=? WHERE id=? AND status=?", (STATUS_PROCESSING, datetime.now().isoformat(), request_id, STATUS_PENDING))
        await self._log_event(request_id, STATUS_PENDING, STATUS_PROCESSING, 'admin', 'Взято в работу')

    async def send_requisites(self, request_id: int, requisites_text: str):
        await self._run_execute("UPDATE requests SET status=?, requisites_sent_at=?, requisites_text=? WHERE id=? AND status=?", (STATUS_REQUISITES_SENT, datetime.now().isoformat(), requisites_text, request_id, STATUS_PROCESSING))
        await self._log_event(request_id, STATUS_PROCESSING, STATUS_REQUISITES_SENT, 'admin', 'Реквизиты отправлены')
        await self.create_reminder_record(request_id)

    async def mark_paid(self, request_id: int, pdf_file_id: str):
        await self._run_execute("UPDATE requests SET status=?, paid_at=?, pdf_file_id=? WHERE id=? AND status=?", (STATUS_PAID, datetime.now().isoformat(), pdf_file_id, request_id, STATUS_REQUISITES_SENT))
        await self._log_event(request_id, STATUS_REQUISITES_SENT, STATUS_PAID, 'user', 'Чек получен')
        await self.delete_reminder_record(request_id)

    async def complete_request(self, request_id: int, user_id: int, amount: float):
        now = datetime.now().isoformat()
        await self._run_execute("UPDATE requests SET status=?, completed_at=? WHERE id=? AND status=?", (STATUS_COMPLETED, now, request_id, STATUS_PAID))
        await self._log_event(request_id, STATUS_PAID, STATUS_COMPLETED, 'admin', 'Сделка завершена')
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
        await self._log_event(request_id, old_status, status, cancelled_by, f'Заявка отменена ({cancelled_by})')
        await self.delete_reminder_record(request_id)
        return True

    # --- Напоминания ---
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

    # --- Отзывы ---
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

    # --- Настройки ---
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

def calculate_client_total(amount: float, discount_percent: float = 0.0, use_free_deal: bool = False) -> float:
    if use_free_deal:
        return amount + 285
    return (amount * (1 + max(0, 0.169 - (discount_percent / 100)))) + 285

def get_progress_bar(current: int, needed: int) -> str:
    if needed <= 0:
        return "▰" * 10
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
    try:
        return await context.bot.send_message(chat_id=user_id, text=text, **kwargs)
    except Forbidden:
        logging.warning(f"User {user_id} blocked the bot.")
    except Exception as e:
        logging.error(f"Error sending message to {user_id}: {e}")
    return None

def reset_request_flow(context: ContextTypes.DEFAULT_TYPE):
    for key in ['step', 'temp_amount', 'operation_type', 'base_operation', 'selected_coin',
                'invoice_link', 'feedback_req_id', 'feedback_rating', 'pending_free_deal',
                'temp_client_total_with_commission', 'temp_client_total_without_commission',
                'temp_op_type', 'temp_coin_data', 'temp_invoice_link']:
        context.user_data.pop(key, None)

# ================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🔥 НОВЫЙ ЗАПРОС")],
        [KeyboardButton("⭐ ОТЗЫВЫ")],
        [KeyboardButton("📜 ПРАВИЛА"), KeyboardButton("👤 ПРОФИЛЬ")],
        [KeyboardButton("📞 ПОДДЕРЖКА"), KeyboardButton("❓ КАК ОПЛАТИТЬ")],
        [KeyboardButton("🎁 РЕФЕРАЛЫ")],
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
    if not user:
        return
    args = context.args
    ref_code = args[0] if args else None
    if ref_code:
        if ref_code.startswith("ref_"):
            ref_code = ref_code[4:]
        referrer = await db.get_user_by_referral_code(ref_code)
        if referrer and referrer['user_id'] != user.id:
            await db.add_client(user.id, user.username)
            success = await db.add_referral(referrer['user_id'], user.id)
            if success:
                context.user_data['referred_by'] = referrer['user_id']
                await update.message.reply_text(f"🎉 Вас пригласил @{referrer['username']}!\n\n🎁 Вы получите скидку 3% на первый обмен!\n\n➡️ Нажмите «НОВЫЙ ЗАПРОС», чтобы начать.", reply_markup=get_main_keyboard())
                return
    await db.add_client(user.id, user.username)
    await db.update_last_activity(user.id)
    reset_request_flow(context)
    rate = await get_usdt_rate()
    stats = await db.get_client_stats(user.id)
    deals = stats['total_deals'] if stats else 0
    rank_name, rank_emoji, discount, _ = get_rank_and_discount(deals)
    free_deals = stats['free_deals_count'] if stats else 0
    safe_name = html.escape(user.first_name)
    if user.id == ADMIN_ID:
        await update.message.reply_text("🔐 ДОБРО ПОЖАЛОВАТЬ, АДМИНИСТРАТОР!\n\nИспользуйте команду /admin для входа в панель.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text(f"🔥 <b>ДОБРО ПОЖАЛОВАТЬ, {safe_name}!</b> 🔥\n\nSVEN OBMEN — быстрый обмен криптовалюты\n\n📊 <b>СЕГОДНЯ:</b>\n• Курс USDT: {rate:.1f} ₽\n• Ваш ранг: {rank_emoji} {rank_name}\n• Скидка: {discount}%\n• Бесплатных сделок: {free_deals}\n\n▶️ <b>НАЧНИТЕ ОБМЕН ПРЯМО СЕЙЧАС:</b>\n\n📚 <b>ПОЛЕЗНОЕ:</b>\n• 📜 ПРАВИЛА | 👤 ПРОФИЛЬ | ⭐ ОТЗЫВЫ\n• 📞 ПОДДЕРЖКА | ❓ КАК ОПЛАТИТЬ\n• 🎁 РЕФЕРАЛЫ — приглашай друзей и получай бесплатные сделки!", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_last_activity(update.effective_user.id)
    await update.message.reply_text(f"📖 <b>СПРАВОЧНИК</b>\n\n<b>Основные команды:</b>\n/start — перезапуск бота\n/help — это сообщение\n/stats — ваш профиль\n/status — статус текущей заявки\n/skip — пропустить ввод комментария к отзыву\n/cancel — отменить текущую заявку\n/referral — реферальная программа\n\n<b>Как начать обмен:</b>\n1️⃣ Нажмите «🔥 НОВЫЙ ЗАПРОС»\n2️⃣ Выберите тип операции\n3️⃣ Выберите криптовалюту (Биткоин или Tether)\n4️⃣ Введите сумму\n5️⃣ Подтвердите данные\n6️⃣ Ожидайте реквизиты от оператора\n\n📞 <b>Поддержка:</b> {SUPPORT_CONTACT}", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    stats = await db.get_client_stats(user_id)
    if not stats or stats['total_deals'] == 0:
        await update.message.reply_text("🔒 Реферальная программа доступна только после первой сделки.\n\nСовершите обмен, чтобы получить свою реферальную ссылку!", reply_markup=get_main_keyboard())
        return
    ref_code = await db.get_referral_code(user_id)
    if not ref_code:
        await update.message.reply_text("❌ Ошибка: реферальный код не найден.", reply_markup=get_main_keyboard())
        return
    bot_username = context.bot.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{ref_code}"
    referrals_count = stats['referral_completed_count'] or 0
    free_deals = stats['free_deals_count'] or 0
    needed = (3 - (referrals_count % 3)) % 3
    await update.message.reply_text(f"🔗 <b>ВАША РЕФЕРАЛЬНАЯ ССЫЛКА</b>\n\n<code>{ref_link}</code>\n\n📊 <b>СТАТИСТИКА:</b>\n• Друзей приглашено: {referrals_count}\n• До следующей бесплатной сделки: {needed} чел.\n• Бесплатных сделок доступно: {free_deals}\n\n🎁 <b>КАК РАБОТАЕТ:</b>\n• Пригласи 3 друзей, и они совершат обмен\n• Ты получишь 1 сделку БЕЗ КОМИССИИ!\n• Бесплатные сделки накапливаются и используются по желанию\n\n🔥 Чем больше друзей, тем больше бесплатных обменов!", parse_mode="HTML", reply_markup=get_main_keyboard())

@not_banned
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    active = await db.get_user_active_request(user_id)
    if not active:
        await update.message.reply_text("❌ У вас нет активных заявок.", reply_markup=get_main_keyboard())
        return
    success = await db.cancel_request(active['id'], "user", user_id)
    if success:
        await update.message.reply_text(f"✅ Заявка #{active['id']} отменена.", reply_markup=get_main_keyboard())
        await context.bot.send_message(ADMIN_ID, f"⚠️ Пользователь {format_user(update.effective_user.username, user_id)} отменил заявку #{active['id']}")
    else:
        await update.message.reply_text("❌ Эту заявку нельзя отменить (реквизиты уже отправлены).", reply_markup=get_main_keyboard())

@not_banned
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await db.update_last_activity(user_id)
    active = await db.get_user_active_request(user_id)
    if not active:
        await update.message.reply_text("📋 У вас нет активных заявок.\n\nНажмите «🔥 НОВЫЙ ЗАПРОС», чтобы начать обмен.", reply_markup=get_main_keyboard())
        return
    req_id = active['id']
    amount = active['amount']
    client_total = active['client_total']
    status = active['status']
    operation_type = active['operation_type']
    progress_bar, percent = get_status_progress_bar(status)
    status_text = format_status(status)
    if "Биткоин" in operation_type or "BTC" in operation_type:
        coin_symbol, coin_code, rate = "₿", "BTC", await get_btc_rate()
    else:
        coin_symbol, coin_code, rate = "🪙", "USDT", await get_usdt_rate()
    crypto_amount = client_total / rate
    text = f"📋 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n📊 <b>СТАТУС:</b> {status_text}\n💰 Сумма: {amount:.0f} ₽\n💸 К оплате: {client_total:.0f} ₽\n{coin_symbol} Вы получите: {crypto_amount:.8f} {coin_code}\n📌 Курс: ≈{rate:.1f} ₽ за 1 {coin_code}\n\n"
    if status == STATUS_REQUISITES_SENT:
        text += "📎 <b>После оплаты пришлите PDF-чек!</b>\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>Неоплата влечёт БЛОКИРОВКУ АККАУНТА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    elif status == STATUS_PAID:
        text += "🔍 <b>Чек на проверке. Ожидайте подтверждения.</b>\n\n"
    elif status == STATUS_COMPLETED:
        text += "✅ <b>Сделка завершена! Спасибо за обращение!</b>\n\n"
    text += "🚫 Отменить заявку: /cancel"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_main_keyboard())

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
        await update.message.reply_text("✅ Отзыв сохранен без комментария.", reply_markup=get_main_keyboard())
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
            await update.message.reply_text("😴 Бот временно не принимает новые заявки.")
            return
        active_count = await db.get_active_requests_count(user_id)
        if active_count >= MAX_ACTIVE_REQUESTS:
            await update.message.reply_text(f"⚠️ У вас уже {active_count} активных заявок.\nМаксимум: {MAX_ACTIVE_REQUESTS}\n\nДождитесь завершения или отмените их.")
            return
        reset_request_flow(context)
        await update.message.reply_text("💰 ВЫБЕРИТЕ ТИП ОПЕРАЦИИ:", reply_markup=get_operation_keyboard())
    elif text == "⭐ ОТЗЫВЫ":
        context.user_data['reviews_page'] = 0
        await show_reviews(update, context)
    elif text == "📜 ПРАВИЛА":
        rules = await db.get_setting('rules') or "Правила не заданы."
        await update.message.reply_text(rules, reply_markup=get_main_keyboard())
    elif text == "👤 ПРОФИЛЬ":
        await show_profile(update, context, user_id)
    elif text == "📞 ПОДДЕРЖКА":
        await update.message.reply_text(f"📞 <b>СЛУЖБА ПОДДЕРЖКИ</b>\n\nПо всем вопросам обращайтесь к оператору:\n{SUPPORT_CONTACT}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВАЖНЫЕ ПРАВИЛА ОБЩЕНИЯ:</b>\n\n1️⃣ <b>Описывайте вопрос в ОДНОМ сообщении</b>\n2️⃣ <b>Указывайте номер заявки</b> (если она создана)\n3️⃣ <b>Неоплата созданной заявки = БАН</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n", parse_mode="HTML", reply_markup=get_main_keyboard())
    elif text == "❓ КАК ОПЛАТИТЬ":
        await update.message.reply_text(f"📎 <b>ИНСТРУКЦИЯ ПО ОПЛАТЕ</b>\n\n<b>Для оплаты по СБП (по номеру телефона):</b>\n1️⃣ Откройте приложение банка с СБП\n2️⃣ Выберите «Оплата по номеру телефона»\n3️⃣ Введите номер телефона из реквизитов\n4️⃣ Укажите точную сумму из реквизитов\n5️⃣ Подтвердите платеж\n\n<b>Для оплаты по номеру карты:</b>\n1️⃣ Откройте приложение банка\n2️⃣ Выберите «Перевод по номеру карты»\n3️⃣ Введите номер карты из реквизитов\n4️⃣ Укажите сумму из реквизитов\n5️⃣ Назначьте платеж: «Оплата обмена USDT»\n6️⃣ Подтвердите платеж\n\n📎 <b>ЧТО ДЕЛАТЬ ПОСЛЕ ОПЛАТЫ?</b>\n• Сделайте скриншот или сохраните PDF чек\n• Отправьте файл в этот чат\n• Оператор проверит чек и завершит сделку\n\n⚠️ <b>ВАЖНО:</b> Неоплата влечёт блокировку аккаунта!", parse_mode="HTML", reply_markup=get_main_keyboard())
    elif text == "🎁 РЕФЕРАЛЫ":
        await referral_command(update, context)
    elif user_id == ADMIN_ID:
        if text == "📋 ЗАЯВКИ":
            await show_requests_list(update, context)
        elif text == "⚙️ НАСТРОЙКИ":
            await update.message.reply_text("⚙️ НАСТРОЙКИ\n\n/edit_rules — изменить правила\n/edit_schedule — изменить график\n/edit_links — изменить ссылки\n/afk on — закрыть приём заявок\n/afk off — открыть приём заявок", reply_markup=get_admin_keyboard())
        elif text == "📊 СТАТИСТИКА":
            await show_admin_stats(update, context)
        elif text == "🚫 ЗАБАНЕННЫЕ":
            await show_banned_users(update, context)
        elif text == "◀️ ВЫЙТИ":
            await update.message.reply_text("🔐 Выход из админ-панели.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Пожалуйста, используйте кнопки меню.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Пожалуйста, используйте кнопки меню.", reply_markup=get_main_keyboard())

async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    stats = await db.get_client_stats(user_id)
    if not stats or stats['total_deals'] == 0:
        await update.message.reply_text("👤 <b>ПРОФИЛЬ</b>\n\n📊 У вас пока нет завершённых сделок.\n\n🔥 Нажмите «НОВЫЙ ЗАПРОС», чтобы начать обмен и получить первый ранг!", parse_mode="HTML", reply_markup=get_main_keyboard())
        return
    deals = stats['total_deals']
    volume = stats['total_volume']
    rating = stats['avg_rating'] or 0
    rating_count = stats['ratings_count'] or 0
    free_deals = stats['free_deals_count'] or 0
    referrals_count = stats['referral_completed_count'] or 0
    username_from_db = stats['username'] or str(user_id)
    rank_name, rank_emoji, discount, next_rank_deals = get_rank_and_discount(deals)
    progress_bar = get_progress_bar(deals, next_rank_deals)
    next_rank_name = ""
    if deals < 3: next_rank_name = "Ходок"
    elif deals < 7: next_rank_name = "Опытный"
    elif deals < 10: next_rank_name = "Мастер"
    elif deals < 15: next_rank_name = "Легенда"
    next_rank_text = f"📌 До ранга <b>{next_rank_name}</b>: {next_rank_deals} сделок" if next_rank_deals > 0 and next_rank_name else "🏆 Максимальный ранг!"
    bonus_text = f"💰 Ваша скидка: {discount}%" if discount > 0 else "🔥 Совершите 3 сделки для получения скидки!"
    await update.message.reply_text(f"👤 <b>ПРОФИЛЬ</b> | @{username_from_db}\n\n🏆 <b>{rank_emoji} {rank_name}</b>\n📊 <b>ПРОГРЕСС:</b>\n<code>{progress_bar}</code>\n{next_rank_text}\n\n🎁 <b>РЕФЕРАЛЬНАЯ ПРОГРАММА:</b>\n• Приглашено друзей: {referrals_count}\n• Бесплатных сделок доступно: {free_deals}\n\n📈 <b>СТАТИСТИКА:</b>\n• Сделок: <b>{deals}</b>\n• Объём: <b>{volume:.0f} ₽</b>\n• Рейтинг: ⭐ <b>{rating:.1f}</b> ({rating_count} отзывов)\n\n{bonus_text}", parse_mode="HTML", reply_markup=get_main_keyboard())

async def show_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = context.user_data.get('reviews_page', 0)
    limit = 5
    reviews = await db.get_feedback_for_display(limit, page * limit)
    total = await db.get_feedback_count()
    avg_rating = await db.get_avg_rating()
    if not reviews:
        if update.callback_query:
            await update.callback_query.answer("Больше отзывов нет.")
            return
        else:
            await update.message.reply_text("⭐ ПОКА НЕТ ОТЗЫВОВ.\nБудьте первым!", reply_markup=get_main_keyboard())
            return
    text = f"⭐ ОТЗЫВЫ КЛИЕНТОВ\n\nВсего: {total} | Средний рейтинг: {avg_rating:.1f} ⭐\n━━━━━━━━━━━━━\n\n"
    for r in reviews:
        stars = "⭐" * r['rating'] if r['rating'] is not None else "📝"
        username = r['username'] or "User"
        created = (r['created_at'] or "")[:10]
        text += f"👤 @{username}\n📅 {created}\n"
        if r['comment']:
            text += f'💬 "{r["comment"]}"\n'
        text += f"Оценка: {stars}\n━━━━━━━━━━━━━\n"
    kb_row = []
    if total > (page + 1) * limit:
        kb_row.append(InlineKeyboardButton("📌 ПОКАЗАТЬ ЕЩЁ", callback_data="reviews_next"))
    kb_row.append(InlineKeyboardButton("◀️ В МЕНЮ", callback_data="back_to_menu_ui"))
    reply_markup = InlineKeyboardMarkup([kb_row])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

@admin_only
async def show_banned_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    banned = await db.get_banned_users()
    if not banned:
        await update.message.reply_text("🚫 НЕТ ЗАБАНЕННЫХ ПОЛЬЗОВАТЕЛЕЙ.", reply_markup=get_admin_keyboard())
        return
    text = "🚫 ЗАБАНЕННЫЕ ПОЛЬЗОВАТЕЛИ\n\n"
    for u in banned:
        text += f"👤 {format_user(u['username'], u['user_id'])} (ID: {u['user_id']})\n📅 Забанен: {(u['banned_at'] or '')[:10]}\n📝 Причина: {u['ban_reason']}\n━━━━━━━━━━━━━\n"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_requests_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await db.get_all_pending_requests(limit=10)
    processing = await db.get_all_processing_requests(limit=10)
    if not pending and not processing:
        await update.message.reply_text("📋 НЕТ АКТИВНЫХ ЗАЯВОК.", reply_markup=get_admin_keyboard())
        return
    text = "📋 АКТИВНЫЕ ЗАЯВКИ\n\n"
    if pending:
        text += "🟡 В ОЖИДАНИИ:\n"
        for req in pending:
            link_info = f"\n    🔗 {html.escape(req['invoice_link'][:40])}..." if req['invoice_link'] else ""
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | ID:{req['user_id']}{link_info}\n"
        text += "\n"
    if processing:
        text += "🟢 В РАБОТЕ:\n"
        for req in processing:
            ico = "⏳" if req['status'] == STATUS_PROCESSING else "💳"
            link_info = f"\n    🔗 {html.escape(req['invoice_link'][:40])}..." if req['invoice_link'] else ""
            text += f"  #{req['id']} | {req['amount']:.0f} ₽ | {ico}{link_info}\n"
    text += "\n🔧 Команды:\n/take <id> | /send <id> <реквизиты> | /confirm <id> | /reject <id> | /getpdf <id> | /getlink <id>"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

@admin_only
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clients = await db.get_all_clients()
    avg_rating = await db.get_avg_rating()
    total_deals = sum(c['total_deals'] for c in clients)
    total_volume = sum(c['total_volume'] for c in clients)
    text = f"📊 ОБЩАЯ СТАТИСТИКА\n\n• Клиентов (активных): {len(clients)}\n• Всего сделок: {total_deals}\n• Общий объём: {total_volume:.0f} ₽\n• Ср. рейтинг: ⭐ {avg_rating:.1f}\n• Расч. прибыль (~10%): {total_volume * 0.1:.0f} ₽\n"
    if clients:
        text += "\n🏆 ТОП КЛИЕНТОВ:\n"
        for i, c in enumerate(clients[:10], 1):
            text += f"{i}. {format_user(c['username'], c['user_id'])} — {c['total_deals']} сделок\n"
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

# ================== CALLBACK =====================
@idempotent_callback(timeout=5)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
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
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(user_id, "Главное меню:", reply_markup=get_main_keyboard())
    elif data == "back_to_menu_ui":
        reset_request_flow(context)
        try:
            await query.message.delete()
        except Exception:
            pass
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
            coin_data = {'name': "Биткоин (BTC)", 'symbol': "₿", 'code': "BTC", 'rate_func': get_btc_rate, 'fallback_rate': 5500000}
        else:
            coin_data = {'name': "Tether (USDT)", 'symbol': "🪙", 'code': "USDT", 'rate_func': get_usdt_rate, 'fallback_rate': 92.5}
        context.user_data['selected_coin'] = coin_data
        op_map = {
            ("OXAPAY", "btc"): OPERATION_OXAPAY_BTC, ("OXAPAY", "usdt"): OPERATION_OXAPAY_USDT,
            ("BITPAPA", "btc"): OPERATION_BITPAPA_BTC, ("BITPAPA", "usdt"): OPERATION_BITPAPA_USDT,
            ("CRYPTO", "btc"): OPERATION_CRYPTO_BTC, ("CRYPTO", "usdt"): OPERATION_CRYPTO_USDT,
            ("SHOP", "btc"): OPERATION_SHOP_BTC, ("SHOP", "usdt"): OPERATION_SHOP_USDT,
        }
        context.user_data['operation_type'] = op_map.get((base_op, coin), OPERATION_CRYPTO_USDT)
        context.user_data['step'] = ASKING_AMOUNT
        await query.edit_message_text(f"💰 ВВЕДИТЕ СУММУ В РУБЛЯХ\n\nВы выбрали: {coin_data['name']}\nМинимальная сумма: 1000 ₽.", reply_markup=get_back_inline())
    elif data == "confirm_request":
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        coin_data = context.user_data.get('selected_coin')
        invoice_link = context.user_data.get('invoice_link')
        if not amount or not op_type or not coin_data:
            await query.edit_message_text("❌ Ошибка данных. Начните заново.", reply_markup=get_operation_keyboard())
            return
        if await is_afk_mode() and user_id != ADMIN_ID:
            await query.edit_message_text("😴 Бот временно не принимает заявки.", reply_markup=get_operation_keyboard())
            return
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        free_deals_count = stats['free_deals_count'] if stats else 0
        if free_deals_count > 0:
            context.user_data.update({
                'temp_amount': amount, 'temp_op_type': op_type, 'temp_coin_data': coin_data,
                'temp_invoice_link': invoice_link,
                'temp_client_total_with_commission': calculate_client_total(amount, discount, False),
                'temp_client_total_without_commission': calculate_client_total(amount, discount, True),
                'pending_free_deal': True
            })
            await query.edit_message_text(f"🎁 <b>У ВАС ЕСТЬ БЕСПЛАТНАЯ СДЕЛКА!</b>\n\n💰 Сумма без скидки: {calculate_client_total(amount, discount, False):.0f} ₽\n🎁 С бесплатной сделкой: {calculate_client_total(amount, discount, True):.0f} ₽\n\nВыберите вариант:", parse_mode="HTML", reply_markup=get_free_deal_keyboard())
            return
        client_total = calculate_client_total(amount, discount, False)
        rate = await coin_data['rate_func']() or coin_data['fallback_rate']
        crypto_amount = client_total / rate
        req_id, error = await db.create_request_atomic(user_id, op_type, amount, client_total, False, invoice_link)
        if req_id is None:
            await query.edit_message_text(error, reply_markup=get_operation_keyboard())
            return
        progress_bar, _ = get_status_progress_bar(STATUS_PENDING)
        msg = f"✅ <b>ЗАЯВКА #{req_id} СОЗДАНА!</b>\n\n📋 Тип: {op_type}\n💎 Монета: {coin_data['name']}\n💰 Сумма: {amount:.0f} ₽\n{coin_data['symbol']} Вы получите: {crypto_amount:.8f} {coin_data['code']}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n"
        if invoice_link:
            msg += f"🔗 Ссылка: {html.escape(invoice_link)}\n"
        msg += f"\n📌 Курс: ≈{rate:.2f} ₽ за 1 {coin_data['code']}\n\n📊 <b>ПРОГРЕСС СДЕЛКИ:</b>\n{progress_bar}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n👨‍💼 Оператор скоро свяжется с вами.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВАЖНО:</b> После получения реквизитов вы ОБЯЗАНЫ произвести оплату.\nНеоплата созданной заявки влечёт <b>БЛОКИРОВКУ АККАУНТА</b> (БАН).\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}\n\n🚫 Отменить заявку можно по кнопке ниже."
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=get_cancel_keyboard(req_id))
        await context.bot.send_message(ADMIN_ID, f"🔔 <b>НОВАЯ ЗАЯВКА #{req_id}</b>\n\n👤 {format_user(query.from_user.username, user_id)}\n📋 Тип: {op_type}\n💎 Монета: {coin_data['name']}\n💰 {amount:.0f} ₽\n{coin_data['symbol']} {crypto_amount:.8f} {coin_data['code']}\n💸 К оплате: {client_total:.0f} ₽\n" + (f"🔗 {invoice_link}\n" if invoice_link else ""), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ ВЗЯТЬ В РАБОТУ", callback_data=f"admin_take_{req_id}")], [InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"admin_reject_{req_id}")]]))
        reset_request_flow(context)
    elif data in ("use_free_deal", "skip_free_deal"):
        if not context.user_data.get('pending_free_deal'):
            await query.edit_message_text("❌ Ошибка. Начните заново.", reply_markup=get_operation_keyboard())
            return
        use_free = (data == "use_free_deal")
        amount = context.user_data['temp_amount']
        op_type = context.user_data['temp_op_type']
        coin_data = context.user_data['temp_coin_data']
        invoice_link = context.user_data['temp_invoice_link']
        client_total = context.user_data['temp_client_total_without_commission'] if use_free else context.user_data['temp_client_total_with_commission']
        rate = await coin_data['rate_func']() or coin_data['fallback_rate']
        crypto_amount = client_total / rate
        req_id, error = await db.create_request_atomic(user_id, op_type, amount, client_total, use_free, invoice_link)
        if req_id is None:
            await query.edit_message_text(error, reply_markup=get_operation_keyboard())
            return
        progress_bar, _ = get_status_progress_bar(STATUS_PENDING)
        free_text = " (БЕСПЛАТНАЯ СДЕЛКА)" if use_free else ""
        msg = f"✅ <b>ЗАЯВКА #{req_id} СОЗДАНА{free_text}!</b>\n\n📋 Тип: {op_type}\n💎 Монета: {coin_data['name']}\n💰 Сумма: {amount:.0f} ₽\n{coin_data['symbol']} Вы получите: {crypto_amount:.8f} {coin_data['code']}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n"
        if invoice_link:
            msg += f"🔗 Ссылка: {html.escape(invoice_link)}\n"
        msg += f"\n📌 Курс: ≈{rate:.2f} ₽ за 1 {coin_data['code']}\n\n📊 <b>ПРОГРЕСС СДЕЛКИ:</b>\n{progress_bar}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n👨‍💼 Оператор скоро свяжется с вами.\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВАЖНО:</b> После получения реквизитов вы ОБЯЗАНЫ произвести оплату.\nНеоплата созданной заявки влечёт <b>БЛОКИРОВКУ АККАУНТА</b> (БАН).\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}\n\n🚫 Отменить заявку можно по кнопке ниже."
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=get_cancel_keyboard(req_id))
        await context.bot.send_message(ADMIN_ID, f"🔔 <b>НОВАЯ ЗАЯВКА #{req_id}{free_text}</b>\n\n👤 {format_user(query.from_user.username, user_id)}\n📋 Тип: {op_type}\n💎 Монета: {coin_data['name']}\n💰 {amount:.0f} ₽\n{coin_data['symbol']} {crypto_amount:.8f} {coin_data['code']}\n💸 К оплате: {client_total:.0f} ₽\n" + (f"🔗 {invoice_link}\n" if invoice_link else ""), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ ВЗЯТЬ В РАБОТУ", callback_data=f"admin_take_{req_id}")], [InlineKeyboardButton("❌ ОТКЛОНИТЬ", callback_data=f"admin_reject_{req_id}")]]))
        reset_request_flow(context)
    elif data.startswith("admin_take_"):
        if user_id != ADMIN_ID:
            await query.answer("Только для администратора.", show_alert=True)
            return
        req_id = int(data.split("_")[2])
        req = await db.get_request(req_id)
        if not req or req['status'] != STATUS_PENDING:
            await query.edit_message_text("❌ Заявка не найдена или уже обработана.")
            return
        await db.take_request(req_id)
        await query.edit_message_text(f"✅ Заявка #{req_id} взята в работу!")
        progress_bar, _ = get_status_progress_bar(STATUS_PROCESSING)
        await safe_send(context, req['user_id'], f"🔄 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n👨‍💼 Оператор начал обработку вашей заявки.\n\nОжидайте реквизиты в ближайшее время.\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")
    elif data.startswith("admin_reject_"):
        if user_id != ADMIN_ID:
            await query.answer("Только для администратора.", show_alert=True)
            return
        req_id = int(data.split("_")[2])
        req = await db.get_request(req_id)
        if not req:
            await query.edit_message_text("❌ Заявка не найдена.")
            return
        await db.cancel_request(req_id, "admin")
        await query.edit_message_text(f"❌ Заявка #{req_id} отклонена!")
        await safe_send(context, req['user_id'], f"❌ <b>ЗАЯВКА #{req_id} ОТКЛОНЕНА</b>\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")
    elif data.startswith("cancel_"):
        try:
            req_id = int(data.split("_")[1])
            success = await db.cancel_request(req_id, "user", user_id)
            if success:
                await query.edit_message_text(f"✅ ЗАЯВКА #{req_id} ОТМЕНЕНА.", reply_markup=get_operation_keyboard())
                await context.bot.send_message(ADMIN_ID, f"⚠️ Пользователь {format_user(query.from_user.username, user_id)} отменил заявку #{req_id}")
            else:
                await query.answer("Невозможно отменить эту заявку.", show_alert=True)
        except Exception as e:
            logging.exception(f"cancel callback error: {e}")
    elif data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                req_id = int(parts[1])
                rating_str = parts[2]
                if rating_str == "skip":
                    await db.add_feedback(user_id, req_id, None, None)
                    await query.edit_message_text("✅ Отзыв пропущен.", reply_markup=get_main_keyboard())
                else:
                    rating = int(rating_str)
                    context.user_data.update({'feedback_req_id': req_id, 'feedback_rating': rating, 'step': ASKING_FEEDBACK_COMMENT})
                    await query.edit_message_text(f"Вы поставили {rating}⭐\n\n✏️ Оставьте комментарий или отправьте /skip для пропуска:")
            except Exception:
                pass

# ================== ОБРАБОТКА СООБЩЕНИЙ ===========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    
    banned, reason = await db.is_banned(user_id)
    if banned:
        reset_request_flow(context)
        await update.message.reply_text(f"⛔ ДОСТУП ЗАБЛОКИРОВАН\n\nПричина: {reason}", reply_markup=get_main_keyboard())
        return
    
    last_active = await db.get_last_activity(user_id)
    if last_active and (datetime.now().timestamp() - last_active) > SESSION_TIMEOUT_SECONDS and context.user_data.get('step'):
        reset_request_flow(context)
        await update.message.reply_text("⏳ <b>Сессия истекла.</b> Возвращаю в главное меню.\n\nНажмите «🔥 НОВЫЙ ЗАПРОС», чтобы начать заново.", parse_mode="HTML", reply_markup=get_main_keyboard())
        return
    
    await db.update_last_activity(user_id)
    
    if update.message.document:
        if update.message.document.mime_type == 'application/pdf':
            active = await db.get_user_active_request(user_id)
            if not active or active['status'] != STATUS_REQUISITES_SENT:
                await update.message.reply_text(f"❌ У вас нет заявок в статусе ожидания оплаты.\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")
                return
            await db.mark_paid(active['id'], update.message.document.file_id)
            await update.message.reply_text("✅ ЧЕК ПРИНЯТ!\nСтатус: 🔍 ПРОВЕРКА ОПЕРАТОРОМ\nОбычно это занимает 5–15 минут.")
            await context.bot.send_message(ADMIN_ID, f"💳 ПОЛУЧЕН ЧЕК\n👤 {format_user(update.effective_user.username, user_id)}\n📋 Заявка #{active['id']}\n📄 /getpdf {active['id']}\n✅ /confirm {active['id']}")
        else:
            await update.message.reply_text("❌ Пожалуйста, отправьте чек именно в формате PDF.")
        return
    
    msg_text = update.message.text or ""

    if user_id == ADMIN_ID and context.user_data.get('editing_setting'):
        key = context.user_data.pop('editing_setting')
        await db.update_setting(key, msg_text)
        await update.message.reply_text(f"✅ Настройка '{key}' обновлена!", reply_markup=get_admin_keyboard())
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

    if context.user_data.get('step') == ASKING_AMOUNT:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        amount = extract_amount(msg_text)
        if not amount or amount < 1000:
            await update.message.reply_text("❌ Введите корректную сумму (минимум 1000 ₽).", reply_markup=get_back_inline())
            return
        context.user_data['temp_amount'] = amount
        op_type = context.user_data.get('operation_type')
        if "OxaPay" in str(op_type):
            context.user_data['step'] = ASKING_LINK
            await update.message.reply_text("🔗 ВВЕДИТЕ ССЫЛКУ НА СЧЁТ OXAPAY\n\nПример: https://pay.oxapay.com/invoice/xxxxxxxx", reply_markup=get_back_inline())
        else:
            stats = await db.get_client_stats(user_id)
            deals = stats['total_deals'] if stats else 0
            _, _, discount, _ = get_rank_and_discount(deals)
            coin_data = context.user_data.get('selected_coin')
            rate = await coin_data['rate_func']() if coin_data and coin_data['code'] == "BTC" else await get_usdt_rate()
            coin_symbol = coin_data['symbol'] if coin_data else "🪙"
            coin_code = coin_data['code'] if coin_data else "USDT"
            client_total = calculate_client_total(amount, discount, False)
            crypto_amount = client_total / rate
            await update.message.reply_text(f"📝 <b>ПРОВЕРЬТЕ ДАННЫЕ</b>\n\n📋 Тип: {op_type}\n💰 Сумма: {amount:.0f} ₽\n{coin_symbol} Вы получите: {crypto_amount:.8f} {coin_code}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n📌 Курс: ≈{rate:.2f} ₽ за 1 {coin_code}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВНИМАНИЕ!</b>\n• После получения реквизитов вы ОБЯЗАНЫ произвести оплату\n• <b>Неоплата влечёт БЛОКИРОВКУ АККАУНТА (БАН)</b>\n• Отменить заявку после получения реквизитов НЕВОЗМОЖНО\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n✅ <b>ПОДТВЕРДИТЕ, ЧТО ГОТОВЫ ОПЛАТИТЬ</b>", parse_mode="HTML", reply_markup=get_confirm_keyboard())
            context.user_data.pop('step', None)
        return

    if context.user_data.get('step') == ASKING_LINK:
        if msg_text in MENU_BUTTONS:
            reset_request_flow(context)
            await handle_menu(update, context)
            return
        link = msg_text.strip()
        if not (link.startswith("https://pay.oxapay.com/invoice/") or link.startswith("https://oxapay.com/invoice/")):
            await update.message.reply_text("❌ Неверный формат ссылки.\n\nСсылка должна начинаться с:\nhttps://pay.oxapay.com/invoice/\nили\nhttps://oxapay.com/invoice/", reply_markup=get_back_inline())
            return
        context.user_data['invoice_link'] = link
        amount = context.user_data.get('temp_amount')
        op_type = context.user_data.get('operation_type')
        stats = await db.get_client_stats(user_id)
        deals = stats['total_deals'] if stats else 0
        _, _, discount, _ = get_rank_and_discount(deals)
        coin_data = context.user_data.get('selected_coin')
        rate = await coin_data['rate_func']() if coin_data and coin_data['code'] == "BTC" else await get_usdt_rate()
        coin_symbol = coin_data['symbol'] if coin_data else "🪙"
        coin_code = coin_data['code'] if coin_data else "USDT"
        client_total = calculate_client_total(amount, discount, False)
        crypto_amount = client_total / rate
        await update.message.reply_text(f"📝 <b>ПРОВЕРЬТЕ ДАННЫЕ</b>\n\n📋 Тип: {op_type}\n💰 Сумма: {amount:.0f} ₽\n{coin_symbol} Вы получите: {crypto_amount:.8f} {coin_code}\n💸 К ОПЛАТЕ: {client_total:.0f} ₽\n🔗 Ссылка: {html.escape(link)}\n📌 Курс: ≈{rate:.2f} ₽ за 1 {coin_code}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВНИМАНИЕ!</b>\n• После подтверждения вы обязуетесь оплатить счёт\n• <b>Неоплата влечёт БЛОКИРОВКУ АККАУНТА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n✅ <b>ПОДТВЕРДИТЕ, ЧТО ГОТОВЫ ОПЛАТИТЬ</b>", parse_mode="HTML", reply_markup=get_confirm_keyboard())
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
    await update.message.reply_text(f"🎁 <b>БЕСПЛАТНЫЕ СДЕЛКИ</b>\n\n• Доступно бесплатных сделок: {free_deals}\n• Завершённых рефералов: {referrals_count}\n• До следующей бесплатной сделки: {needed} рефералов\n\n🔥 Пригласи 3 друзей и получи 1 сделку без комиссии!\n\n<b>Как это работает:</b>\n1️⃣ Поделитесь своей реферальной ссылкой (/referral)\n2️⃣ Друг регистрируется и совершает обмен\n3️⃣ За каждых 3 друзей — 1 бесплатный обмен для вас!\n\n💡 При создании новой заявки бот сам предложит использовать бесплатную сделку.", parse_mode="HTML", reply_markup=get_main_keyboard())

@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔐 АДМИНИСТРАТОРСКАЯ ПАНЕЛЬ\n\nВыберите действие:", reply_markup=get_admin_keyboard())

@admin_only
async def take_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID заявки: /take <id>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PENDING:
        await update.message.reply_text("❌ Заявка не найдена или уже обработана.")
        return
    await db.take_request(req_id)
    await update.message.reply_text(f"✅ Заявка #{req_id} взята в работу!")
    progress_bar, _ = get_status_progress_bar(STATUS_PROCESSING)
    await safe_send(context, req['user_id'], f"🔄 <b>ЗАЯВКА #{req_id}</b>\n\n{progress_bar}\n\n👨‍💼 Оператор начал обработку вашей заявки.\n\nОжидайте реквизиты в ближайшее время.\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")

@admin_only
async def send_requisites_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /send <id> <реквизиты>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    requisites_text = " ".join(context.args[1:])
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PROCESSING:
        await update.message.reply_text("❌ Заявка не найдена или не в статусе обработки.")
        return
    await db.send_requisites(req_id, requisites_text)
    if "Биткоин" in req['operation_type'] or "BTC" in req['operation_type']:
        coin_symbol, coin_code, rate = "₿", "BTC", await get_btc_rate() or 5500000
    else:
        coin_symbol, coin_code, rate = "🪙", "USDT", await get_usdt_rate() or 92.5
    crypto_amount = req['client_total'] / rate
    formatted_reqs = format_requisites(requisites_text, req['client_total'])
    progress_bar, _ = get_status_progress_bar(STATUS_REQUISITES_SENT)
    await safe_send(context, req['user_id'], f"💳 <b>РЕКВИЗИТЫ ДЛЯ ЗАЯВКИ #{req_id}</b>\n\n{progress_bar}\n\n{formatted_reqs}\n\n📊 <b>ИНФОРМАЦИЯ О СДЕЛКЕ:</b>\n• Сумма к оплате: {req['client_total']:.0f} ₽\n• {coin_symbol} Вы получите: {crypto_amount:.8f} {coin_code}\n• Курс: ≈{rate:.2f} ₽ за 1 {coin_code}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>ВАЖНО!</b>\n• Оплатите ТОЧНО указанную сумму\n• После оплаты отправьте PDF-чек в этот чат\n• <b>Неоплата влечёт БЛОКИРОВКУ АККАУНТА</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")
    await update.message.reply_text(f"✅ Реквизиты отправлены клиенту по заявке #{req_id}")

@admin_only
async def confirm_payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID заявки: /confirm <id>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    req = await db.get_request(req_id)
    if not req or req['status'] != STATUS_PAID:
        await update.message.reply_text("❌ Заявка не найдена или не в статусе оплаты.")
        return
    await db.complete_request(req_id, req['user_id'], req['amount'])
    if "Биткоин" in req['operation_type'] or "BTC" in req['operation_type']:
        coin_symbol, coin_code, rate = "₿", "BTC", await get_btc_rate() or 5500000
    else:
        coin_symbol, coin_code, rate = "🪙", "USDT", await get_usdt_rate() or 92.5
    crypto_amount = req['client_total'] / rate
    progress_bar, _ = get_status_progress_bar(STATUS_COMPLETED)
    await safe_send(context, req['user_id'], f"✅ <b>ЗАЯВКА #{req_id} ЗАВЕРШЕНА!</b>\n\n{progress_bar}\n\n🎉 <b>СДЕЛКА УСПЕШНО ЗАВЕРШЕНА!</b>\n\n📊 <b>ДЕТАЛИ:</b>\n• Сумма: {req['amount']:.0f} ₽\n• Оплачено: {req['client_total']:.0f} ₽\n• {coin_symbol} Получено: {crypto_amount:.8f} {coin_code}\n• Курс: ≈{rate:.2f} ₽ за 1 {coin_code}\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⭐ <b>Пожалуйста, оцените наш сервис!</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML", reply_markup=get_rating_keyboard(req_id))
    await update.message.reply_text(f"✅ Заявка #{req_id} завершена!")

@admin_only
async def reject_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID заявки: /reject <id>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    success = await db.cancel_request(req_id, "admin")
    if success:
        req = await db.get_request(req_id)
        await update.message.reply_text(f"❌ Заявка #{req_id} отклонена!")
        if req:
            await safe_send(context, req['user_id'], f"❌ <b>ЗАЯВКА #{req_id} ОТКЛОНЕНА</b>\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Не удалось отклонить заявку.")

@admin_only
async def get_pdf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID заявки: /getpdf <id>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    req = await db.get_request(req_id)
    if not req or not req['pdf_file_id']:
        await update.message.reply_text("❌ Заявка не найдена или чек отсутствует.")
        return
    await update.message.reply_document(document=req['pdf_file_id'], caption=f"Чек по заявке #{req_id}")

@admin_only
async def get_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID заявки: /getlink <id>")
        return
    try:
        req_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    req = await db.get_request(req_id)
    if not req or not req['invoice_link']:
        await update.message.reply_text("❌ Заявка не найдена или ссылка отсутствует.")
        return
    await update.message.reply_text(f"🔗 Ссылка по заявке #{req_id}:\n{req['invoice_link']}")

@admin_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Использование: /ban <user_id или @username> <причина>")
        return
    identifier = context.args[0]
    reason = " ".join(context.args[1:])
    user_id = None
    if identifier.startswith("@"):
        user_id = await db.find_user_id_by_username(identifier[1:])
    else:
        try:
            user_id = int(identifier)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат.")
            return
    if not user_id:
        await update.message.reply_text(f"❌ Пользователь {identifier} не найден.")
        return
    await db.ban_user(user_id, reason)
    await update.message.reply_text(f"🚫 Пользователь {format_user(None, user_id)} заблокирован.\nПричина: {reason}")
    await safe_send(context, user_id, f"⛔ <b>ДОСТУП ЗАБЛОКИРОВАН</b>\n\nПричина: {reason}\n\n📞 Для апелляции: {SUPPORT_CONTACT}", parse_mode="HTML")

@admin_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите ID пользователя: /unban <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID.")
        return
    await db.unban_user(user_id)
    await update.message.reply_text(f"✅ Пользователь {format_user(None, user_id)} разблокирован.")
    await safe_send(context, user_id, "✅ <b>ДОСТУП ВОССТАНОВЛЕН</b>\n\nВы снова можете пользоваться ботом.\n\nНажмите /start, чтобы начать.", parse_mode="HTML")

@admin_only
async def edit_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📝 Отправьте новый текст правил:")
        context.user_data['editing_setting'] = 'rules'
        return
    await db.update_setting('rules', " ".join(context.args))
    await update.message.reply_text("✅ Правила обновлены!")

@admin_only
async def edit_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("📝 Отправьте новый график работы:")
        context.user_data['editing_setting'] = 'schedule'
        return
    await db.update_setting('schedule', " ".join(context.args))
    await update.message.reply_text("✅ График обновлен!")

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
        await update.message.reply_text(f"Текущий статус AFK: {current}\nИспользуйте: /afk on или /afk off")
        return
    mode = context.args[0].lower()
    if mode == 'on':
        await db.update_setting('afk_mode', '1')
        await update.message.reply_text("😴 Режим AFK включен. Новые заявки не принимаются.")
    elif mode == 'off':
        await db.update_setting('afk_mode', '0')
        await update.message.reply_text("✅ Режим AFK выключен. Приём заявок открыт.")
    else:
        await update.message.reply_text("❌ Используйте: /afk on или /afk off")

async def check_and_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        due_reminders = await db.get_due_reminders()
        for reminder in due_reminders:
            request_id = reminder['request_id']
            reminders_sent = reminder['reminders_sent']
            user_id = reminder['user_id']
            client_total = reminder['client_total']
            if reminders_sent == 0:
                message = f"⏰ <b>НАПОМИНАНИЕ ОБ ОПЛАТЕ</b>\n\n📋 Заявка #{request_id}\n💸 Сумма к оплате: {client_total:.0f} ₽\n\n⚠️ Пожалуйста, оплатите заявку и отправьте PDF-чек.\n⏳ У вас есть 15 минут до следующего предупреждения.\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}"
            else:
                message = f"🚨 <b>ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ!</b>\n\n📋 Заявка #{request_id}\n💸 Сумма к оплате: {client_total:.0f} ₽\n\n⚠️ Если оплата не поступит в ближайшее время, заявка будет отменена.\n⛔ Неоплата влечёт БЛОКИРОВКУ АККАУНТА!\n\n📞 <b>Вопросы:</b> {SUPPORT_CONTACT}"
            await safe_send(context, user_id, message, parse_mode="HTML")
            await db.update_reminder_sent(request_id)
            logging.info(f"Sent reminder {reminders_sent + 1}/2 for request #{request_id}")
            if reminders_sent == 1:
                await context.bot.send_message(ADMIN_ID, f"⚠️ Отправлено второе предупреждение по заявке #{request_id}\n👤 Пользователь: {user_id}\n💸 Сумма: {client_total:.0f} ₽")
    except Exception as e:
        logging.error(f"Error in check_and_send_reminders: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        if update and isinstance(update, Update) and update.effective_user:
            await safe_send(context, update.effective_user.id, "❌ Произошла внутренняя ошибка. Пожалуйста, попробуйте позже или обратитесь в поддержку.")
    except Exception:
        pass

# ================== MAIN ==========================
def main() -> None:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("skip", skip_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("free_deal", free_deal_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("take", take_request_command))
    application.add_handler(CommandHandler("send", send_requisites_command))
    application.add_handler(CommandHandler("confirm", confirm_payment_command))
    application.add_handler(CommandHandler("reject", reject_request_command))
    application.add_handler(CommandHandler("getpdf", get_pdf_command))
    application.add_handler(CommandHandler("getlink", get_link_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("edit_rules", edit_rules_command))
    application.add_handler(CommandHandler("edit_schedule", edit_schedule_command))
    application.add_handler(CommandHandler("edit_links", edit_links_command))
    application.add_handler(CommandHandler("afk", afk_command))

    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_message))

    # Напоминания
    application.job_queue.run_repeating(check_and_send_reminders, interval=60, first=10)

    application.add_error_handler(error_handler)

    logging.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    main()