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
import json
import os
import sys
import asyncio
from dotenv import load_dotenv
from functools import wraps
import time
from threading import Thread
from flask import Flask
import socket
import requests
import uuid
import calendar

# =============================================
# НАСТРОЙКА ЛОГГИРОВАНИЯ И КОНФИГУРАЦИИ
# =============================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CRYPTO_BOT_TOKEN = os.getenv('CRYPTO_BOT_TOKEN')
ADMIN_IDS = [int(id.strip()) for id in os.getenv('ADMIN_IDS', '').split(',') if id.strip()]
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api"

if not all([TELEGRAM_TOKEN, CRYPTO_BOT_TOKEN, ADMIN_IDS]):
    raise ValueError("Необходимые переменные окружения не установлены!")

# Интервалы (в секундах)
PAYMENT_CHECK_INTERVAL = 300
KEEP_ALIVE_INTERVAL = 300
RESTART_DELAY = 10
RATE_UPDATE_INTERVAL = 3600

# Состояния для ConversationHandler
(
    GET_CHANNEL, GET_DATE, GET_TIME, GET_DURATION, 
    CONFIRM_ORDER, ADMIN_BALANCE_CHANGE, GET_AMOUNT,
    ADMIN_ADD_BALANCE, ADMIN_SET_BALANCE, ADMIN_ADD_ADMIN,
    ADMIN_ORDER_ACTION
) = range(11)

# Глобальные переменные
usdt_rate = 80.0
DATA_FILE = 'data.json'

# =============================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (JSON)
# =============================================

def init_db():
    """Инициализация JSON базы данных"""
    default_data = {
        'users': {},
        'orders': {},
        'admins': {str(admin_id): {'added_by': 0, 'added_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")} 
                  for admin_id in ADMIN_IDS},
        'payments': {},
        'system': {
            'last_restart': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'usdt_rate': str(usdt_rate),
            'last_activity': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    }
    
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
            # Обновляем только если файл существует
            if 'admins' not in data:
                data['admins'] = default_data['admins']
    except (FileNotFoundError, json.JSONDecodeError):
        data = default_data
    
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def save_data(data):
    """Сохраняет данные в JSON файл"""
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def load_data():
    """Загружает данные из JSON файла"""
    try:
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        init_db()
        return load_data()

def get_user(user_id):
    """Получает данные пользователя"""
    data = load_data()
    return data['users'].get(str(user_id))

def update_user(user_id, updates):
    """Обновляет данные пользователя"""
    data = load_data()
    if str(user_id) not in data['users']:
        data['users'][str(user_id)] = {
            'username': '',
            'first_name': '',
            'last_name': '',
            'balance': 0.0,
            'registration_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_activity': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    data['users'][str(user_id)].update(updates)
    save_data(data)

def create_order(order_data):
    """Создает новый заказ"""
    data = load_data()
    order_id = str(uuid.uuid4())
    order_data['order_id'] = order_id
    order_data['order_date'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order_data['status'] = 'pending'
    data['orders'][order_id] = order_data
    save_data(data)
    return order_id

def update_order(order_id, updates):
    """Обновляет данные заказа"""
    data = load_data()
    if order_id in data['orders']:
        data['orders'][order_id].update(updates)
        save_data(data)

def create_payment(payment_data):
    """Создает запись о платеже"""
    data = load_data()
    invoice_id = payment_data['invoice_id']
    data['payments'][invoice_id] = payment_data
    save_data(data)

def update_payment(invoice_id, updates):
    """Обновляет статус платежа"""
    data = load_data()
    if invoice_id in data['payments']:
        data['payments'][invoice_id].update(updates)
        save_data(data)

def get_pending_payments():
    """Получает неоплаченные платежи"""
    data = load_data()
    return [payment for payment in data['payments'].values() if payment.get('status') == 'created']

def get_user_orders(user_id):
    """Получает заказы пользователя"""
    data = load_data()
    return [order for order in data['orders'].values() if order['user_id'] == user_id]

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором"""
    data = load_data()
    return str(user_id) in data['admins']

def get_service_prices(platform):
    """Возвращает цены для услуг"""
    prices = {
        'twitch': {
            'chat_ru': 100,
            'chat_eng': 120,
            'viewers': 150,
            'followers': 200
        },
        'youtube': {
            'chat_ru': 90,
            'chat_eng': 110,
            'viewers': 140,
            'followers': 180
        },
        'kick': {
            'chat_ru': 80,
            'chat_eng': 100,
            'viewers': 130,
            'followers': 160
        }
    }
    return prices.get(platform.lower(), prices['twitch'])

def get_service_name(service_key):
    """Возвращает читаемое название услуги"""
    names = {
        'chat_ru': 'Чат (RU)',
        'chat_eng': 'Чат (ENG)',
        'viewers': 'Зрители',
        'followers': 'Подписчики'
    }
    return names.get(service_key, service_key)

# =============================================
# ДЕКОРАТОРЫ И СЛУЖЕБНЫЕ ФУНКЦИИ
# =============================================

def catch_errors(func):
    """Декоратор для перехвата ошибок с автоматическим перезапуском"""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            logging.error(f"Ошибка в функции {func.__name__}: {e}", exc_info=True)
            
            # Сохраняем ошибку в систему
            data = load_data()
            data['system'][f"error_{datetime.now().timestamp()}"] = str(e)
            save_data(data)
            
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
        data = load_data()
        data['system']['usdt_rate'] = str(usdt_rate)
        save_data(data)
    except Exception as e:
        logger.error(f"Ошибка при получении курса USDT: {e}")
        # Пробуем получить последний сохраненный курс
        data = load_data()
        if 'usdt_rate' in data['system']:
            usdt_rate = float(data['system']['usdt_rate'])

async def keep_alive(context: ContextTypes.DEFAULT_TYPE):
    """Функция для поддержания активности бота"""
    try:
        # Просто проверяем доступность данных
        load_data()
        logging.info("Проверка активности: данные доступны")
        
        # Обновляем время последней активности
        data = load_data()
        data['system']['last_activity'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_data(data)
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
        payments = get_pending_payments()
        
        for payment in payments:
            invoice_id = payment['invoice_id']
            user_id = payment['user_id']
            amount = payment['amount']
            
            payment_info = await check_crypto_payment(invoice_id)
            if payment_info and payment_info['status'] == 'paid':
                # Обновляем статус платежа
                update_payment(invoice_id, {
                    'status': 'paid',
                    'paid_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                
                # Пополняем баланс пользователя
                user = get_user(user_id)
                new_balance = user['balance'] + amount
                update_user(user_id, {'balance': new_balance})
                
                # Уведомляем пользователя
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"✅ Ваш баланс пополнен на {amount} RUB!\n"
                             f"Новый баланс: {new_balance:.2f} RUB"
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить пользователя {user_id}: {e}")
                
                logger.info(f"Подтвержден платеж {invoice_id} для пользователя {user_id}")
        
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
    current_user = get_user(user.id)
    
    if not current_user:
        update_user(user.id, {
            'username': user.username or '',
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'balance': 0.0,
            'registration_date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'last_activity': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        logger.info(f"Зарегистрирован новый пользователь: {user.id} ({user.username})")
    else:
        update_user(user.id, {'last_activity': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    
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
    user_data = get_user(user.id)
    
    if not user_data:
        await update.message.reply_text("Профиль не найден. Пожалуйста, начните с /start")
        return
    
    # Получаем заказы пользователя
    user_orders = get_user_orders(user.id)
    orders_count = len(user_orders)
    total_spent = sum(order['amount'] for order in user_orders)
    last_orders = sorted(user_orders, key=lambda x: x['order_date'], reverse=True)[:3]
    
    profile_text = (
        f"📊 <b>Ваш профиль</b>\n\n"
        f"👤 <b>Имя:</b> {user_data['first_name']} {user_data['last_name']}\n"
        f"🆔 <b>ID:</b> {user.id}\n"
        f"📅 <b>Дата регистрации:</b> {user_data['registration_date']}\n\n"
        f"💰 <b>Баланс:</b> {user_data['balance']:.2f} RUB\n"
        f"🛒 <b>Всего заказов:</b> {orders_count}\n"
        f"💸 <b>Всего потрачено:</b> {total_spent:.2f} RUB\n\n"
        f"📦 <b>Последние заказы:</b>\n"
    )
    
    for order in last_orders:
        status_icon = "✅" if order['status'] == "completed" else "🔄" if order['status'] == "pending" else "❌"
        profile_text += (
            f"{status_icon} <b>Заказ #{order['order_id']}</b>\n"
            f"   Платформа: {order['platform'].capitalize()}\n"
            f"   Услуга: {get_service_name(order['service'])}\n"
            f"   Сумма: {order['amount']:.2f} RUB\n\n"
        )
    
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
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает справку"""
    help_text = (
        "ℹ️ <b>Справка по боту</b>\n\n"
        "Этот бот позволяет заказывать различные услуги для стримов:\n"
        "- 💬 Чат (RU/ENG)\n"
        "- 👀 Зрители\n"
        "- 👥 Подписчики\n\n"
        "Доступные платформы:\n"
        "- Twitch\n"
        "- YouTube\n"
        "- Kick\n\n"
        "Для начала работы нажмите «Сделать заказ» или пополните баланс."
    )
    await update.message.reply_text(
        text=help_text,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            [["Назад в меню"]],
            resize_keyboard=True
        )
    )

@catch_errors
async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает в главное меню"""
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text="Главное меню:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Мой профиль", "Помощь"],
                    ["Сделать заказ", "Пополнить баланс"]
                ],
                resize_keyboard=True
            )
        )
    else:
        await update.message.reply_text(
            text="Главное меню:",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Мой профиль", "Помощь"],
                    ["Сделать заказ", "Пополнить баланс"]
                ],
                resize_keyboard=True
            )
        )
    return ConversationHandler.END

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
    create_payment({
        'invoice_id': invoice['invoice_id'],
        'user_id': user_id,
        'amount': amount,
        'currency': 'RUB',
        'status': 'created',
        'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    
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

@catch_errors
async def cancel_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена платежа"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="Пополнение баланса отменено.",
        reply_markup=ReplyKeyboardMarkup(
            [["Назад в меню"]],
            resize_keyboard=True
        )
    )
    return ConversationHandler.END

@catch_errors
async def check_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка статуса платежа"""
    query = update.callback_query
    await query.answer()
    
    invoice_id = query.data.split('_')[-1]
    payment_info = await check_crypto_payment(invoice_id)
    
    if payment_info and payment_info['status'] == 'paid':
        await query.edit_message_text(
            text="✅ Платеж успешно получен! Баланс уже пополнен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад в меню", callback_data='back_to_menu')]
            ])
        )
    else:
        await query.edit_message_text(
            text="🔄 Платеж еще не поступил. Попробуйте проверить позже.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Проверить снова", callback_data=f'check_payment_{invoice_id}')],
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
async def back_to_platforms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору платформы"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
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
    return ConversationHandler.END

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
async def back_to_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору услуг"""
    query = update.callback_query
    await query.answer()
    
    platform = context.user_data['platform']
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
    
    await query.edit_message_text(
        text=f"<b>Платформа:</b> {platform.capitalize()}\n\n"
             "<b>Выберите услугу:</b>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
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

async def show_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает календарь для выбора даты"""
    now = datetime.now()
    year = now.year
    month = now.month
    
    keyboard = []
    
    # Заголовок с месяцем и годом
    keyboard.append([InlineKeyboardButton(
        f"{calendar.month_name[month]} {year}", 
        callback_data="ignore"
    )])
    
    # Дни недели
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([
        InlineKeyboardButton(day, callback_data="ignore") for day in week_days
    ])
    
    # Дни месяца
    month_days = calendar.monthcalendar(year, month)
    for week in month_days:
        week_buttons = []
        for day in week:
            if day == 0:
                week_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
            elif datetime(year, month, day) < datetime.now():
                week_buttons.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                week_buttons.append(InlineKeyboardButton(
                    str(day), 
                    callback_data=f"calendar_{year}-{month}-{day}"
                ))
        keyboard.append(week_buttons)
    
    # Кнопки навигации
    keyboard.append([
        InlineKeyboardButton("Назад", callback_data="back_to_channel"),
        InlineKeyboardButton("Отмена", callback_data="cancel_order")
    ])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text="📅 <b>Выберите дату стрима:</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text="📅 <b>Выберите дату стрима:</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

@catch_errors
async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора даты из календаря"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("calendar_"):
        date_str = query.data.split('_')[1]
        year, month, day = map(int, date_str.split('-'))
        selected_date = datetime(year, month, day)
        
        context.user_data['stream_date'] = selected_date.strftime("%Y-%m-%d")
        
        await query.edit_message_text(
            text=f"📅 <b>Выбрана дата:</b> {selected_date.str}\n"
            f"📅 <b>Выбрана дата:</b> {selected_date.strftime('%d.%m.%Y')}\n\n"
            "⏰ <b>Введите время начала стрима (в формате ЧЧ:ММ):</b>\n"
            "Пример: 18:30",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="back_to_calendar")],
                [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
            ])
        )
        return GET_TIME

@catch_errors
async def back_to_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к выбору даты"""
    query = update.callback_query
    await query.answer()
    await show_calendar(update, context)
    return GET_DATE

@catch_errors
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного времени"""
    time_str = update.message.text.strip()
    
    try:
        # Проверяем формат времени
        hours, minutes = map(int, time_str.split(':'))
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError
        
        selected_date = datetime.strptime(context.user_data['stream_date'], "%Y-%m-%d")
        stream_datetime = datetime(
            selected_date.year,
            selected_date.month,
            selected_date.day,
            hours,
            minutes
        )
        
        # Проверяем, что время не в прошлом
        if stream_datetime < datetime.now():
            await update.message.reply_text(
                "Время уже прошло. Пожалуйста, введите корректное время:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="back_to_calendar")],
                    [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
                ])
            )
            return GET_TIME
        
        context.user_data['start_time'] = time_str
        
        await update.message.reply_text(
            text="⏳ <b>Введите продолжительность стрима в часах (от 1 до 24):</b>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1 час", callback_data="duration_1")],
                [InlineKeyboardButton("2 часа", callback_data="duration_2")],
                [InlineKeyboardButton("4 часа", callback_data="duration_4")],
                [InlineKeyboardButton("Назад", callback_data="back_to_time")],
                [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
            ])
        )
        return GET_DURATION
        
    except (ValueError, IndexError):
        await update.message.reply_text(
            "Некорректный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="back_to_calendar")],
                [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
            ])
        )
        return GET_TIME

@catch_errors
async def back_to_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат к вводу времени"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="⏰ <b>Введите время начала стрима (в формате ЧЧ:ММ):</b>\n"
             "Пример: 18:30",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Назад", callback_data="back_to_calendar")],
            [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
        ])
    )
    return GET_TIME

@catch_errors
async def get_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка продолжительности стрима"""
    if update.callback_query:
        # Если продолжительность выбрана через кнопку
        query = update.callback_query
        await query.answer()
        duration = int(query.data.split('_')[1])
    else:
        # Если продолжительность введена вручную
        try:
            duration = int(update.message.text)
            if not 1 <= duration <= 24:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Пожалуйста, введите число от 1 до 24:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="back_to_time")],
                    [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
                ])
            )
            return GET_DURATION
    
    context.user_data['duration'] = duration
    
    # Рассчитываем стоимость
    platform = context.user_data['platform']
    service = context.user_data['service']
    prices = get_service_prices(platform)
    price_per_hour = prices[service]
    total_amount = price_per_hour * duration
    
    # Проверяем баланс пользователя
    user = get_user(update.effective_user.id)
    balance = user['balance']
    
    context.user_data['amount'] = total_amount
    
    order_text = (
        f"📋 <b>Детали заказа:</b>\n\n"
        f"🏷️ <b>Платформа:</b> {platform.capitalize()}\n"
        f"🛠️ <b>Услуга:</b> {get_service_name(service)}\n"
        f"📺 <b>Канал:</b> {context.user_data['channel']}\n"
        f"📅 <b>Дата:</b> {context.user_data['stream_date']}\n"
        f"⏰ <b>Время:</b> {context.user_data['start_time']}\n"
        f"⏳ <b>Продолжительность:</b> {duration} час(а/ов)\n\n"
        f"💰 <b>Стоимость:</b> {total_amount:.2f} RUB\n"
        f"💳 <b>Ваш баланс:</b> {balance:.2f} RUB\n\n"
    )
    
    if balance >= total_amount:
        order_text += "✅ <b>На вашем счету достаточно средств</b>"
        keyboard = [
            [InlineKeyboardButton("Подтвердить заказ", callback_data="confirm_order")],
            [InlineKeyboardButton("Изменить данные", callback_data="change_order")],
            [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
        ]
    else:
        order_text += (
            "❌ <b>Недостаточно средств на балансе</b>\n"
            f"Не хватает: {total_amount - balance:.2f} RUB\n\n"
            "Пожалуйста, пополните баланс или измените параметры заказа"
        )
        keyboard = [
            [InlineKeyboardButton("Пополнить баланс", callback_data="topup_balance")],
            [InlineKeyboardButton("Изменить данные", callback_data="change_order")],
            [InlineKeyboardButton("Отмена", callback_data="cancel_order")]
        ]
    
    if update.callback_query:
        await query.edit_message_text(
            text=order_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text=order_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    return CONFIRM_ORDER

@catch_errors
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение и создание заказа"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_data = get_user(user_id)
    order_data = {
        'user_id': user_id,
        'platform': context.user_data['platform'],
        'service': context.user_data['service'],
        'channel': context.user_data['channel'],
        'stream_date': context.user_data['stream_date'],
        'start_time': context.user_data['start_time'],
        'duration': context.user_data['duration'],
        'amount': context.user_data['amount'],
        'status': 'pending',
        'payment_method': 'balance'
    }
    
    # Создаем заказ
    order_id = create_order(order_data)
    
    # Списание средств с баланса
    new_balance = user_data['balance'] - order_data['amount']
    update_user(user_id, {'balance': new_balance})
    
    # Уведомление пользователя
    await query.edit_message_text(
        text=f"✅ <b>Заказ #{order_id} успешно создан!</b>\n\n"
             f"С вашего баланса списано {order_data['amount']:.2f} RUB\n"
             f"Новый баланс: {new_balance:.2f} RUB\n\n"
             "Мы уведомим вас о статусе заказа.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Мои заказы", callback_data="my_orders")],
            [InlineKeyboardButton("Назад в меню", callback_data="back_to_menu")]
        ])
    )
    
    # Уведомление админов
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🛎️ <b>Новый заказ #{order_id}</b>\n\n"
                     f"Пользователь: @{query.from_user.username or query.from_user.id}\n"
                     f"Услуга: {get_service_name(order_data['service'])}\n"
                     f"Сумма: {order_data['amount']:.2f} RUB",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора {admin_id}: {e}")
    
    return ConversationHandler.END

@catch_errors
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена заказа"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="Заказ отменен.",
        reply_markup=ReplyKeyboardMarkup(
            [["Назад в меню"]],
            resize_keyboard=True
        )
    )
    return ConversationHandler.END

@catch_errors
async def change_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменение параметров заказа"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        text="Что вы хотите изменить?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Платформу", callback_data="change_platform")],
            [InlineKeyboardButton("Услугу", callback_data="change_service")],
            [InlineKeyboardButton("Канал", callback_data="change_channel")],
            [InlineKeyboardButton("Дату", callback_data="change_date")],
            [InlineKeyboardButton("Время", callback_data="change_time")],
            [InlineKeyboardButton("Продолжительность", callback_data="change_duration")],
            [InlineKeyboardButton("Назад", callback_data="back_to_order")]
        ])
    )

# =============================================
# АДМИН ФУНКЦИИ
# =============================================

@catch_errors
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель администратора"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Доступ запрещен.")
        return
    
    data = load_data()
    total_users = len(data['users'])
    total_orders = len(data['orders'])
    total_payments = sum(p['amount'] for p in data['payments'].values() if p['status'] == 'paid')
    
    admin_text = (
        f"👑 <b>Панель администратора</b>\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🛒 Заказов: {total_orders}\n"
        f"💰 Общая сумма платежей: {total_payments:.2f} RUB\n\n"
        f"📊 Курс USDT: {usdt_rate:.2f} RUB"
    )
    
    await update.message.reply_text(
        text=admin_text,
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup(
            [
                ["Статистика", "Пользователи"],
                ["Заказы", "Платежи"],
                ["Изменить курс", "Назад в меню"]
            ],
            resize_keyboard=True
        )
    )

@catch_errors
async def admin_change_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Изменение курса вручную"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Доступ запрещен.")
        return
    
    await update.message.reply_text(
        text=f"Текущий курс USDT: {usdt_rate:.2f} RUB\n\n"
             "Введите новый курс:",
        reply_markup=ReplyKeyboardMarkup(
            [["Отмена"]],
            resize_keyboard=True
        )
    )
    return ADMIN_BALANCE_CHANGE

@catch_errors
async def admin_set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка нового курса"""
    try:
        new_rate = float(update.message.text)
        if new_rate <= 0:
            raise ValueError
        
        global usdt_rate
        usdt_rate = new_rate
        
        # Сохраняем в базу
        data = load_data()
        data['system']['usdt_rate'] = str(new_rate)
        save_data(data)
        
        await update.message.reply_text(
            text=f"✅ Курс USDT обновлен: {new_rate:.2f} RUB",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["Статистика", "Пользователи"],
                    ["Заказы", "Платежи"],
                    ["Изменить курс", "Назад в меню"]
                ],
                resize_keyboard=True
            )
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите корректное число:",
            reply_markup=ReplyKeyboardMarkup(
                [["Отмена"]],
                resize_keyboard=True
            )
        )
        return ADMIN_BALANCE_CHANGE

import os
import socket
import sys
from flask import Flask
import psutil
import signal

def kill_process_on_port(port):
    """Находит и завершает процесс, занимающий указанный порт"""
    for proc in psutil.process_iter(['pid', 'name', 'connections']):
        try:
            for conn in proc.connections():
                if conn.laddr.port == port:
                    print(f"Найден процесс {proc.pid} на порту {port}, завершаю...")
                    os.kill(proc.pid, signal.SIGTERM)
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, AttributeError):
            continue
    return False

def run_web_server():
    """Запуск Flask-сервера для Render с принудительным освобождением порта"""
    app = Flask(name)

    @app.route('/')
    def home():
        return "Telegram Bot is running!", 200

    @app.route('/health')
    def health():
        try:
            load_data()
            return "OK", 200
        except Exception as e:
            return f"Error: {str(e)}", 500

    port = int(os.environ.get("PORT", 8000))
    
    # Попытка освободить порт
    if kill_process_on_port(port):
        print(f"Процесс на порту {port} был завершен")
    
    try:
        # Проверка доступности порта
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('0.0.0.0', port))
        
        # Если порт свободен, запускаем сервер
        print(f"Запуск сервера на порту {port}")
        app.run(host='0.0.0.0', port=port)
        
    except OSError as e:
        print(f"Ошибка: {e}")
        print("Попытка завершить конфликтующий процесс...")
        
        if kill_process_on_port(port):
            print("Повторная попытка запуска сервера...")
            app.run(host='0.0.0.0', port=port)
        else:
            print("Не удалось освободить порт. Используйте другой порт.")
            sys.exit(1)

# =============================================
# ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА
# =============================================

def main():
    """Основная функция запуска бота"""
    init_db()
    
    # Запуск веб-сервера в отдельном потоке для Koyeb
    Thread(target=run_web_server, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))

    # Добавление обработчиков сообщений
    application.add_handler(MessageHandler(filters.Text(["Мой профиль"]), show_profile))
    application.add_handler(MessageHandler(filters.Text(["Помощь"]), show_help))
    application.add_handler(MessageHandler(filters.Text(["Сделать заказ"]), choose_platform))
    application.add_handler(MessageHandler(filters.Text(["Пополнить баланс"]), topup_balance))
    application.add_handler(MessageHandler(filters.Text(["Назад в меню"]), back_to_menu))
    application.add_handler(MessageHandler(filters.Text(["Twitch", "YouTube", "Kick"]), get_platform))
    application.add_handler(MessageHandler(filters.Text(["Админ панель"]), admin_panel))

    # Добавление обработчиков callback-запросов
    application.add_handler(CallbackQueryHandler(process_crypto_payment, pattern='^pay_crypto$'))
    application.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    application.add_handler(CallbackQueryHandler(back_to_platforms, pattern='^back_to_platforms$'))
    application.add_handler(CallbackQueryHandler(back_to_services, pattern='^back_to_services$'))
    application.add_handler(CallbackQueryHandler(back_to_channel, pattern='^back_to_channel$'))
    application.add_handler(CallbackQueryHandler(back_to_calendar, pattern='^back_to_calendar$'))
    application.add_handler(CallbackQueryHandler(back_to_time, pattern='^back_to_time$'))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern='^cancel_order$'))
    application.add_handler(CallbackQueryHandler(cancel_payment, pattern='^cancel_payment$'))
    application.add_handler(CallbackQueryHandler(confirm_order, pattern='^confirm_order$'))
    application.add_handler(CallbackQueryHandler(change_order, pattern='^change_order$'))
    application.add_handler(CallbackQueryHandler(check_payment_status, pattern='^check_payment_'))
    application.add_handler(CallbackQueryHandler(ask_channel, pattern='^service_'))

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
            GET_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_duration),
                CallbackQueryHandler(get_duration, pattern='^duration_')
            ],
            CONFIRM_ORDER: [CallbackQueryHandler(confirm_order, pattern='^confirm_order$')]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_order, pattern='^cancel_order$'),
            CommandHandler("start", start)
        ],
        allow_reentry=True
    )
    application.add_handler(order_conv)

    # Conversation handler для админ-панели
    admin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["Изменить курс"]), admin_change_rate)],
        states={
            ADMIN_BALANCE_CHANGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_rate)]
        },
        fallbacks=[
            CommandHandler("admin", admin_panel),
            CommandHandler("start", start)
        ]
    )
    application.add_handler(admin_conv)

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
