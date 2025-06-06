import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InputFile
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
    JobQueue
)
from datetime import datetime, timedelta
from calendar import monthrange
import sqlite3
import uuid
import requests
import json
import os
import sys
import asyncio
from dotenv import load_dotenv
from functools import wraps
import time
from threading import Thread
from flask import Flask

# =============================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ И КОНФИГУРАЦИИ
# =============================================

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

# Конфигурационные переменные
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_BOT_TOKEN = os.getenv('CRYPTO_BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api"
RENDER = os.getenv('RENDER', 'false').lower() == 'true'

# Проверка обязательных переменных
if not all([TELEGRAM_TOKEN, CRYPTO_BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("Необходимые переменные окружения не установлены!")

# Интервалы (в секундах)
PAYMENT_CHECK_INTERVAL = 300  # 5 минут
KEEP_ALIVE_INTERVAL = 300     # 5 минут
RESTART_DELAY = 10            # 10 секунд при ошибке
RATE_UPDATE_INTERVAL = 3600   # 1 час

# Состояния для ConversationHandler
(
    GET_CHANNEL, GET_DATE, GET_TIME, GET_DURATION, 
    CONFIRM_ORDER, ADMIN_BALANCE_CHANGE, GET_AMOUNT,
    ADMIN_ADD_BALANCE, ADMIN_SET_BALANCE, ADMIN_ADD_ADMIN,
    ADMIN_ORDER_ACTION
) = range(11)

# Глобальные переменные
usdt_rate = 80.0  # начальное значение курса

# =============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================

def init_db():
    """Инициализация базы данных"""
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
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_date TEXT
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
        )''',
        '''CREATE TABLE IF NOT EXISTS system (
            key TEXT PRIMARY KEY,
            value TEXT
        )'''
    ]
    
    for table in tables:
        cursor.execute(table)
    
    # Добавляем администраторов
    for admin_id in ADMIN_IDS:
        cursor.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)",
            (admin_id, 0, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
    
    # Инициализация системных настроек
    cursor.execute(
        "INSERT OR IGNORE INTO system (key, value) VALUES ('last_restart', ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),)
    )
    conn.commit()
    conn.close()

def catch_errors(func):
    """Декоратор для перехвата ошибок с автоматическим перезапуском"""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            logging.error(f"Ошибка в функции {func.__name__}: {e}", exc_info=True)
            
            # Записываем ошибку в базу данных
            conn = sqlite3.connect('bot.db')
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO system (key, value) VALUES (?, ?)",
                (f"error_{datetime.now().timestamp()}", str(e))
            )
            conn.commit()
            conn.close()
            
            if update and update.effective_chat:
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="⚠️ Произошла ошибка. Бот будет перезапущен автоматически."
                    )
                except:
                    pass
            
            # Планируем перезапуск
            asyncio.create_task(restart_bot(context))
            raise
    return wrapped

async def restart_bot(context: ContextTypes.DEFAULT_TYPE):
    """Перезапуск бота с задержкой"""
    logging.info(f"Запланирован перезапуск через {RESTART_DELAY} секунд...")
    await asyncio.sleep(RESTART_DELAY)
    python = sys.executable
    os.execl(python, python, *sys.argv)

async def get_usdt_rate():
    """Получает текущий курс USDT к рублю"""
    global usdt_rate
    try:
        response = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB", timeout=10)
        data = response.json()
        usdt_rate = float(data['price'])
        logger.info(f"Обновлен курс USDT: {usdt_rate} RUB")
        
        # Сохраняем курс в базу
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO system (key, value) VALUES (?, ?)",
            ('usdt_rate', str(usdt_rate)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при получении курса USDT: {e}")
        # Пробуем получить последний сохраненный курс
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system WHERE key = 'usdt_rate'")
        result = cursor.fetchone()
        if result:
            usdt_rate = float(result[0])
        conn.close()

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    result = cursor.fetchone() is not None
    conn.close()
    return result

async def keep_alive(context: ContextTypes.DEFAULT_TYPE):
    """Функция для поддержания активности бота"""
    try:
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        logging.info("Проверка активности: БД доступна")
        
        # Обновляем время последней активности
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO system (key, value) VALUES (?, ?)",
            ('last_activity', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка проверки активности: {e}")
        await restart_bot(context)

# =============================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С КРИПТОПЛАТЕЖАМИ
# =============================================

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
async def check_crypto_payment(invoice_id: str):
    """Проверяет статус криптоплатежа"""
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

@catch_errors
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
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Ваш баланс пополнен на {amount} RUB!\n"
                             f"Новый баланс: {get_user_balance(user_id):.2f} RUB"
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
                
                conn.commit()
                logger.info(f"Подтвержден платеж {invoice_id} для пользователя {user_id}")
        
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при проверке платежей: {e}")
        await restart_bot(context)

# =============================================
# ОСНОВНЫЕ ФУНКЦИИ БОТА
# =============================================

@catch_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user = update.effective_user
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    # Регистрируем пользователя, если он новый
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    if not cursor.fetchone():
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, last_name, registration_date, last_activity) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.username, user.first_name, user.last_name, 
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        logger.info(f"Зарегистрирован новый пользователь: {user.id} ({user.username})")
    else:
        cursor.execute(
            "UPDATE users SET last_activity = ? WHERE user_id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user.id)
        )
    
    conn.commit()
    conn.close()
    
    # Отправляем приветственное сообщение с фото
    try:
        with open('assets/welcome.jpg', 'rb') as photo:
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
    except FileNotFoundError:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🌟 Добро пожаловать, {user.first_name}!\n\n"
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
    """Показывает профиль пользователя"""
    user = update.effective_user
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    
    # Получаем данные пользователя
    cursor.execute(
        "SELECT balance, registration_date FROM users WHERE user_id = ?", 
        (user.id,)
    )
    balance, reg_date = cursor.fetchone()
    
    # Статистика заказов
    cursor.execute(
        "SELECT COUNT(*), SUM(amount) FROM orders WHERE user_id = ?", 
        (user.id,)
    )
    orders_count, total_spent = cursor.fetchone()
    total_spent = total_spent or 0
    
    # Последние заказы
    cursor.execute(
        "SELECT order_id, platform, service, amount, status FROM orders "
        "WHERE user_id = ? ORDER BY order_date DESC LIMIT 3",
        (user.id,)
    )
    last_orders = cursor.fetchall()
    
    conn.close()
    
    # Формируем текст профиля
    profile_text = (
        f"📊 <b>Ваш профиль</b>\n\n"
        f"👤 <b>Имя:</b> {user.first_name or ''} {user.last_name or ''}\n"
        f"🆔 <b>ID:</b> {user.id}\n"
        f"📅 <b>Дата регистрации:</b> {reg_date}\n\n"
        f"💰 <b>Баланс:</b> {balance:.2f} RUB\n"
        f"🛒 <b>Всего заказов:</b> {orders_count}\n"
        f"💸 <b>Всего потрачено:</b> {total_spent:.2f} RUB\n\n"
        f"📦 <b>Последние заказы:</b>\n"
    )
    
    for order in last_orders:
        order_id, platform, service, amount, status = order
        status_icon = "✅" if status == "completed" else "🔄" if status == "pending" else "❌"
        profile_text += (
            f"{status_icon} <b>Заказ #{order_id}</b>\n"
            f"   Платформа: {platform.capitalize()}\n"
            f"   Услуга: {get_service_name(service)}\n"
            f"   Сумма: {amount:.2f} RUB\n\n"
        )
    
    # Кнопки для админа
    keyboard = [["Пополнить баланс"]]
    if is_admin(user.id):
        keyboard.append(["Админ панель"])
    keyboard.append(["Назад в меню"])
    
    await update.message.reply_text(
        text=profile_text,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

@catch_errors
async def topup_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало процесса пополнения баланса"""
    await update.message.reply_text(
        text="💰 <b>Пополнение баланса</b>\n\n"
             "Введите сумму пополнения в рублях (минимум 100 RUB):",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            [["500", "1000", "2000"], ["Назад в меню"]],
            resize_keyboard=True
        )
    )
    return GET_AMOUNT

@catch_errors
async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенной суммы для пополнения"""
    try:
        amount = float(update.message.text)
        if amount < 100:
            await update.message.reply_text(
                "Минимальная сумма пополнения - 100 RUB. Введите сумму еще раз:",
                reply_markup=ReplyKeyboardMarkup(
                    [["500", "1000", "2000"], ["Назад в меню"]],
                    resize_keyboard=True
                )
            )
            return GET_AMOUNT
        
        context.user_data['topup_amount'] = amount
        
        await get_usdt_rate()  # Обновляем курс
        amount_usdt = round(amount / usdt_rate, 2)
        
        keyboard = [
            [InlineKeyboardButton(
                f"Оплатить {amount} RUB (~{amount_usdt} USDT)", 
                callback_data='pay_crypto'
            )],
            [InlineKeyboardButton("Отмена", callback_data='cancel_payment')]
        ]
        
        await update.message.reply_text(
            text=f"💰 <b>Подтверждение пополнения</b>\n\n"
                 f"Сумма: {amount} RUB (~{amount_usdt} USDT)\n"
                 f"📊 Текущий курс: 1 USDT = {usdt_rate:.2f} RUB\n\n"
                 "Выберите способ оплаты:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
        return CONFIRM_ORDER
    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите корректную сумму (число):",
            reply_markup=ReplyKeyboardMarkup(
                [["500", "1000", "2000"], ["Назад в меню"]],
                resize_keyboard=True
            )
        )
        return GET_AMOUNT

@catch_errors
async def process_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание крипто-счета для оплаты"""
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
    )
    conn.commit()
    conn.close()
    
    await query.edit_message_text(
        text=f"💳 <b>Счет для оплаты создан</b>\n\n"
             f"Сумма: {amount} RUB (~{round(amount / usdt_rate, 2)} USDT)\n"
             f"📊 Курс: 1 USDT = {usdt_rate:.2f} RUB\n\n"
             f"Ссылка для оплаты: {invoice['pay_url']}\n\n"
             f"После оплаты баланс будет пополнен автоматически в течение 5 минут.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Проверить оплату", callback_data=f'check_payment_{invoice["invoice_id"]}')],
            [InlineKeyboardButton("Назад в меню", callback_data='back_to_menu')]
        ])
    )

# =============================================
# ФУНКЦИИ ДЛЯ ЗАКАЗОВ
# =============================================

@catch_errors
async def choose_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор платформы для заказа"""
    try:
        with open('assets/platforms.jpg', 'rb') as photo:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo,
                caption="<b>Выберите платформу для заказа:</b>",
                parse_mode='HTML',
                reply_markup=ReplyKeyboardMarkup(
                    [
                        ["Twitch", "YouTube", "Kick"],
                        ["Назад в меню"]
                    ],
                    resize_keyboard=True
                )
            )
    except FileNotFoundError:
        await update.message.reply_text(
            text="<b>Выберите платформу для заказа:</b>",
            parse_mode='HTML',
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
    """Обработка выбора платформы"""
    platform = update.message.text.lower()
    if platform not in ['twitch', 'youtube', 'kick']:
        await update.message.reply_text(
            "Пожалуйста, выберите платформу из предложенных:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Twitch", "YouTube", "Kick"],
                    ["Назад в меню"]
                ],
                resize_keyboard=True
            )
        )
        return
    
    context.user_data['platform'] = platform
    
    # Формируем клавиатуру с услугами
    prices = get_service_prices(platform)
    keyboard = [
        [InlineKeyboardButton(
            f"💬 Чат (RU) - {prices['chat_ru']} RUB/час", 
            callback_data='service_chat_ru'
        )],
        [InlineKeyboardButton(
            f"💬 Чат (ENG) - {prices['chat_eng']} RUB/час", 
            callback_data='service_chat_eng'
        )],
        [InlineKeyboardButton(
            f"👀 Зрители - {prices['viewers']} RUB/час", 
            callback_data='service_viewers'
        )],
        [InlineKeyboardButton(
            f"👥 Подписчики - {prices['followers']} RUB/час", 
            callback_data='service_followers'
        )],
        [InlineKeyboardButton("Назад", callback_data='back_to_platforms')]
    ]
    
    await update.message.reply_text(
        text=f"<b>Платформа:</b> {platform.capitalize()}\n\n"
             "<b>Выберите услугу:</b>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

@catch_errors
async def ask_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос ссылки на канал"""
    query = update.callback_query
    await query.answer()
    
    service = query.data.split('_')[1]
    context.user_data['service'] = service
    
    await query.edit_message_text(
        text="<b>Введите юзернейм или ссылку на ваш канал:</b>\n\n"
             "Примеры:\n"
             "- https://twitch.tv/username\n"
             "- @username\n"
             "- username",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Назад", callback_data='back_to_services')]
        ])
    )
    
    return GET_CHANNEL

@catch_errors
async def get_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного канала"""
    channel = update.message.text.strip()
    
    # Простая валидация канала
    if not channel or len(channel) > 100:
        await update.message.reply_text(
            "Пожалуйста, введите корректный юзернейм или ссылку (макс. 100 символов):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data='back_to_services')]
            ])
        )
        return GET_CHANNEL
    
    context.user_data['channel'] = channel
    await show_calendar(update, context)
    return GET_DATE

# ... (продолжение кода с остальными функциями)

# =============================================
# ЗАПУСК БОТА И ВЕБ-СЕРВЕРА
# =============================================

def run_web_server():
    """Запуск Flask-сервера для Render"""
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Telegram Bot is running!", 200

    @app.route('/health')
    def health():
        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        return "OK", 200

    port = int(os.environ.get("PORT", 2500))
    app.run(host="0.0.0.0", port=port)

def main():
    """Основная функция запуска бота"""
    # Инициализация базы данных
    init_db()
    
    # Запуск веб-сервера в отдельном потоке (для Render)
    if RENDER:
        Thread(target=run_web_server, daemon=True).start()

    # Создание и настройка приложения бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))

    # Добавление обработчиков сообщений
    application.add_handler(MessageHandler(filters.Text(["Мой профиль"]), show_profile))
    application.add_handler(MessageHandler(filters.Text(["Помощь"]), show_help))
    application.add_handler(MessageHandler(filters.Text(["Сделать заказ"]), choose_platform))
    application.add_handler(MessageHandler(filters.Text(["Пополнить баланс"]), topup_balance))
    application.add_handler(MessageHandler(filters.Text(["Назад в меню"]), back_to_menu))
    application.add_handler(MessageHandler(filters.Text(["Twitch", "YouTube", "Kick"]), get_platform))

    # Добавление обработчиков callback-запросов
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
        interval=RATE_UPDATE_INTERVAL,
        first=5
    )
    application.job_queue.run_repeating(
        keep_alive,
        interval=KEEP_ALIVE_INTERVAL,
        first=10
    )

    # Запуск бота
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    while True:
        try:
            main()
        except Exception as e:
            logging.error(f"Фатальная ошибка: {e}. Перезапуск через {RESTART_DELAY} секунд...")
            time.sleep(RESTART_DELAY)
