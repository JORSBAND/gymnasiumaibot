import os
import asyncio
import uuid
import json
import logging
# import time # ВИДАЛЕНО, використовуємо asyncio.sleep
from datetime import datetime, time as dt_time
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)
# Не забудьте встановити: pip install python-telegram-bot google-generativeai requests beautifulsoup4 pytz aiohttp gspread oauth2client
import requests
from bs4 import BeautifulSoup
import pytz
from typing import Any, Callable, Dict
import re
import hashlib
import gspread # ДОДАНО: Google Sheets API
from oauth2client.service_account import ServiceAccountCredentials # ДОДАНО: Для авторизації
# --- Web App мінімальні імпорти для Render (залишено для імітації відкритого порту) ---
from aiohttp import web

# --- Налаштування ---
# !!! ВАЖЛИВО: Замініть "YOUR_NEW_TELEGRAM_BOT_TOKEN_HERE" на ваш дійсний токен Telegram !!!
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8223675237:AAF_kmo6SP4XZS23NeXWFxgkQNUaEZOWNx0")
# !!! КРИТИЧНО: Переконайтеся, що всі ключі Gemini дійсні та мають активний баланс! !!!
GEMINI_API_KEYS_STR = os.environ.get("GEMINI_API_KEYS", "AIzaSyAixFLqi1TZav-zeloDyz3doEc6awxrbU,AIzaSyARQhOvxTxLUUKc0f370d5u4nQAmQPiCYA,AIzaSyA6op6ah5PD5U_mICb_QXY_IH-3RGVEwEs")
GEMINI_API_KEYS = [key.strip() for key in GEMINI_API_KEYS_STR.split(',') if key.strip()]
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "238b1178c9612fc52ccb303667c92687")
# !!! КРИТИЧНО: Токен Cloudflare не працює (401). Перевірте токен Cloudflare! !!!
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "v6HjMgCHEqTiElwnW_hK73j1uqQKud1fG-rPInWD")
STABILITY_AI_API_KEY = os.environ.get("STABILITY_AI_API_KEY", "sk-uDtr8UAPxC7JHLG9QAyXt9s4QY142fkbOQA7uZZEgjf99iWp")

# ВАЖЛИВО: Встановіть URL вашого сервісу Render
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://gymnasiumaibot.onrender.com/")
WEBHOOK_PATH = f"/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_URL = RENDER_EXTERNAL_URL.rstrip('/') + WEBHOOK_PATH

ADMIN_IDS = [
    838464083,
    6484405296,
    1374181841,
    5268287971,
]
GYMNASIUM_URL = "https://brodygymnasium.e-schools.info"
TARGET_CHANNEL_ID = -1002946740131
ADMIN_CONTACTS_FILE = 'admin_contacts.json'
CONVERSATIONS_FILE = 'conversations.json'
SCHEDULED_POSTS_FILE = 'scheduled_posts.json'
KNOWLEDGE_BASE_FILE = 'knowledge_base.json' # Локальний кеш бази знань
USER_IDS_FILE = 'user_ids.json' # Локальний кеш ID користувачів

# --- НАЛАШТУВАННЯ GOOGLE SHEETS (КРИТИЧНО) ---
# Назва вашої Google Таблиці
GSHEET_NAME = os.environ.get("GSHEET_NAME", "Бродівська гімназія - База Знань")
# Назва листа (вкладки) у таблиці для Бази Знань
GSHEET_WORKSHEET_NAME = os.environ.get("GSHEET_WORKSHEET_NAME", "База_Знань")
# Назва листа (вкладки) у таблиці для Користувачів
USERS_GSHEET_WORKSHEET_NAME = os.environ.get("USERS_GSHEET_WORKSHEET_NAME", "Користувачі")
# JSON-ключі сервісного облікового запису (як змінна оточення)
GCP_CREDENTIALS_JSON = os.environ.get("GCP_CREDENTIALS_JSON", "{}") 

# --- КЛЮЧІ ДЛЯ БАЗИ ЗНАНЬ ---
KB_KEY_QUESTION = "Питання"
KB_KEY_ANSWER = "Відповідь"
# НОВИЙ КЛЮЧ: Використовується для позначення запису як FAQ (будь-яке значення, окрім пустого)
KB_KEY_IS_FAQ = "FAQ" 
# --- Кінець налаштувань ---

# Логування
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- СТАНИ ДЛЯ CONVERSATIONHANDLER (ПОВНИЙ СПИСОК) ---
# ВИПРАВЛЕНО: Видалено зайву ** перед WAITING_FOR_ADMIN_MESSAGE
(SELECTING_CATEGORY, IN_CONVERSATION, WAITING_FOR_REPLY,
 WAITING_FOR_ANONYMOUS_MESSAGE, WAITING_FOR_ANONYMOUS_REPLY,
 WAITING_FOR_BROADCAST_MESSAGE, CONFIRMING_BROADCAST,
 WAITING_FOR_KB_KEY, WAITING_FOR_KB_VALUE, CONFIRMING_AI_REPLY,
 WAITING_FOR_NEWS_TEXT, CONFIRMING_NEWS_ACTION, WAITING_FOR_MEDIA,
 SELECTING_TEST_USER, WAITING_FOR_TEST_NAME, WAITING_FOR_TEST_ID,
 WAITING_FOR_TEST_MESSAGE, WAITING_FOR_KB_EDIT_VALUE,
 WAITING_FOR_SCHEDULE_TEXT, WAITING_FOR_SCHEDULE_TIME, CONFIRMING_SCHEDULE_POST,
 WAITING_FOR_ADMIN_MESSAGE) = range(22) 
# --- GOOGLE SHEETS УТИЛІТИ ---

GSHEET_SCOPE = [
    'https://spreadsheets.google.com/feeds', 
    'https://www.googleapis.com/auth/drive'
]

def get_gsheet_client(worksheet_name: str):
    """Створює та повертає gspread клієнт для взаємодії з конкретним листом."""
    try:
        # Для коректного парсингу
        creds_dict = json.loads(GCP_CREDENTIALS_JSON)
        if not creds_dict or "private_key" not in creds_dict:
            logger.error(f"GCP_CREDENTIALS_JSON порожній або невірний. Неможливо підключитися до Google Sheets (лист: {worksheet_name}).")
            return None
            
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, GSHEET_SCOPE)
        client = gspread.authorize(creds)
        
        # Відкриття таблиці
        sheet = client.open(GSHEET_NAME)
        # Отримання робочого листа за назвою
        worksheet = sheet.worksheet(worksheet_name)
        return worksheet
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"Таблиця Google з назвою '{GSHEET_NAME}' не знайдена.")
        return None
    except gspread.exceptions.WorksheetNotFound:
        logger.error(f"Лист Google з назвою '{worksheet_name}' не знайдений у таблиці '{GSHEET_NAME}'.")
        return None
    except Exception as e:
        logger.error(f"Помилка ініціалізації GSheet Client (лист: {worksheet_name}): {e}")
        return None

def save_data_to_gsheet(kb_data: Dict[str, dict]) -> bool:
    """Зберігає поточну базу знань у Google Sheets."""
    worksheet = get_gsheet_client(GSHEET_WORKSHEET_NAME)
    if not worksheet:
        logger.error("Не вдалося отримати клієнт Google Sheets для збереження KB.")
        return False
    
    try:
        # Перетворюємо словник у список списків [[KB_KEY_QUESTION, KB_KEY_ANSWER, KB_KEY_IS_FAQ], ...]
        records = [[KB_KEY_QUESTION, KB_KEY_ANSWER, KB_KEY_IS_FAQ]] # Заголовок
        
        for key, data in kb_data.items():
            records.append([
                key,
                data.get(KB_KEY_ANSWER, ''),
                data.get(KB_KEY_IS_FAQ, '')
            ])
        
        # Очищуємо весь лист і завантажуємо нові дані
        worksheet.batch_clear(["A1:Z1000"]) 
        worksheet.update('A1', records)
        logger.info(f"✅ Успішно збережено {len(kb_data)} записів у Google Sheets (KB).")
        return True
    except Exception as e:
        logger.error(f"Помилка запису KB в Google Sheets: {e}")
        return False

def save_users_to_gsheet(users: list[dict]) -> bool:
    """Зберігає список користувачів та їхні дані у Google Sheets (лист Користувачі)."""
    worksheet = get_gsheet_client(USERS_GSHEET_WORKSHEET_NAME)
    if not worksheet:
        logger.error("Не вдалося отримати клієнт Google Sheets для збереження користувачів.")
        return False

    try:
        # Форматуємо дані: [["ID", "Ім'я користувача", "Дата останнього запуску"], ...]
        records = [["ID", "Ім'я користувача", "Дата останнього запуску"]] 
        
        for user in users:
            # === ВИПРАВЛЕННЯ: Додано перевірку, чи є елемент словником ===
            if not isinstance(user, dict):
                 logger.warning(f"Пропущено невірний елемент користувача (не словник) при записі у Sheets: {user}")
                 continue
            # ==========================================================

            records.append([
                user.get('id', ''),
                user.get('username', user.get('full_name', 'N/A')),
                user.get('last_run', '')
            ])
        
        # Очищуємо весь лист і завантажуємо нові дані
        worksheet.batch_clear(["A1:C1000"])
        worksheet.update('A1', records)
        logger.info(f"✅ Успішно збережено {len(users)} записів користувачів у Google Sheets.")
        return True
    except Exception as e:
        logger.error(f"Помилка запису користувачів в Google Sheets: {e}")
        return False

def fetch_kb_from_sheets() -> Dict[str, dict] | None:
    """
    Завантажує базу знань із Google Sheets. 
    Очікує: [Питання, Відповідь, FAQ]
    Повертає: {'Питання': {'Відповідь': 'текст', 'FAQ': 'значення'}, ...}
    """
    worksheet = get_gsheet_client(GSHEET_WORKSHEET_NAME)
    if not worksheet:
        return None 
    
    try:
        list_of_lists = worksheet.get_all_values()
        
        if not list_of_lists or len(list_of_lists) < 2:
            logger.warning("Google Sheets (KB) порожній або містить лише заголовок.")
            return {}

        # Ідентифікуємо індекси стовпців за заголовками
        header = [h.strip() for h in list_of_lists[0]]
        q_idx = header.index(KB_KEY_QUESTION) if KB_KEY_QUESTION in header else 0
        a_idx = header.index(KB_KEY_ANSWER) if KB_KEY_ANSWER in header else 1
        faq_idx = header.index(KB_KEY_IS_FAQ) if KB_KEY_IS_FAQ in header else -1 # -1, якщо FAQ стовпця немає
        
        data_rows = list_of_lists[1:]
        kb = {}
        for row in data_rows:
            if len(row) > q_idx and row[q_idx].strip():
                question = row[q_idx].strip()
                answer = row[a_idx].strip() if len(row) > a_idx else ""
                is_faq = row[faq_idx].strip() if faq_idx >= 0 and len(row) > faq_idx else ""
                
                kb[question] = {
                    KB_KEY_ANSWER: answer,
                    KB_KEY_IS_FAQ: is_faq
                }
        
        logger.info(f"✅ Успішно завантажено {len(kb)} записів із Google Sheets (KB).")
        return kb

    except Exception as e:
        logger.error(f"Помилка читання KB з Google Sheets: {e}")
        return None
# --- КІНЕЦЬ GOOGLE SHEETS УТИЛІТ ---

# --- Функція для отримання початкових даних бази знань (резерв) ---
def get_default_knowledge_base() -> Dict[str, dict]:
    """Повертає початковий вміст бази знань у форматі 'ключ: значення'."""
    return {
        "Хто є директор школи?": {KB_KEY_ANSWER: "Директор школи: Кіт Ярослав Ярославович. Телефон: +380976929979", KB_KEY_IS_FAQ: "x"},
        "Контактні дані школи": {KB_KEY_ANSWER: (
            "Офіційне найменування: Бродівська гімназія імені Івана Труша Бродівської міської ради Львівської області. "
            "Тип: Установа загальної середньої освіти. "
            "Адреса: 80600, м. Броди, вул. Коцюбинського, 2. "
            "Телефон директора: +3803266 27991. E-mail: brodyg@ukr.net"
        ), KB_KEY_IS_FAQ: ""},
        "Хто є адміністратором?": {KB_KEY_ANSWER: "Вам відповідає Штучний Інтелект. Наразі адміністратор, який модерує цього бота, не оголошений.", KB_KEY_IS_FAQ: "x"},
        "Розклад уроків": {KB_KEY_ANSWER: "Інформація коригується. На жаль, наразі не можемо надати розклад уроків, оскільки він не є сталим і ще коригується. Як тільки буде затверджено стабільний розклад, ми зможемо його надіслати.", KB_KEY_IS_FAQ: ""},
        "Хто є в складі адміністрації школи?": {KB_KEY_ANSWER: (
            "Адміністрація: Директор: Кіт Ярослав Ярославович. "
            "Заступники директора (завучі): Губач Оксана Богданівна, Демидчук Оксана Андріївна, Янчук Галина Ярославівна."
        ), KB_KEY_IS_FAQ: ""},
        "Вчителі (повний список за предметами)": {KB_KEY_ANSWER: (
            "Інформатика: Крутяк Назарій Олегович, Янчук Роман Володимирович. "
            "Фізична культура та Захист Вітчизни: Кіт Ярослав Ярославович, Рак Мар'ян Володимирович. "
            "Фізика: Данчук Валентина Володимирівна, Мартинюк Ігор Степанович. "
            "Німецька мова: Гончар Іванна Богданівна, Ковальчук Ольга Михайлівна. "
            "Хімія: Дудчак Ганна Миколаївна, Типусяк Наталія Петрівна. "
            "Мистецтво та Технології: Гілевич Ганна Іванівна, Шнайдрук Галина Степанівна. "
            "Українська мова та література: Булишин Богдана Павлівна, Гаврікова Наталія Володимирівна, Демидчук Оксана Андріївна, Паськів Ірина Василівна, Стрільчук Ірина Петрівна. "
            "Англійська мова: Лисик Галина Іванівна, Пащук Оксана Луківна, Стеблій Оксана Петрівна. "
            "Математика: Вороняк Галина Ярославівна, Губач Оксана Богданівна, Надорожняк Наталія Миронівна, Паньків Галина Йосипівна, Янчук Галина Ярославівна. "
            "Історія: Авдєєнко Тетяна Петрівна, Дискант Марія Петрівна, Корчак Андрій Михайлович, Мельник Тарас Юрійович. "
            "Суспільний цикл: Кашуба Ірина Данилівна, Климко Валентина Володимирівна, Козіцька Тетяна Володимирівна, Корольчук Ірина Іванівна, Корчак Оксана Євгенівна. "
            "Біологія та географія: Білостоцька Ірина Богданівна, Демчинська Галина Орестівна, Неверенчук Марія Іванівна, Підгурська Ірина Богданівна."
        ), KB_KEY_IS_FAQ: ""},
        "Що зроблено у гімназії (проєкти)?": {KB_KEY_ANSWER: (
            "1997-2007: Програма вивчення німецької мови (OeAD). 2001-2005: IREX/IATP «Віртуальний центр з громадянської освіти». "
            "2003-2015: Пілотна школа програми «Школа як осередок розвитку громади» («Крок за кроком»). 2003: Проєкт \"Інтернет для сільських шкіл\" (посольство Канади). "
            "2004-2006: «Ми є одна громада» (програма малих грантів). 2005 - сьогодення: Обмінні проєкти з польськими школами-партнерами. "
            "2017-2021: Обмінні проекти зі школою м. Зіген. 2019-2021: Учасник проєкту «Ми будуємо спільноту...» (RITA). "
            "2017-2021: Спільні проєкти з Білокуракинським ліцеєм №1 («Змінимо країну разом»). 2021: Пілотна школа проєкту «SELFIE». "
            "2021: Учасники проєкту 'MOODLE – це про100'. 2021: Виконавець проєкту 'Ватра-фест - 2021'."
        ), KB_KEY_IS_FAQ: ""},
        "Важливі посилання": {KB_KEY_ANSWER: "Telegram-канал: https://t.me/+2NB0puCLx6o5NDk6. Офіційний сайт: https://brodygymnasium.e-schools.info/", KB_KEY_IS_FAQ: "x"}
    }

# --- Утиліти для збереження/зчитування JSON ---
def load_data(filename: str, default_type: Any = None) -> Any:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if filename == KNOWLEDGE_BASE_FILE and not data:
                raise json.JSONDecodeError("Local KB is empty or corrupted, forcing reload.", f.name, 0)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        if filename == KNOWLEDGE_BASE_FILE:
            # 1. Спроба завантажити з Google Sheets
            kb_from_sheets = fetch_kb_from_sheets()
            
            # 2. Якщо завантаження з Sheets не вдалося або воно порожнє, використовуємо резерв
            if kb_from_sheets is None or not kb_from_sheets:
                logger.warning("Завантаження з Google Sheets не вдалося або порожнє. Використовуються дані за замовчуванням.")
                kb_data = get_default_knowledge_base()
            else:
                kb_data = kb_from_sheets

            # 3. Зберігаємо локально для кешування та подальших модифікацій
            save_data(kb_data, filename)
            return kb_data
        
        if filename == USER_IDS_FILE:
             return []
            
        if default_type is not None:
            return default_type
        return {}

def save_data(data: Any, filename: str) -> None:
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        loop = asyncio.get_running_loop()
        
        # Якщо ми зберігаємо базу знань, синхронізуємо її з Google Sheets
        if filename == KNOWLEDGE_BASE_FILE:
            # Запускаємо синхронну функцію в окремому потоці
            asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(save_data_to_gsheet, data),
                loop
            )
        # Якщо ми зберігаємо список користувачів, синхронізуємо його з Google Sheets
        if filename == USER_IDS_FILE:
             asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(save_users_to_gsheet, data),
                loop
            )
            
    except Exception as e:
        logger.error(f"Помилка save_data({filename}): {e}")

async def send_telegram_reply(ptb_app: Application, user_id: int, text: str):
    """Надсилає відповідь користувачу та зберігає її в історію розмов (тільки Telegram)."""
    conversations = load_data(CONVERSATIONS_FILE, {})
    user_id_str = str(user_id)
    
    if not isinstance(user_id, int): 
         logger.warning(f"Спроба відправити відповідь користувачу з не-int ID: {user_id}. Пропущено.")
         return

    if user_id_str not in conversations: conversations[user_id_str] = []
    conversations[user_id_str].append({"sender": "bot", "text": text, "timestamp": datetime.now().isoformat()})
    save_data(conversations, CONVERSATIONS_FILE)

    try:
        await ptb_app.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
        logger.info(f"Надіслано відповідь через Telegram користувачу {user_id}")
    except Exception as e:
         logger.error(f"Не вдалося надіслати в Telegram користувачу {user_id}: {e}")

def update_user_list(user_id: int, username: str | None, first_name: str | None, last_name: str | None):
    """Додає або оновлює користувача у списку для статистики."""
    user_data = load_data(USER_IDS_FILE) # Це список словників
    
    full_name = ' '.join(filter(None, [first_name, last_name]))
    
    # Виправляємо проблему: якщо load_data повернув старий формат [1, 2, 3...], 
    # він буде мігрувати його в main(), але тут він може знову прочитати старий кеш.
    # Ми перевіряємо, чи елемент є словником, перш ніж викликати .get()
    
    found = False
    for i, user_item in enumerate(user_data):
        if isinstance(user_item, dict) and user_item.get('id') == user_id:
            # Знайдено: оновлюємо дані
            user_data[i]['username'] = username or user_data[i].get('username')
            user_data[i]['full_name'] = full_name
            user_data[i]['last_run'] = datetime.now(pytz.timezone("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")
            found = True
            break
            
    if not found:
        # Додаємо нового користувача
        new_user = {
            'id': user_id,
            'username': username,
            'full_name': full_name,
            'last_run': datetime.now(pytz.timezone("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")
        }
        user_data.append(new_user)
        logger.info(f"Новий користувач додано: {user_id} ({full_name})")

    save_data(user_data, USER_IDS_FILE) # Зберігаємо локально і синхронізуємо з Sheets

# --- Основна логіка бота (заповнення пропущених функцій) ---

# Використання генерації тексту з експоненційним відступом
async def generate_text_with_fallback(prompt: str) -> str | None:
    # --- Спроба 1: Gemini API ---
    for api_key in GEMINI_API_KEYS:
        for attempt in range(3): # 3 спроби на ключ
            try:
                logger.info(f"Спроба {attempt+1} з Gemini API ключем ...{api_key[-4:]}")
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-2.5-flash') 
                response = await asyncio.to_thread(model.generate_content, prompt, request_options={'timeout': 45})
                if response.text:
                    logger.info("Успішна відповідь від Gemini.")
                    return response.text
            except Exception as e:
                logger.warning(f"Gemini ключ ...{api_key[-4:]} не спрацював на спробі {attempt+1}: {e}")
                await asyncio.sleep(2 ** attempt) # Експоненційний відступ
                continue
        
    # --- Спроба 2: Cloudflare AI ---
    logger.warning("Усі ключі Gemini не спрацювали. Переходжу до Cloudflare AI.")
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN or "your_cf" in CLOUDFLARE_ACCOUNT_ID:
        logger.error("Не налаштовано дані для Cloudflare AI.")
        return None

    for attempt in range(3):
        try:
            cf_url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-2-7b-chat-int8"
            headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
            payload = {"messages": [{"role": "user", "content": prompt}]}
            response = await asyncio.to_thread(
                requests.post, cf_url, headers=headers, json=payload, timeout=45
            )
            response.raise_for_status()
            result = response.json()
            cf_text = result.get("result", {}).get("response")
            if cf_text:
                logger.info("Успішна відповідь від Cloudflare AI.")
                return cf_text
            else:
                logger.error(f"Cloudflare AI повернув порожню відповідь: {result}")
                return None
        except Exception as e:
            logger.error(f"Резервний варіант Cloudflare AI не спрацював на спробі {attempt+1}: {e}")
            await asyncio.sleep(2 ** attempt)
            continue
    
    logger.error("Усі спроби генерації тексту ШІ не вдалися.")
    return None

async def generate_image(prompt: str) -> bytes | None:
    api_url = "https://api.stability.ai/v2beta/stable-image/generate/core"
    headers = {
        "authorization": f"Bearer {STABILITY_AI_API_KEY}",
        "accept": "image/*"
    }
    data = {
        "prompt": f"Minimalistic, symbolic, modern vector illustration for a school news article. The theme is: '{prompt}'. No text on the image, clean style.",
        "output_format": "jpeg",
        "aspect_ratio": "1:1"
    }
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(
                requests.post,
                api_url,
                headers=headers,
                files={"none": ''},
                data=data,
                timeout=30
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            logger.error(f"Помилка генерації зображення через Stability AI на спробі {attempt+1}: {e}")
            if e.response is not None:
                logger.error(f"Відповідь сервера: {e.response.text}")
            await asyncio.sleep(2 ** attempt)
            continue
        except Exception as e:
            logger.error(f"Невідома помилка при генерації зображення: {e}")
            await asyncio.sleep(2 ** attempt)
            continue
    return None

def get_all_text_from_website() -> str | None:
    try:
        url = GYMNASIUM_URL.rstrip('/') + "/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.extract()

        text = soup.body.get_text(separator='\n', strip=True)
        cleaned_text = re.sub(r'\n\s*\n', '\n\n', text)
        return cleaned_text if cleaned_text else None
    except requests.RequestException as e:
        logger.error(f"Помилка отримання даних з сайту: {e}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при парсингу сайту: {e}")
        return None

def get_teachers_info() -> str | None:
    try:
        url = "https://brodygymnasium.e-schools.info/teachers"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        content_area = soup.find('div', class_='content-inner')
        if content_area:
            for element in content_area(["script", "style"]):
                element.extract()
            text = content_area.get_text(separator='\n', strip=True)
            cleaned_text = re.sub(r'\n\s*\n', '\n', text)
            return cleaned_text
        return None
    except requests.RequestException as e:
        logger.error(f"Помилка отримання даних про вчителів: {e}")
        return None
    except Exception as e:
        logger.error(f"Невідома помилка при парсингу сторінки вчителів: {e}")
        return None
    
async def gather_all_context(query: str) -> str:
    teacher_keywords = ['вчител', 'викладач', 'директор', 'завуч']
    is_teacher_query = any(keyword in query.lower() for keyword in teacher_keywords)

    site_text_task = asyncio.to_thread(get_all_text_from_website)
    teachers_info_task = asyncio.to_thread(get_teachers_info) if is_teacher_query else asyncio.sleep(0, result=None)

    site_text, teachers_info = await asyncio.gather(site_text_task, teachers_info_task)

    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    relevant_kb_simple = {}
    if isinstance(kb, dict):
        qwords = set(query.lower().split())
        for q_key, data in kb.items():
            # Використовуємо тільки ключ і відповідь для пошуку
            full_text = f"{q_key} {data.get(KB_KEY_ANSWER, '')}".lower()
            if qwords & set(full_text.split()):
                relevant_kb_simple[q_key] = data.get(KB_KEY_ANSWER, '(Відповідь відсутня)')
        

    context_parts = []
    if teachers_info:
        context_parts.append(f"**Контекст зі сторінки вчителів:**\n{teachers_info[:2000]}")

    if site_text:
        context_parts.append(f"**Контекст з головної сторінки сайту:**\n{site_text[:2000]}")
    else:
        context_parts.append("**Контекст з сайту:**\nНе вдалося отримати.")

    if relevant_kb_simple:
        # === КРИТИЧНЕ ВИПРАВЛЕННЯ ДЛЯ ШІ: ФОРМАТУВАННЯ КОНТЕКСТУ ===
        # Замість простого dump JSON, ми даємо ШІ краще структурований текст,
        # щоб він міг його переписати, а не просто вивести.
        kb_text = "--- База знань (ФАКТИ) ---\n"
        for key, value in relevant_kb_simple.items():
            kb_text += f"- Заголовок: {key}\n  Дані: {value}\n"
            
        context_parts.append(kb_text)
    else:
        context_parts.append("**Контекст з бази даних:**\nНічого релевантного не знайдено.")

    return "\n\n".join(context_parts)

async def try_ai_autoreply(user_question: str) -> str | None:
    logger.info("Запускаю спробу авто відповіді ШІ...")
    
    additional_context = await gather_all_context(user_question)

    prompt = (
        "Ти — корисний та точний асистент для шкільного чату. "
        "Твоє завдання — відповісти на запитання користувача на основі наданого КОНТЕКСТУ. "
        "Якщо ти вважаєш, що можеш дати **конкретну, точну та корисну** відповідь (інформація знайдена в контексті), познач себе як CONFIDENT. "
        "Якщо інформації недостатньо, запитання вимагає людської уваги (скарга, пропозиція) або відповідь буде неконкретною, познач себе як UNCERTAIN.\n\n"
        "--- КОНТЕКСТ (з сайту та бази знань) ---\n"
        f"{additional_context}\n\n"
        "--- ЗАПИТАННЯ КОРИСТУВАЧА ---\n"
        f"'{user_question}'\n\n"
        "--- ІНСТРУКЦІЯ ---"
        "Якщо ти CONFIDENT, ти повинен **переформулювати та об'єднати** знайдені факти з 'Бази знань' та 'Контекту з сайту' у **плавний, природний текст**, уникаючи прямого цитування та стокових фраз. Відповідь має бути ввічливою українською мовою. "
        "Розпочни свою відповідь одним із двох токенів: [CONFIDENT] або [UNCERTAIN]. Після токена напиши саму відповідь. "
        "--- ТВОЯ ВІДПОВІДЬ (починається з [CONFIDENT] або [UNCERTAIN]) ---"
    )

    ai_raw_response = await generate_text_with_fallback(prompt)
    
    if ai_raw_response and ai_raw_response.strip().startswith('[CONFIDENT]'):
        reply_text = ai_raw_response.strip().replace('[CONFIDENT]', '', 1).strip()
        if reply_text:
            return reply_text
    
    return None


async def check_website_for_updates(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Виконую щоденну перевірку оновлень на сайті...")
    new_text = get_all_text_from_website()
    if not new_text:
        logger.info("Не вдалося отримати текст з сайту.")
        return

    last_check_data = load_data('website_content.json', {})
    previous_text = last_check_data.get('text', '')

    if new_text != previous_text:
        logger.info("Знайдено оновлення на сайті!")
        save_data({'text': new_text, 'timestamp': datetime.now().isoformat()}, 'website_content.json')
        await propose_website_update(context, new_text)
        
async def propose_website_update(context: ContextTypes.DEFAULT_TYPE, text_content: str):
    truncated_text = text_content[:800] + "..." if len(text_content) > 800 else text_content
    broadcast_id = f"website_update_{uuid.uuid4().hex[:8]}"
    context.bot_data[broadcast_id] = text_content

    keyboard = [
        [InlineKeyboardButton("Зробити розсилку 📢", callback_data=f"broadcast_website:{broadcast_id}")],
        [InlineKeyboardButton("Скасувати ❌", callback_data=f"cancel_website_update:{broadcast_id}")]
    ]
    message = f"**Знайдено оновлення на сайті!**\n\n**Новий вміст:**\n---\n{truncated_text}"

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Не вдалося надіслати оновлення сайту адміну {admin_id}: {e}")
            
async def website_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, broadcast_id = query.data.split(':', 1)

    if action == 'broadcast_website':
        full_text = context.bot_data.get(broadcast_id)
        if not full_text:
            await query.edit_message_text("Помилка: текст для розсилки застарів або не знайдено.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"📢 *Починаю розсилку оновлення з сайту...*")
        success, fail = await do_broadcast(context, text_content=full_text)
        await query.message.reply_text(f"✅ Розсилку оновлення завершено.\nНадіслано: {success}\nПомилок: {fail}")
    elif action == 'cancel_website_update':
        original_text = query.message.text
        new_text = f"{original_text}\n\n--- \n❌ **Скасовано.**"
        await query.edit_message_text(text=new_text, parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)

    if broadcast_id in context.bot_data:
        del context.bot_data[broadcast_id]
        
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return # Перевірка прав доступу
    
    keyboard = [
        [
            InlineKeyboardButton("Створити новину ✍️", callback_data="admin_create_news"),
            InlineKeyboardButton("Запланувати новину 🗓️", callback_data="admin_schedule_news")
        ],
        [
             InlineKeyboardButton("Заплановані пости 🕒", callback_data="admin_view_scheduled"),
             InlineKeyboardButton("Зробити розсилку 📢", callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("Внести дані в базу ✍️", callback_data="admin_kb_add"),
            InlineKeyboardButton("Перевірити базу знань 🔎", callback_data="admin_kb_view")
        ],
        [
            InlineKeyboardButton("Сповістити адмінів 🔔", callback_data="admin_notify_admins"), # НОВА КНОПКА
            InlineKeyboardButton("Статистика 📊", callback_data="admin_stats")
        ]
    ]
    await update.message.reply_text("🔐 **Адміністративна панель:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# НОВИЙ БЛОК: Сповіщення адмінів
async def start_notify_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    
    await query.edit_message_text(
        "Надішліть повідомлення (з текстом, фото або відео) для сповіщення інших адміністраторів.\n\n/cancel для скасування."
    )
    
    return WAITING_FOR_ADMIN_MESSAGE

async def receive_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sender_id = update.effective_user.id
    sender_name = get_admin_name(sender_id)
    message = update.message
    
    text = message.caption or message.text or ""
    photo = message.photo[-1].file_id if message.photo else None
    video = message.video.file_id if message.video else None

    if not (text or photo or video):
        await update.message.reply_text("Будь ласка, надішліть текст або медіафайл.")
        return WAITING_FOR_ADMIN_MESSAGE
        
    forward_text = f"🔔 **Сповіщення від адміністратора:**\n\n**Від:** {sender_name} (ID: {sender_id})\n\n**Текст:**\n---\n{text}"
    
    success_count = 0
    fail_count = 0
    
    for admin_id in ADMIN_IDS:
        if admin_id != sender_id: # Надсилаємо всім, крім відправника
            try:
                if photo:
                    await context.bot.send_photo(chat_id=admin_id, photo=photo, caption=forward_text, parse_mode='Markdown')
                elif video:
                    await context.bot.send_video(chat_id=admin_id, video=video, caption=forward_text, parse_mode='Markdown')
                else:
                    await context.bot.send_message(chat_id=admin_id, text=forward_text, parse_mode='Markdown')
                success_count += 1
            except Exception as e:
                logger.error(f"Не вдалося надіслати сповіщення адміну {admin_id}: {e}")
                fail_count += 1
                
    await update.message.reply_text(
        f"✅ Сповіщення надіслано.\nОтримано: {success_count} адмінами.\nПомилок: {fail_count}"
    )

    return ConversationHandler.END
# КІНЕЦЬ НОВОГО БЛОКУ

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return # Перевірка прав доступу
    
    info_text_1 = (
        "🔐 **Інструкція для Адміністратора**\n\n"
        "Ось повний перелік функцій та команд, доступних для вас:\n\n"
        "--- \n"
        "**Основні Команди**\n\n"
        "• `/admin` - Відкриває головну адміністративну панель.\n"
        "• `/info` - Показує цю інструкцію.\n"
        "• `/faq` - Показує список поширених запитань з бази знань.\n"
        "• `/testm` - Запускає процес створення тестового звернення для перевірки."
    )
    info_text_2 = (
        "--- \n"
        "**Взаємодія зі Зверненнями**\n\n"
        "Коли користувач надсилає повідомлення, ви отримуєте сповіщення з кнопками:\n\n"
        "• **Відповісти особисто ✍️**: Натисніть, щоб бот попросив вас ввести відповідь.\n"
        "• **Відповісти за допомогою ШІ 🤖**: Бот генерує відповідь на основі даних з сайту та бази знань. Вам буде показано попередній перегляд.\n"
        "• **Пряма відповідь (Reply)**: Використовуйте функцію \"Reply\" в Telegram на повідомленні від бота, і ваша відповідь буде автоматично перенаправлена користувачу.\n\n"
        "Коли один адмін відповідає, інші отримують сповіщення."
    )
    info_text_3 = (
        "--- \n"
        "**Функції Адмін-панелі (`/admin`)**\n\n"
        "• **Статистика 📊**: Кількість унікальних користувачів бота.\n"
        "• **Створити новину ✍️**: Створює пост для негайної розсилки.\n"
        "• **Запланувати новину 🗓️**: Створює пост для розсилки у вказаний час.\n"
        "• **Заплановані пости 🕒**: Показує список запланованих постів з можливістю їх скасування.\n"
        "• **Зробити розсилку 📢**: Швидкий спосіб надіслати текстове повідомлення всім користувачам.\n"
        "• **Внести дані в базу ✍️**: Додає нову інформацію (питання-відповідь) до бази знань.\n"
        "• **Перевірити базу знань 🔎**: Показує весь вміст бази з кнопками для редагування/видалення.\n"
        "• **Сповістити адмінів 🔔**: Надсилає повідомлення з медіа або без іншим адмінам.\n"
        "• **Створити пост з сайту 📰**: Автоматично генерує новину з головної сторінки сайту."
    )
    info_text_4 = (
        "--- \n"
        "**Тестові Команди**\n\n"
        "• `/testsite` - Перевіряє доступ до сайту гімназії.\n"
        "• `/testai` - Перевіряє роботу ШІ.\n"
        "• `/testimage` - Перевіряє генерацію зображень.\n\n"
        "--- \n"
        "**Важливо:**\n"
        "• Адміністратори не можуть створювати звернення через загальний функціонал, щоб уникнути плутанини. Використовуйте `/testm` для тестування."
    )
    await update.message.reply_text(info_text_1, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_2, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_3, parse_mode='Markdown')
    await asyncio.sleep(0.2)
    await update.message.reply_text(info_text_4, parse_mode='Markdown')
    
async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    
    # Завантажуємо дані локально
    user_data = load_data(USER_IDS_FILE)
    user_count = len(user_data)
    
    # Виводимо інформацію
    stats_text = f"📊 **Статистика бота:**\n\n"
    stats_text += f"Всього унікальних користувачів: **{user_count}**\n"
    stats_text += "\n_Ці дані автоматично синхронізуються з вкладкою 'Користувачі' у Google Sheets._"
    
    await query.edit_message_text(stats_text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад до Адмін-панелі", callback_data="admin_panel")]
    ]))
    
async def start_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Введіть **ключ** для нових даних (наприклад, 'Директор').\n\nДля скасування введіть /cancel.", parse_mode='Markdown')
    return WAITING_FOR_KB_KEY

async def get_kb_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['kb_key'] = update.message.text
    await update.message.reply_text(f"Ключ '{update.message.text}' збережено. Тепер введіть **значення**.", parse_mode='Markdown')
    return WAITING_FOR_KB_VALUE

async def get_kb_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.chat_data.pop('kb_key', None)
    value = update.message.text
    if not key:
        await update.message.reply_text("Ключ не знайдено. Повторіть операцію.", parse_mode='Markdown')
        return ConversationHandler.END
        
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if not isinstance(kb, dict): kb = {}
    
    # Створюємо новий запис у форматі словника з KB_KEY_ANSWER та KB_KEY_IS_FAQ (за замовчуванням порожній)
    kb[key] = {
        KB_KEY_ANSWER: value,
        KB_KEY_IS_FAQ: "" 
    }
    
    save_data(kb, KNOWLEDGE_BASE_FILE) # Зберігає локально і синхронізує з Sheets
    
    await update.message.reply_text(f"✅ Дані успішно збережено та синхронізовано з Google Sheets!\n\n**{key}**: {value}", parse_mode='Markdown')
    return ConversationHandler.END

async def view_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    
    # Завантажуємо актуальні дані (з Sheets, якщо локальний кеш неактуальний)
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if not kb or not isinstance(kb, dict):
        await query.edit_message_text("База знань порожня або пошкоджена.")
        return

    await query.edit_message_text("Ось вміст бази знань. Ви можете редагувати або видаляти записи.")
    
    if 'kb_key_map' not in context.bot_data:
        context.bot_data['kb_key_map'] = {}
    context.bot_data['kb_key_map'].clear()

    for key, data in kb.items():
        key_hash = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        context.bot_data['kb_key_map'][key_hash] = key
        
        is_faq = bool(data.get(KB_KEY_IS_FAQ, ''))
        
        faq_button_text = "Видалити з FAQ ❌" if is_faq else "Додати в FAQ ✨"
        faq_callback = f"kb_faq_toggle:{key_hash}"
        faq_status_mark = "✨ (FAQ)" if is_faq else "(Звичайна KB)"

        keyboard = [
            [
                InlineKeyboardButton("Редагувати ✏️", callback_data=f"kb_edit:{key_hash}"),
                InlineKeyboardButton("Видалити 🗑️", callback_data=f"kb_delete:{key_hash}")
            ],
            [
                InlineKeyboardButton(faq_button_text, callback_data=faq_callback)
            ]
        ]
        answer = data.get(KB_KEY_ANSWER, "--- Відповідь відсутня ---")
        
        text = f"**Ключ:** `{key}` {faq_status_mark}\n\n**Значення:**\n`{answer}`"
        
        if len(text) > 4000:
            text = text[:4000] + "..."
            
        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        await asyncio.sleep(0.1)

async def toggle_kb_faq_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()

    key_hash = query.data.split(':', 1)[1]
    key_to_edit = context.bot_data.get('kb_key_map', {}).get(key_hash)

    if not key_to_edit:
        await query.edit_message_text("❌ Помилка: цей запис застарів.")
        return

    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    data = kb.get(key_to_edit)
    
    if not data:
        await query.edit_message_text(f"❌ Помилка: запис з ключем `{key_to_edit}` не знайдено.")
        return

    is_faq = bool(data.get(KB_KEY_IS_FAQ, ''))
    
    # Змінюємо статус: якщо було FAQ (x), робимо порожнім, і навпаки
    new_faq_status = "" if is_faq else "x"
    data[KB_KEY_IS_FAQ] = new_faq_status
    kb[key_to_edit] = data
    
    save_data(kb, KNOWLEDGE_BASE_FILE) # Зберігає локально і синхронізує з Sheets
    
    # Оновлюємо кнопки та текст повідомлення
    
    faq_button_text = "Видалити з FAQ ❌" if new_faq_status else "Додати в FAQ ✨"
    faq_status_mark = "✨ (FAQ)" if new_faq_status else "(Звичайна KB)"
    
    keyboard = [
        [
            InlineKeyboardButton("Редагувати ✏️", callback_data=f"kb_edit:{key_hash}"),
            InlineKeyboardButton("Видалити 🗑️", callback_data=f"kb_delete:{key_hash}")
        ],
        [
            InlineKeyboardButton(faq_button_text, callback_data=f"kb_faq_toggle:{key_hash}")
        ]
    ]

    new_text = query.message.text.split("\n\n**Значення:**")[0] # Залишаємо тільки ключ
    
    status_message = "Додано до FAQ" if new_faq_status else "Видалено з FAQ"
    
    await query.edit_message_text(
        text=f"✅ {status_message}.\n\n{key_to_edit} {faq_status_mark}\n\n**Значення:**\n`{data.get(KB_KEY_ANSWER)}`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def delete_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    
    key_hash = query.data.split(':', 1)[1]
    key_to_delete = context.bot_data.get('kb_key_map', {}).get(key_hash)

    if not key_to_delete:
        await query.edit_message_text(f"❌ Помилка: цей запит застарів. Будь ласка, відкрийте базу знань знову.", parse_mode='Markdown')
        return

    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if key_to_delete in kb:
        del kb[key_to_delete]
        save_data(kb, KNOWLEDGE_BASE_FILE) # Зберігає локально і синхронізує з Sheets
        await query.edit_message_text(f"✅ Запис з ключем `{key_to_delete}` видалено та синхронізовано.", parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ Помилка: запис з ключем `{key_to_delete}` не знайдено (можливо, вже видалено).", parse_mode='Markdown')
        
async def start_kb_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()

    key_hash = query.data.split(':', 1)[1]
    key_to_edit = context.bot_data.get('kb_key_map', {}).get(key_hash)

    if not key_to_edit:
        await query.message.reply_text("❌ Помилка: цей запит застарів. Будь ласка, відкрийте базу знань знову і спробуйте ще раз.")
        return ConversationHandler.END

    context.chat_data['key_to_edit'] = key_to_edit
    
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    current_value = kb.get(key_to_edit, {}).get(KB_KEY_ANSWER, "Не знайдено")

    await query.message.reply_text(
        f"Редагування запису.\n**Ключ:** `{key_to_edit}`\n"
        f"**Поточне значення:** `{current_value}`\n\n"
        "Введіть нове значення для цього ключа.\n\n/cancel для скасування.",
        parse_mode='Markdown'
    )
    
    return WAITING_FOR_KB_EDIT_VALUE

async def get_kb_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key_to_edit = context.chat_data.pop('key_to_edit', None)
    new_value = update.message.text

    if not key_to_edit:
        await update.message.reply_text("❌ Помилка: ключ для редагування втрачено. Спробуйте знову.")
        return ConversationHandler.END

    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if not isinstance(kb, dict): kb = {}
    
    # Оновлюємо лише поле відповіді, зберігаючи статус FAQ
    data = kb.get(key_to_edit, {})
    data[KB_KEY_ANSWER] = new_value
    kb[key_to_edit] = data
    
    save_data(kb, KNOWLEDGE_BASE_FILE) # Зберігає локально і синхронізує з Sheets

    await update.message.reply_text(f"✅ Запис успішно оновлено та синхронізовано з Google Sheets!\n\n**{key_to_edit}**: {new_value}", parse_mode='Markdown')
    return ConversationHandler.END

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Завантажуємо актуальні дані (з Sheets, якщо локальний кеш неактуальний)
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if not kb or not isinstance(kb, dict):
        await update.message.reply_text("Наразі поширених запитань немає.")
        return

    if 'faq_key_map' not in context.bot_data:
        context.bot_data['faq_key_map'] = {}
    context.bot_data['faq_key_map'].clear()

    buttons = []
    
    # Фільтруємо лише ті записи, де KB_KEY_IS_FAQ (стовпець FAQ) не порожній
    faq_questions = {k: v for k, v in kb.items() if v.get(KB_KEY_IS_FAQ)}
    
    for key in faq_questions.keys():
        key_hash = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        context.bot_data['faq_key_map'][key_hash] = key
        buttons.append([InlineKeyboardButton(key, callback_data=f"faq_key:{key_hash}")])

    if not buttons:
        await update.message.reply_text("Наразі поширених запитань немає. Адміністратор може додати їх через /admin.")
        return

    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Ось список поширених запитань:", reply_markup=reply_markup)
    
async def faq_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    key_hash = query.data.split(':', 1)[1]
    key = context.bot_data.get('faq_key_map', {}).get(key_hash)

    if not key:
        await query.message.reply_text("Вибачте, це питання застаріло.")
        return

    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    data = kb.get(key, {})
    answer = data.get(KB_KEY_ANSWER)

    if answer:
        await query.message.reply_text(f"**{key}**\n\n{answer}", parse_mode='Markdown')
    else:
        await query.message.reply_text("Відповідь на це питання не знайдено.")

async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    logger.info(f"Виконую заплановану розсилку: {job_data.get('text', '')[:30]}")
    await do_broadcast(
        context,
        text_content=job_data.get('text', ''),
        photo=job_data.get('photo'),
        video=job_data.get('video')
    )
    scheduled_posts = load_data('scheduled_posts.json', [])
    updated_posts = [p for p in scheduled_posts if p.get('id') != context.job.name]
    save_data(updated_posts, 'scheduled_posts.json')

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True

async def start_schedule_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Надішліть текст для запланованої новини. /cancel для скасування.")
    return WAITING_FOR_SCHEDULE_TEXT

async def get_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['schedule_text'] = update.message.text
    context.chat_data['schedule_photo'] = None 
    context.chat_data['schedule_video'] = None
    
    await update.message.reply_text(
        "Текст збережено. Тепер введіть дату та час для розсилки.\n\n"
        "**Формат: `ДД.ММ.РРРР ГГ:ХХ`**\n"
        "Наприклад: `25.12.2024 18:30`\n\n"
        "/cancel для скасування.",
        parse_mode='Markdown'
    )
    return WAITING_FOR_SCHEDULE_TIME

async def get_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    time_str = update.message.text
    kyiv_timezone = pytz.timezone("Europe/Kyiv")
    
    try:
        schedule_time = datetime.strptime(time_str, "%d.%m.%Y %H:%M")
        schedule_time_aware = kyiv_timezone.localize(schedule_time)
        
        if schedule_time_aware < datetime.now(kyiv_timezone):
            await update.message.reply_text("❌ Вказаний час вже минув. Будь ласка, введіть майбутню дату та час.")
            return WAITING_FOR_SCHEDULE_TIME
            
        context.chat_data['schedule_time_str'] = schedule_time_aware.strftime("%d.%m.%Y о %H:%M")
        context.chat_data['schedule_time_obj'] = schedule_time_aware

        text = context.chat_data['schedule_text']
        
        preview_message = (
            f"**Попередній перегляд запланованого поста:**\n\n"
            f"{text}\n\n"
            f"---\n"
            f"🗓️ Запланувати розсилку на **{context.chat_data['schedule_time_str']}**?"
        )
        
        keyboard = [
            [InlineKeyboardButton("Так, запланувати ✅", callback_data="confirm_schedule_post")],
            [InlineKeyboardButton("Ні, скасувати ❌", callback_data="cancel_schedule_post")]
        ]
        
        await update.message.reply_text(preview_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIRMING_SCHEDULE_POST

    except ValueError:
        await update.message.reply_text(
            "❌ **Неправильний формат дати.**\n"
            "Будь ласка, введіть дату та час у форматі `ДД.ММ.РРРР ГГ:ХХ`.\n"
            "Наприклад: `25.12.2024 18:30`"
        )
        return WAITING_FOR_SCHEDULE_TIME
    
async def confirm_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    post_data = {
        'text': context.chat_data.get('schedule_text'),
        'photo': context.chat_data.get('schedule_photo'),
        'video': context.chat_data.get('schedule_video'),
    }
    schedule_time = context.chat_data.get('schedule_time_obj')
    
    if not post_data['text'] or not schedule_time:
        await query.edit_message_text("❌ Помилка: дані для планування втрачено. Почніть знову.")
        return ConversationHandler.END

    job_id = f"scheduled_post_{uuid.uuid4().hex[:10]}"
    
    scheduled_posts = load_data('scheduled_posts.json', [])
    scheduled_posts.append({'id': job_id, 'text': post_data['text'], 'time': schedule_time.isoformat()})
    save_data(scheduled_posts, 'scheduled_posts.json')

    context.job_queue.run_once(scheduled_broadcast_job, when=schedule_time, data=post_data, name=job_id)

    time_str = context.chat_data.get('schedule_time_str', 'невідомий час')
    await query.edit_message_text(f"✅ **Пост успішно заплановано на {time_str}.**", parse_mode='Markdown')
    
    context.chat_data.clear()
    return ConversationHandler.END
async def cancel_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Планування скасовано.")
    context.chat_data.clear()
    return ConversationHandler.END
async def view_scheduled_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    
    scheduled_posts = load_data('scheduled_posts.json', [])
    
    if not scheduled_posts:
        await query.edit_message_text("Немає запланованих постів.")
        return

    await query.edit_message_text("**Список запланованих постів:**", parse_mode='Markdown')
    kyiv_timezone = pytz.timezone("Europe/Kyiv")

    for post in scheduled_posts:
        run_time = datetime.fromisoformat(post['time']).astimezone(kyiv_timezone).strftime("%d.%m.%Y о %H:%M")
        text = post.get('text', '')[:200]
        
        message = (
            f"🗓️ **Час відправки:** {run_time}\n\n"
            f"**Текст:**\n_{text}..._"
        )
        
        keyboard = [[InlineKeyboardButton("Скасувати розсилку ❌", callback_data=f"cancel_job:{post['id']}")]]
        
        await query.message.reply_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        await asyncio.sleep(0.1)
async def cancel_scheduled_job_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    
    job_name = query.data.split(':', 1)[1]
    
    scheduled_posts = load_data('scheduled_posts.json', [])
    updated_list = [p for p in scheduled_posts if p['id'] != job_name]
    save_data(updated_list, 'scheduled_posts.json')

    if remove_job_if_exists(job_name, context):
        await query.edit_message_text("✅ Заплановану розсилку скасовано.")
    else:
        await query.edit_message_text("❌ Цей пост вже було надіслано або скасовано раніше.")
async def generate_post_from_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    await query.edit_message_text("⏳ *Збираю дані з сайту...*", parse_mode='Markdown')

    site_text = await asyncio.to_thread(get_all_text_from_website)
    if not site_text:
        await query.edit_message_text("❌ Не вдалося отримати дані з сайту. Спробуйте пізніше.")
        return

    try:
        await query.edit_message_text("🧠 *Аналізую текст та створюю пост...*", parse_mode='Markdown')
        summary_prompt = (
            "Проаналізуй наступний текст з веб-сайту. Створи з нього короткий, цікавий та інформативний пост для телеграм-каналу. "
            "Виділи найголовнішу думку або новину. Пост має бути написаний українською мовою.\n\n"
            f"--- ТЕКСТ З САЙТУ ---\n{site_text[:2500]}\n\n"
            "--- ПОСТ ДЛЯ ТЕЛЕГРАМ-КАНАЛУ ---"
        )
        post_text = await generate_text_with_fallback(summary_prompt)
        if not post_text:
            await query.edit_message_text("❌ Не вдалося згенерувати текст поста. Усі системи ШІ недоступні.")
            return

        processed_text = post_text 

        await query.edit_message_text("🎨 *Генерую зображення для поста...*", parse_mode='Markdown')
        image_prompt_for_ai = f"Створи короткий опис (3-7 слів) англійською мовою для генерації зображення на основі цього тексту: {processed_text[:300]}"
        image_prompt = await generate_text_with_fallback(image_prompt_for_ai)
        image_bytes = await generate_image(image_prompt.strip() if image_prompt else "school news")

        post_id = uuid.uuid4().hex[:8]
        context.bot_data[f"manual_post_{post_id}"] = {'text': processed_text, 'photo': image_bytes}

        keyboard = [
            [InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")],
            [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]
        ]
        await query.delete_message()
        caption = f"{processed_text}\n\n---\n*Робити розсилку цієї новини?*"

        if image_bytes:
            await context.bot.send_photo(
                chat_id=query.from_user.id, photo=image_bytes, caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
        else:
            await context.bot.send_message(
                chat_id=query.from_user.id, text=f"{caption}\n\n(Не вдалося згенерувати зображення)",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Помилка при створенні поста з сайту: {e}")
        try:
            await query.edit_message_text(f"❌ *Сталася помилка:* {e}")
        except:
            await context.bot.send_message(query.from_user.id, f"❌ *Сталася помилка:* {e}")
async def handle_post_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query: return
    if query.from_user.id not in ADMIN_IDS: return
    await query.answer()
    action, post_id = query.data.split(':', 1)
    post_data_key = f"manual_post_{post_id}"
    post_data = context.bot_data.get(post_data_key)

    if not post_data:
        await query.edit_message_text("Помилка: цей пост застарів або вже був оброблений.")
        return

    if action == 'confirm_post':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("📢 *Починаю розсилку поста...*")
        success, fail = await do_broadcast(context,
            text_content=post_data['text'],
            photo=post_data.get('photo'),
            video=post_data.get('video')
        )
        await query.message.reply_text(f"✅ Розсилку завершено.\nНадіслано: {success}\nПомилок: {fail}")
    elif action == 'cancel_post':
        original_caption = query.message.caption or query.message.text
        text_to_keep = original_caption.split("\n\n---\n")[0]
        if query.message.photo:
            await query.edit_message_caption(caption=f"{text_to_keep}\n\n--- \n❌ **Скасовано.**", parse_mode='Markdown')
        else:
            await query.edit_message_text(text=f"{text_to_keep}\n\n--- \n❌ **Скасовано.**", parse_mode='Markdown')

    if post_data_key in context.bot_data:
        del context.bot_data[post_data_key]
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    post = update.channel_post
    if not post or post.chat.id != TARGET_CHANNEL_ID: return
    post_text = post.text or post.caption or ""
    if not post_text: return
    logger.info(f"Отримано пост з цільового каналу: {post_text[:50]}...")
    if 'channel_posts' not in context.bot_data:
        context.bot_data['channel_posts'] = []
    context.bot_data['channel_posts'].insert(0, post_text)
    context.bot_data['channel_posts'] = context.bot_data['channel_posts'][:20]
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    
    # Оновлення списку користувачів (статистика)
    update_user_list(
        user.id, 
        user.username, 
        user.first_name, 
        user.last_name
    )

    if user.id in ADMIN_IDS:
        await admin_panel(update, context)
        return

    if 'user_ids' not in context.bot_data:
        context.bot_data['user_ids'] = set()
    context.bot_data['user_ids'].add(user.id)
    # Зберігаємо локально для кешування (синхронізація з sheets відбувається всередині save_data)
    save_data(list(context.bot_data['user_ids']), 'user_ids.json')
    await update.message.reply_text(
        'Вітаємо! Це офіційний бот каналу новин Бродівської гімназії.\n\n'
        '➡️ Напишіть ваше запитання або пропозицію, щоб відправити її адміністратору.\n'
        '➡️ Використовуйте команду /anonymous, щоб надіслати анонімне звернення.\n'
        '➡️ Використовуйте /faq для перегляду поширених запитань.'
    )
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # Оновлення списку користувачів (статистика)
    user = update.effective_user
    update_user_list(
        user.id, 
        user.username, 
        user.first_name, 
        user.last_name
    )
    
    if user_id in ADMIN_IDS:
        help_text = (
            "🔐 **Адміністративна Допомога**\n\n"
            "**Основні функції:**\n"
            "• `/admin` - Головна панель керування.\n"
            "• `/info` - Детальна інструкція по роботі з ботом.\n"
            "• `/faq` - Список поширених запитань.\n"
            "• `/testm` - Створити тестове звернення для перевірки функціоналу.\n"
            "• `/anonymous` - Створити анонімне звернення (для тестування).\n\n"
            "**Обробка звернень:**\n"
            "Повідомлення від користувачів, на які ШІ не зміг відповісти, надходять із кнопками 'Відповісти за допомогою ШІ' та 'Відповісти особисто'. "
            "Ви також можете просто **відповісти (Reply)** на повідомленні від бота з повідомленням від користувача, щоб надіслати йому пряму відповідь."
        )
    else:
        help_text = (
            "🙋 **Допомога та Інструкція**\n\n"
            "**Функціонал бота:**\n"
            "1. **Запитання до адміністрації:** Просто напишіть ваше повідомлення (запитання, пропозицію чи скаргу). Бот спробує відповісти автоматично за допомогою ШІ та бази знань. Якщо ШІ не впевнений, ваше повідомлення буде надіслано адміністратору.\n"
            "2. **Анонімне звернення:** Використовуйте `/anonymous`, щоб надіслати повідомлення без розкриття вашого імені.\n"
            "3. **Поширені запитання:** Використовуйте `/faq`, щоб знайти відповіді на найпопулярніші запитання.\n"
            "4. **Інструкція:** Ця команда (`/help`) показує цю довідку."
        )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("Адміністратори не можуть створювати звернення. Використовуйте /admin для доступу до панелі.")
        return ConversationHandler.END

    message = update.message
    user_data = context.user_data
    user_id = update.effective_user.id
    user_info = {'id': user_id, 'name': update.effective_user.full_name}
    text = message.text or message.caption or ""
    
    # 1. Збереження повідомлення в історію
    conversations = load_data('conversations.json', {})
    user_id_str = str(user_id)
    if user_id_str not in conversations: conversations[user_id_str] = []
    conversations[user_id_str].append({"sender": "user", "text": text, "timestamp": datetime.now().isoformat()})
    save_data(conversations, 'conversations.json')

    # 2. Визначаємо, чи є медіа-вміст. Якщо так, пропускаємо ШІ і йдемо прямо до адмінів.
    has_media = message.photo or message.video
    ai_response = None
    
    if not has_media:
        # Спроба авто-відповіді ШІ тільки для чистого тексту
        ai_response = await try_ai_autoreply(text)

    if ai_response:
        # АВТО-ВІДПОВІДЬ ЗНАЙДЕНА
        await send_telegram_reply(context.application, user_id, f"🤖 **Автоматична відповідь від ШІ:**\n\n{ai_response}")
        
        # Сповіщення адмінів про автоматичну відповідь
        notification_text = (
            f"✅ **АВТО-ВІДПОВІДЬ (ШІ)**\n\n"
            f"**Від:** {user_info['name']} (ID: {user_info['id']})\n"
            f"**Запит:**\n---\n{text}\n\n"
            f"**Відповідь ШІ:**\n---\n{ai_response}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Не змогли переслати сповіщення про авто-відповідь адміну {admin_id}: {e}")

        # Закінчуємо розмову (не переходимо у стан очікування)
        return ConversationHandler.END
    else:
        # АВТО-ВІДПОВІДЬ НЕ ЗНАЙДЕНА -> Переадресація адмінам
        
        user_data['user_info'] = user_info
        user_data['user_message'] = text
        # Коректно зберігаємо file_id медіа, якщо воно є
        user_data['file_id'] = message.photo[-1].file_id if message.photo else (message.video.file_id if message.video else None)
        user_data['media_type'] = 'photo' if message.photo else ('video' if message.video else None)

        keyboard = [
            [InlineKeyboardButton("Запитання ❓", callback_data="category_question")],
            [InlineKeyboardButton("Пропозиція 💡", callback_data="category_suggestion")],
            [InlineKeyboardButton("Скарга 📄", callback_data="category_complaint")]
        ]
        await update.message.reply_text("Будь ласка, оберіть категорію вашого звернення:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SELECTING_CATEGORY

async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    category_map = {"category_question": "Запитання ❓", "category_suggestion": "Пропозиція 💡", "category_complaint": "Скарга 📄"}
    category = category_map.get(query.data, "Без категорії")

    user_data = context.user_data
    user_data['category'] = category
    user_message = user_data.get('user_message', '')
    user_info = user_data.get('user_info', {'id': update.effective_user.id, 'name': update.effective_user.full_name})
    media_type = user_data.get('media_type')
    file_id = user_data.get('file_id')

    keyboard = [
        [InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")],
        [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    forward_text = (f"📩 **Нове звернення (Потребує ручної обробки)**\n\n" # Додано позначку
                    f"**Категорія:** {category}\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{user_message}")

    for admin_id in ADMIN_IDS:
        try:
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif media_type == 'video':
                await context.bot.send_video(chat_id=admin_id, video=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не змогли переслати звернення адміну {admin_id}: {e}")

    # ВИПРАВЛЕНО: Редагування повідомлення про вибір категорії
    await query.edit_message_text("✅ Дякуємо! Ваше повідомлення надіслано. Якщо у вас є доповнення, просто напишіть їх наступним повідомленням.")
    return IN_CONVERSATION

async def continue_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in ADMIN_IDS: return ConversationHandler.END # Адміни не ведуть розмови тут

    user_info = context.user_data.get('user_info', {'id': update.effective_user.id, 'name': update.effective_user.full_name})
    category = context.user_data.get('category', 'Без категорії')
    
    # Збереження доповнення в історію
    user_id = update.effective_user.id
    text = update.message.text or update.message.caption or ""
    conversations = load_data('conversations.json', {})
    user_id_str = str(user_id)
    if user_id_str not in conversations: conversations[user_id_str] = []
    conversations[user_id_str].append({"sender": "user", "text": text, "timestamp": datetime.now().isoformat()})
    save_data(conversations, 'conversations.json')


    keyboard = [
        [InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")],
        [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    forward_text = (f"➡️ **Доповнення до розмови**\n\n"
                    f"**Категорія:** {category}\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{update.message.text or update.message.caption or ''}")

    for admin_id in ADMIN_IDS:
        try:
            if update.message.photo:
                await context.bot.send_photo(admin_id, photo=update.message.photo[-1].file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif update.message.video:
                await context.bot.send_video(admin_id, video=update.message.video.file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=admin_id, text=forward_text, parse_mode='Markdown', reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Не змогли переслати доповнення адміну {admin_id}: {e}")

    await update.message.reply_text("✅ Доповнення надіслано.")
    return IN_CONVERSATION
async def anonymous_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("Напишіть ваше анонімне повідомлення (як адмін). /cancel для скасування.")
    else:
        await update.message.reply_text("Напишіть ваше анонімне повідомлення... Для скасування введіть /cancel.")
        
    return WAITING_FOR_ANONYMOUS_MESSAGE
async def receive_anonymous_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    anon_id = str(uuid.uuid4())[:8]
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # 1. Збереження повідомлення в історію
    conversations = load_data('conversations.json', {})
    user_id_str = str(user_id)
    if user_id_str not in conversations: conversations[user_id_str] = []
    conversations[user_id_str].append({"sender": "user", "text": f"(Анонімно) {message_text}", "timestamp": datetime.now().isoformat()})
    save_data(conversations, 'conversations.json')

    # 2. Спроба авто-відповіді ШІ
    ai_response = await try_ai_autoreply(message_text)
    
    if ai_response:
        # АВТО-ВІДПОВІДЬ ЗНАЙДЕНА
        
        # Надсилаємо відповідь користувачу
        await send_telegram_reply(context.application, user_id, f"🤫 **Відповідь на ваше анонімне звернення (від ШІ):**\n\n{ai_response}")
        
        # Сповіщення адмінів про автоматичну відповідь
        notification_text = (
            f"✅ **АВТО-ВІДПОВІДЬ АНОНІМУ (ШІ){admin_note}**\n\n"
            f"**ID:** {user_id}\n"
            f"**Запит:**\n---\n{message_text}\n\n"
            f"**Відповідь ШІ:**\n---\n{ai_response}"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Не змогли переслати сповіщення про авто-відповідь адміну {admin_id}: {e}")

        # Закінчуємо розмову
        if user_id in ADMIN_IDS:
            await update.message.reply_text("✅ Ваше тестове анонімне повідомлення було оброблено ШІ.")
        else:
            await update.message.reply_text("✅ Ваше анонімне повідомлення надіслано (оброблено ШІ).")
        return ConversationHandler.END
        
    else:
        # АВТО-ВІДПОВІДЬ НЕ ЗНАЙДЕНА -> Переадресація адмінам
        
        # Зберігаємо ID аноніма для ручної відповіді
        if 'anonymous_map' not in context.bot_data:
            context.bot_data['anonymous_map'] = {}
        context.bot_data['anonymous_map'][anon_id] = user_id

        keyboard = [
            [InlineKeyboardButton("Відповісти з ШІ 🤖", callback_data=f"anon_ai_reply:{anon_id}")],
            [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"anon_reply:{anon_id}")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Додаємо примітку, якщо це тестове звернення від адміна
        admin_note = " [ТЕСТ]" if user_id in ADMIN_IDS else ""
        forward_text = f"🤫 **Нове анонімне звернення (Ручна обробка){admin_note} (ID: {anon_id})**\n\n**Текст:**\n---\n{message_text}"
        
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Не вдалося переслати анонімне адміну {admin_id}: {e}")
                
        if user_id in ADMIN_IDS:
            await update.message.reply_text("✅ Ваше тестове анонімне повідомлення надіслано адміністраторам.")
        else:
            await update.message.reply_text("✅ Ваше анонімне повідомлення надіслано.")
            
        return ConversationHandler.END

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Надішліть повідомлення для розсилки. /cancel для скасування.")
    return WAITING_FOR_BROADCAST_MESSAGE
async def get_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['broadcast_message'] = update.message.text
    user_count = len(context.bot_data.get('user_ids', set()))
    keyboard = [
        [InlineKeyboardButton("Так, надіслати ✅", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("Ні, скасувати ❌", callback_data="cancel_broadcast")]
    ]
    await update.message.reply_text(
        f"**Попередній перегляд:**\n\n{update.message.text}\n\n---\nНадіслати **{user_count}** користувачам?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )
    return CONFIRMING_BROADCAST
async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("📢 *Починаю розсилку...*", parse_mode='Markdown')
    message_text = context.chat_data.get('broadcast_message', '')
    success, fail = await do_broadcast(context, text_content=message_text)
    await query.edit_message_text(f"✅ Розсилку завершено.\nНадіслано: {success}\nПомилок: {fail}")
    context.chat_data.clear()
    return ConversationHandler.END
async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Розсилку скасовано.")
    context.chat_data.clear()
    return ConversationHandler.END
async def start_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    action, target_user_id_str = query.data.split(':', 1)

    context.chat_data['target_user_id'] = target_user_id_str
    original_text = query.message.text or query.message.caption or ""

    # ВИПРАВЛЕНО: Використовуємо message_id для подальшого редагування
    context.chat_data['original_message_id'] = query.message.message_id 

    user_question_part = original_text.split('---\n')
    context.chat_data['original_user_message'] = user_question_part[-1] if user_question_part else ""

    if action == "manual_reply":
        # ВИПРАВЛЕНО: Не редагуємо, а надсилаємо нове повідомлення, щоб уникнути конфлікту "Message is not modified"
        # Попереднє повідомлення з кнопками залишається як "історія"
        await query.message.reply_text(f"✍️ *Напишіть вашу відповідь користувачу (ID: {target_user_id_str}). /cancel для скасування*", parse_mode='Markdown')
        return WAITING_FOR_REPLY

    elif action == "ai_reply":
        # ВИПРАВЛЕНО: Редагуємо повідомлення, щоб відобразити статус генерації
        await query.edit_message_text(text=f"{original_text}\n\n🤔 *Генерую відповідь (це може зайняти до 45 секунд)...*", parse_mode='Markdown')
        try:
            user_question = context.chat_data.get('original_user_message', '')
            if not user_question:
                raise ValueError("Не вдалося отримати текст запиту користувача.")

            logger.info("Збираю контекст для відповіді ШІ...")
            additional_context = await gather_all_context(user_question)

            prompt = (
                "Ти — корисний асистент для адміністратора шкільного чату. Дай відповідь на запитання користувача. "
                "Спочатку проаналізуй наданий контекст. Якщо він релевантний, використай його для відповіді. Якщо ні, відповідай на основі загальних знань.\n\n"
                f"--- КОНТЕКСТ (з сайту та бази знань) ---\n{additional_context}\n\n"
                f"--- ЗАПИТАННЯ КОРИСТУВАЧА ---\n'{user_question}'\n\n"
                f"--- ВІДПОВІДЬ ---\n"
            )

            ai_response_text = await generate_text_with_fallback(prompt)
            if not ai_response_text:
                raise ValueError("Не вдалося згенерувати відповідь. Усі системи ШІ недоступні.")

            context.chat_data['ai_response'] = ai_response_text

            keyboard = [
                [InlineKeyboardButton("Надіслати відповідь ✅", callback_data=f"send_ai_reply:{context.chat_data['target_user_id']}")],
                [InlineKeyboardButton("Скасувати ❌", callback_data="cancel_ai_reply")]
            ]
            preview_text = f"{original_text}\n\n🤖 **Ось відповідь від ШІ:**\n\n{ai_response_text}\n\n---\n*Надіслати цю відповідь користувачу?*"
            
            # ВИПРАВЛЕНО: Використовуємо edit_message_text для оновлення повідомлення
            await query.edit_message_text(text=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            return CONFIRMING_AI_REPLY

        except Exception as e:
            logger.error(f"Помилка генерації відповіді ШІ: {e}")
            await query.edit_message_text(
                text=f"{original_text}\n\n❌ *Помилка генерації відповіді ШІ: {e}*",
                parse_mode='Markdown'
            )
            return ConversationHandler.END
async def send_ai_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()

    ai_response_text = context.chat_data.get('ai_response')
    target_user_id = context.chat_data.get('target_user_id')
    original_message = context.chat_data.get('original_user_message', 'Невідоме звернення')
    
    if not ai_response_text or not target_user_id:
        await query.edit_message_text("❌ Помилка: дані для відповіді втрачено. Спробуйте знову.")
        return ConversationHandler.END

    try:
        # Target ID will always be int from a Telegram user now
        target_user_id_typed = int(target_user_id)
        await send_telegram_reply(context.application, target_user_id_typed, ai_response_text)
        
        # ВИПРАВЛЕНО: Редагуємо повідомлення, щоб позначити, що на його відповіли
        original_text = query.message.text.split("\n\n🤖 **Ось відповідь від ШІ:**")[0]
        final_text = f"{original_text}\n\n✅ **ВІДПОВІДЬ НАДІСЛАНА (ШІ).**"
        
        await query.edit_message_text(text=final_text, parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)
        await notify_other_admins(context, query.from_user.id, original_message)
    except Exception as e:
        logger.error(f"Помилка надсилання відповіді ШІ користувачу {target_user_id}: {e}")
        # ВИПРАВЛЕНО: Редагуємо лише текст, якщо сталася помилка
        await query.message.reply_text(f"❌ *Помилка надсилання відповіді: {e}*", parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None) # Прибираємо кнопки

    context.chat_data.clear()
    return ConversationHandler.END
async def receive_manual_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_user_id = context.chat_data.get('target_user_id')
    original_message = context.chat_data.get('original_user_message', 'Невідоме звернення')
    
    if not target_user_id:
        await update.message.reply_text("❌ Не знайдено цільового користувача.")
        return ConversationHandler.END

    owner_reply_text = update.message.text
    try:
        # Target ID will always be int from a Telegram user now
        target_user_id_typed = int(target_user_id)
        await send_telegram_reply(context.application, target_user_id_typed, f"✉️ **Відповідь від адміністратора:**\n\n{owner_reply_text}")
        await update.message.reply_text("✅ Вашу відповідь надіслано.")
        
        # ВИПРАВЛЕНО: Редагуємо оригінальне повідомлення (яке містило кнопки), щоб позначити його як оброблене
        original_message_id = context.chat_data.get('original_message_id')
        if original_message_id:
            try:
                original_msg = await context.bot.get_message(chat_id=update.effective_chat.id, message_id=original_message_id)
                
                # Запобігаємо помилці "Message is not modified"
                # Якщо текст оригінального повідомлення вже не містить маркерів "✍️ *Напишіть вашу відповідь",
                # це означає, що повідомлення було змінено в іншому місці (наприклад, ШІ).
                if "✍️ *Напишіть вашу відповідь" in original_msg.text:
                    original_text = original_msg.text.split("\n\n✍️ *Напишіть вашу відповідь")[0]
                elif "🤖 **Ось відповідь від ШІ:**" in original_msg.text:
                     original_text = original_msg.text.split("\n\n🤖 **Ось відповідь від ШІ:**")[0]
                else:
                    original_text = original_msg.text
                
                final_text = f"{original_text}\n\n✅ **ВІДПОВІДЬ НАДІСЛАНА (РУЧНА).**"
                
                # Додаємо перевірку, чи вміст дійсно змінився, щоб уникнути помилки.
                if original_msg.text != final_text:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=original_message_id,
                        text=final_text,
                        parse_mode='Markdown',
                        reply_markup=None
                    )
            except Exception as e:
                 logger.warning(f"Не вдалося відредагувати оригінальне повідомлення після ручної відповіді: {e}")
        
        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}")

    context.chat_data.clear()
    return ConversationHandler.END
async def start_anonymous_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)

    context.chat_data['anon_id_to_reply'] = anon_id
    original_text = query.message.text or ""
    user_question = original_text.split('---\n')[-1].strip()
    context.chat_data['original_user_message'] = user_question
    context.chat_data['original_message_id'] = query.message.message_id 

    await query.edit_message_text(text=f"{original_text}\n\n🤔 *Генерую відповідь для аноніма (це може зайняти до 45 секунд)...*", parse_mode='Markdown')
    try:
        if not user_question:
            raise ValueError("Не вдалося отримати текст анонімного запиту.")

        logger.info("Збираю контекст для відповіді ШІ аноніму...")
        additional_context = await gather_all_context(user_question)

        prompt = (
            "Ти — корисний асистент. Дай відповідь на анонімне запитання. Будь ввічливим та інформативним.\n\n"
            f"--- КОНТЕКСТ (з сайту та бази знань) ---\n{additional_context}\n\n"
            f"--- АНОНІМНЕ ЗАПИТАННЯ ---\n'{user_question}'\n\n"
            f"--- ВІДПОВІДЬ ---\n"
        )

        ai_response_text = await generate_text_with_fallback(prompt)
        if not ai_response_text:
            raise ValueError("Не вдалося згенерувати відповідь. Усі системи ШІ недоступні.")

        context.chat_data['ai_response'] = ai_response_text
        keyboard = [
            [InlineKeyboardButton("Надіслати відповідь ✅", callback_data=f"send_anon_ai_reply:{anon_id}")],
            [InlineKeyboardButton("Скасувати ❌", callback_data="cancel_ai_reply")]
        ]
        preview_text = f"{original_text}\n\n🤖 **Ось відповідь від ШІ для аноніма (ID: {anon_id}):**\n\n{ai_response_text}\n\n---\n*Надіслати цю відповідь?*"
        
        # ВИПРАВЛЕНО: Редагуємо повідомлення, щоб відобразити прев'ю
        await query.edit_message_text(text=preview_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIRMING_AI_REPLY

    except Exception as e:
        logger.error(f"Помилка генерації відповіді ШІ для аноніма: {e}")
        await query.edit_message_text(text=f"{original_text}\n\n❌ *Помилка генерації відповіді ШІ: {e}*", parse_mode='Markdown')
        return ConversationHandler.END
async def send_anonymous_ai_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)

    ai_response_text = context.chat_data.get('ai_response')
    user_id = context.bot_data.get('anonymous_map', {}).get(anon_id)
    original_message = context.chat_data.get('original_user_message', 'Невідоме анонімне звернення')

    if not ai_response_text or not user_id:
        await query.edit_message_text("❌ Помилка: дані для відповіді аноніму втрачено.")
        return ConversationHandler.END

    try:
        await send_telegram_reply(context.application, user_id, f"🤫 **Відповідь на ваше анонімне звернення (від ШІ):**\n\n{ai_response_text}")
        
        # ВИПРАВЛЕНО: Редагуємо повідомлення, щоб позначити, що на його відповіли
        original_text = query.message.text.split("\n\n🤖 **Ось відповідь від ШІ для аноніма")[0]
        final_text = f"{original_text}\n\n✅ **ВІДПОВІДЬ АНОНІМУ НАДІСЛАНА (ШІ).**"
        
        await query.edit_message_text(text=final_text, parse_mode='Markdown')
        await query.edit_message_reply_markup(reply_markup=None)

        await notify_other_admins(context, query.from_user.id, original_message)
    except Exception as e:
        logger.error(f"Помилка надсилання ШІ-відповіді аноніму {user_id}: {e}")
        await query.message.reply_text(f"❌ *Помилка надсилання відповіді: {e}*", parse_mode='Markdown')

    context.chat_data.clear()
    return ConversationHandler.END
async def start_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    _, anon_id = query.data.split(':', 1)
    context.chat_data['anon_id_to_reply'] = anon_id
    
    original_text = query.message.text or ""
    user_question = original_text.split('---\n')[-1].strip()
    context.chat_data['original_user_message'] = user_question
    context.chat_data['original_message_id'] = query.message.message_id 

    # ВИПРАВЛЕНО: Надсилаємо нове повідомлення, щоб не конфліктувати з inline-кнопками
    await query.message.reply_text(f"✍️ Напишіть вашу відповідь для аноніма (ID: {anon_id}). /cancel для скасування.")
    
    return WAITING_FOR_ANONYMOUS_REPLY
async def send_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    anon_id = context.chat_data.get('anon_id_to_reply')
    user_id = context.bot_data.get('anonymous_map', {}).get(anon_id)
    original_message = context.chat_data.get('original_user_message', 'Невідоме анонімне звернення')
    
    if not user_id:
        await update.message.reply_text("❌ Помилка: не знайдено отримувача.")
        return ConversationHandler.END
        
    admin_reply_text = update.message.text
    try:
        await send_telegram_reply(context.application, user_id, f"🤫 **Відповідь на ваше анонімне звернення:**\n\n{admin_reply_text}")
        await update.message.reply_text(f"✅ Вашу відповідь аноніму (ID: {anon_id}) надіслано.")
        
        # ВИПРАВЛЕНО: Редагуємо оригінальне повідомлення (яке містило кнопки)
        original_message_id = context.chat_data.get('original_message_id')
        if original_message_id:
            try:
                original_msg = await context.bot.get_message(chat_id=update.effective_chat.id, message_id=original_message_id)
                original_text = original_msg.text.split("---\n")[0]
                final_text = f"{original_text}\n\n✅ **ВІДПОВІДЬ АНОНІМУ НАДІСЛАНА (РУЧНА).**"
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=original_message_id,
                    text=final_text,
                    parse_mode='Markdown',
                    reply_markup=None
                )
            except Exception as e:
                logger.warning(f"Не вдалося відредагувати оригінальне анонімне повідомлення після ручної відповіді: {e}")

        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}")
    context.chat_data.clear()
    return ConversationHandler.END
async def handle_admin_direct_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    replied_message = update.message.reply_to_message
    if not replied_message or replied_message.from_user.id != context.bot.id: return

    target_user_id = None
    text_to_scan = replied_message.text or replied_message.caption or ""
    original_message = text_to_scan.split('---\n')[-1].strip()
    
    # Шукаємо ID звичайного користувача (тільки цифри)
    match = re.search(r"\(ID: (\d+)\)", text_to_scan)
    if match:
        target_user_id = int(match.group(1))
        reply_intro = "✉️ **Відповідь від адміністратора:**"
    else:
        # Шукаємо ID анонімного користувача (короткий UUID)
        anon_match = re.search(r"\(ID: ([a-f0-9\-]+)\)", text_to_scan)
        if anon_match:
            anon_id = anon_match.group(1)
            target_user_id = context.bot_data.get('anonymous_map', {}).get(anon_id)
            reply_intro = "🤫 **Відповідь на ваше анонімне звернення:**"

    if not target_user_id: return

    try:
        reply_text = update.message.text or update.message.caption or ""
        
        # Для прямих відповідей медіа відправляємо через send_photo/send_video
        if update.message.photo or update.message.video:
             if update.message.photo:
                await context.bot.send_photo(chat_id=target_user_id, photo=update.message.photo[-1].file_id, caption=f"{reply_intro}\n\n{reply_text}", parse_mode='Markdown')
             elif update.message.video:
                await context.bot.send_video(chat_id=target_user_id, video=update.message.video.file_id, caption=f"{reply_intro}\n\n{reply_text}", parse_mode='Markdown')
             
             # Зберігаємо в історію лише текст
             await send_telegram_reply(context.application, target_user_id, f"{reply_intro}\n\n{reply_text}")

        else:
            await send_telegram_reply(context.application, target_user_id, f"{reply_intro}\n\n{reply_text}")

        await update.message.reply_text("✅ Вашу відповідь надіслано.", quote=True)
        await notify_other_admins(context, update.effective_user.id, original_message)
    except Exception as e:
        logger.error(f"Не вдалося надіслати пряму відповідь користувачу {target_user_id}: {e}")
        await update.message.reply_text(f"❌ Не вдалося надіслати: {e}", quote=True)
async def start_news_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Будь ласка, надішліть текст для вашої новини. /cancel для скасування.")
    return WAITING_FOR_NEWS_TEXT
async def get_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['news_text'] = update.message.text
    keyboard = [
        [InlineKeyboardButton("Обробити через ШІ 🤖", callback_data="news_ai")],
        [InlineKeyboardButton("Вручну додати медіа 🖼️", callback_data="news_manual")]
    ]
    await update.message.reply_text("Текст збережено. Як продовжити?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRMING_NEWS_ACTION
async def handle_news_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    action = query.data
    news_text = context.chat_data.get('news_text')

    if not news_text:
        await query.edit_message_text("❌ Помилка: текст новини втрачено. Почніть знову.")
        return ConversationHandler.END

    if action == 'news_ai':
        try:
            await query.edit_message_text("🧠 *Обробляю текст та створюю заголовок...*", parse_mode='Markdown')
            summary_prompt = f"Перепиши цей текст, щоб він був цікавим та лаконічним постом для телеграм-каналу новин. Збережи головну суть. Текст:\n\n{news_text}"
            processed_text = await generate_text_with_fallback(summary_prompt)
            if not processed_text:
                await query.edit_message_text("❌ Не вдалося обробити текст. Усі системи ШІ недоступні.")
                return ConversationHandler.END

            await query.edit_message_text("🎨 *Генерую зображення...*", parse_mode='Markdown')
            image_prompt_for_ai = f"Створи короткий опис (3-7 слів) англійською мовою для генерації зображення на основі цього тексту: {processed_text[:300]}"
            image_prompt = await generate_text_with_fallback(image_prompt_for_ai)
            image_bytes = await generate_image(image_prompt.strip() if image_prompt else "school news")

            post_id = uuid.uuid4().hex[:8]
            context.bot_data[f"manual_post_{post_id}"] = {'text': processed_text, 'photo': image_bytes}

            keyboard = [[InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]]
            caption = f"{processed_text}\n\n---\n*Робити розсилку цієї новини?*"

            await query.delete_message()
            if image_bytes:
                await context.bot.send_photo(chat_id=query.from_user.id, photo=image_bytes, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=query.from_user.id, text=f"{caption}\n\n(Не вдалося згенерувати зображення)", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

        except Exception as e:
            logger.error(f"Помилка при обробці новини через ШІ: {e}")
            await query.edit_message_text(f"❌ Сталася помилка: {e}")

        return ConversationHandler.END

    elif action == 'news_manual':
        await query.edit_message_text("Будь ласка, надішліть фото або відео для цього посту.")
        return WAITING_FOR_MEDIA
async def get_news_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    news_text = context.chat_data.get('news_text')
    photo = update.message.photo[-1].file_id if update.message.photo else None
    video = update.message.video.file_id if update.message.video else None

    if not (photo or video):
        await update.message.reply_text("Будь ласка, надішліть фото або відео.")
        return WAITING_FOR_MEDIA

    post_id = uuid.uuid4().hex[:8]
    context.bot_data[f"manual_post_{post_id}"] = {'text': news_text, 'photo': photo, 'video': video}

    keyboard = [[InlineKeyboardButton("Так, розіслати ✅", callback_data=f"confirm_post:{post_id}")], [InlineKeyboardButton("Ні, скасувати ❌", callback_data=f"cancel_post:{post_id}")]]
    caption = f"{news_text}\n\n---\n*Робити розсилку цієї новини?*"

    if photo:
        await update.message.reply_photo(photo=photo, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif video:
        await update.message.reply_video(video=video, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    return ConversationHandler.END
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"Користувач {update.effective_user.id} викликав /cancel.")
    
    if update.callback_query:
        await update.callback_query.answer()

    if context.chat_data or context.user_data:
        await update.effective_message.reply_text(
            'Операцію скасовано.',
            reply_markup=ReplyKeyboardRemove()
        )
        if 'original_message_id' in context.chat_data and update.effective_chat.id in ADMIN_IDS:
             try:
                await context.bot.edit_message_reply_markup(
                    chat_id=update.effective_chat.id,
                    message_id=context.chat_data['original_message_id'],
                    reply_markup=None
                )
             except Exception:
                 pass 
                 
        context.user_data.clear()
        context.chat_data.clear()
        return ConversationHandler.END
    else:
        await update.effective_message.reply_text(
            'Немає активних операцій для скасування.',
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END
async def test_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Запускаю тестову перевірку сайту...*")
    site_text = get_all_text_from_website()
    if not site_text:
        await update.message.reply_text("❌ Не вдалося отримати текст з сайту. Перевірте лог на помилки.")
        return
    message = f"✅ Успішно отримано {len(site_text)} символів з сайту.\n\n**Початок тексту:**\n\n{site_text[:500]}..."
    await update.message.reply_text(message)
async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Тестую систему ШІ з резервуванням...*")
    response = await generate_text_with_fallback("Привіт! Скажи 'тест успішний'")
    if response:
        await update.message.reply_text(f"✅ Відповідь від ШІ:\n\n{response}")
    else:
        await update.message.reply_text("❌ Помилка: жоден із сервісів ШІ (Gemini, Cloudflare) не відповів.")
async def test_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔍 *Тестую Stability AI API...*")
    try:
        image_bytes = await generate_image("school emblem")
        if image_bytes:
            await update.message.reply_photo(photo=image_bytes, caption="✅ Тестове зображення успішно згенеровано!")
        else:
            await update.message.reply_text("❌ Stability AI API повернуло порожню відповідь. Перевірте ключ та баланс кредитів.")
    except Exception as e:
        logger.error(f"Помилка тестування Stability AI API: {e}")
        await update.message.reply_text(f"❌ Помилка Stability AI API: {e}")
async def test_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END # Перевірка прав доступу

    keyboard = [
        [InlineKeyboardButton("Використати мої дані (тест)", callback_data="test_user_default")],
        [InlineKeyboardButton("Ввести дані вручну", callback_data="test_user_custom")]
    ]
    await update.message.reply_text(
        "🛠️ **Тестування вхідного повідомлення**\n\n"
        "Оберіть, від імені якого користувача надіслати тестове повідомлення:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return SELECTING_TEST_USER
async def handle_test_user_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query: return ConversationHandler.END
    if query.from_user.id not in ADMIN_IDS: return ConversationHandler.END
    await query.answer()
    choice = query.data

    if choice == 'test_user_default':
        context.chat_data['test_user_info'] = {
            'id': query.from_user.id,
            'name': get_admin_name(query.from_user.id)
        }
        await query.edit_message_text("Добре. Тепер надішліть тестове повідомлення (текст, фото або відео), яке ви хочете перевірити.\n\n/cancel для скасування.")
        return WAITING_FOR_TEST_MESSAGE
    elif choice == 'test_user_custom':
        await query.edit_message_text("Будь ласка, введіть тимчасове **ім'я** користувача для тесту.\n\n/cancel для скасування.", parse_mode='Markdown')
        return WAITING_FOR_TEST_NAME
async def get_test_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['test_user_name'] = update.message.text
    await update.message.reply_text("Ім'я збережено. Тепер введіть тимчасовий **ID** користувача (лише цифри).\n\n/cancel для скасування.", parse_mode='Markdown')
    return WAITING_FOR_TEST_ID
async def get_test_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id_text = update.message.text
    if not user_id_text.isdigit():
        await update.message.reply_text("❌ Помилка: ID має складатися лише з цифр. Спробуйте ще раз.")
        return WAITING_FOR_TEST_ID

    user_id = int(user_id_text)
    user_name = context.chat_data.pop('test_user_name')
    context.chat_data['test_user_info'] = {'id': user_id, 'name': user_name}

    await update.message.reply_text("Дані збережено. Тепер надішліть тестове повідомлення (текст, фото або відео).\n\n/cancel для скасування.")
    return WAITING_FOR_TEST_MESSAGE
async def receive_test_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_info = context.chat_data.get('test_user_info')
    if not user_info:
        await update.message.reply_text("❌ Помилка: дані тестового користувача втрачено. Почніть знову з /testm.")
        return ConversationHandler.END

    message = update.message
    media_type = None
    file_id = None
    user_message = ""

    if message.text:
        user_message = message.text
    elif message.photo:
        user_message = message.caption or ""
        media_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.video:
        user_message = message.caption or ""
        media_type = 'video'
        file_id = message.video.file_id

    keyboard = [
        [InlineKeyboardButton("Відповісти за допомогою ШІ 🤖", callback_data=f"ai_reply:{user_info['id']}")],
        [InlineKeyboardButton("Відповісти особисто ✍️", callback_data=f"manual_reply:{user_info['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    forward_text = (f"📩 **Нове звернення [ТЕСТ]**\n\n"
                    f"**Категорія:** Тест\n"
                    f"**Від:** {user_info['name']} (ID: {user_info['id']})\n\n"
                    f"**Текст:**\n---\n{user_message}")

    for admin_id in ADMIN_IDS:
        try:
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=admin_id, photo=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            elif media_type == 'video':
                await context.bot.send_video(chat_id=admin_id, video=file_id, caption=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=admin_id, text=forward_text, reply_markup=reply_markup, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Не вдалося надіслати тестове повідомлення адміну {admin_id}: {e}")

    await update.message.reply_text("✅ Тестове повідомлення надіслано всім адміністраторам.")
    context.chat_data.clear()
    return ConversationHandler.END

# --- Задача для запобігання засинанню (Pinging) ---
async def ping_self_for_wakeup(context: ContextTypes.DEFAULT_TYPE):
    """
    Надсилає HTTP-запит до самого себе, щоб запобігти засинанню сервісу Render.
    """
    if not RENDER_EXTERNAL_URL:
        logger.error("RENDER_EXTERNAL_URL не встановлено, функція 'пінг' не може бути виконана.")
        return
        
    ping_url = RENDER_EXTERNAL_URL.rstrip('/') + '/'
    
    try:
        response = await asyncio.to_thread(requests.get, ping_url, timeout=5)
        response.raise_for_status() 
        logger.info(f"✅ Успішний пінг самого себе ({ping_url}). Статус: {response.status_code}")
    except requests.RequestException as e:
        logger.warning(f"❌ Помилка пінг-запиту, але це, можливо, розбудило Render: {e}")
    except Exception as e:
        logger.error(f"Невідома помилка під час пінг-запиту: {e}")

# --- Фіктивний Web-сервер для задоволення Render ---
async def dummy_handler(request):
    """Обробник, який просто повертає 200 OK і повідомляє, що порт відкрито."""
    return web.Response(text="Bot is running (WebHook mode).", status=200)

# --- Обробник вхідних вебхуків Telegram ---
async def handle_telegram_webhook(request: web.Request) -> web.Response:
    """Обробляє вхідні оновлення від Telegram."""
    application = request.app['ptb_app']
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return web.Response()
    except json.JSONDecodeError:
        logger.warning("Не вдалося розпарсити JSON з вебхука Telegram.")
        return web.Response(status=400)
    except Exception as e:
        logger.error(f"Помилка в обробнику вебхука: {e}")
        return web.Response(status=500)

async def start_web_server(application):
    """Створює і запускає мінімальний веб-сервер aiohttp."""
    web_app = web.Application()
    web_app['ptb_app'] = application
    
    # Маршрути для вебхука Telegram та перевірки здоров'я
    web_app.router.add_post(WEBHOOK_PATH, handle_telegram_webhook)
    web_app.router.add_get('/', dummy_handler) # Для перевірки здоров'я Render
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) # Використовуємо 8080 як дефолт
    
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"WebHook-сервер AIOHTTP запущено на http://0.0.0.0:{port}")
    
    return runner

# --- Основна функція ---
async def main() -> None:
    # --- Створення та налаштування Application ---
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # --- Налаштування даних бота та обробників ---
    
    # Завантаження початкових даних (викликає синхронізацію з Sheets, якщо локальний кеш порожній)
    application.bot_data['kb_data'] = load_data(KNOWLEDGE_BASE_FILE)
    application.bot_data['admin_contacts'] = load_data('admin_contacts.json')
    
    # Завантаження ID користувачів
    user_data = load_data(USER_IDS_FILE)
    
    # === ФІКС ПОМИЛКИ: САНІТИЗАЦІЯ ДАНИХ КОРИСТУВАЧІВ (Migration) ===
    # Виправлення проблеми, коли старі ID зберігалися як прості числа (int)
    sanitized_user_data = []
    for item in user_data:
        if isinstance(item, dict) and 'id' in item:
            # Новий, правильний формат (словник)
            sanitized_user_data.append(item)
        elif isinstance(item, int):
            # Старий, простий формат (ціле число). Конвертуємо у словник.
            sanitized_user_data.append({'id': item, 'full_name': 'Migrated User', 'username': None, 'last_run': 'N/A (Migrated)'})
        # Інакше ігноруємо невідомий або пошкоджений елемент
            
    # Тепер створюємо множину з санітизованих даних
    application.bot_data['user_ids'] = {user['id'] for user in sanitized_user_data if 'id' in user}
    # ===============================================================
    
    application.bot_data['anonymous_map'] = {}
    logger.info(f"Завантажено {len(application.bot_data['user_ids'])} унікальних ID користувачів.")


    user_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, start_conversation)],
        states={
            SELECTING_CATEGORY: [CallbackQueryHandler(select_category, pattern='^category_.*$')],
            IN_CONVERSATION: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, continue_conversation)],
        },
        fallbacks=[CommandHandler('cancel', cancel)], conversation_timeout=3600
    )
    anonymous_conv = ConversationHandler(
        entry_points=[CommandHandler('anonymous', anonymous_command)],
        states={ WAITING_FOR_ANONYMOUS_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_anonymous_message)] },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_broadcast, pattern='^admin_broadcast$')],
        states={
            WAITING_FOR_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_broadcast_message)],
            CONFIRMING_BROADCAST: [
                CallbackQueryHandler(send_broadcast, pattern='^confirm_broadcast$'),
                CallbackQueryHandler(cancel_broadcast, pattern='^cancel_broadcast$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    kb_entry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_entry, pattern='^admin_kb_add$')],
        states={
            WAITING_FOR_KB_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_key)],
            WAITING_FOR_KB_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_value)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    kb_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_edit, pattern=r'^kb_edit:.*$')],
        states={ WAITING_FOR_KB_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_kb_edit_value)] },
        fallbacks=[CommandHandler('cancel', cancel)], conversation_timeout=600
    )
    anonymous_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_anonymous_reply, pattern='^anon_reply:.*$')],
        states={ WAITING_FOR_ANONYMOUS_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_anonymous_reply)] },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    admin_reply_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_admin_reply, pattern='^ai_reply:.*$'),
            CallbackQueryHandler(start_admin_reply, pattern='^manual_reply:.*$'),
            CallbackQueryHandler(start_anonymous_ai_reply, pattern='^anon_ai_reply:.*')
        ],
        states={
            WAITING_FOR_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_manual_reply)],
            CONFIRMING_AI_REPLY: [
                CallbackQueryHandler(send_ai_reply_to_user, pattern='^send_ai_reply:.*$'),
                CallbackQueryHandler(send_anonymous_ai_reply_to_user, pattern='^send_anon_ai_reply:.*$'),
                CallbackQueryHandler(cancel, pattern='^cancel_ai_reply$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)], allow_reentry=True
    )
    create_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_news_creation, pattern='^admin_create_news$')],
        states={
            WAITING_FOR_NEWS_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_news_text)],
            CONFIRMING_NEWS_ACTION: [CallbackQueryHandler(handle_news_action, pattern='^news_.*$')],
            WAITING_FOR_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, get_news_media)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    schedule_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_schedule_news, pattern='^admin_schedule_news$')],
        states={
            WAITING_FOR_SCHEDULE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_text)],
            WAITING_FOR_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_schedule_time)],
            CONFIRMING_SCHEDULE_POST: [
                CallbackQueryHandler(confirm_schedule_post, pattern='^confirm_schedule_post$'),
                CallbackQueryHandler(cancel_schedule_post, pattern='^cancel_schedule_post$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    # НОВА КОНВЕРСАЦІЯ ДЛЯ СПОВІЩЕННЯ АДМІНІВ
    admin_notify_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_notify_admins, pattern='^admin_notify_admins$')],
        states={
            WAITING_FOR_ADMIN_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, receive_admin_message)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    test_message_conv = ConversationHandler(
        entry_points=[CommandHandler("testm", test_message_command)],
        states={
            SELECTING_TEST_USER: [CallbackQueryHandler(handle_test_user_choice, pattern='^test_user_.*$')],
            WAITING_FOR_TEST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_test_name)],
            WAITING_FOR_TEST_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_test_id)],
            WAITING_FOR_TEST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, receive_test_message)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # --- Реєстрація хендлерів ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command)) # Додано help
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("faq", faq_command))
    
    # Прямі команди для адмінів
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("testsite", test_site_command))
    application.add_handler(CommandHandler("testai", test_ai_command))
    application.add_handler(CommandHandler("testimage", test_image_command))
    
    application.add_handler(MessageHandler(filters.REPLY & filters.User(ADMIN_IDS), handle_admin_direct_reply))
    application.add_handler(CallbackQueryHandler(admin_stats_handler, pattern='^admin_stats$'))
    application.add_handler(CallbackQueryHandler(website_update_handler, pattern='^(broadcast_website|cancel_website_update):.*$'))
    application.add_handler(CallbackQueryHandler(generate_post_from_site, pattern='^admin_generate_post$'))
    application.add_handler(CallbackQueryHandler(handle_post_broadcast_confirmation, pattern='^(confirm_post|cancel_post):.*$')) # ВИПРАВЛЕНО: CallbackHandler на CallbackQueryHandler
    application.add_handler(CallbackQueryHandler(view_kb, pattern='^admin_kb_view$'))
    application.add_handler(CallbackQueryHandler(delete_kb_entry, pattern=r'^kb_delete:.*$'))
    application.add_handler(CallbackQueryHandler(toggle_kb_faq_status, pattern=r'^kb_faq_toggle:.*$')) # НОВИЙ ХЕНДЛЕР ДЛЯ FAQ КНОПКИ
    application.add_handler(CallbackQueryHandler(faq_button_handler, pattern='^faq_key:'))
    application.add_handler(CallbackQueryHandler(view_scheduled_posts, pattern='^admin_view_scheduled$'))
    application.add_handler(CallbackQueryHandler(cancel_scheduled_job_button, pattern='^cancel_job:'))
    
    # Хендлери-конверсації
    application.add_handler(broadcast_conv)
    application.add_handler(kb_entry_conv)
    application.add_handler(kb_edit_conv)
    application.add_handler(anonymous_conv)
    application.add_handler(anonymous_reply_conv)
    application.add_handler(admin_reply_conv)
    application.add_handler(create_news_conv)
    application.add_handler(schedule_news_conv)
    application.add_handler(admin_notify_conv) # ДОДАНО НОВУ КОНВЕРСАЦІЮ
    application.add_handler(test_message_conv)
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    application.add_handler(user_conv)

    # --- Запуск JobQueue та Application (WebHook) ---
    await application.initialize()
    
    # Запускаємо заплановані задачі
    kyiv_timezone = pytz.timezone("Europe/Kyiv")
    application.job_queue.run_daily(check_website_for_updates, time=dt_time(hour=9, minute=0, tzinfo=kyiv_timezone))
    
    # ДОДАНО: Задача для запобігання засинанню (кожні 10 хвилин)
    application.job_queue.run_repeating(
        ping_self_for_wakeup,
        interval=600,
        first=10, 
        name='self_ping_job'
    )
    logger.info("Задача на запобігання засинанню (пінг) встановлена на кожні 10 хвилин.")
    
    # Встановлюємо вебхук
    await application.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
    logger.info(f"Вебхук успішно встановлено на {WEBHOOK_URL}")

    # Запуск WebHook-сервера
    web_runner = await start_web_server(application)

    # Запуск WebHook-режиму
    await application.start()
    logger.info("Бот запущено в режимі WebHook.")

    # Основний цикл підтримки життя
    try:
        # Чекаємо на невизначений Future, який утримуватиме цикл подій
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # Коректне завершення роботи
        logger.info("Завершую роботу бота...")
        await application.bot.delete_webhook()
        logger.info("Вебхук видалено.")
        await web_runner.cleanup()
        await application.stop()
        logger.info("Додаток повністю зупинено.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено вручну.")
