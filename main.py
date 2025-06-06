import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from datetime import datetime, timedelta
from calendar import monthrange
from telegram.ext import JobQueue
import sqlite3
import uuid
import requests
import json
import time
import sys
import os
import asyncio
from telegram.ext import Application, JobQueue
from functools import wraps
from dotenv import load_dotenv

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# Загрузка конфигурации
load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_BOT_TOKEN = os.getenv('CRYPTO_BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api"

# Проверка обязательных переменных
if not all([TELEGRAM_TOKEN, CRYPTO_BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("Необходимые переменные окружения не установлены!")

# Интервалы
PAYMENT_CHECK_INTERVAL = 300  # 5 минут
KEEP_ALIVE_INTERVAL = 300    # 5 минут
RESTART_DELAY = 10           # 10 секунд при ошибке

# Состояния для ConversationHandler
GET_CHANNEL, GET_DATE, GET_TIME, GET_DURATION, CONFIRM_ORDER, ADMIN_BALANCE_CHANGE = range(6)
def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    tables = [
        '''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 0,
            registration_date TEXT
        )''',
        '''CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            platform TEXT,
            service TEXT,
            channel TEXT,
            stream_date TEXT,
            start_time TEXT,
            duration TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            payment_method TEXT,
            order_date TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )''',
        '''CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )''',
        '''CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            currency TEXT DEFAULT 'RUB',
            status TEXT DEFAULT 'created',
            created_at TEXT,
            paid_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )'''
    ]
    
    for table in tables:
        cursor.execute(table)
    
    # Добавляем администраторов
    for admin_id in ADMIN_IDS:
        cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (admin_id,))
    
    conn.commit()
    conn.close()
    
def restart_bot():
    """Перезапускает бота"""
    logging.info("Перезапуск бота...")
    python = sys.executable
    os.execl(python, python, *sys.argv)

async def keep_alive(context: ContextTypes.DEFAULT_TYPE):
    """Функция для поддержания активности бота"""
    try:
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        logging.info("Бот активен, проверка БД успешна")
    except Exception as e:
        logging.error(f"Ошибка проверки активности: {e}")
        restart_bot()

def catch_errors(func):
    """Декоратор для перехвата ошибок"""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            logging.error(f"Ошибка в функции {func.__name__}: {e}")
            if update and update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Произошла ошибка. Пожалуйста, попробуйте позже."
                )
            raise
    return wrapped
@catch_errors
async def create_crypto_invoice(user_id: int, amount: float):
    headers = {
        'Crypto-Pay-API-Token': CRYPTO_BOT_TOKEN,
        'Content-Type': 'application/json'
    }
    
    payload = {
        "amount": amount,
        "asset": "USDT",
        "description": f"Пополнение баланса для пользователя {user_id}",
        "paid_btn_name": "viewItem",
        "paid_btn_url": f"https://t.me/your_bot",
        "payload": str(user_id),
        "allow_comments": False,
        "allow_anonymous": False
    }
    
    response = requests.post(
        f"{CRYPTO_BOT_API_URL}/createInvoice",
        headers=headers,
        data=json.dumps(payload),
        timeout=10
    )
    response.raise_for_status()
    return response.json().get('result')

@catch_errors
async def check_crypto_payment(invoice_id: str):
    headers = {
        'Crypto-Pay-API-Token': CRYPTO_BOT_TOKEN
    }
    
    response = requests.get(
        f"{CRYPTO_BOT_API_URL}/invoices/{invoice_id}",
        headers=headers,
        timeout=10
    )
    response.raise_for_status()
    return response.json().get('result')

async def check_pending_payments(context: ContextTypes.DEFAULT_TYPE):
    """Проверяет неоплаченные счета"""
    try:
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT invoice_id, user_id, amount FROM payments WHERE status = 'created'")
        payments = cursor.fetchall()
        
        for invoice_id, user_id, amount in payments:
            payment = await check_crypto_payment(invoice_id)
            if payment and payment['status'] == 'paid':
                # Обновляем статус платежа
                cursor.execute(
                    "UPDATE payments SET status = 'paid', paid_at = ? WHERE invoice_id = ?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), invoice_id)
                )
                # Пополняем баланс
                cursor.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (amount, user_id))
                
                # Уведомляем пользователя
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Ваш платеж на {amount} RUB подтвержден! Баланс пополнен."
                )
                
                conn.commit()
                logging.info(f"Подтвержден платеж {invoice_id} для пользователя {user_id}")
        
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка при проверке платежей: {e}")
@catch_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, username, registration_date) VALUES (?, ?, ?)",
            (user.id, user.username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("Мой профиль", callback_data='profile')],
        [InlineKeyboardButton("Помощь", callback_data='help')],
        [InlineKeyboardButton("Сделать заказ", callback_data='make_order')],
    ]
    
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("Админ раздел", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! Я бот для заказа услуг для стримов. Выберите действие:",
        reply_markup=reply_markup
    )

def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result

@catch_errors
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == 'profile':
        await show_profile(update, context)
    elif data == 'help':
        await show_help(update, context)
    elif data == 'make_order':
        await choose_platform(update, context)
    elif data == 'admin_panel':
        await admin_panel(update, context)
    elif data == 'back_to_menu':
        await start(update, context)
    elif data.startswith('platform_'):
        context.user_data['platform'] = data.split('_')[1]
        await choose_service(update, context)
    elif data.startswith('service_'):
        context.user_data['service'] = data.split('_')[1]
        return await ask_channel(update, context)
    elif data.startswith('calendar_'):
        return await handle_calendar(update, context, data)
    elif data == 'confirm_order':
        await confirm_order(update, context)
    elif data == 'pay_crypto':
        await process_crypto_payment(update, context)
    elif data == 'pay_card':
        await process_card_payment(update, context)
    elif data == 'topup_balance':
        await topup_balance(update, context)
    elif data.startswith('admin_'):
        await handle_admin_actions(update, context, data)
@catch_errors
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    balance = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,))
    orders_count = cursor.fetchone()[0]
    
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("Пополнить баланс", callback_data='topup_balance')],
        [InlineKeyboardButton("Назад", callback_data='back_to_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"📊 Ваш профиль:\n\n"
             f"💰 Баланс: {balance} руб\n"
             f"🛒 Всего заказов: {orders_count}\n\n"
             f"Выберите действие:",
        reply_markup=reply_markup
    )

@catch_errors
async def topup_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("Криптовалюта (CryptoBot)", callback_data='pay_crypto')],
        [InlineKeyboardButton("Банковская карта", callback_data='pay_card')],
        [InlineKeyboardButton("Назад", callback_data='profile')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="💰 Пополнение баланса\n\n"
             "Выберите способ оплаты:",
        reply_markup=reply_markup
    )

@catch_errors
async def process_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    amount = 1000  # Можно сделать выбор суммы
    
    invoice = await create_crypto_invoice(user_id, amount)
    if not invoice:
        await query.edit_message_text("Ошибка при создании счета. Попробуйте позже.")
        return
    
    # Сохраняем платеж в базу
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO payments (invoice_id, user_id, amount, created_at) VALUES (?, ?, ?, ?)",
        (invoice['invoice_id'], user_id, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()
    
    keyboard = [
        [InlineKeyboardButton("Проверить оплату", callback_data=f'check_payment_{invoice["invoice_id"]}')],
        [InlineKeyboardButton("Назад", callback_data='profile')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"💳 Счет для оплаты создан\n\n"
             f"Сумма: {amount} RUB\n"
             f"Адрес для оплаты: {invoice['pay_url']}\n\n"
             f"После оплаты нажмите кнопку 'Проверить оплату'.",
        reply_markup=reply_markup
    )
@catch_errors
async def process_card_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    await query.edit_message_text(
        text="Для оплаты банковской картой, пожалуйста, свяжитесь с менеджером @manager_username\n\n"
             "Укажите сумму пополнения и ваш ID: " + str(query.from_user.id)
    )

@catch_errors
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    keyboard = [[InlineKeyboardButton("Назад", callback_data='back_to_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="📞 Помощь\n\n"
             "Если у вас возникли вопросы или проблемы, свяжитесь с нашим менеджером: @manager_username\n\n"
             "Мы работаем круглосуточно.",
        reply_markup=reply_markup
    )

@catch_errors
async def choose_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("🟣 Twitch", callback_data='platform_twitch')],
        [InlineKeyboardButton("🟢 Kick", callback_data='platform_kick')],
        [InlineKeyboardButton("🔴 YouTube", callback_data='platform_youtube')],
        [InlineKeyboardButton("Назад", callback_data='back_to_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="🗂 Пожалуйста, выберите платформу:",
        reply_markup=reply_markup
    )
@catch_errors
async def choose_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    platform = context.user_data['platform']
    
    prices = {
        'twitch': {'chat_ru': 250, 'chat_eng': 400, 'viewers': 1, 'followers': 1},
        'kick': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1},
        'youtube': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1}
    }
    
    keyboard = [
        [InlineKeyboardButton(f"💬 Живой чат (RU) - {prices[platform]['chat_ru']} руб/час", callback_data='service_chat_ru')],
        [InlineKeyboardButton(f"💬 Живой чат (ENG) - {prices[platform]['chat_eng']} руб/час", callback_data='service_chat_eng')],
        [InlineKeyboardButton(f"👀 Зрители - {prices[platform]['viewers']} руб/час", callback_data='service_viewers')],
        [InlineKeyboardButton(f"👥 Фолловеры - {prices[platform]['followers']} руб/час", callback_data='service_followers')],
        [InlineKeyboardButton("Назад", callback_data='make_order')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    platform_name = {
        'twitch': '🟣 TWITCH',
        'kick': '🔴 KICK',
        'youtube': '📺 YOUTUBE'
    }.get(platform, platform.upper())
    
    await query.edit_message_text(
        text=f"Платформа: {platform_name}\n\n"
             "🗂 Пожалуйста, выберите нужную услугу:",
        reply_markup=reply_markup
    )

@catch_errors
async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['service'] = query.data.split('_')[1]
    
    await query.edit_message_text(
        text="🗯️ Отправьте ссылку или юзернейм Вашего канала:"
    )
    
    return GET_CHANNEL

@catch_errors
async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = update.message.text
    context.user_data['channel'] = channel
    
    await show_calendar(update, context)
    
    return GET_DATE
@catch_errors
async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, month=None, year=None):
    now = datetime.now()
    if not month:
        month = now.month
    if not year:
        year = now.year
    
    month_name = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 
                 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'][month-1]
    
    num_days = monthrange(year, month)[1]
    first_day = monthrange(year, month)[0]
    
    keyboard = []
    
    header = [
        InlineKeyboardButton("<", callback_data=f'calendar_{year}_{month-1}'),
        InlineKeyboardButton(f"{month_name} {year}", callback_data='ignore'),
        InlineKeyboardButton(">", callback_data=f'calendar_{year}_{month+1}')
    ]
    keyboard.append(header)
    
    week_days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    keyboard.append([InlineKeyboardButton(day, callback_data='ignore') for day in week_days])
    
    day_buttons = []
    day_buttons.extend([InlineKeyboardButton(" ", callback_data='ignore') for _ in range(first_day)])
    
    for day in range(1, num_days + 1):
        date = datetime(year, month, day)
        if date.date() < now.date():
            day_buttons.append(InlineKeyboardButton(" ", callback_data='ignore'))
        else:
            day_buttons.append(InlineKeyboardButton(str(day), callback_data=f'calendar_select_{year}-{month}-{day}'))
        
        if len(day_buttons) % 7 == 0:
            keyboard.append(day_buttons)
            day_buttons = []
    
    if day_buttons:
        keyboard.append(day_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text="📅 Выберите дату стрима:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text="📅 Выберите дату стрима:",
            reply_markup=reply_markup
        )
@catch_errors
async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    
    if data.startswith('calendar_select_'):
        selected_date = data.split('_')[2]
        context.user_data['stream_date'] = selected_date
        
        await query.edit_message_text(
            text=f"1. Платформа: {get_platform_emoji(context.user_data['platform'])} {context.user_data['platform'].upper()}\n"
                 f"2. Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"3. Канал: {context.user_data['channel']}\n"
                 f"4. Дата стрима: {selected_date.split('-')[2]}.{selected_date.split('-')[1]}\n\n"
                 "🕔 Введите время начала стрима, в формате: 12:00"
        )
        
        return GET_TIME
    else:
        parts = data.split('_')
        year = int(parts[1])
        month = int(parts[2])
        
        if month == 0:
            month = 12
            year -= 1
        elif month == 13:
            month = 1
            year += 1
        
        await show_calendar(update, context, month, year)

def get_platform_emoji(platform):
    return {
        'twitch': '🟣',
        'kick': '🔴',
        'youtube': '📺'
    }.get(platform, '')

def get_service_name(service):
    prices = {
        'chat_ru': 'Живой чат (RU) - 250 руб/час',
        'chat_eng': 'Живой чат (ENG) - 400 руб/час',
        'viewers': 'Зрители - 1 руб/час',
        'followers': 'Фолловеры - 1 руб/час'
    }
    return prices.get(service, service)
@catch_errors
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    
    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data['start_time'] = time_str
        
        await update.message.reply_text(
            text=f"1. Платформа: {get_platform_emoji(context.user_data['platform'])} {context.user_data['platform'].upper()}\n"
                 f"2. Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"3. Канал: {context.user_data['channel']}\n"
                 f"4. Дата стрима: {context.user_data['stream_date'].split('-')[2]}.{context.user_data['stream_date'].split('-')[1]}\n"
                 f"5. Время начала: {time_str}\n\n"
                 "⏳ Введите продолжительность стрима в формате: 1:00"
        )
        
        return GET_DURATION
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите время в правильном формате (HH:MM):")

@catch_errors
async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    duration_str = update.message.text
    
    try:
        datetime.strptime(duration_str, "%H:%M")
        context.user_data['duration'] = duration_str
        
        price_per_hour = get_price(context.user_data['platform'], context.user_data['service'])
        hours = int(duration_str.split(':')[0]) + int(duration_str.split(':')[1]) / 60
        amount = round(price_per_hour * hours, 2)
        context.user_data['amount'] = amount
        
        keyboard = [
            [InlineKeyboardButton("Подтвердить заказ", callback_data='confirm_order')],
            [InlineKeyboardButton("Отмена", callback_data='back_to_menu')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            text=f"📝 Подтвердите заказ:\n\n"
                 f"Платформа: {get_platform_emoji(context.user_data['platform'])} {context.user_data['platform'].upper()}\n"
                 f"Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"Канал: {context.user_data['channel']}\n"
                 f"Дата стрима: {context.user_data['stream_date'].split('-')[2]}.{context.user_data['stream_date'].split('-')[1]}\n"
                 f"Время начала: {context.user_data['start_time']}\n"
                 f"Длительность: {duration_str}\n\n"
                 f"💰 Итого к оплате: {amount} руб",
            reply_markup=reply_markup
        )
        
        return CONFIRM_ORDER
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите длительность в правильном формате (H:MM):")

def get_price(platform, service):
    prices = {
        'twitch': {'chat_ru': 250, 'chat_eng': 400, 'viewers': 1, 'followers': 1},
        'kick': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1},
        'youtube': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1}
    }
    return prices.get(platform, {}).get(service, 0)
@catch_errors
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    keyboard = [
        [InlineKeyboardButton("Оплатить криптовалютой (CryptoBot)", callback_data='pay_crypto')],
        [InlineKeyboardButton("Оплатить картой", callback_data='pay_card')],
        [InlineKeyboardButton("Отмена", callback_data='back_to_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="💰 Выберите способ оплаты:",
        reply_markup=reply_markup
    )

@catch_errors
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not is_admin(query.from_user.id):
        await query.answer("У вас нет прав доступа к этой функции!")
        return
    
    keyboard = [
        [InlineKeyboardButton("Статистика пользователей", callback_data='admin_stats')],
        [InlineKeyboardButton("Просмотр заказов", callback_data='admin_orders')],
        [InlineKeyboardButton("Изменить баланс пользователя", callback_data='admin_balance')],
        [InlineKeyboardButton("Назад", callback_data='back_to_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text="⚙️ Админ панель\n\nВыберите действие:",
        reply_markup=reply_markup
    )

@catch_errors
async def handle_admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    if data == 'admin_stats':
        await show_admin_stats(update, context)
    elif data == 'admin_orders':
        await show_admin_orders(update, context)
    elif data == 'admin_balance':
        await ask_user_for_balance_change(update, context)
@catch_errors
async def show_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT COUNT(*) FROM users WHERE date(registration_date) = ?", (today,))
    new_users_today = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE date(order_date) = ?", (today,))
    orders_today = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(amount) FROM orders WHERE status = 'completed'")
    total_sales = cursor.fetchone()[0] or 0
    
    conn.close()
    
    keyboard = [[InlineKeyboardButton("Назад", callback_data='admin_panel')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"📊 Статистика:\n\n"
             f"👥 Всего пользователей: {total_users}\n"
             f"🆕 Новых сегодня: {new_users_today}\n\n"
             f"🛒 Всего заказов: {total_orders}\n"
             f"📦 Заказов сегодня: {orders_today}\n\n"
             f"💰 Общий объем продаж: {total_sales} руб",
        reply_markup=reply_markup
    )

@catch_errors
async def show_admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT o.order_id, u.username, o.platform, o.service, o.amount, o.status 
        FROM orders o
        JOIN users u ON o.user_id = u.user_id
        ORDER BY o.order_date DESC
        LIMIT 10
    """)
    
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        text = "Нет заказов для отображения."
    else:
        text = "📦 Последние заказы:\n\n"
        for order in orders:
            order_id, username, platform, service, amount, status = order
            status_emoji = "✅" if status == "completed" else "🕒" if status == "pending" else "❌"
            text += (f"{status_emoji} Заказ #{order_id}\n"
                    f"👤 {username}\n"
                    f"🛒 {platform.upper()} - {service}\n"
                    f"💰 {amount} руб\n\n")
    
    keyboard = [[InlineKeyboardButton("Назад", callback_data='admin_panel')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup
    )
@catch_errors
async def ask_user_for_balance_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    await query.edit_message_text(
        text="Введите ID пользователя и сумму изменения (например: 123456789 +500):"
    )
    
    return ADMIN_BALANCE_CHANGE

@catch_errors
async def admin_balance_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    try:
        parts = text.split()
        user_id = int(parts[0])
        amount_change = float(parts[1])
        
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        
        if not result:
            await update.message.reply_text("Пользователь не найден!")
            return
        
        current_balance = result[0]
        new_balance = current_balance + amount_change
        
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"Баланс пользователя {user_id} изменен:\n"
            f"Старый баланс: {current_balance} руб\n"
            f"Изменение: {'+' if amount_change >= 0 else ''}{amount_change} руб\n"
            f"Новый баланс: {new_balance} руб"
        )
        
        return ConversationHandler.END
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите ID пользователя и сумму изменения (например: 123456789 +500):")
async def main():
    """Основная асинхронная функция запуска бота"""
    init_db()
    
    # Инициализация Application с JobQueue
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .job_queue(JobQueue())
        .build()
    )
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        GET_CHANNEL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)
        ],
        GET_DATE: [
            CallbackQueryHandler(handle_calendar, pattern="^calendar_")
        ],
        GET_TIME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)
        ],
        GET_DURATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)
        ],
        CONFIRM_ORDER: [
            CallbackQueryHandler(confirm_order, pattern="^confirm_order$")
        ],
        ADMIN_BALANCE_CHANGE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_balance_change)
        ]
    },
    fallbacks=[CommandHandler("start", start)],
    per_message=False,  # Явно указываем эту настройку
    per_chat=True,
    per_user=True
)
application.add_handler(conv_handler)
    
    # Регистрация обработчика кнопок
application.add_handler(CallbackQueryHandler(button))
    
    # Настройка периодических задач
    application.job_queue.run_repeating(
        check_pending_payments,
        interval=PAYMENT_CHECK_INTERVAL,
        first=10
    )
    application.job_queue.run_repeating(
        keep_alive,
        interval=KEEP_ALIVE_INTERVAL,
        first=10
    )
    
    # Запуск бота
    await application.run_polling()

def run_bot():
    """Функция-обертка для запуска асинхронного кода"""
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            logging.critical(f"Критическая ошибка: {e}")
            logging.info(f"Повторная попытка через {RESTART_DELAY} секунд...")
            time.sleep(RESTART_DELAY)

if __name__ == '__main__':
    run_bot()
