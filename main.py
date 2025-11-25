import os
import asyncio
import uuid
import json
import logging
from datetime import datetime, time as dt_time, timedelta
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler, ApplicationBuilder
)
from telegram.error import Forbidden, TelegramError
import requests
from bs4 import BeautifulSoup
import pytz
from typing import Any, Callable, Dict, List, Set
import re
import hashlib
import gspread 
from oauth2client.service_account import ServiceAccountCredentials 
from urllib.parse import parse_qs
from aiohttp import web
import aiohttp_cors

# --- Налаштування ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8223675237:AAF_kmo6SP4XZS23NeXWFxgkQNUaEZOWNx0")
GEMINI_API_KEYS_STR = os.environ.get("GEMINI_API_KEYS", "AIzaSyBtIxTceQYA6UAUyr9R0RrQWQzFNEnWXYA,AIzaSyDH5sprfzkyfltY8wSjSBYvccRcpArvLRo,AIzaSyDhEA8jiGQ9ngcYn3hc445slrQIIVrPocI")
GEMINI_API_KEYS = [key.strip() for key in GEMINI_API_KEYS_STR.split(',') if key.strip()]
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "238b1178c6912fc52ccb303667c92687")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "v6HjMgCHEqTiElwnW_hK73j1uqQKud1fG-rPInWD")
STABILITY_AI_API_KEY = os.environ.get("STABILITY_AI_API_KEY", "sk-uDtr8UAPxC7JHLG9QAyXt9s4QY142fkbOQA7uZZEgjf99iWp")

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
KNOWLEDGE_BASE_FILE = 'knowledge_base.json'
USER_IDS_FILE = 'user_ids.json'

GSHEET_NAME = os.environ.get("GSHEET_NAME", "Бродівська гімназія - База Знань")
GSHEET_WORKSHEET_NAME = os.environ.get("GSHEET_WORKSHEET_NAME", "База_Знань")
USERS_GSHEET_WORKSHEET_NAME = os.environ.get("USERS_GSHEET_WORKSHEET_NAME", "Користувачі")
SCHEDULE_GSHEET_WORKSHEET_NAME = os.environ.get("SCHEDULE_GSHEET_WORKSHEET_NAME", "Заплановані_Пости")
GCP_CREDENTIALS_JSON = os.environ.get("GCP_CREDENTIALS_JSON", "{}") 

KB_KEY_QUESTION = "Питання"
KB_KEY_ANSWER = "Відповідь"
KB_KEY_IS_FAQ = "FAQ" 

# Логування
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Глобальні змінні для веб-сервера ---
active_websockets: Dict[str, web.WebSocketResponse] = {}
web_sessions: Dict[str, Dict] = {} 

# --- СТАНИ ---
(SELECTING_CATEGORY, IN_CONVERSATION, WAITING_FOR_REPLY,
 WAITING_FOR_ANONYMOUS_MESSAGE, WAITING_FOR_ANONYMOUS_REPLY,
 WAITING_FOR_BROADCAST_MESSAGE, CONFIRMING_BROADCAST,
 WAITING_FOR_KB_KEY, WAITING_FOR_KB_VALUE, CONFIRMING_AI_REPLY,
 WAITING_FOR_NEWS_TEXT, CONFIRMING_NEWS_ACTION, WAITING_FOR_MEDIA,
 SELECTING_TEST_USER, WAITING_FOR_TEST_NAME, WAITING_FOR_TEST_ID,
 WAITING_FOR_TEST_MESSAGE, WAITING_FOR_KB_EDIT_VALUE,
 WAITING_FOR_SCHEDULE_TEXT, WAITING_FOR_SCHEDULE_TIME, CONFIRMING_SCHEDULE_POST) = range(21)


# --- GOOGLE SHEETS УТИЛІТИ ---

GSHEET_SCOPE = [
    'https://spreadsheets.google.com/feeds', 
    'https://www.googleapis.com/auth/drive'
]

def get_gsheet_client(worksheet_name: str):
    try:
        creds_dict = json.loads(GCP_CREDENTIALS_JSON)
        if not creds_dict or "private_key" not in creds_dict:
            logger.error(f"GCP_CREDENTIALS_JSON порожній. (лист: {worksheet_name}).")
            return None
            
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, GSHEET_SCOPE)
        client = gspread.authorize(creds)
        
        sheet = client.open(GSHEET_NAME)
        worksheet = sheet.worksheet(worksheet_name)
        return worksheet
    except Exception as e:
        logger.error(f"Помилка ініціалізації GSheet Client (лист: {worksheet_name}): {e}")
        return None

def save_data_to_gsheet(kb_data: Dict[str, dict]) -> bool:
    worksheet = get_gsheet_client(GSHEET_WORKSHEET_NAME)
    if not worksheet: return False
    
    try:
        records = [[KB_KEY_QUESTION, KB_KEY_ANSWER, KB_KEY_IS_FAQ]]
        for key, data in kb_data.items():
            records.append([
                key,
                data.get(KB_KEY_ANSWER, ''),
                data.get(KB_KEY_IS_FAQ, '')
            ])
        worksheet.batch_clear(["A1:Z1000"]) 
        worksheet.update('A1', records)
        return True
    except Exception as e:
        logger.error(f"Помилка запису KB в Google Sheets: {e}")
        return False

def save_scheduled_to_gsheet(scheduled_posts: List[dict]) -> bool:
    worksheet = get_gsheet_client(SCHEDULE_GSHEET_WORKSHEET_NAME)
    if not worksheet: return False
    
    try:
        records = [["ID", "Час відправки (ISO)", "Текст", "Photo ID", "Video ID"]]
        for post in scheduled_posts:
            records.append([
                post.get('id', ''),
                post.get('time', ''),
                post.get('text', ''),
                post.get('photo', ''),
                post.get('video', '')
            ])
        worksheet.batch_clear(["A1:Z1000"]) 
        worksheet.update('A1', records)
        return True
    except Exception as e:
        logger.error(f"Помилка запису запланованих постів у Sheets: {e}")
        return False

def save_users_to_gsheet(users: List[dict]) -> bool:
    worksheet = get_gsheet_client(USERS_GSHEET_WORKSHEET_NAME)
    if not worksheet: return False

    try:
        records = [["ID", "Ім'я користувача (нік)", "Повне Ім'я", "Дата останнього запуску"]] 
        for user in users:
            if not isinstance(user, dict): continue
            records.append([
                str(user.get('id', '')),
                user.get('username', ''),
                user.get('full_name', ''),
                user.get('last_run', '')
            ])
        
        num_rows = len(records)
        num_cols = len(records[0]) if records else 0
        end_col = chr(ord('A') + num_cols - 1)
        range_to_update = f"A1:{end_col}{num_rows}"
        worksheet.update(range_to_update, records)
        return True
    except Exception as e:
        logger.error(f"Помилка запису користувачів в Sheets: {e}")
        return False

def fetch_kb_from_sheets() -> Dict[str, dict] | None:
    worksheet = get_gsheet_client(GSHEET_WORKSHEET_NAME)
    if not worksheet: return None 
    
    try:
        list_of_lists = worksheet.get_all_values()
        if not list_of_lists or len(list_of_lists) < 2: return {}

        header = [h.strip() for h in list_of_lists[0]]
        q_idx = header.index(KB_KEY_QUESTION) if KB_KEY_QUESTION in header else 0
        a_idx = header.index(KB_KEY_ANSWER) if KB_KEY_ANSWER in header else 1
        faq_idx = header.index(KB_KEY_IS_FAQ) if KB_KEY_IS_FAQ in header else -1
        
        data_rows = list_of_lists[1:]
        kb = {}
        for row in data_rows:
            if len(row) > q_idx and row[q_idx].strip():
                question = row[q_idx].strip()
                answer = row[a_idx].strip() if len(row) > a_idx else ""
                is_faq = row[faq_idx].strip() if faq_idx >= 0 and len(row) > faq_idx else ""
                kb[question] = {KB_KEY_ANSWER: answer, KB_KEY_IS_FAQ: is_faq}
        return kb
    except Exception as e:
        logger.error(f"Помилка читання KB: {e}")
        return None

def fetch_scheduled_from_sheets() -> List[dict] | None:
    worksheet = get_gsheet_client(SCHEDULE_GSHEET_WORKSHEET_NAME)
    if not worksheet: return None 
    try:
        list_of_lists = worksheet.get_all_values()
        if not list_of_lists or len(list_of_lists) < 2: return []

        header = [h.strip() for h in list_of_lists[0]]
        id_idx = header.index("ID") if "ID" in header else 0
        time_idx = header.index("Час відправки (ISO)") if "Час відправки (ISO)" in header else 1
        text_idx = header.index("Текст") if "Текст" in header else 2
        photo_idx = header.index("Photo ID") if "Photo ID" in header else 3
        video_idx = header.index("Video ID") if "Video ID" in header else 4

        posts = []
        for row in list_of_lists[1:]:
            post_id = row[id_idx].strip() if len(row) > id_idx else None
            if not post_id: continue
            posts.append({
                'id': post_id,
                'time': row[time_idx].strip() if len(row) > time_idx else None,
                'text': row[text_idx].strip() if len(row) > text_idx else None,
                'photo': row[photo_idx].strip() if len(row) > photo_idx and row[photo_idx].strip() else None,
                'video': row[video_idx].strip() if len(row) > video_idx and row[video_idx].strip() else None
            })
        return posts
    except Exception as e:
        logger.error(f"Помилка читання запланованих постів: {e}")
        return None

def fetch_users_from_sheets() -> List[dict] | None:
    worksheet = get_gsheet_client(USERS_GSHEET_WORKSHEET_NAME)
    if not worksheet: return None 
    try:
        list_of_lists = worksheet.get_all_values()
        if not list_of_lists or len(list_of_lists) < 2: return []

        header = [h.strip() for h in list_of_lists[0]]
        id_idx = header.index("ID") if "ID" in header else 0
        username_idx = header.index("Ім'я користувача (нік)") if "Ім'я користувача (нік)" in header else 1
        fullname_idx = header.index("Повне Ім'я") if "Повне Ім'я" in header else 2 
        lastrun_idx = header.index("Дата останнього запуску") if "Дата останнього запуску" in header else 3
        
        users = []
        for row in list_of_lists[1:]:
            user_id_str = row[id_idx].strip() if len(row) > id_idx else None
            if not user_id_str: continue
            try:
                user_id = int(user_id_str)
            except ValueError:
                user_id = user_id_str 
            
            users.append({
                'id': user_id,
                'username': row[username_idx].strip() if len(row) > username_idx else None,
                'full_name': row[fullname_idx].strip() if len(row) > fullname_idx else None,
                'last_run': row[lastrun_idx].strip() if len(row) > lastrun_idx else None
            })
        return users
    except Exception as e:
        logger.error(f"Помилка читання користувачів: {e}")
        return None

# --- FILE UTILS ---
def get_default_knowledge_base() -> Dict[str, dict]:
    return {
        "Хто є директор школи?": {KB_KEY_ANSWER: "Директор школи: Кіт Ярослав Ярославович. Телефон: +380976929979", KB_KEY_IS_FAQ: "x"},
        "Контактні дані школи": {KB_KEY_ANSWER: "Адреса: 80600, м. Броди, вул. Коцюбинського, 2. Телефон: +3803266 27991. E-mail: brodyg@ukr.net", KB_KEY_IS_FAQ: ""},
        "Важливі посилання": {KB_KEY_ANSWER: "Telegram-канал: https://t.me/+2NB0puCLx6o5NDk6. Офіційний сайт: https://brodygymnasium.e-schools.info/", KB_KEY_IS_FAQ: "x"}
    }

def load_data(filename: str, default_type: Any = None) -> Any:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
            if filename == KNOWLEDGE_BASE_FILE and not data:
                raise json.JSONDecodeError("Empty KB", f.name, 0)
            
            if filename == USER_IDS_FILE:
                sanitized_users = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and 'id' in item:
                            sanitized_users.append(item)
                        elif isinstance(item, int):
                            sanitized_users.append({'id': item, 'full_name': 'N/A', 'username': None, 'last_run': 'N/A (Migrated)'})
                return sanitized_users

            if filename == SCHEDULED_POSTS_FILE and default_type == []:
                scheduled_from_sheets = fetch_scheduled_from_sheets()
                if scheduled_from_sheets is not None:
                    save_data(scheduled_from_sheets, filename)
                    return scheduled_from_sheets
                return data

            return data
    except (FileNotFoundError, json.JSONDecodeError):
        if filename == KNOWLEDGE_BASE_FILE:
            kb_from_sheets = fetch_kb_from_sheets()
            if kb_from_sheets is None or not kb_from_sheets:
                kb_data = get_default_knowledge_base()
            else:
                kb_data = kb_from_sheets
            save_data(kb_data, filename)
            return kb_data
            
        if filename == USER_IDS_FILE:
            users_from_sheets = fetch_users_from_sheets()
            if users_from_sheets is not None:
                save_data(users_from_sheets, filename)
                return users_from_sheets
            return []
        
        if filename == SCHEDULED_POSTS_FILE and default_type == []:
            scheduled_from_sheets = fetch_scheduled_from_sheets()
            if scheduled_from_sheets is not None:
                save_data(scheduled_from_sheets, filename)
                return scheduled_from_sheets
            return []
            
        if default_type is not None:
            return default_type
        return {}

def save_data(data: Any, filename: str) -> None:
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            if filename == KNOWLEDGE_BASE_FILE:
                asyncio.run_coroutine_threadsafe(asyncio.to_thread(save_data_to_gsheet, data), loop)
            if filename == USER_IDS_FILE and isinstance(data, list):
                asyncio.run_coroutine_threadsafe(asyncio.to_thread(save_users_to_gsheet, data), loop)
            if filename == SCHEDULED_POSTS_FILE and isinstance(data, list):
                asyncio.run_coroutine_threadsafe(asyncio.to_thread(save_scheduled_to_gsheet, data), loop)
            
    except Exception as e:
        logger.error(f"Помилка save_data({filename}): {e}")

async def send_telegram_reply(ptb_app: Application, user_id: int, text: str):
    conversations = load_data(CONVERSATIONS_FILE, {})
    user_id_str = str(user_id)
    
    if not isinstance(user_id, int): 
        return

    if user_id_str not in conversations: conversations[user_id_str] = []
    conversations[user_id_str].append({"sender": "bot", "text": text, "timestamp": datetime.now().isoformat()})
    save_data(conversations, CONVERSATIONS_FILE)

    try:
        await ptb_app.bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Не вдалося надіслати в Telegram користувачу {user_id}: {e}")

def update_user_list(user_id: int, username: str | None, first_name: str | None, last_name: str | None):
    user_data = load_data(USER_IDS_FILE) 
    current_full_name = ' '.join(filter(None, [first_name, last_name]))
    
    found = False
    for i, user_item in enumerate(user_data):
        if isinstance(user_item, dict) and user_item.get('id') == user_id:
            if username: user_data[i]['username'] = username
            if current_full_name.strip(): user_data[i]['full_name'] = current_full_name
            user_data[i]['last_run'] = datetime.now(pytz.timezone("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")
            found = True
            break
            
    if not found:
        new_user = {
            'id': user_id,
            'username': username or 'N/A',
            'full_name': current_full_name or 'N/A',
            'last_run': datetime.now(pytz.timezone("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")
        }
        user_data.append(new_user)
        logger.info(f"Новий користувач: {user_id}")

    save_data(user_data, USER_IDS_FILE)

# --- ДОДАНІ ФУНКЦІЇ ---

def get_admin_name(user_id: int) -> str:
    """Отримує ім'я адміна за його ID (з локальних даних або заглушка)."""
    user_data = load_data(USER_IDS_FILE)
    for u in user_data:
        if u.get('id') == user_id:
            return u.get('full_name', f"Admin_{user_id}")
    return f"Admin_{user_id}"

async def notify_other_admins(context: ContextTypes.DEFAULT_TYPE, sender_id: int, original_message: str):
    """Сповіщає інших адміністраторів, що на звернення вже дано відповідь."""
    sender_name = get_admin_name(sender_id)
    text = f"ℹ️ **Інфо:** Адміністратор {sender_name} вже відповів на звернення:\n\n_{original_message[:50]}..._"
    
    for admin_id in ADMIN_IDS:
        if admin_id != sender_id:
            try:
                await context.bot.send_message(chat_id=admin_id, text=text, parse_mode='Markdown')
            except Exception:
                pass

async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, text_content: str, photo: str = None, video: str = None) -> tuple[int, int]:
    """Виконує масову розсилку всім користувачам з бази."""
    user_data_list = load_data(USER_IDS_FILE)
    # Отримуємо унікальні ID
    user_ids = {u['id'] for u in user_data_list if isinstance(u, dict) and 'id' in u}
    
    success_count = 0
    fail_count = 0
    
    for uid in user_ids:
        try:
            if photo:
                await context.bot.send_photo(chat_id=uid, photo=photo, caption=text_content, parse_mode='Markdown')
            elif video:
                await context.bot.send_video(chat_id=uid, video=video, caption=text_content, parse_mode='Markdown')
            else:
                await context.bot.send_message(chat_id=uid, text=text_content, parse_mode='Markdown')
            success_count += 1
            await asyncio.sleep(0.05) # Rate limit protection
        except Forbidden:
            # Користувач заблокував бота
            fail_count += 1
        except TelegramError as e:
            logger.warning(f"Не вдалося надіслати повідомлення користувачу {uid}: {e}")
            fail_count += 1
        except Exception as e:
            logger.error(f"Критична помилка при розсилці {uid}: {e}")
            fail_count += 1
            
    return success_count, fail_count

# --- AI & Parsing Logic ---
async def generate_text_with_fallback(prompt: str) -> str | None:
    GEMINI_MODEL = 'gemini-2.5-flash'
    for api_key in GEMINI_API_KEYS:
        for attempt in range(3):
            try:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(GEMINI_MODEL) 
                response = await asyncio.to_thread(model.generate_content, prompt, request_options={'timeout': 45})
                if response.text and response.candidates and response.candidates[0].finish_reason != 'SAFETY':
                    return response.text
                elif response.candidates and response.candidates[0].finish_reason == 'SAFETY':
                    break 
                else:
                    raise Exception("Порожня відповідь")
            except Exception as e:
                if "404" in str(e): break
                if attempt < 2: await asyncio.sleep(2 ** attempt)
                continue
        continue

    # Cloudflare fallback
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN or "your_cf" in CLOUDFLARE_ACCOUNT_ID: return None
    for attempt in range(3):
        try:
            cf_url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/meta/llama-2-7b-chat-int8"
            headers = {"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}
            payload = {"messages": [{"role": "user", "content": prompt}]}
            response = await asyncio.to_thread(requests.post, cf_url, headers=headers, json=payload, timeout=45)
            response.raise_for_status()
            result = response.json()
            cf_text = result.get("result", {}).get("response")
            if cf_text: return cf_text
        except Exception as e:
            if attempt < 2: await asyncio.sleep(2 ** attempt)
            continue
    return None

async def generate_image(prompt: str) -> bytes | None:
    api_url = "https://api.stability.ai/v2beta/stable-image/generate/core"
    headers = {"authorization": f"Bearer {STABILITY_AI_API_KEY}", "accept": "image/*"}
    data = {"prompt": f"Minimalistic, symbolic, school illustration: '{prompt}'. No text.", "output_format": "jpeg", "aspect_ratio": "1:1"}
    for attempt in range(3):
        try:
            response = await asyncio.to_thread(requests.post, api_url, headers=headers, files={"none": ''}, data=data, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            if attempt < 2: await asyncio.sleep(2 ** attempt)
    return None

def get_all_text_from_website() -> str | None:
    try:
        response = requests.get(GYMNASIUM_URL.rstrip('/') + "/", timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        for element in soup(["script", "style", "nav", "footer", "header"]): element.extract()
        return re.sub(r'\n\s*\n', '\n\n', soup.body.get_text(separator='\n', strip=True))
    except Exception: return None

def get_teachers_info() -> str | None:
    try:
        response = requests.get("https://brodygymnasium.e-schools.info/teachers", timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        content = soup.find('div', class_='content-inner')
        if content:
            for el in content(["script", "style"]): el.extract()
            return re.sub(r'\n\s*\n', '\n', content.get_text(separator='\n', strip=True))
        return None
    except Exception: return None
    
async def gather_all_context(query: str) -> str:
    is_teacher_query = any(k in query.lower() for k in ['вчител', 'викладач', 'директор', 'завуч'])
    site_text, teachers_info = await asyncio.gather(
        asyncio.to_thread(get_all_text_from_website),
        asyncio.to_thread(get_teachers_info) if is_teacher_query else asyncio.sleep(0, result=None)
    )
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    relevant_kb = {}
    if isinstance(kb, dict):
        qwords = set(query.lower().split())
        for q_key, data in kb.items():
            if qwords & set(f"{q_key} {data.get(KB_KEY_ANSWER, '')}".lower().split()):
                relevant_kb[q_key] = data.get(KB_KEY_ANSWER, '')
                
    parts = []
    if teachers_info: parts.append(f"**Вчителі:**\n{teachers_info[:2000]}")
    parts.append(f"**Сайт:**\n{site_text[:2000] if site_text else 'N/A'}")
    if relevant_kb:
        kb_text = "--- База знань ---\n" + "\n".join([f"- {k}: {v}" for k,v in relevant_kb.items()])
        parts.append(kb_text)
    return "\n\n".join(parts)

async def try_ai_autoreply(user_question: str) -> str | None:
    ctx = await gather_all_context(user_question)
    prompt = (f"Ти — асистент шкільного чату. Контекст:\n{ctx}\n\nПитання: '{user_question}'\n"
              "Якщо є точна відповідь в контексті, почни з [CONFIDENT]. Якщо ні — [UNCERTAIN].")
    raw = await generate_text_with_fallback(prompt)
    if raw and raw.strip().startswith('[CONFIDENT]'):
        return raw.strip().replace('[CONFIDENT]', '', 1).strip()
    return None

# --- Handlers ---
async def check_website_for_updates(context: ContextTypes.DEFAULT_TYPE):
    new_text = get_all_text_from_website()
    if not new_text: return
    last = load_data('website_content.json', {}).get('text', '')
    if new_text != last:
        save_data({'text': new_text, 'timestamp': datetime.now().isoformat()}, 'website_content.json')
        await propose_website_update(context, new_text)
        
async def propose_website_update(context: ContextTypes.DEFAULT_TYPE, text_content: str):
    broadcast_id = f"website_update_{uuid.uuid4().hex[:8]}"
    context.bot_data[broadcast_id] = text_content
    kb = [[InlineKeyboardButton("Розсилка 📢", callback_data=f"broadcast_website:{broadcast_id}"),
           InlineKeyboardButton("Скасувати ❌", callback_data=f"cancel_website_update:{broadcast_id}")]]
    for aid in ADMIN_IDS:
        try: await context.bot.send_message(aid, f"**Оновлення на сайті:**\n{text_content[:800]}...", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        except: pass
            
async def website_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, bid = query.data.split(':', 1)
    if action == 'broadcast_website':
        txt = context.bot_data.get(bid)
        if not txt: 
            await query.edit_message_text("Помилка: текст застарів.")
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("📢 *Розсилка...*")
        s, f = await do_broadcast(context, txt)
        await query.message.reply_text(f"✅ Готово.\n+: {s}\n-: {f}")
    elif action == 'cancel_website_update':
        await query.edit_message_text("❌ Скасовано.")
    if bid in context.bot_data: del context.bot_data[bid]

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    kb = [
        [InlineKeyboardButton("Створити новину ✍️", callback_data="admin_create_news"), InlineKeyboardButton("Запланувати 🗓️", callback_data="admin_schedule_news")],
        [InlineKeyboardButton("Заплановані 🕒", callback_data="admin_view_scheduled"), InlineKeyboardButton("Розсилка 📢", callback_data="admin_broadcast")],
        [InlineKeyboardButton("Додати в базу ✍️", callback_data="admin_kb_add"), InlineKeyboardButton("База знань 🔎", callback_data="admin_kb_view")],
        [InlineKeyboardButton("Пост з сайту 📰", callback_data="admin_generate_post"), InlineKeyboardButton("Статистика 📊", callback_data="admin_stats")]
    ]
    await update.message.reply_text("🔐 **Адмін-панель:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("🔐 **Команди:**\n/admin - Панель\n/testm - Тест повідомлення\n/schedule - Перевірка розкладу")

async def admin_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query.from_user.id not in ADMIN_IDS: return
    user_count = len(load_data(USER_IDS_FILE))
    await update.callback_query.edit_message_text(f"📊 **Статистика:**\nКористувачів: **{user_count}**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]), parse_mode='Markdown')

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    schedule = kb.get("Розклад уроків", {}).get(KB_KEY_ANSWER, "Інформація відсутня.")
    await update.message.reply_text(f"📅 **Розклад:**\n\n{schedule}", parse_mode='Markdown')

# --- KB Management ---
async def start_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Введіть **ключ**.")
    return WAITING_FOR_KB_KEY

async def get_kb_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['kb_key'] = update.message.text
    await update.message.reply_text("Введіть **значення**.")
    return WAITING_FOR_KB_VALUE

async def get_kb_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.chat_data.pop('kb_key', None)
    if not key: return ConversationHandler.END
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    kb[key] = {KB_KEY_ANSWER: update.message.text, KB_KEY_IS_FAQ: ""}
    save_data(kb, KNOWLEDGE_BASE_FILE)
    await update.message.reply_text(f"✅ Збережено: {key}")
    return ConversationHandler.END

async def view_kb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    if not kb:
        await update.callback_query.edit_message_text("База порожня.")
        return
    context.bot_data['kb_key_map'] = {}
    await update.callback_query.edit_message_text("Записи бази знань:")
    for key, data in kb.items():
        kh = hashlib.sha1(key.encode()).hexdigest()[:16]
        context.bot_data['kb_key_map'][kh] = key
        btn_text = "Видалити з FAQ" if data.get(KB_KEY_IS_FAQ) else "Додати в FAQ"
        kb_markup = [[InlineKeyboardButton("Edit", callback_data=f"kb_edit:{kh}"), InlineKeyboardButton("Del", callback_data=f"kb_delete:{kh}")],
                     [InlineKeyboardButton(btn_text, callback_data=f"kb_faq_toggle:{kh}")]]
        await update.callback_query.message.reply_text(f"**{key}**\n{data.get(KB_KEY_ANSWER)[:100]}...", reply_markup=InlineKeyboardMarkup(kb_markup), parse_mode='Markdown')

async def toggle_kb_faq_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kh = update.callback_query.data.split(':', 1)[1]
    key = context.bot_data.get('kb_key_map', {}).get(kh)
    if not key: return
    kb = load_data(KNOWLEDGE_BASE_FILE)
    kb[key][KB_KEY_IS_FAQ] = "" if kb[key].get(KB_KEY_IS_FAQ) else "x"
    save_data(kb, KNOWLEDGE_BASE_FILE)
    await update.callback_query.answer("Статус змінено")
    await view_kb(update, context) # Refresh logic simplified

async def delete_kb_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kh = update.callback_query.data.split(':', 1)[1]
    key = context.bot_data.get('kb_key_map', {}).get(kh)
    if key:
        kb = load_data(KNOWLEDGE_BASE_FILE)
        if key in kb: del kb[key]
        save_data(kb, KNOWLEDGE_BASE_FILE)
        await update.callback_query.edit_message_text(f"Видалено: {key}")

async def start_kb_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kh = update.callback_query.data.split(':', 1)[1]
    key = context.bot_data.get('kb_key_map', {}).get(kh)
    if not key: return ConversationHandler.END
    context.chat_data['key_to_edit'] = key
    await update.callback_query.message.reply_text(f"Нове значення для: {key}")
    return WAITING_FOR_KB_EDIT_VALUE

async def get_kb_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = context.chat_data.pop('key_to_edit', None)
    if key:
        kb = load_data(KNOWLEDGE_BASE_FILE)
        kb[key][KB_KEY_ANSWER] = update.message.text
        save_data(kb, KNOWLEDGE_BASE_FILE)
        await update.message.reply_text("✅ Оновлено.")
    return ConversationHandler.END

async def faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = load_data(KNOWLEDGE_BASE_FILE) or {}
    faq = {k: v for k, v in kb.items() if v.get(KB_KEY_IS_FAQ)}
    if not faq:
        await update.message.reply_text("FAQ порожній.")
        return
    context.bot_data['faq_key_map'] = {}
    btns = []
    for k in faq:
        kh = hashlib.sha1(k.encode()).hexdigest()[:16]
        context.bot_data['faq_key_map'][kh] = k
        btns.append([InlineKeyboardButton(k, callback_data=f"faq_key:{kh}")])
    await update.message.reply_text("Поширені запитання:", reply_markup=InlineKeyboardMarkup(btns))

async def faq_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kh = update.callback_query.data.split(':', 1)[1]
    key = context.bot_data.get('faq_key_map', {}).get(kh)
    if key:
        kb = load_data(KNOWLEDGE_BASE_FILE)
        ans = kb.get(key, {}).get(KB_KEY_ANSWER, "Err")
        await update.callback_query.message.reply_text(f"**{key}**\n\n{ans}", parse_mode='Markdown')

# --- Scheduling ---
async def scheduled_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    data = job.data
    logger.info(f"Запуск запланованого посту: {job.name}")
    await do_broadcast(context, data.get('text', ''), data.get('photo'), data.get('video'))
    # Clean up
    posts = load_data(SCHEDULED_POSTS_FILE, [])
    save_data([p for p in posts if p.get('id') != data.get('id')], SCHEDULED_POSTS_FILE)

async def start_schedule_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Текст новини?")
    return WAITING_FOR_SCHEDULE_TEXT

async def get_schedule_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['schedule_text'] = update.message.text
    await update.message.reply_text("Медіа? (або /skip_media)")
    return WAITING_FOR_MEDIA

async def skip_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['schedule_photo'] = None
    context.chat_data['schedule_video'] = None
    await update.message.reply_text("Дата (ДД.ММ.РРРР ГГ:ХХ)?")
    return WAITING_FOR_SCHEDULE_TIME

async def get_schedule_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['schedule_photo'] = update.message.photo[-1].file_id if update.message.photo else None
    context.chat_data['schedule_video'] = update.message.video.file_id if update.message.video else None
    await update.message.reply_text("Дата (ДД.ММ.РРРР ГГ:ХХ)?")
    return WAITING_FOR_SCHEDULE_TIME

async def get_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        dt = datetime.strptime(update.message.text, "%d.%m.%Y %H:%M")
        dt_aware = pytz.timezone("Europe/Kyiv").localize(dt)
        if dt_aware < datetime.now(pytz.timezone("Europe/Kyiv")): raise ValueError
        context.chat_data['schedule_time_obj'] = dt_aware
        context.chat_data['schedule_time_iso'] = dt_aware.isoformat()
        kb = [[InlineKeyboardButton("Так", callback_data="confirm_schedule_post"), InlineKeyboardButton("Ні", callback_data="cancel_schedule_post")]]
        await update.message.reply_text(f"Запланувати на {dt_aware}?", reply_markup=InlineKeyboardMarkup(kb))
        return CONFIRMING_SCHEDULE_POST
    except ValueError:
        await update.message.reply_text("Невірний формат або час минув.")
        return WAITING_FOR_SCHEDULE_TIME

async def confirm_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_id = f"scheduled_{uuid.uuid4().hex[:8]}"
    data = {
        'id': job_id,
        'time': context.chat_data['schedule_time_iso'],
        'text': context.chat_data['schedule_text'],
        'photo': context.chat_data.get('schedule_photo'),
        'video': context.chat_data.get('schedule_video')
    }
    posts = load_data(SCHEDULED_POSTS_FILE, [])
    posts.append(data)
    save_data(posts, SCHEDULED_POSTS_FILE)
    context.job_queue.run_once(scheduled_broadcast_job, context.chat_data['schedule_time_obj'], data=data, name=job_id)
    await update.callback_query.edit_message_text("✅ Заплановано.")
    return ConversationHandler.END

async def cancel_schedule_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Скасовано.")
    return ConversationHandler.END

async def view_scheduled_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    posts = load_data(SCHEDULED_POSTS_FILE, [])
    if not posts:
        await update.callback_query.edit_message_text("Порожньо.")
        return
    await update.callback_query.edit_message_text("Заплановані:")
    for p in posts:
        kb = [[InlineKeyboardButton("Cancel", callback_data=f"cancel_job:{p['id']}")]]
        await update.callback_query.message.reply_text(f"ID: {p['id']}\nTime: {p['time']}\nText: {p['text'][:50]}...", reply_markup=InlineKeyboardMarkup(kb))

async def cancel_scheduled_job_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jid = update.callback_query.data.split(':', 1)[1]
    posts = load_data(SCHEDULED_POSTS_FILE, [])
    save_data([p for p in posts if p['id'] != jid], SCHEDULED_POSTS_FILE)
    jobs = context.job_queue.get_jobs_by_name(jid)
    for j in jobs: j.schedule_removal()
    await update.callback_query.edit_message_text("Видалено.")

# --- Broadcast & News ---
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Текст розсилки?")
    return WAITING_FOR_BROADCAST_MESSAGE

async def get_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['broadcast_message'] = update.message.text
    kb = [[InlineKeyboardButton("Так", callback_data="confirm_broadcast"), InlineKeyboardButton("Ні", callback_data="cancel_broadcast")]]
    await update.message.reply_text("Підтвердити?", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRMING_BROADCAST

async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("⏳ Розсилка...")
    s, f = await do_broadcast(context, context.chat_data['broadcast_message'])
    await update.callback_query.message.reply_text(f"✅ Успіх: {s}, Помилок: {f}")
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Скасовано.")
    return ConversationHandler.END

async def start_news_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Текст новини?")
    return WAITING_FOR_NEWS_TEXT

async def get_news_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['news_text'] = update.message.text
    kb = [[InlineKeyboardButton("AI обробка", callback_data="news_ai"), InlineKeyboardButton("Медіа вручну", callback_data="news_manual")]]
    await update.message.reply_text("Дії?", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRMING_NEWS_ACTION

async def handle_news_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    act = update.callback_query.data
    if act == 'news_manual':
        await update.callback_query.edit_message_text("Надішліть медіа.")
        return WAITING_FOR_MEDIA
    elif act == 'news_ai':
        await update.callback_query.edit_message_text("⏳ AI працює...")
        txt = context.chat_data['news_text']
        new_txt = await generate_text_with_fallback(f"Зроби з цього пост для Telegram: {txt}")
        img = await generate_image(f"Abstract illustration for: {txt[:50]}")
        pid = uuid.uuid4().hex[:8]
        context.bot_data[f"manual_post_{pid}"] = {'text': new_txt, 'photo': img}
        kb = [[InlineKeyboardButton("Send", callback_data=f"confirm_post:{pid}"), InlineKeyboardButton("Cancel", callback_data=f"cancel_post:{pid}")]]
        if img: await context.bot.send_photo(update.effective_chat.id, img, caption=new_txt[:1000], reply_markup=InlineKeyboardMarkup(kb))
        else: await context.bot.send_message(update.effective_chat.id, new_txt, reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END

async def get_news_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pid = uuid.uuid4().hex[:8]
    context.bot_data[f"manual_post_{pid}"] = {
        'text': context.chat_data['news_text'],
        'photo': update.message.photo[-1].file_id if update.message.photo else None,
        'video': update.message.video.file_id if update.message.video else None
    }
    kb = [[InlineKeyboardButton("Send", callback_data=f"confirm_post:{pid}"), InlineKeyboardButton("Cancel", callback_data=f"cancel_post:{pid}")]]
    await update.message.reply_text("Готово до відправки?", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def handle_post_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    act, pid = update.callback_query.data.split(':', 1)
    if act == 'cancel_post':
        await update.callback_query.edit_message_caption("❌ Скасовано.")
        return
    data = context.bot_data.get(f"manual_post_{pid}")
    if data:
        await update.callback_query.edit_message_reply_markup(None)
        await update.callback_query.message.reply_text("⏳ Розсилка...")
        s, f = await do_broadcast(context, data['text'], data.get('photo'), data.get('video'))
        await update.callback_query.message.reply_text(f"✅ Результат: +{s} / -{f}")

# --- User Interaction ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    update_user_list(u.id, u.username, u.first_name, u.last_name)
    if u.id in ADMIN_IDS:
        await admin_panel(update, context)
    else:
        await update.message.reply_text("Привіт! Я шкільний бот. Пиши питання або /faq.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = "/start - Старт\n/faq - Питання\n/anonymous - Анонімно"
    if update.effective_user.id in ADMIN_IDS: txt += "\n/admin - Адмінка"
    await update.message.reply_text(txt)

async def start_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id in ADMIN_IDS: 
        await update.message.reply_text("Адміни використовують /admin.")
        return ConversationHandler.END
    
    msg = update.message.text or (update.message.caption if update.message.caption else "")
    if not update.message.photo and not update.message.video:
        ai_reply = await try_ai_autoreply(msg)
        if ai_reply:
            await update.message.reply_text(f"🤖 AI: {ai_reply}")
            return ConversationHandler.END
            
    # Manual handling
    context.user_data['user_message'] = msg
    context.user_data['user_info'] = {'id': update.effective_user.id, 'name': update.effective_user.full_name}
    context.user_data['file_id'] = update.message.photo[-1].file_id if update.message.photo else (update.message.video.file_id if update.message.video else None)
    context.user_data['media_type'] = 'photo' if update.message.photo else ('video' if update.message.video else None)
    
    kb = [[InlineKeyboardButton("Питання", callback_data="category_question"), InlineKeyboardButton("Скарга", callback_data="category_complaint")]]
    await update.message.reply_text("Оберіть категорію:", reply_markup=InlineKeyboardMarkup(kb))
    return SELECTING_CATEGORY

async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cat = update.callback_query.data
    uid = context.user_data['user_info']['id']
    msg = context.user_data['user_message']
    
    kb = [[InlineKeyboardButton("AI відповідь", callback_data=f"ai_reply:{uid}"), InlineKeyboardButton("Ручна відповідь", callback_data=f"manual_reply:{uid}")]]
    txt = f"📩 **Нове звернення ({cat})**\nВід: {uid}\n\n{msg}"
    
    for aid in ADMIN_IDS:
        if context.user_data.get('media_type') == 'photo':
            await context.bot.send_photo(aid, context.user_data['file_id'], caption=txt, reply_markup=InlineKeyboardMarkup(kb))
        elif context.user_data.get('media_type') == 'video':
            await context.bot.send_video(aid, context.user_data['file_id'], caption=txt, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await context.bot.send_message(aid, txt, reply_markup=InlineKeyboardMarkup(kb))
            
    await update.callback_query.edit_message_text("✅ Надіслано адміністратору.")
    return ConversationHandler.END

async def continue_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return ConversationHandler.END 

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- Anonymous ---
async def anonymous_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ваше анонімне повідомлення?")
    return WAITING_FOR_ANONYMOUS_MESSAGE

async def receive_anonymous_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message.text
    ai_reply = await try_ai_autoreply(msg)
    if ai_reply:
        await update.message.reply_text(f"🤖 (AI): {ai_reply}")
        return ConversationHandler.END
        
    anon_id = uuid.uuid4().hex[:8]
    context.bot_data.setdefault('anonymous_map', {})[anon_id] = update.effective_user.id
    
    kb = [[InlineKeyboardButton("Відповісти", callback_data=f"anon_reply:{anon_id}")]]
    for aid in ADMIN_IDS:
        await context.bot.send_message(aid, f"🤫 **Анонімно (ID: {anon_id}):**\n{msg}", reply_markup=InlineKeyboardMarkup(kb))
    await update.message.reply_text("Надіслано анонімно.")
    return ConversationHandler.END

async def start_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.chat_data['anon_id'] = update.callback_query.data.split(':', 1)[1]
    await update.callback_query.message.reply_text("Текст відповіді?")
    return WAITING_FOR_ANONYMOUS_REPLY

async def send_anonymous_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    aid = context.chat_data['anon_id']
    uid = context.bot_data['anonymous_map'].get(aid)
    if uid:
        await context.bot.send_message(uid, f"📨 **Відповідь:**\n{update.message.text}")
        await update.message.reply_text("Надіслано.")
    else:
        await update.message.reply_text("Користувача не знайдено.")
    return ConversationHandler.END

# --- Admin Replies ---
async def start_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    act, uid = update.callback_query.data.split(':', 1)
    context.chat_data['target_uid'] = uid
    if act == 'manual_reply':
        await update.callback_query.message.reply_text("Ваша відповідь?")
        return WAITING_FOR_REPLY
    elif act == 'ai_reply':
        await update.callback_query.message.reply_text("⏳ Генерую...")
        orig = update.callback_query.message.text or update.callback_query.message.caption
        orig = orig.split('\n\n')[-1] # Try to extract last part
        ai = await generate_text_with_fallback(f"Відповісти на це: {orig}")
        context.chat_data['ai_response'] = ai
        kb = [[InlineKeyboardButton("Надіслати", callback_data=f"send_ai_reply:{uid}"), InlineKeyboardButton("Скасувати", callback_data="cancel_ai_reply")]]
        await update.callback_query.message.reply_text(f"🤖 AI пропонує:\n{ai}", reply_markup=InlineKeyboardMarkup(kb))
        return CONFIRMING_AI_REPLY

async def receive_manual_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = context.chat_data['target_uid']
    try:
        await context.bot.send_message(uid, f"✉️ **Адміністратор:**\n{update.message.text}")
        await update.message.reply_text("✅ Надіслано.")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")
    return ConversationHandler.END

async def send_ai_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = context.chat_data['target_uid']
    try:
        await context.bot.send_message(uid, f"✉️ **Бот:**\n{context.chat_data['ai_response']}")
        await update.callback_query.edit_message_text("✅ AI відповідь надіслано.")
    except Exception as e:
        await update.callback_query.message.reply_text(f"Помилка: {e}")
    return ConversationHandler.END

async def generate_post_from_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.edit_message_text("⏳ Аналізую сайт...")
    txt = get_all_text_from_website()
    if not txt:
        await update.callback_query.message.reply_text("Помилка сайту.")
        return
    
    post = await generate_text_with_fallback(f"Зроби новину з цього тексту: {txt[:2000]}")
    img = await generate_image("School news abstract")
    
    pid = uuid.uuid4().hex[:8]
    context.bot_data[f"manual_post_{pid}"] = {'text': post, 'photo': img}
    kb = [[InlineKeyboardButton("Send", callback_data=f"confirm_post:{pid}"), InlineKeyboardButton("Cancel", callback_data=f"cancel_post:{pid}")]]
    
    if img: await context.bot.send_photo(update.effective_chat.id, img, caption=post[:1000], reply_markup=InlineKeyboardMarkup(kb))
    else: await context.bot.send_message(update.effective_chat.id, post, reply_markup=InlineKeyboardMarkup(kb))

# --- Tests ---
async def test_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    await update.message.reply_text("Надішліть тестове повідомлення.")
    return WAITING_FOR_TEST_MESSAGE

async def receive_test_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message.text or "Media message"
    kb = [[InlineKeyboardButton("AI Reply", callback_data=f"ai_reply:{update.effective_user.id}")]]
    await update.message.reply_text(f"Test received:\n{msg}", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END

async def test_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = get_all_text_from_website()
    await update.message.reply_text(f"Site len: {len(txt) if txt else 0}")

async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    res = await generate_text_with_fallback("Hello")
    await update.message.reply_text(f"AI: {res}")

async def test_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    img = await generate_image("Test")
    if img: await update.message.reply_photo(img)
    else: await update.message.reply_text("Img fail")

async def ping_self_for_wakeup(context: ContextTypes.DEFAULT_TYPE):
    try: requests.get(RENDER_EXTERNAL_URL.rstrip('/') + '/', timeout=5)
    except: pass

async def handle_telegram_webhook(request: web.Request) -> web.Response:
    app = request.app['ptb_app']
    await app.process_update(Update.de_json(await request.json(), app.bot))
    return web.Response()

async def dummy_handler(request): return web.Response(text="Running", status=200)

async def start_web_server(application):
    web_app = web.Application()
    web_app['ptb_app'] = application
    web_app.router.add_post(WEBHOOK_PATH, handle_telegram_webhook)
    web_app.router.add_get('/', dummy_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()
    return runner

async def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Load Initial Data
    app.bot_data['kb_data'] = load_data(KNOWLEDGE_BASE_FILE)
    users = load_data(USER_IDS_FILE)
    app.bot_data['user_ids'] = {u['id'] for u in users if isinstance(u, dict)}
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("faq", faq_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    
    # Admin commands
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("testsite", test_site_command))
    app.add_handler(CommandHandler("testai", test_ai_command))
    app.add_handler(CommandHandler("testimage", test_image_command))
    
    # --- Розгорнута ініціалізація ConversationHandlers для читабельності ---
    
    # 1. User Conversation (Звернення)
    user_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.VIDEO, start_conversation)],
        states={
            SELECTING_CATEGORY: [CallbackQueryHandler(select_category, pattern='^category_.*$')]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(user_conv)

    # 2. Anonymous Message
    anonymous_conv = ConversationHandler(
        entry_points=[CommandHandler('anonymous', anonymous_command)],
        states={
            WAITING_FOR_ANONYMOUS_MESSAGE: [MessageHandler(filters.TEXT, receive_anonymous_message)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(anonymous_conv)

    # 3. Broadcast
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_broadcast, pattern='^admin_broadcast$')],
        states={
            WAITING_FOR_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT, get_broadcast_message)],
            CONFIRMING_BROADCAST: [
                CallbackQueryHandler(send_broadcast, pattern='^confirm_broadcast$'), 
                CallbackQueryHandler(cancel_broadcast, pattern='^cancel_broadcast$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(broadcast_conv)

    # 4. KB Add
    kb_entry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_entry, pattern='^admin_kb_add$')],
        states={
            WAITING_FOR_KB_KEY: [MessageHandler(filters.TEXT, get_kb_key)], 
            WAITING_FOR_KB_VALUE: [MessageHandler(filters.TEXT, get_kb_value)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(kb_entry_conv)

    # 5. KB Edit
    kb_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kb_edit, pattern=r'^kb_edit:.*$')],
        states={
            WAITING_FOR_KB_EDIT_VALUE: [MessageHandler(filters.TEXT, get_kb_edit_value)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(kb_edit_conv)

    # 6. Anonymous Reply (Admin)
    anonymous_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_anonymous_reply, pattern='^anon_reply:.*$')],
        states={
            WAITING_FOR_ANONYMOUS_REPLY: [MessageHandler(filters.TEXT, send_anonymous_reply)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(anonymous_reply_conv)

    # 7. Admin Reply (Direct/AI)
    admin_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_admin_reply, pattern='^(ai|manual)_reply:.*$')],
        states={
            WAITING_FOR_REPLY: [MessageHandler(filters.TEXT, receive_manual_reply)], 
            CONFIRMING_AI_REPLY: [
                CallbackQueryHandler(send_ai_reply_to_user, pattern='^send_ai_reply:.*$'), 
                CallbackQueryHandler(cancel, pattern='^cancel_ai_reply$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(admin_reply_conv)

    # 8. Create News
    create_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_news_creation, pattern='^admin_create_news$')],
        states={
            WAITING_FOR_NEWS_TEXT: [MessageHandler(filters.TEXT, get_news_text)], 
            CONFIRMING_NEWS_ACTION: [CallbackQueryHandler(handle_news_action, pattern='^news_.*$')], 
            WAITING_FOR_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, get_news_media)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(create_news_conv)

    # 9. Schedule News
    schedule_news_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_schedule_news, pattern='^admin_schedule_news$')],
        states={
            WAITING_FOR_SCHEDULE_TEXT: [MessageHandler(filters.TEXT, get_schedule_text)], 
            WAITING_FOR_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO, get_schedule_media), 
                CommandHandler('skip_media', skip_media)
            ], 
            WAITING_FOR_SCHEDULE_TIME: [MessageHandler(filters.TEXT, get_schedule_time)], 
            CONFIRMING_SCHEDULE_POST: [
                CallbackQueryHandler(confirm_schedule_post, pattern='^confirm_schedule_post$'), 
                CallbackQueryHandler(cancel_schedule_post, pattern='^cancel_schedule_post$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(schedule_news_conv)

    # 10. Test Message
    test_message_conv = ConversationHandler(
        entry_points=[CommandHandler("testm", test_message_command)],
        states={
            WAITING_FOR_TEST_MESSAGE: [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, receive_test_message)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    app.add_handler(test_message_conv)
    
    # --- Callbacks ---
    app.add_handler(CallbackQueryHandler(admin_stats_handler, pattern='^admin_stats$'))
    app.add_handler(CallbackQueryHandler(website_update_handler, pattern='^(broadcast_website|cancel_website_update):.*$'))
    app.add_handler(CallbackQueryHandler(generate_post_from_site, pattern='^admin_generate_post$'))
    app.add_handler(CallbackQueryHandler(handle_post_broadcast_confirmation, pattern='^(confirm_post|cancel_post):.*$'))
    app.add_handler(CallbackQueryHandler(view_kb, pattern='^admin_kb_view$'))
    app.add_handler(CallbackQueryHandler(delete_kb_entry, pattern=r'^kb_delete:.*$'))
    app.add_handler(CallbackQueryHandler(toggle_kb_faq_status, pattern=r'^kb_faq_toggle:.*$'))
    app.add_handler(CallbackQueryHandler(faq_button_handler, pattern='^faq_key:'))
    app.add_handler(CallbackQueryHandler(view_scheduled_posts, pattern='^admin_view_scheduled$'))
    app.add_handler(CallbackQueryHandler(cancel_scheduled_job_button, pattern='^cancel_job:'))

    # Jobs
    app.job_queue.run_daily(check_website_for_updates, time=dt_time(hour=9, minute=0, tzinfo=pytz.timezone("Europe/Kyiv")))
    app.job_queue.run_repeating(ping_self_for_wakeup, interval=600, first=10)
    
    # Restore scheduled
    scheduled_posts = load_data(SCHEDULED_POSTS_FILE, [])
    for p in scheduled_posts:
        try:
            dt = datetime.fromisoformat(p['time'])
            if dt > datetime.now(pytz.utc):
                app.job_queue.run_once(scheduled_broadcast_job, dt, data=p, name=p['id'])
        except: pass

    # Webhook
    await app.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
    runner = await start_web_server(app)
    await app.start()
    
    try:
        while True: await asyncio.sleep(3600)
    finally:
        await app.bot.delete_webhook()
        await runner.cleanup()
        await app.stop()

if __name__ == '__main__':
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
