import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
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
import os
from dotenv import load_dotenv
from PIL import Image
import io

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
EXCHANGE_RATE_URL = "https://api.exchangerate-api.com/v4/latest/USD"

# Проверка обязательных переменных
if not all([TELEGRAM_TOKEN, CRYPTO_BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("Необходимые переменные окружения не установлены!")

# Интервалы
PAYMENT_CHECK_INTERVAL = 300  # 5 минут
KEEP_ALIVE_INTERVAL = 300    # 5 минут

# Состояния для ConversationHandler
GET_CHANNEL, GET_DATE, GET_TIME, GET_DURATION, CONFIRM_ORDER, ADMIN_BALANCE_CHANGE, GET_AMOUNT = range(7)

# Курс USDT к рублю (будет обновляться)
usdt_rate = 80.0  # начальное значение

def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    tables = [
        '''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            balance REAL DEFAULT 0,
            registration_date TEXT,
            last_activity TEXT
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

async def get_usdt_rate():
    """Получает текущий курс USDT к рублю"""
    global usdt_rate
    try:
        response = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB")
        data = response.json()
        usdt_rate = float(data['price'])
        logger.info(f"Обновлен курс USDT: {usdt_rate} RUB")
    except Exception as e:
        logger.error(f"Ошибка при получении курса USDT: {e}")
        # Используем предыдущее значение, если не удалось обновить

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
async def create_crypto_invoice(user_id: int, amount_rub: float):
    """Создает счет в криптовалюте по сумме в рублях"""
    await get_usdt_rate()  # Обновляем курс перед созданием счета
    amount_usdt = round(amount_rub / usdt_rate, 2)
    
    headers = {
        'Crypto-Pay-API-Token': CRYPTO_BOT_TOKEN,
        'Content-Type': 'application/json'
    }
    
    payload = {
        "amount": amount_usdt,
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, registration_date, last_activity) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.username, user.first_name, user.last_name, 
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
    else:
        cursor.execute(
            "UPDATE users SET last_activity = ? WHERE user_id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user.id)
        )
    
    conn.commit()
    conn.close()
    
    # Отправляем приветственное фото
    with open('welcome.jpg', 'rb') as photo:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=photo,
            caption=f"🌟 Добро пожаловать, {user.first_name}!\n\n"
                    "Я бот для заказа услуг для стримов. Выберите действие:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Мой профиль", "Помощь"],
                    ["Сделать заказ", "Пополнить баланс"]
                ],
                resize_keyboard=True
            )
        )

@catch_errors
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT balance, registration_date FROM users WHERE user_id = ?", (user.id,))
    balance, reg_date = cursor.fetchone()
    
    cursor.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user.id,))
    orders_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(amount) FROM orders WHERE user_id = ? AND status = 'completed'", (user.id,))
    total_spent = cursor.fetchone()[0] or 0
    
    conn.close()
    
    await update.message.reply_text(
        text=f"📊 Ваш профиль:\n\n"
             f"👤 Имя: {user.first_name or ''} {user.last_name or ''}\n"
             f"🆔 ID: {user.id}\n"
             f"📅 Дата регистрации: {reg_date}\n\n"
             f"💰 Баланс: {balance:.2f} руб\n"
             f"🛒 Всего заказов: {orders_count}\n"
             f"💸 Всего потрачено: {total_spent:.2f} руб",
        reply_markup=ReplyKeyboardMarkup(
            [
                ["Пополнить баланс"],
                ["Назад в меню"]
            ],
            resize_keyboard=True
        )
    )

@catch_errors
async def topup_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        text="💰 Введите сумму пополнения в рублях:",
        reply_markup=ReplyKeyboardMarkup(
            [["500", "1000", "2000"], ["Назад в меню"]],
            resize_keyboard=True
        )
    )
    return GET_AMOUNT

@catch_errors
async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
        if amount < 100:
            await update.message.reply_text("Минимальная сумма пополнения - 100 руб. Введите сумму еще раз:")
            return GET_AMOUNT
        
        context.user_data['topup_amount'] = amount
        
        await get_usdt_rate()  # Обновляем курс
        amount_usdt = round(amount / usdt_rate, 2)
        
        keyboard = [
            [InlineKeyboardButton(f"Оплатить {amount} руб (~{amount_usdt} USDT)", callback_data='pay_crypto')],
            [InlineKeyboardButton("Отмена", callback_data='cancel_payment')]
        ]
        
        await update.message.reply_text(
            text=f"💰 Сумма пополнения: {amount} руб (~{amount_usdt} USDT)\n"
                 f"📊 Текущий курс: 1 USDT = {usdt_rate:.2f} руб\n\n"
                 "Выберите способ оплаты:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return CONFIRM_ORDER
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректную сумму:")
        return GET_AMOUNT

@catch_errors
async def process_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    amount = context.user_data['topup_amount']
    
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
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        text=f"💳 Счет для оплаты создан\n\n"
             f"Сумма: {amount} руб (~{round(amount / usdt_rate, 2)} USDT)\n"
             f"📊 Курс: 1 USDT = {usdt_rate:.2f} руб\n\n"
             f"Ссылка для оплаты: {invoice['pay_url']}\n\n"
             f"После оплаты баланс будет пополнен автоматически в течение 5 минут.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Проверить оплату", callback_data=f'check_payment_{invoice["invoice_id"]}')],
            [InlineKeyboardButton("Назад в меню", callback_data='back_to_menu')]
        ])
    )

@catch_errors
async def choose_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Отправляем фото с выбором платформы
    with open('platforms.jpg', 'rb') as photo:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=photo,
            caption="Выберите платформу для заказа:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Twitch", "YouTube", "Kick"],
                    ["Назад в меню"]
                ],
                resize_keyboard=True
            )
        )

@catch_errors
async def get_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    platform = update.message.text.lower()
    if platform not in ['twitch', 'youtube', 'kick']:
        await update.message.reply_text("Пожалуйста, выберите платформу из предложенных:")
        return
    
    context.user_data['platform'] = platform
    
    prices = {
        'twitch': {'chat_ru': 250, 'chat_eng': 400, 'viewers': 1, 'followers': 1},
        'kick': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1},
        'youtube': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1}
    }
    
    keyboard = [
        [InlineKeyboardButton(f"💬 Чат (RU) - {prices[platform]['chat_ru']} руб/час", callback_data='service_chat_ru')],
        [InlineKeyboardButton(f"💬 Чат (ENG) - {prices[platform]['chat_eng']} руб/час", callback_data='service_chat_eng')],
        [InlineKeyboardButton(f"👀 Зрители - {prices[platform]['viewers']} руб/час", callback_data='service_viewers')],
        [InlineKeyboardButton(f"👥 Подписчики - {prices[platform]['followers']} руб/час", callback_data='service_followers')],
        [InlineKeyboardButton("Назад", callback_data='back_to_platforms')]
    ]
    
    await update.message.reply_text(
        text=f"Вы выбрали платформу: {platform.capitalize()}\n\n"
             "Теперь выберите услугу:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@catch_errors
async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    service = query.data.split('_')[1]
    context.user_data['service'] = service
    
    await query.edit_message_text(
        text="Введите юзернейм или ссылку на ваш канал:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Назад", callback_data='back_to_services')]
        ])
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
    
    keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_channel')])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text="📅 Выберите дату стрима:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text="📅 Выберите дату стрима:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

@catch_errors
async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    
    if data.startswith('calendar_select_'):
        selected_date = data.split('_')[2]
        context.user_data['stream_date'] = selected_date
        
        await query.edit_message_text(
            text=f"1. Платформа: {context.user_data['platform'].capitalize()}\n"
                 f"2. Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"3. Канал: {context.user_data['channel']}\n"
                 f"4. Дата: {selected_date.split('-')[2]}.{selected_date.split('-')[1]}.{selected_date.split('-')[0]}\n\n"
                 "⏰ Введите время начала стрима (например, 18:30):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data='back_to_calendar')]
            ])
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

@catch_errors
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    
    try:
        datetime.strptime(time_str, "%H:%M")
        context.user_data['start_time'] = time_str
        
        await update.message.reply_text(
            text=f"1. Платформа: {context.user_data['platform'].capitalize()}\n"
                 f"2. Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"3. Канал: {context.user_data['channel']}\n"
                 f"4. Дата: {context.user_data['stream_date'].split('-')[2]}.{context.user_data['stream_date'].split('-')[1]}.{context.user_data['stream_date'].split('-')[0]}\n"
                 f"5. Время: {time_str}\n\n"
                 "⏳ Введите продолжительность стрима (например, 2:30):",
            reply_markup=ReplyKeyboardMarkup(
                [["1:00", "2:00", "3:00"], ["Назад"]],
                resize_keyboard=True
            )
        )
        
        return GET_DURATION
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите время в формате ЧЧ:ММ:")
        return GET_TIME

@catch_errors
async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    duration_str = update.message.text
    
    try:
        hours, minutes = map(int, duration_str.split(':'))
        if hours < 0 or minutes < 0 or minutes >= 60:
            raise ValueError
        
        context.user_data['duration'] = duration_str
        
        price_per_hour = get_price(context.user_data['platform'], context.user_data['service'])
        total_hours = hours + minutes / 60
        amount = round(price_per_hour * total_hours, 2)
        context.user_data['amount'] = amount
        
        await update.message.reply_text(
            text=f"📝 Подтвердите заказ:\n\n"
                 f"🔹 Платформа: {context.user_data['platform'].capitalize()}\n"
                 f"🔹 Услуга: {get_service_name(context.user_data['service'])}\n"
                 f"🔹 Канал: {context.user_data['channel']}\n"
                 f"🔹 Дата: {context.user_data['stream_date'].split('-')[2]}.{context.user_data['stream_date'].split('-')[1]}.{context.user_data['stream_date'].split('-')[0]}\n"
                 f"🔹 Время: {context.user_data['start_time']}\n"
                 f"🔹 Длительность: {duration_str}\n\n"
                 f"💰 Итого к оплате: {amount} руб",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить заказ", callback_data='confirm_order')],
                [InlineKeyboardButton("❌ Отменить", callback_data='cancel_order')]
            ])
        )
        
        return CONFIRM_ORDER
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите длительность в формате Ч:ММ (например, 1:30):")
        return GET_DURATION

@catch_errors
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    order_data = context.user_data
    order_id = str(uuid.uuid4())[:8]
    
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    # Проверяем баланс пользователя
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    balance = cursor.fetchone()[0]
    
    if balance >= order_data['amount']:
        # Создаем заказ
        cursor.execute(
            """INSERT INTO orders 
            (order_id, user_id, platform, service, channel, stream_date, start_time, duration, amount, order_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, user_id, order_data['platform'], order_data['service'], order_data['channel'],
             order_data['stream_date'], order_data['start_time'], order_data['duration'], 
             order_data['amount'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        
        # Списание средств
        cursor.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (order_data['amount'], user_id))
        
        conn.commit()
        
        await query.edit_message_text(
            text=f"🎉 Заказ #{order_id} успешно создан!\n\n"
                 f"С вашего баланса списано {order_data['amount']} руб.\n"
                 f"Новый баланс: {balance - order_data['amount']:.2f} руб\n\n"
                 "Спасибо за заказ!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В меню", callback_data='back_to_menu')]
            ])
        )
    else:
        await query.edit_message_text(
            text=f"⚠️ Недостаточно средств на балансе!\n\n"
                 f"Требуется: {order_data['amount']} руб\n"
                 f"Ваш баланс: {balance:.2f} руб\n\n"
                 "Пополните баланс для завершения заказа.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Пополнить баланс", callback_data='topup_balance')],
                [InlineKeyboardButton("Отменить заказ", callback_data='cancel_order')]
            ])
        )
    
    conn.close()
    return ConversationHandler.END

@catch_errors
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await start(update, context)
    return ConversationHandler.END

@catch_errors
async def back_to_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await choose_platform(update, context)
    return ConversationHandler.END

@catch_errors
async def back_to_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await get_platform(update, context)
    return GET_CHANNEL

@catch_errors
async def back_to_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await ask_channel(update, context)
    return GET_CHANNEL

@catch_errors
async def back_to_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await show_calendar(update, context)
    return GET_DATE

@catch_errors
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="Заказ отменен.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("В меню", callback_data='back_to_menu')]
        ])
    )
    return ConversationHandler.END

@catch_errors
async def cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="Пополнение баланса отменено.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("В меню", callback_data='back_to_menu')]
        ])
    )
    return ConversationHandler.END

def get_service_name(service):
    services = {
        'chat_ru': 'Чат (RU)',
        'chat_eng': 'Чат (ENG)',
        'viewers': 'Зрители',
        'followers': 'Подписчики'
    }
    return services.get(service, service)

def get_price(platform, service):
    prices = {
        'twitch': {'chat_ru': 250, 'chat_eng': 400, 'viewers': 1, 'followers': 1},
        'kick': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1},
        'youtube': {'chat_ru': 319, 'chat_eng': 419, 'viewers': 1, 'followers': 1}
    }
    return prices.get(platform, {}).get(service, 0)

def main():
    init_db()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    
    # Обработчики сообщений
    application.add_handler(MessageHandler(filters.Text(["Мой профиль"]), show_profile))
    application.add_handler(MessageHandler(filters.Text(["Помощь"]), show_help))
    application.add_handler(MessageHandler(filters.Text(["Сделать заказ"]), choose_platform))
    application.add_handler(MessageHandler(filters.Text(["Пополнить баланс"]), topup_balance))
    application.add_handler(MessageHandler(filters.Text(["Назад в меню"]), back_to_menu))
    application.add_handler(MessageHandler(filters.Text(["Twitch", "YouTube", "Kick"]), get_platform))
    
    # Обработчики callback-запросов
    application.add_handler(CallbackQueryHandler(process_crypto_payment, pattern='^pay_crypto$'))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    application.add_handler(CallbackQueryHandler(back_to_platforms, pattern='^back_to_platforms$'))
    application.add_handler(CallbackQueryHandler(back_to_services, pattern='^back_to_services$'))
    application.add_handler(CallbackQueryHandler(back_to_channel, pattern='^back_to_channel$'))
    application.add_handler(CallbackQueryHandler(back_to_calendar, pattern='^back_to_calendar$'))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern='^cancel_order$'))
    application.add_handler(CallbackQueryHandler(cancel_payment, pattern='^cancel_payment$'))
    
    # Conversation handler для пополнения баланса
    topup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["Пополнить баланс"]), topup_balance)],
        states={
            GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
            CONFIRM_ORDER: [CallbackQueryHandler(process_crypto_payment, pattern='^pay_crypto$')]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_payment, pattern='^cancel_payment$'),
            CommandHandler("start", start)
        ]
    )
    application.add_handler(topup_conv)
    
    # Conversation handler для создания заказа
    order_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["Twitch", "YouTube", "Kick"]), get_platform)],
        states={
            GET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel)],
            GET_DATE: [CallbackQueryHandler(handle_calendar, pattern='^calendar_')],
            GET_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
            GET_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration)],
            CONFIRM_ORDER: [CallbackQueryHandler(confirm_order, pattern='^confirm_order$')]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_order, pattern='^cancel_order$'),
            CommandHandler("start", start)
        ],
        allow_reentry=True
    )
    application.add_handler(order_conv)
    
    # Планировщик задач
    application.job_queue.run_repeating(
        check_pending_payments,
        interval=PAYMENT_CHECK_INTERVAL,
        first=10
    )
    application.job_queue.run_repeating(
        get_usdt_rate,
        interval=3600,  # Обновляем курс каждый час
        first=5
    )
    
    application.run_polling()

if __name__ == '__main__':
    main()
