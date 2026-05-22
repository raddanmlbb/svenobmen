import re
import logging
import sqlite3
import aiohttp
import json
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, filters, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

# ==================================================
# ================== НАСТРОЙКИ ====================
# ==================================================

BOT_TOKEN = "8709537229:AAHOW9CE7g4MYc3w5n-K4yRf09fVxS81zrA"
ADMIN_ID = 5243173039  # Замените на свой Telegram ID

COMMISSION_RULES = [(0, 5000, 5), (5000, float('inf'), 3)]
MIN_COMMISSION_RUB = 150
MIN_AMOUNT_RUB = 1000

CACHE_TIME_SECONDS = 3600

# Режим AFK (не работаю)
afk_mode = False

# ==================================================
# ================== БАЗА ДАННЫХ ===================
# ==================================================

class Database:
    def __init__(self, db_file="clients.db"):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                total_deals INTEGER DEFAULT 0,
                total_volume_rub REAL DEFAULT 0,
                total_volume_usdt REAL DEFAULT 0,
                last_deal_date TEXT
            )
        """)
        self.conn.commit()

    def add_client(self, user_id, username):
        self.cursor.execute(
            "INSERT OR IGNORE INTO clients (user_id, username) VALUES (?, ?)",
            (user_id, username)
        )
        self.conn.commit()

    def complete_deal(self, user_id, amount_rub, amount_usdt, username):
        now = datetime.now().isoformat()
        self.cursor.execute("""
            UPDATE clients
            SET username = ?,
                total_deals = total_deals + 1,
                total_volume_rub = total_volume_rub + ?,
                total_volume_usdt = total_volume_usdt + ?,
                last_deal_date = ?
            WHERE user_id = ?
        """, (username, amount_rub, amount_usdt, now, user_id))
        self.conn.commit()

    def get_stats(self, user_id):
        self.cursor.execute(
            "SELECT total_deals, total_volume_rub, total_volume_usdt, last_deal_date "
            "FROM clients WHERE user_id = ?", (user_id,)
        )
        return self.cursor.fetchone()

    def get_all_clients(self):
        self.cursor.execute(
            "SELECT user_id, username, total_deals, total_volume_rub "
            "FROM clients WHERE total_deals > 0 ORDER BY total_deals DESC LIMIT 20"
        )
        return self.cursor.fetchall()

    def find_user_by_username(self, username):
        self.cursor.execute(
            "SELECT user_id, username FROM clients WHERE username = ?", (username,)
        )
        return self.cursor.fetchone()


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
    except Exception as e:
        logging.error(f"Ошибка получения курса: {e}")

    return 92.5


# ==================================================
# ================== ЛОГИКА РАСЧЁТА ================
# ==================================================

def calculate(amount: float, rate: float):
    if amount < 5000:
        commission = amount * 5 / 100
    else:
        commission = amount * 3 / 100

    if commission < MIN_COMMISSION_RUB:
        commission = MIN_COMMISSION_RUB

    total = amount + commission
    usdt = total / rate
    return total, usdt


# ==================================================
# ================== КНОПКИ ========================
# ==================================================

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📜 Правила", callback_data="rules"),
         InlineKeyboardButton("⏰ График", callback_data="schedule")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
         InlineKeyboardButton("🔗 Ссылки", callback_data="links")],
    ])

def get_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="back")]
    ])


# ==================================================
# ============= ОСНОВНОЙ ОБРАБОТЧИК ================
# ==================================================

async def auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    user_id = update.effective_user.id
    username = update.effective_user.username or str(user_id)
    
    if user_id == ADMIN_ID:
        return

    text = update.message.text.strip()
    match = re.search(r"(\d+)", text)
    last_msg_id = context.user_data.get(f'last_msg_{user_id}')

    # ===== НЕТ ЧИСЛА =====
    if not match:
        reply_text = (
            "👋 Приветствую!\n\n"
            "Напишите сумму в рублях, на которую вы хотите совершить платёж/обмен.\n"
            "Пример: 5000"
        )

        if last_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=last_msg_id,
                    text=reply_text,
                    reply_markup=get_main_keyboard()
                )
                return
            except:
                pass

        msg = await update.message.reply_text(reply_text, reply_markup=get_main_keyboard())
        context.user_data[f'last_msg_{user_id}'] = msg.message_id
        return

    # ===== ЕСТЬ ЧИСЛО =====
    amount = float(match.group(1))
    
    # Уведомление админу (всегда)
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"💰 **Запрос на обмен**\n\n"
            f"👤 @{username}\n"
            f"🆔 ID: {user_id}\n"
            f"💬 Сумма: {amount:.0f} ₽"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление: {e}")

    # Расчёт суммы
    if amount < MIN_AMOUNT_RUB:
        reply_text = f"❌ Минимальная сумма: {MIN_AMOUNT_RUB} ₽\nВы отправили: {amount:.0f} ₽"
        keyboard = get_main_keyboard()
    else:
        rate = await get_usdt_rate()
        total, usdt = calculate(amount, rate)

        reply_text = (
            f"💰 {amount:.0f} ₽ → {usdt:.2f} USDT\n"
            f"💸 Сумма к оплате: {total:.0f} ₽\n\n"
            f"📌 Курс: {rate:.1f} ₽ за 1 USDT"
        )
        keyboard = get_main_keyboard()

        # Сохраняем данные для сделки
        context.user_data[f'deal_{user_id}'] = {
            'amount_rub': total,
            'amount_usdt': usdt,
            'username': username
        }
        db.add_client(user_id, username)
        
        # Если режим AFK включён, добавляем предупреждение
        if afk_mode:
            reply_text += (
                f"\n\n⚠️ **Оператор сейчас не работает.**\n"
                f"Ваша заявка принята. Ожидайте ответа позже."
            )

    # Отправка или редактирование сообщения
    if last_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=last_msg_id,
                text=reply_text,
                reply_markup=keyboard
            )
            return
        except:
            pass

    msg = await update.message.reply_text(reply_text, reply_markup=keyboard)
    context.user_data[f'last_msg_{user_id}'] = msg.message_id


# ==================================================
# ============= ОБРАБОТКА КНОПОК ===================
# ==================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id
    temp_id = context.user_data.get(f'temp_msg_{user_id}')

    if data == "rules":
        text = (
            "📜 **ПРАВИЛА ОБМЕНА**\n\n"
            "• Минимальная сумма: 1000 ₽\n"
            "• Комиссия: до 5000₽ — 5%, от 5000₽ — 3%\n"
            "• Минимальная комиссия: 150 ₽"
        )
        if temp_id:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
            context.user_data[f'temp_msg_{user_id}'] = msg.message_id

    elif data == "schedule":
        text = (
            "⏰ **ГРАФИК РАБОТЫ**\n\n"
            "• Пн–Пт: 10:00 – 22:00\n"
            "• Сб: 11:00 – 20:00\n"
            "• Вс: выходной\n\n"
            "Заявки вне рабочего времени — на следующий день."
        )
        if temp_id:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
            context.user_data[f'temp_msg_{user_id}'] = msg.message_id

    elif data == "profile":
        stats = db.get_stats(user_id)
        if stats and stats[0] > 0:
            text = (
                f"👤 **ВАШ ПРОФИЛЬ**\n\n"
                f"• Сделок: {stats[0]}\n"
                f"• Объём (₽): {stats[1]:.0f}\n"
                f"• Объём (USDT): {stats[2]:.2f}\n"
                f"• Последняя сделка: {stats[3][:10] if stats[3] else 'нет'}"
            )
        else:
            text = "👤 У вас пока нет завершённых сделок."

        if temp_id:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
        else:
            msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard())
            context.user_data[f'temp_msg_{user_id}'] = msg.message_id

    elif data == "links":
        text = (
            "🔗 **ПОЛЕЗНЫЕ ССЫЛКИ**\n\n"
            "• 📢 Канал с отзывами: https://t.me/ваш_канал\n"
            "• 📊 Актуальный курс: https://www.bestchange.ru\n"
            "• 💬 Чат клиентов: https://t.me/ваш_чат"
        )
        if temp_id:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard(), disable_web_page_preview=True)
        else:
            msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=get_back_keyboard(), disable_web_page_preview=True)
            context.user_data[f'temp_msg_{user_id}'] = msg.message_id

    elif data == "back":
        if temp_id:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=temp_id)
            except:
                pass
            context.user_data[f'temp_msg_{user_id}'] = None


# ==================================================
# ================== КОМАНДЫ =======================
# ==================================================

async def afk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global afk_mode
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    
    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: /afk on  или  /afk off")
        return
    
    if args[0].lower() == "on":
        afk_mode = True
        await update.message.reply_text("✅ Режим «Не работаю» ВКЛЮЧЁН. Бот будет добавлять предупреждение к расчётам.")
    elif args[0].lower() == "off":
        afk_mode = False
        await update.message.reply_text("✅ Режим «Не работаю» ВЫКЛЮЧЁН. Бот работает в обычном режиме.")
    else:
        await update.message.reply_text("❌ Используйте /afk on  или  /afk off")


async def complete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ Использование: /complete @username сумма_usdt")
        return

    username = args[0].replace("@", "")
    try:
        amount_usdt = float(args[1])
    except ValueError:
        await update.message.reply_text("❌ Сумма USDT должна быть числом")
        return

    user_data = db.find_user_by_username(username)
    if not user_data:
        await update.message.reply_text(f"❌ Клиент @{username} не найден")
        return

    user_id = user_data[0]

    deal = context.user_data.get(f'deal_{user_id}')
    if not deal:
        await update.message.reply_text(f"❌ Нет данных о последней сделке для @{username}")
        return

    db.complete_deal(user_id, deal['amount_rub'], amount_usdt, username)

    await update.message.reply_text(f"✅ Сделка для @{username} подтверждена")

    try:
        await context.bot.send_message(
            user_id,
            f"✅ Сделка завершена!\n"
            f"Сумма: {deal['amount_rub']:.0f} ₽ → {amount_usdt:.2f} USDT"
        )
    except Exception:
        pass

    context.user_data[f'deal_{user_id}'] = None


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id == ADMIN_ID and context.args:
        username = context.args[0].replace("@", "")
        user_data = db.find_user_by_username(username)
        if user_data:
            stats = db.get_stats(user_data[0])
            if stats and stats[0] > 0:
                await update.message.reply_text(
                    f"📊 @{username}:\n"
                    f"Сделок: {stats[0]}\n"
                    f"Объём: {stats[1]:.0f} ₽ / {stats[2]:.2f} USDT"
                )
            else:
                await update.message.reply_text(f"❌ У @{username} нет сделок")
        else:
            await update.message.reply_text(f"❌ Клиент @{username} не найден")
        return

    if user_id == ADMIN_ID:
        clients = db.get_all_clients()
        if clients:
            msg = "📊 ТОП КЛИЕНТОВ:\n"
            for c in clients:
                msg += f"• @{c[1] or c[0]} | сделок: {c[2]} | объём: {c[3]:.0f} ₽\n"
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("📊 Нет данных")
        return

    stats = db.get_stats(user_id)
    if stats and stats[0] > 0:
        await update.message.reply_text(
            f"📊 ВАША СТАТИСТИКА:\n"
            f"Сделок: {stats[0]}\n"
            f"Объём: {stats[1]:.0f} ₽ / {stats[2]:.2f} USDT"
        )
    else:
        await update.message.reply_text("📊 У вас пока нет сделок")


# ==================================================
# ==================== ЗАПУСК ======================
# ==================================================

def main():
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reply))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(CommandHandler("afk", afk_command))
    app.add_handler(CommandHandler("complete", complete_command))
    app.add_handler(CommandHandler("stats", stats_command))

    print("✅ Бот запущен. Режим: автоответчик в личных чатах.")
    app.run_polling()


if __name__ == "__main__":
    main()
