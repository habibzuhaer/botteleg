import ccxt
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# Настройка логирования для Termux
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Загрузка переменных окружения
load_dotenv()

# ================== НАСТРОЙКИ ==================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY', '')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET', '')

# Упрощенный список торговых пар для Termux (меньше нагрузка)
SYMBOLS = {
    'SUI': 'SUI/USDT:USDT',
    'ETH': 'ETH/USDT:USDT',
    'BNB': 'BNB/USDT:USDT',
    'SOL': 'SOL/USDT:USDT',
    'SEI': 'SEI/USDT:USDT',
    'ADA': 'ADA/USDT:USDT',
    'JUP': 'JUP/USDT:USDT',
}

TIMEFRAME = '15m'
MIN_PERCENT_CHANGE = 1.67
CHECK_INTERVAL = 60  # Увеличиваем интервал проверки для Termux
MIN_MESSAGE_INTERVAL = 300  # 5 минут между похожими сообщениями
# ===============================================

# Инициализация биржи с настройками для Termux
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
        'adjustForTimeDifference': True,  # Важно для Termux
        'recvWindow': 10000,  # Увеличиваем окно получения
    },
    'timeout': 30000,  # Увеличиваем таймаут
})

# Хранилище последних сообщений
message_history = {}
last_checked_timestamps = {symbol: 0 for symbol in SYMBOLS.values()}


def format_time(dt):
    return dt.strftime('%Y-%m-%d %H:%M') + ' UTC'


def is_similar(candle1, candle2):
    """Проверка схожести двух свечей"""
    if candle1 is None or candle2 is None:
        return False

    change_diff = abs(candle1['change'] - candle2['change']) / max(abs(candle1['change']), 1)
    volume_diff = abs(candle1['volume'] - candle2['volume']) / max(candle1['volume'], 1)

    return (change_diff < 0.2 and volume_diff < 0.3)


def is_candle_closed(candle_timestamp):
    """Проверяет, закрыта ли свеча по текущему времени"""
    now = datetime.now(timezone.utc).timestamp() * 1000
    candle_end_time = candle_timestamp + 15 * 60 * 1000
    return now > candle_end_time


async def get_significant_candle(symbol):
    """Получение последней значительной свечи с проверкой частоты"""
    try:
        # Синхронизируем время перед запросом
        exchange.load_time_difference()
        
        # Получаем только 2 последние свечи
        candles = await asyncio.to_thread(
            exchange.fetch_ohlcv, symbol, TIMEFRAME, limit=2
        )

        if len(candles) < 2:
            return None

        last_closed_candle = candles[-2]
        candle_timestamp = last_closed_candle[0]

        if candle_timestamp <= last_checked_timestamps[symbol]:
            return None

        last_checked_timestamps[symbol] = candle_timestamp

        if not is_candle_closed(candle_timestamp):
            return None

        change = (last_closed_candle[4] - last_closed_candle[1]) / last_closed_candle[1] * 100

        if abs(change) < MIN_PERCENT_CHANGE:
            return None

        candle_data = {
            'symbol': symbol,
            'time': datetime.fromtimestamp(candle_timestamp / 1000, timezone.utc),
            'open': last_closed_candle[1],
            'high': last_closed_candle[2],
            'low': last_closed_candle[3],
            'close': last_closed_candle[4],
            'change': change,
            'volume': last_closed_candle[5],
            'timestamp': candle_timestamp
        }

        now = datetime.now(timezone.utc)
        last_message = message_history.get(symbol)

        if last_message:
            time_diff = (now - last_message['time']).total_seconds()
            if time_diff < MIN_MESSAGE_INTERVAL and is_similar(last_message['candle'], candle_data):
                return None

        return candle_data

    except Exception as e:
        logging.error(f"Ошибка получения данных для {symbol}: {str(e)}")
        return None


def create_keyboard():
    """Создает клавиатуру в несколько рядов"""
    rows = []
    symbols_list = list(SYMBOLS.items())

    for i in range(0, len(symbols_list), 3):  # Меньше кнопок в ряду для мобильных устройств
        row = [
            InlineKeyboardButton(name, callback_data=f"req_{symbol}")
            for name, symbol in symbols_list[i:i + 3]
        ]
        rows.append(row)

    rows.append([InlineKeyboardButton("Меню", callback_data="menu")])

    return InlineKeyboardMarkup(rows)


async def send_candle_message(chat_id, candle_data, application=None, is_update=False):
    """Отправка/обновление сообщения о свече"""
    if not candle_data:
        return

    symbol = candle_data['symbol']
    direction = "🟢" if candle_data['change'] >= 0 else "🔴"
    symbol_name = symbol.split('/')[0]

    message = (
        f"<b>{direction} {symbol_name} {abs(candle_data['change']):.2f}%</b>\n"
        f"┌ Время: <i>{format_time(candle_data['time'])}</i>\n"
        f"├ Цена: <b>{candle_data['close']:.4f}</b>\n"
        f"├ Объем: {candle_data['volume']:.2f} USDT\n"
        f"└ Диапазон: {candle_data['low']:.4f}-{candle_data['high']:.4f}"
    )

    message_history[symbol] = {
        'time': datetime.now(timezone.utc),
        'candle': candle_data
    }

    if not application:
        return

    try:
        if is_update and symbol in message_history and 'message_id' in message_history[symbol]:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_history[symbol]['message_id'],
                text=message,
                parse_mode='HTML',
                reply_markup=create_keyboard()
            )
        else:
            sent = await application.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode='HTML',
                reply_markup=create_keyboard()
            )
            message_history[symbol]['message_id'] = sent.message_id
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {str(e)}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start"""
    await update.message.reply_text(
        text="📊 Выберите пару для запроса последней крупной свечи (>1.67%):",
        reply_markup=create_keyboard()
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопок"""
    query = update.callback_query
    await query.answer()

    if query.data.startswith('req_'):
        symbol = query.data.split('_')[1]
        candle_data = await get_significant_candle(symbol)

        if not candle_data:
            symbol_name = symbol.split('/')[0]
            await query.edit_message_text(
                text=f"❌ Для {symbol_name} нет свечей >{MIN_PERCENT_CHANGE}%",
                reply_markup=create_keyboard()
            )
            return

        await send_candle_message(
            chat_id=query.message.chat_id,
            candle_data=candle_data,
            application=context.application,
            is_update=True
        )
    elif query.data == "menu":
        await query.edit_message_text(
            text="📊 Выберите пару для запроса последней крупной свечи (>1.67%):",
            reply_markup=create_keyboard()
        )


async def check_market_updates(application):
    """Проверка обновлений рынка с защитой от дублирования"""
    while True:
        try:
            for symbol in SYMBOLS.values():
                candle_data = await get_significant_candle(symbol)
                if candle_data:
                    await send_candle_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        candle_data=candle_data,
                        application=application,
                        is_update=True
                    )
                await asyncio.sleep(2)  # Небольшая задержка между запросами
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logging.error(f"Ошибка в check_market_updates: {str(e)}")
            await asyncio.sleep(60)  # Пауза при ошибке


async def init_bot(application):
    """Инициализация бота"""
    try:
        await application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🔔 Бот активирован в Termux | TF: {TIMEFRAME} | >{MIN_PERCENT_CHANGE}%"
        )
        asyncio.create_task(check_market_updates(application))
    except Exception as e:
        logging.error(f"Ошибка инициализации бота: {str(e)}")


def run_bot():
    """Запуск бота"""
    try:
        app = ApplicationBuilder() \
            .token(TELEGRAM_BOT_TOKEN) \
            .post_init(init_bot) \
            .build()

        app.add_handler(CommandHandler('start', start))
        app.add_handler(CallbackQueryHandler(handle_button))

        print(f"Бот запущен в Termux. Минимальное изменение: {MIN_PERCENT_CHANGE}%")
        print("Для остановки нажмите Ctrl+C")
        
        # Упрощенный запуск для Termux
        app.run_polling(
            poll_interval=1.0,
            timeout=10,
            drop_pending_updates=True
        )
    except Exception as e:
        logging.error(f"Ошибка запуска бота: {str(e)}")
    finally:
        print("Бот остановлен")


if __name__ == "__main__":
    run_bot()
