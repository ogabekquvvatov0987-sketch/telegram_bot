import sqlite3
import asyncio
import logging
import os
import random
import re
from datetime import datetime
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import pytz
import json
import time
import aiohttp
import aiogram.exceptions
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import types, Dispatcher
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ----------------- Config & Logger -----------------
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN .env faylida topilmadi")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5435595297"))
VIRUSTOTAL_API_KEY = os.getenv("VIRUSTOTAL_API_KEY")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# ----------------- Ma'lumotlar bazasi -----------------
DB_FILE = 'bot_database.db'
@contextmanager
def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    try:
        yield conn
    finally:
        conn.close()
def create_db():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                last_active REAL,
                profile TEXT,
                banned INTEGER DEFAULT 0,
                in_chat_with_admin INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                data TEXT,
                timestamp REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                chat_id INTEGER PRIMARY KEY,
                rating INTEGER,
                timestamp REAL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                chat_id INTEGER,
                details TEXT,
                timestamp REAL
            )
        """)
        conn.commit()
def migrate_db():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        # actions table: create if missing, add timestamp if absent
        cursor.execute("PRAGMA table_info(actions)")
        columns = [row[1] for row in cursor.fetchall()]
        if not columns:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT,
                    chat_id INTEGER,
                    details TEXT,
                    timestamp REAL
                )
            """)
        else:
            if 'timestamp' not in columns:
                cursor.execute("ALTER TABLE actions ADD COLUMN timestamp REAL DEFAULT 0")

        # orders table: create if missing, ensure timestamp column exists
        cursor.execute("PRAGMA table_info(orders)")
        columns = [row[1] for row in cursor.fetchall()]
        if not columns:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    data TEXT,
                    timestamp REAL
                )
            """)
        else:
            # if table exists but missing timestamp, add it (preserve existing rows)
            if 'timestamp' not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN timestamp REAL DEFAULT 0")
            # If older schema lacked id (rare), recreate preserving nothing is dangerous; keep safe - do not drop.

        # Add in_chat_with_admin to chats if missing
        cursor.execute("PRAGMA table_info(chats)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'in_chat_with_admin' not in columns:
            cursor.execute("ALTER TABLE chats ADD COLUMN in_chat_with_admin INTEGER DEFAULT 0")
        conn.commit()
def ensure_chat_exists(chat_id: int):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO chats (chat_id, last_active)
            VALUES (?, ?)
        """, (chat_id, time.time()))
        conn.commit()
def update_chat_activity(chat_id: int):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE chats SET last_active = ?
            WHERE chat_id = ?
        """, (time.time(), chat_id))
        conn.commit()
def get_chat_profile(chat_id: int):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT profile FROM chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in profile for chat_id {chat_id}")
    return None
def set_chat_profile(chat_id: int, profile: dict):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        profile_json = json.dumps(profile)
        cursor.execute("""
            UPDATE chats SET profile = ?, last_active = ?
            WHERE chat_id = ?
        """, (profile_json, time.time(), chat_id))
        conn.commit()
def delete_chat_profile(chat_id: int):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE chats SET profile = NULL, last_active = ?
            WHERE chat_id = ?
        """, (time.time(), chat_id))
        conn.commit()
def is_banned(chat_id: int) -> bool:
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT banned FROM chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row and row[0] == 1 if row else False
def set_banned(chat_id: int, banned: bool):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE chats SET banned = ?, last_active = ?
            WHERE chat_id = ?
        """, (1 if banned else 0, time.time(), chat_id))
        conn.commit()
def set_in_chat(chat_id: int, value: bool):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE chats SET in_chat_with_admin = ?, last_active = ?
            WHERE chat_id = ?
        """, (1 if value else 0, time.time(), chat_id))
        conn.commit()
def is_in_chat(chat_id: int) -> bool:
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT in_chat_with_admin FROM chats WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row and row[0] == 1 if row else False
def get_total_chats():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM chats")
        return cursor.fetchone()[0]
def save_order(order_data: dict):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        data_json = json.dumps(order_data)
        cursor.execute("""
            INSERT INTO orders (chat_id, data, timestamp)
            VALUES (?, ?, ?)
        """, (order_data['chat_id'], data_json, time.time()))
        order_id = cursor.lastrowid
        conn.commit()
        return str(order_id)
def get_orders():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, data, timestamp FROM orders ORDER BY timestamp DESC
        """)
        rows = cursor.fetchall()
        orders = {}
        for row in rows:
            try:
                data = json.loads(row[1])
                data['timestamp'] = row[2]
                orders[str(row[0])] = data
            except json.JSONDecodeError:
                pass
        return orders
def get_recent_actions():
    now = time.time()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, type, chat_id, details FROM actions
            WHERE timestamp > ? ORDER BY timestamp DESC
        """, (now - 86400,))
        rows = cursor.fetchall()
        actions = {}
        for row in rows:
            actions[str(row[0])] = {'type': row[1], 'chat_id': row[2], 'details': row[3]}
        return actions
def save_rating(chat_id: int, rating: int):
    ensure_chat_exists(chat_id)
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO ratings (chat_id, rating, timestamp)
            VALUES (?, ?, ?)
        """, (chat_id, rating, time.time()))
        conn.commit()
def get_average_rating():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT AVG(rating) FROM ratings")
        avg = cursor.fetchone()[0]
        return round(avg, 2) if avg else 0.0
def get_service_stats():
    now = time.time()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM orders WHERE timestamp > ?", (now - 86400,))
        rows = cursor.fetchall()
        stats = {}
        for row in rows:
            if row[0]:
                try:
                    data = json.loads(row[0])
                    service = data.get('operator', 'Unknown')
                    stats[service] = stats.get(service, 0) + 1
                except json.JSONDecodeError:
                    pass
        return stats
async def get_all_users_with_names():
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, profile FROM chats ORDER BY last_active DESC LIMIT 20
        """)
        rows = cursor.fetchall()
        chat_ids = [row[0] for row in rows]
    users = []
    for cid in chat_ids:
        try:
            chat = await bot.get_chat(cid)
            full_name = chat.full_name or f"User {cid}"
            username = f"@{chat.username}" if chat.username else ""
            display_name = f"{full_name} {username}".strip()
            profile = get_chat_profile(cid)
            users.append({'id': cid, 'name': display_name, 'profile': profile})
        except Exception as e:
            logger.error(f"Error getting chat {cid}: {e}")
            profile = get_chat_profile(cid)
            users.append({'id': cid, 'name': profile.get('ism_familya', f"User {cid}"), 'profile': profile})
    return users
def get_users_status():
    now = time.time()
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT chat_id, profile FROM chats WHERE last_active > ?
        """, (now - 300,))
        online_rows = cursor.fetchall()
        cursor.execute("""
            SELECT chat_id, profile FROM chats WHERE last_active <= ? AND last_active > 0
        """, (now - 300,))
        offline_rows = cursor.fetchall()
        online = [json.loads(row[1]).get('ism_familya', f"User {row[0]}") if row[1] else f"User {row[0]}" for row in online_rows]
        offline = [json.loads(row[1]).get('ism_familya', f"User {row[0]}") if row[1] else f"User {row[0]}" for row in offline_rows]
        return online, offline
async def broadcast_message(message: types.Message):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM chats WHERE banned = 0")
        users = cursor.fetchall()
    success = 0
    total = len(users)
    for user in users:
        chat_id = user[0]
        try:
            # Try to copy the original message (preserves media, captions, formatting)
            try:
                await bot.copy_message(chat_id=chat_id, from_chat_id=message.chat.id, message_id=message.message_id)
            except Exception:
                # Fallback to manual send if copy_message is not permitted for target chat
                if message.photo:
                    await bot.send_photo(chat_id, message.photo[-1].file_id, caption=message.caption or '')
                elif message.video:
                    await bot.send_video(chat_id, message.video.file_id, caption=message.caption or '')
                elif message.document:
                    await bot.send_document(chat_id, message.document.file_id, caption=message.caption or '')
                else:
                    await bot.send_message(chat_id, message.text or "")
            success += 1
        except aiogram.exceptions.BotBlocked:
            logger.warning(f"User {chat_id} blocked the bot")
        except Exception as e:
            logger.error(f"Broadcast xato {chat_id}: {e}")
    logger.info(f"Broadcast finished: {success}/{total} delivered")
    return success, total
def save_action(action_data: dict):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        details_json = json.dumps(action_data.get('details', {}))
        cursor.execute("""
            INSERT INTO actions (type, chat_id, details, timestamp)
            VALUES (?, ?, ?, ?)
        """, (action_data['type'], action_data['chat_id'], details_json, time.time()))
        conn.commit()
# ----------------- Bot / Dispatcher -----------------
create_db()
migrate_db()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Add admin_last_user_list (used by admin users/block lists)
admin_last_user_list = {}  # admin_id -> {'page': int, 'users': [...], 'total': int}

# Add admin chat mapping to track active admin <-> user sessions
admin_chat_targets = {}  # admin_id -> target_chat_id

# ----------------- New: global intercept for banned users -----------------
# If a user is banned, block all messages and callback queries (except ADMIN).
# These handlers are registered early so they run before other handlers and prevent any action.
@dp.message(lambda message: is_banned(getattr(message, "chat").id) and getattr(message, "chat").id != ADMIN_ID)
async def _blocked_user_message_intercept(message: types.Message, state: FSMContext):
	# Clear any FSM state and inform the user they are banned.
	try:
		await state.clear()
	except Exception:
		pass
	try:
		await message.reply("🚫 Siz botdan bloklangansiz. Botning hech qanday buyrug'idan foydalana olmaysiz. Agar buni xatolik  deb hisoblasangiz iltimos , admin bilan bog'laning.@Ogabekjon_26_01_06")
	except Exception:
		pass

@dp.callback_query(lambda cq: is_banned(getattr(cq, "from_user").id) and getattr(cq, "from_user").id != ADMIN_ID)
async def _blocked_user_callback_intercept(callback: types.CallbackQuery, state: FSMContext):
	try:
		await state.clear()
	except Exception:
		pass
	try:
		await callback.answer("🚫 Siz botdan bloklangansiz. Bu tugma siz uchun ishlamaydi.", show_alert=True)
	except Exception:
		try:
			await callback.message.reply("🚫 Siz botdan bloklangansiz. Tugmalar ishlamaydi.")
		except Exception:
			pass
# ----------------- Shtatlar -----------------
class RaqamTiklash(StatesGroup):
    operator = State()
    number = State()
    contact_method = State()
    contact_text = State()
    confirm = State()
    waiting_reply = State()
class RaqamBuyurtma(StatesGroup):
    mahalla = State()
    data = State()
    operator = State()
    file_choice = State()
    file_upload = State()
    location = State()
    phone_method = State()
    phone_text = State()
    confirm = State()
    waiting_reply = State()
class HumanCheck(StatesGroup):
    question = State()
class Feedback(StatesGroup):
    waiting = State()
class Reklama(StatesGroup):
    ad_type = State()
    details = State()
    style = State()
    attach_choice = State()
    file_upload = State()
    contact_method = State()
    contact_text = State()
    confirm = State()
    waiting_reply = State()
class Profil(StatesGroup):
    ism_familya = State()
    telefon = State()
    tuman_mahalla = State()
    confirm = State()
    edit_choice = State()
    edit_ism_familya = State()
    edit_telefon = State()
    edit_tuman_mahalla = State()
    delete_confirm = State()
class AdminBroadcast(StatesGroup):
    waiting = State()
class AdminChat(StatesGroup):
    chatting = State()  # foydalanuvchi admin bilan suhbatda
class Rating(StatesGroup):
    waiting = State()   # foydalan
# ----------------- UI yordamchilar -----------------
KB_MAIN_REPLY = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="/start")]], resize_keyboard=True, one_time_keyboard=False)
def get_cancel_kb(service_back: str = None) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ]
    if service_back:
        kb.append([InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data=service_back), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")])
    else:
        kb.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)
def get_main_menu(chat_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="📱 Raqam tiklash", callback_data="tiklash")],
        [InlineKeyboardButton(text="🆕 Raqam buyurtma", callback_data="buyurtma")],
        [InlineKeyboardButton(text="📰 Reklama", callback_data="reklama")],
        [InlineKeyboardButton(text="💬 Fikr bildirish", callback_data="feedback")],
        [InlineKeyboardButton(text="👤 Profil", callback_data="profil")],
        [InlineKeyboardButton(text="📞 Admin bilan chat", callback_data="admin_chat")],
        [InlineKeyboardButton(text="ℹ️ Bot haqida", callback_data="about_bot")]
    ]
    if chat_id == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👨‍💻 Admin panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)
KB_SUBSCRIPTION = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Kanalga obuna bo'lish", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")],
    [InlineKeyboardButton(text="Obuna bo'ldim, tekshirish", callback_data="check_subscription")]
])
KB_TIKLASH_OPERATOR = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📡 Ucell", callback_data="op_Ucell")],
    [InlineKeyboardButton(text="📡 Uzmobile", callback_data="op_Uzmobile")],
    [InlineKeyboardButton(text="📡 Beeline", callback_data="op_Beeline")],
    [InlineKeyboardButton(text="📡 Mobiuz", callback_data="op_Mobiuz")],
    [InlineKeyboardButton(text="📡 Humans", callback_data="op_Humans")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_tiklash_op"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_TIKLASH_CONTACT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 @username orqali bog'lanish", callback_data="ctm_username")],
    [InlineKeyboardButton(text="📞 Telefon raqami orqali", callback_data="ctm_text")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_tiklash_number"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_TIKLASH_CONFIRM = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ So'rovni yuborish", callback_data="tiklash_confirm_yes")],
    [InlineKeyboardButton(text="❌ So'rovni bekor qilish", callback_data="tiklash_confirm_no")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_tiklash_contact"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_BUYURTMA_OPERATOR = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📶 Humans", callback_data="bop_Humans")],
    [InlineKeyboardButton(text="📶 Uzmobile", callback_data="bop_Uzmobile")],
    [InlineKeyboardButton(text="📶 Ucell", callback_data="bop_Ucell")],
    [InlineKeyboardButton(text="📶 Beeline", callback_data="bop_Beeline")],
    [InlineKeyboardButton(text="📶 Mobiuz", callback_data="bop_Mobiuz")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_buyurtma_mah"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
def yes_no_kb(yes_cb, no_cb, back_cb) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, qo'shaman", callback_data=yes_cb)],
        [InlineKeyboardButton(text="❌ Yo'q, kerak emas", callback_data=no_cb)],
        [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data=back_cb), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
    ])
KB_BUYURTMA_PHONE = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 @username orqali bog'lanish", callback_data="phm_username")],
    [InlineKeyboardButton(text="📞 Telefon raqami orqali", callback_data="phm_text")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_buyurtma_file_choice"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_CONFIRM_SEND = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Buyurtmani yuborish", callback_data="confirm_yes")],
    [InlineKeyboardButton(text="✏️ Ma'lumotlarni tahrirlash", callback_data="confirm_edit")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_buyurtma_phone"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_REKLAMA_TYPES = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🖼️ Banner reklama", callback_data="rad_banner")],
    [InlineKeyboardButton(text="🔤 Matnli reklama", callback_data="rad_text")],
    [InlineKeyboardButton(text="🔢 Raqamli reklama", callback_data="rad_number")],
    [InlineKeyboardButton(text="🎥 Video reklama", callback_data="rad_video")],
    [InlineKeyboardButton(text="🔘 Boshqa turdagi reklama", callback_data="rad_other")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_reklama_type"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_REKLAMA_ATTACH = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📎 Ha, fayl qo'shaman", callback_data="rad_attach_yes")],
    [InlineKeyboardButton(text="❌ Yo'q, fayl qo'shmayman", callback_data="rad_attach_no")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_reklama_type"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_REKLAMA_CONTACT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 @username orqali bog'lanish", callback_data="rad_ctm_username")],
    [InlineKeyboardButton(text="📞 Telefon raqami orqali", callback_data="rad_ctm_text")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_reklama_attach"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_REKLAMA_CONFIRM = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Reklama so'rovini yuborish", callback_data="rad_confirm_yes")],
    [InlineKeyboardButton(text="✏️ Ma'lumotlarni tahrirlash", callback_data="rad_confirm_edit")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_reklama_contact"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_PROFIL_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✏️ Profilni tahrirlash", callback_data="profil_edit")],
    [InlineKeyboardButton(text="🗑️ Profilni o'chirish", callback_data="profil_delete")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_profil"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_PROFIL_CONSENT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Ha, ma'lumotlarimni saqlashga roziman", callback_data="profil_consent_yes")],
    [InlineKeyboardButton(text="❌ Yo'q, hozir saqlamayman", callback_data="profil_consent_no")],
    [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_PROFIL_EDIT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 Ism va familiya tahrirlash", callback_data="edit_ism")],
    [InlineKeyboardButton(text="📞 Telefon raqam tahrirlash", callback_data="edit_telefon")],
    [InlineKeyboardButton(text="🏙️ Tuman/mahalla tahrirlash", callback_data="edit_tuman")],
    [InlineKeyboardButton(text="💾 O'zgarishlarni saqlash", callback_data="profil_save_edit")],
    [InlineKeyboardButton(text="⬅️ Orqaga qaytish", callback_data="back_profil"), InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_CONFIRM_PROFIL = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Ha, ma'lumotlar to'g'ri, saqlash", callback_data="profil_confirm_yes")],
    [InlineKeyboardButton(text="✏️ Ma'lumotlarni tahrirlash", callback_data="profil_confirm_edit")],
    [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_DELETE_CONFIRM = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Ha, profilni o'chirish", callback_data="profil_delete_yes")],
    [InlineKeyboardButton(text="❌ Yo'q, o'chirmayman", callback_data="profil_delete_no")],
    [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
])
KB_ADMIN_PANEL = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📢 Reklama yuborish", callback_data="admin_broadcast")],
    [InlineKeyboardButton(text="📋 Barcha buyurtmalar", callback_data="admin_orders")],
    [InlineKeyboardButton(text="📊 Statistika ko'rish", callback_data="admin_stats")],
    [InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin_users")],
    [InlineKeyboardButton(text="📝 So'nggi harakatlar", callback_data="admin_actions")],
    [InlineKeyboardButton(text="🚫 Bloklanganlar", callback_data="admin_blocked")],
    [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")]
])
KB_SHARE_LOCATION = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Joylashuvni ulashish", request_location=True)]], resize_keyboard=True, one_time_keyboard=True)
KB_ADMIN_CHAT_EXIT = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🚪 Admin chatdan chiqish", callback_data="exit_admin_chat")]
])
KB_RATING = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⭐ 1 yulduz", callback_data="rate_1"), InlineKeyboardButton(text="⭐ 2 yulduz", callback_data="rate_2"), InlineKeyboardButton(text="⭐ 3 yulduz", callback_data="rate_3")],
    [InlineKeyboardButton(text="⭐ 4 yulduz", callback_data="rate_4"), InlineKeyboardButton(text="⭐ 5 yulduz", callback_data="rate_5")]
])
KB_FILE_DONE = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Barcha fayllar yuborildi", callback_data="file_done")]])
# ----------------- Mahalla ro'yhati -----------------
MAHALLALAR = [
    "Oyinli", "Xo'jaqiya-1", "Xo'jaqiya-2", "G'urjak-1", "G'urjak-2", "Bog'iobod", "Nurtepa", "Qizil olma", "Zarabog'", "Qorabog'",
    "Galaguzar", "Boybuloq", "Taroqli", "Majnuntol", "Mehrobod", "Vandob", "G'ambur", "Qo'rg'on", "Bahor", "Buyuk ipak yo'li",
    "Yoshlik", "Boyqishloq", "Do'stlik", "Dehqonariq", "Gulchinor", "G'o'rin Gilambob", "Guliston", "Mehnatabod", "Oqtepa",
    "Qulluqsho", "Qishloqbozor", "Katta hayot", "Katta bog'", "Istiqbol", "Uzunsoy", "Uch yog'och", "Oltin voha", "Hakimobod", "Navbur",
    "Sherobod", "Cho'yinchi", "Chuqurko'l", "Xo'jgi", "Cho'mishli", "Balxiguzar", "Chag'atoy", "Poshxurt"
]
PAGE_SIZE = 10
def kb_mahalla_page(page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(MAHALLALAR))
    rows = [[InlineKeyboardButton(text=f"🏘️ {MAHALLALAR[start + i]} mahallasi", callback_data=f"mah_sel_{start + i}")] for i in range(end - start)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi sahifa", callback_data=f"mah_page_{page - 1}"))
    if end < len(MAHALLALAR):
        nav.append(InlineKeyboardButton(text="➡️ Keyingi sahifa", callback_data=f"mah_page_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
# ----------------- Yordamchilar -----------------
async def is_working_hours() -> bool:
    tz = pytz.timezone("Asia/Tashkent")
    return 7 <= datetime.now(tz).hour < 24
def _generate_captcha():
    a, b = random.randint(2, 9), random.randint(2, 9)
    op = random.choice(["+", "-", "×"])
    if op == "-":
        a, b = max(a, b), min(a, b)
        res = a - b
    elif op == "×":
        res = a * b
    else:
        res = a + b
    return f"{a} {op} {b} = ?", res
async def _ask_captcha(message: types.Message, state: FSMContext):
    text, res = _generate_captcha()
    await state.set_state(HumanCheck.question)
    await state.update_data(captcha_text=text, captcha_result=res, captcha_attempts=0)
    await message.answer(f"🧠 Inson ekanligingizni tasdiqlang:\n<code>{text}</code>\nFaqat son yuboring.", reply_markup=KB_MAIN_REPLY)
async def safe_edit_or_send(obj, text: str, reply_markup=None):
    try:
        if isinstance(obj, types.CallbackQuery):
            await obj.message.edit_text(text, reply_markup=reply_markup)
        elif isinstance(obj, types.Message):
            await obj.answer(text, reply_markup=reply_markup)
        else:
            await obj.answer(text, reply_markup=reply_markup)
    except Exception:
        try:
            if isinstance(obj, types.CallbackQuery):
                await obj.answer(text, reply_markup=reply_markup)
            else:
                await obj.answer(text, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
@asynccontextmanager
async def get_aiohttp_session():
    async with aiohttp.ClientSession() as session:
        yield session
async def show_progress(message: types.Message):
    progress_msg = await message.answer("⏳ Fayl yuklanmoqda va tekshirilmoqda. Iltimos, kuting...")
    await asyncio.sleep(1)
    await progress_msg.delete()
async def check_file_for_virus(file_id: str, content_type: str) -> bool:
    if not VIRUSTOTAL_API_KEY:
        return True
    try:
        file = await bot.get_file(file_id)
        if file.file_size > 32 * 1024 * 1024:
            return False
        file_path = Path(f"temp_{file_id}")
        await bot.download_file(file.file_path, file_path)
        async with get_aiohttp_session() as session:
            form = aiohttp.FormData()
            form.add_field('file', open(file_path, 'rb'), filename=file_path.name)
            headers = {'x-apikey': VIRUSTOTAL_API_KEY}
            resp = await session.post('https://www.virustotal.com/api/v3/files', headers=headers, data=form)
            if resp.status != 200:
                return False
            data = await resp.json()
            analysis_id = data['data']['id']
            start_time = time.time()
            while time.time() - start_time < 120:
                resp = await session.get(f'https://www.virustotal.com/api/v3/analyses/{analysis_id}', headers=headers)
                if resp.status != 200:
                    return False
                result = await resp.json()
                if result['data']['attributes']['status'] == 'completed':
                    stats = result['data']['attributes']['stats']
                    return stats.get('malicious', 0) == 0 and stats.get('suspicious', 0) == 0
                await asyncio.sleep(10)
            return False
    except Exception as e:
        logger.error(f"Virus check error: {e}")
        return False
    finally:
        if 'file_path' in locals() and file_path.exists():
            file_path.unlink()
async def send_waiting_reminder(chat_id: int, service: str):
    await asyncio.sleep(300)
    try:
        await bot.send_message(chat_id, f"⌛ Iltimos, kutib turing. Sizning {service} so'rovingiz hozir ko'rib chiqilmoqda. Javob tez orada keladi.")
    except:
        pass
async def auto_reset_state(state: FSMContext, chat_id: int, service: str):
    await asyncio.sleep(900)
    cur_state = await state.get_state()
    if cur_state and str(cur_state).endswith('.waiting_reply'):
        await state.clear()
        try:
            await bot.send_message(chat_id, f"⌛ 15 daqiqa ichida javob kelmadi. {service} so'rovingiz bekor qilindi. Bosh menyuga qaytildi.", reply_markup=get_main_menu(chat_id))
        except:
            pass
async def send_reminder(order_id: str):
    await asyncio.sleep(300)
    try:
        await bot.send_message(ADMIN_ID, f"⌛ Eslatma: Buyurtma ID {order_id} ga hali javob berilmagan. Iltimos, ko'rib chiqing.")
    except:
        pass
async def send_buyurtma_preview(obj, state: FSMContext):
    data = await state.get_data()
    loc = data.get('location', {})
    caption = (
        f"🧾 Buyurtma ma'lumotlari:\n"
        f"🏘️ Mahalla: {data.get('mahalla', 'Kiritilmagan')}\n"
        f"📝 Qo'shimcha ma'lumot: {data.get('malumot', 'Kiritilmagan')}\n"
        f"📶 Operator: {data.get('operator', 'Kiritilmagan')}\n"
        f"📍 Joylashuv: {loc.get('lat', 'N/A')}, {loc.get('lon', 'N/A')}\n"
        f"📞 Bog'lanish usuli: {data.get('phone', 'Kiritilmagan')}\n\n"
        f"Bu ma'lumotlar to'g'ri va to'liqmi? Agar yo'q bo'lsa, tahrirlang."
    )
    await safe_edit_or_send(obj, caption, KB_CONFIRM_SEND)
# ----------------- Handlers -----------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if is_banned(chat_id):
        await message.answer("❌ Siz botdan bloklangansiz. Savollaringiz bo'lsa, admin bilan bog'laning.+998955954727")
        return
    if not await is_working_hours():
        await message.answer("⏰ Bot ish vaqti: 07:00 dan 24:00 gacha. Ertaga qayta urinib ko'ring.")
        return
    data = await state.get_data()
    if REQUIRED_CHANNEL:
        try:
            member = await bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id)
            if member.status in ("left", "kicked"):
                await message.answer(f"❗ Botdan foydalanish uchun {REQUIRED_CHANNEL} kanaliga obuna bo'ling va obuna bo'lganingizni tasdiqlang.", reply_markup=KB_SUBSCRIPTION)
                return
        except Exception as e:
            logger.error(f"Obuna xato: {e}")
            await message.answer("❌ Obuna tekshirishda texnik xato yuz berdi. Qayta urinib ko'ring yoki admin bilan bog'laning.")
            return
    if not data.get("verified"):
        await _ask_captcha(message, state)
        return
    profile = get_chat_profile(chat_id)
    name = profile.get('ism_familya', message.from_user.first_name) if profile else message.from_user.first_name
    await message.answer(f"👋 Xush kelibsiz, {name}! Bot orqali quyidagi xizmatlardan foydalanishingiz mumkin:\n\nQuyidagi tugmalardan birini tanlang va  ko'rsatmalarga amal qiling.", reply_markup=get_main_menu(chat_id))
@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if is_banned(chat_id):
        await callback.answer("❌ Siz botdan bloklangansiz!", show_alert=True)
        return
    if not await is_working_hours():
        await callback.answer("⏰ Bot ish vaqti: 07:00-24:00", show_alert=True)
        return
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, callback.from_user.id)
        if member.status in ("left", "kicked"):
            await safe_edit_or_send(callback, f"❌ Hali {REQUIRED_CHANNEL} kanaliga obuna bo'lmagansiz. Obuna bo'ling va 'Obuna bo'ldim' tugmasini bosing.", KB_SUBSCRIPTION)
        else:
            data = await state.get_data()
            if not data.get("verified"):
                await _ask_captcha(callback.message, state)
            else:
                await safe_edit_or_send(callback, "🎉 Obuna muvaffaqiyatli tasdiqlandi! Endi bot xizmatlaridan foydalanishingiz mumkin. Quyidagi menyudan xizmat tanlang.", get_main_menu(chat_id))
    except Exception as e:
        logger.error(f"Obuna xato: {e}")
        await callback.answer("❌ Obuna tekshirishda xato yuz berdi. Qayta urinib ko'ring.", show_alert=True)
@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.clear()
    await safe_edit_or_send(callback, "🏠 Bosh menyuga qaytdingiz.Quyidagi tugmalardan birini tanlang va  ko'rsatmalarga amal qiling.", get_main_menu(chat_id))
@dp.callback_query(F.data == "cancel_service")
async def cancel_service(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.clear()
    await safe_edit_or_send(callback, "❌ Xizmat bekor qilindi. Bosh menyuga qaytdingiz.", get_main_menu(chat_id))
@dp.callback_query(F.data == "feedback")
async def feedback_start(cb: types.CallbackQuery, state: FSMContext):
    chat_id = cb.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await cb.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga fikringizni yuboring.")
        return
    await state.set_state(Feedback.waiting)
    await safe_edit_or_send(cb, "💬 Iltimos fikr mulohazangizni qoldiring,bu biz uchin muhim", get_cancel_kb())
@dp.message(StateFilter(Feedback.waiting))
async def handle_feedback(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    user_text = message.text or ""
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    user_name = message.from_user.full_name or message.from_user.username or 'Noma\'lum'
    await bot.send_message(ADMIN_ID, f"💬 Foydalanuvchi fikri:\n👤 Foydalanuvchi: {user_name}{profile_text}\n\n📝 Fikr matni:\n{user_text}\n\n🆔 Chat ID: {chat_id}")
    save_action({'type': 'fikr', 'chat_id': chat_id, 'details': user_text})
    await message.answer("✅ Fikr takliflaringiz uchun katta rahmat!,sizning fikringiz biz uchun muhim", reply_markup=get_main_menu(chat_id))
    await state.clear()
@dp.message(StateFilter(HumanCheck.question))
async def human_check_answer(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    correct = data.get("captcha_result")
    attempts = data.get("captcha_attempts", 0)
    try:
        user_val = int(message.text.strip())
    except ValueError:
        attempts += 1
        await state.update_data(captcha_attempts=attempts)
        if attempts >= 3:
            set_banned(chat_id, True)
            await message.reply("❌ Noto'g'ri urinishlar soni ko'p. Siz botdan banlangansiz.")
            await state.clear()
            return
        await message.reply("❌ Faqat son yuboring. Qayta urinib ko'ring.")
        return
    if user_val == correct:
        await state.update_data(verified=True)
        await state.clear()
        await message.answer("✅ Sizning inson ekanligingiz tasdiqlandi! Endi bot xizmatlaridan to'liq foydalanishingiz mumkin. Quyidagi menyudan xizmat tanlang.", reply_markup=get_main_menu(chat_id))
    else:
        attempts += 1
        await state.update_data(captcha_attempts=attempts)
        if attempts >= 3:
            set_banned(chat_id, True)
            await message.reply("❌ Noto'g'ri urinishlar soni ko'p. Siz botdan banlangansiz.")
            await state.clear()
            return
        await message.reply("❌ Javob noto'g'ri. Qayta urinib ko'ring.")
        await _ask_captcha(message, state)
@dp.callback_query(F.data == "about_bot")
async def about_bot(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    text = """
ℹ️ <b>Bot haqida to'liq ma'lumot:</b>
Bu bot mobil aloqa xizmatlari bilan bog'liq masalalar uchun mo'ljallangan:
• <b>📱 Raqam tiklash:</b> Yo'qolgan yoki bloklangan raqamingizni tiklash uchun ariza berish.
• <b>🆕 Raqam buyurtma:</b> Yangi raqam olish uchun mahalla, operator va qo'shimcha ma'lumotlar bilan ariza.
• <b>📰 Reklama:</b> Banner, matn, video yoki boshqa turdagi reklama joylashtirish so'rovi.
• <b>💬 Fikr bildirish:</b> Bot haqida fikr va takliflaringizni yuborish.
• <b>👤 Profil:</b> Shaxsiy ma'lumotlaringizni saqlash va tahrirlash (maxfiylik kafolatlangan).
• <b>📞 Admin bilan chat:</b> Real vaqtda savol-javob uchun suhbat.
<b>Diqqat:</b> Ma'lumotlarni to'g'ri va to'liq kiriting. Noto'g'ri arizalar ko'rib chiqilmaydi. Ish vaqti: 07:00–24:00.
Agar muammo yuzaga kelsa, admin bilan bog'laning.
"""
    await safe_edit_or_send(callback, text, get_main_menu(chat_id))
# ----------------- Profil -----------------
@dp.callback_query(F.data == "profil")
async def profil_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    profile = get_chat_profile(chat_id)
    if profile:
        text = (
            f"👤 <b>Sizning profil ma'lumotlaringiz:</b>\n\n"
            f"👤 Ism va familiya: {profile.get('ism_familya', 'Kiritilmagan')}\n"
            f"📞 Telefon raqami: {profile.get('telefon', 'Kiritilmagan')}\n"
            f"🏙️ Tuman yoki mahalla: {profile.get('tuman_mahalla', 'Kiritilmagan')}\n\n"
            f"Quyidagi tugmalardan profilni tahrirlash yoki o'chirishni tanlang."
        )
        await safe_edit_or_send(callback, text, KB_PROFIL_MENU)
    else:
        await safe_edit_or_send(callback, "📝 <b>Profil yaratish:</b>\n\nShaxsiy ma'lumotlaringizni saqlashga rozimisiz. Bu ma'lumotlar faqat xizmat uchun ishlatiladi va maxfiy saqlanadi. Ma'lumotlaringizni saqlashga rozimisiz? (Agar rozi bo'lmasangiz, har safar qo'lda kiritishingiz mumkin.)", KB_PROFIL_CONSENT)
@dp.callback_query(F.data == "profil_consent_yes")
async def profil_consent_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Profil.ism_familya)
    await safe_edit_or_send(callback, "👤 <b>Profil yaratish bosqichi 1/3:</b>\n\nIsm va familiyangizni to'liq kiriting (masalan: 'Quvvatov Og'abek Baxtiyor O'g'li'):", get_cancel_kb())
@dp.callback_query(F.data == "profil_consent_no")
async def profil_consent_no(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await safe_edit_or_send(callback, "❌ <b>Profil saqlash bekor qilindi.</b>\n\nProfil sizda mavjud emas. Xizmatlardan foydalanishda har safar ma'lumotlarni qo'lda kiritishingiz mumkin. Bosh menyuga qaytish uchun tugmani bosing.", get_main_menu(chat_id))
@dp.message(StateFilter(Profil.ism_familya))
async def profil_ism_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    ism = message.text.strip()
    if not ism or len(ism) < 2:
        await message.reply("❌ Ism va familiya to'liq va to'g'ri kiriting. Kamida 2 harf bo'lishi kerak. Qayta urinib ko'ring.", reply_markup=get_cancel_kb())
        return
    await state.update_data(ism_familya=ism)
    await state.set_state(Profil.telefon)
    await message.answer("📞 <b>Profil yaratish bosqichi 2/3:</b>\n\nTelefon raqamingizni kiriting (masalan: +99895....47). Faqat O'zbekiston raqamlari qabul qilinadi.", reply_markup=get_cancel_kb())
@dp.message(StateFilter(Profil.telefon))
async def profil_telefon_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    phone = message.text.strip()
    if not re.fullmatch(r"\+?\d{9,15}", phone):
        await message.reply("❌ Telefon raqami noto'g'ri formatda. +998 bilan boshlanishi va 9-12 xonali raqam bo'lishi kerak. Qayta kiriting.", reply_markup=get_cancel_kb())
        return
    await state.update_data(telefon=phone)
    await state.set_state(Profil.tuman_mahalla)
    await message.answer("🏙️ <b>Profil yaratish bosqichi 3/3:</b>\n\nTuman yoki mahallangizni kiriting (masalan: 'sherobod tumani, katta hayot mahallasi'):", reply_markup=get_cancel_kb())
@dp.message(StateFilter(Profil.tuman_mahalla))
async def profil_tuman_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    tuman = message.text.strip()
    if not tuman or len(tuman) < 2:
        await message.reply("❌ Tuman yoki mahalla nomini to'liq kiriting. Qayta urinib ko'ring.", reply_markup=get_cancel_kb())
        return
    await state.update_data(tuman_mahalla=tuman)
    await state.set_state(Profil.confirm)
    data = await state.get_data()
    text = (
        f"👤 <b>Profil ma'lumotlarini tasdiqlash:</b>\n\n"
        f"👤 Ism va familiya: {data['ism_familya']}\n"
        f"📞 Telefon raqami: {data['telefon']}\n"
        f"🏙️ Tuman yoki mahalla: {data['tuman_mahalla']}\n\n"
        f"<i>Bu ma'lumotlar to'g'ri va to'liqmi? Agar ha bo'lsa, saqlang. Yo'q bo'lsa, tahrirlang.</i>\n\n"
        f"<b>Eslatma:</b> Profil ma'lumotlari maxfiy saqlanadi va faqat xizmat uchun ishlatiladi."
    )
    await message.answer(text, reply_markup=KB_CONFIRM_PROFIL)
@dp.callback_query(StateFilter(Profil.confirm), F.data == "profil_confirm_yes")
async def profil_confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    profile = {
        "ism_familya": data.get("ism_familya", ""),
        "telefon": data.get("telefon", ""),
        "tuman_mahalla": data.get("tuman_mahalla", ""),
    }
    set_chat_profile(chat_id, profile)
    await state.clear()
    await safe_edit_or_send(callback, "✅ <b>Profil muvaffaqiyatli saqlandi!</b>\n\nEndi xizmatlardan foydalanganda ma'lumotlar avtomatik to'ldiriladi. Boshqa o'zgarishlar uchun profil bo'limiga qayting.", get_main_menu(chat_id))
@dp.callback_query(StateFilter(Profil.confirm), F.data == "profil_confirm_edit")
async def profil_confirm_edit(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Profil.ism_familya)
    await safe_edit_or_send(callback, "👤 <b>Ma'lumotlarni tahrirlash:</b>\n\nIsm va familiyangizni qayta kiriting:", get_cancel_kb())
@dp.callback_query(F.data == "profil_delete")
async def profil_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Profil.delete_confirm)
    await safe_edit_or_send(callback, "🗑️ <b>Profilni o'chirish tasdiqlash:</b>\n\n<b>Ogohlantirish:</b> Profilni o'chirish saqlangan barcha ma'lumotlaringizni (ism, telefon, tuman/mahalla) o'chiradi. Xizmatlardan foydalanishda ularni qayta kiritishingiz kerak bo'ladi. Rostan profilni o'chirishni xohlaysizmi?", KB_DELETE_CONFIRM)
@dp.callback_query(StateFilter(Profil.delete_confirm), F.data == "profil_delete_yes")
async def profil_delete_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    delete_chat_profile(chat_id)
    await state.clear()
    await safe_edit_or_send(callback, "🗑️ <b>Profil muvaffaqiyatli o'chirildi.</b>\n\nEndi profil mavjud emas. Xizmatlardan foydalanishda ma'lumotlarni qo'lda kiritishingiz mumkin. Yangi profil yaratish uchun 'Profil' bo'limiga qayting.", get_main_menu(chat_id))
@dp.callback_query(StateFilter(Profil.delete_confirm), F.data == "profil_delete_no")
async def profil_delete_no(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.clear()
    await safe_edit_or_send(callback, "❌ <b>Profil o'chirish bekor qilindi.</b>\n\nProfilingiz saqlanib qoldi. Boshqa harakatlar uchun menyudan tanlang.", get_main_menu(chat_id))
@dp.callback_query(F.data == "profil_edit")
async def profil_edit(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Profil.edit_choice)
    await safe_edit_or_send(callback, "✏️ <b>Profil tahrirlash menyusi:</b>\n\nQaysi ma'lumotni o'zgartirmoqchisiz? Tanlang va yangi qiymatni kiriting. Saqlash tugmasini bosgandan keyin o'zgarishlar amalga oshiriladi.", KB_PROFIL_EDIT)
@dp.callback_query(StateFilter(Profil.edit_choice), F.data.startswith("edit_"))
async def profil_edit_field(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    field = callback.data.split("_")[1]
    if field == "ism":
        await state.set_state(Profil.edit_ism_familya)
        await safe_edit_or_send(callback, "👤 <b>Ism va familiya tahrirlash:</b>\n\nYangi ism va familiyangizni kiriting (masalan: 'Quvvatov Og'abek Baxtiyor o'g'li'):", get_cancel_kb("back_profil"))
    elif field == "telefon":
        await state.set_state(Profil.edit_telefon)
        await safe_edit_or_send(callback, "📞 <b>Telefon raqam tahrirlash:</b>\n\nYangi telefon raqamingizni kiriting (masalan: +99891.....66):", get_cancel_kb("back_profil"))
    elif field == "tuman":
        await state.set_state(Profil.edit_tuman_mahalla)
        await safe_edit_or_send(callback, "🏙️ <b>Tuman/mahalla tahrirlash:</b>\n\nYangi tuman yoki mahallangizni kiriting (masalan: 'sherobod tumani , katta hayot mahallasi'):", get_cancel_kb("back_profil"))
@dp.message(StateFilter(Profil.edit_ism_familya))
async def profil_edit_ism_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    ism = message.text.strip()
    if not ism or len(ism) < 2:
        await message.reply("❌ Ism va familiya to'liq va to'g'ri kiriting. Qayta urinib ko'ring.", reply_markup=get_cancel_kb("back_profil"))
        return
    await state.update_data(edit_ism_familya=ism)
    await state.set_state(Profil.edit_choice)
    await message.answer("✅ <b>Ism va familiya muvaffaqiyatli yangilandi.</b>\n\nBoshqa o'zgarishlar uchun menyudan tanlang yoki 'Saqlash' tugmasini bosing.", reply_markup=KB_PROFIL_EDIT)
@dp.message(StateFilter(Profil.edit_telefon))
async def profil_edit_telefon_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    phone = message.text.strip()
    if not re.fullmatch(r"\+?\d{9,15}", phone):
        await message.reply("❌ Telefon raqami noto'g'ri formatda. +998 bilan boshlanishi va 9-12 xonali raqam bo'lishi kerak. Qayta kiriting.", reply_markup=get_cancel_kb("back_profil"))
        return
    await state.update_data(edit_telefon=phone)
    await state.set_state(Profil.edit_choice)
    await message.answer("✅ <b>Telefon raqam muvaffaqiyatli yangilandi.</b>\n\nBoshqa o'zgarishlar uchun menyudan tanlang yoki 'Saqlash' tugmasini bosing.", reply_markup=KB_PROFIL_EDIT)
@dp.message(StateFilter(Profil.edit_tuman_mahalla))
async def profil_edit_tuman_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    tuman = message.text.strip()
    if not tuman or len(tuman) < 2:
        await message.reply("❌ Tuman yoki mahalla nomini to'liq kiriting. Qayta urinib ko'ring.", reply_markup=get_cancel_kb("back_profil"))
        return
    await state.update_data(edit_tuman_mahalla=tuman)
    await state.set_state(Profil.edit_choice)
    await message.answer("✅ <b>Tuman/mahalla muvaffaqiyatli yangilandi.</b>\n\nBoshqa o'zgarishlar uchun menyudan tanlang yoki 'Saqlash' tugmasini bosing.", reply_markup=KB_PROFIL_EDIT)
@dp.callback_query(StateFilter(Profil.edit_choice), F.data == "profil_save_edit")
async def profil_save_edit(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    old_profile = get_chat_profile(chat_id) or {}
    new_profile = old_profile.copy()
    if 'edit_ism_familya' in data:
        new_profile['ism_familya'] = data['edit_ism_familya']
    if 'edit_telefon' in data:
        new_profile['telefon'] = data['edit_telefon']
    if 'edit_tuman_mahalla' in data:
        new_profile['tuman_mahalla'] = data['edit_tuman_mahalla']
    set_chat_profile(chat_id, new_profile)
    await state.clear()
    await safe_edit_or_send(callback, "✅ <b>Profil muvaffaqiyatli yangilandi!</b>\n\nO'zgarishlar saqlandi. Profil bo'limiga qaytib, yangi ma'lumotlarni ko'rishingiz mumkin.", get_main_menu(chat_id))
@dp.callback_query(F.data == "back_profil")
async def back_profil(callback: types.CallbackQuery, state: FSMContext):
    await back_main(callback, state)
# ----------------- Raqam tiklash -----------------
@dp.callback_query(F.data == "tiklash")
async def tiklash_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    # NEW: require profile
    profile = get_chat_profile(chat_id)
    if not profile:
        await safe_edit_or_send(callback, "❗ Ushbu xizmatdan foydalanish uchun avval profil ma'lumotlaringizni to'ldiring. Iltimos, profil bo'limiga o'ting va ma'lumotlarni saqlang.", KB_PROFIL_CONSENT)
        return
    await state.clear()
    await state.set_state(RaqamTiklash.operator)
    await safe_edit_or_send(callback, "📱 <b>Raqam tiklash xizmati:</b>\n\nYo'qolgan yoki bloklangan raqamingizni tiklash uchun operatorni tanlang. Keyingi qadamda raqam va bog'lanish usulini kiritasiz.", KB_TIKLASH_OPERATOR)
@dp.callback_query(StateFilter(RaqamTiklash.operator), F.data.startswith("op_"))
async def tiklash_operator_selected(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    operator = callback.data.split("_", 1)[1]
    await state.update_data(operator=operator)
    await state.set_state(RaqamTiklash.number)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Operator tanlashga qaytish", callback_data="back_tiklash_op")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "📱 <b>Raqam tiklash bosqichi 2/3:</b>\n\nTiklanishi kerak bo'lgan telefon raqamingizni kiriting (masalan:+99895.....27).", markup)
@dp.callback_query(F.data == "back_tiklash_op")
async def back_tiklash_op(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamTiklash.operator)
    await safe_edit_or_send(callback, "📱 <b>Raqam tiklash xizmati:</b>\n\nOperatorni tanlang.", KB_TIKLASH_OPERATOR)
@dp.message(StateFilter(RaqamTiklash.number))
async def tiklash_number_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    text = message.text.strip()
    if not re.fullmatch(r"(\+998)?\d{9}", text):
        await message.reply("❌ Telefon raqami noto'g'ri formatda. +998 bilan boshlanishi mumkin. Masalan:+99895.....27. Qayta kiriting.", reply_markup=get_cancel_kb("back_tiklash_op"))
        return
    number = text if text.startswith("+998") else "+998" + text.lstrip("+")
    await state.update_data(number=number)
    await state.set_state(RaqamTiklash.contact_method)
    await message.answer("📞 <b>Raqam tiklash bosqichi 3/3:</b>\n\nBog'lanish usulini tanlang.", reply_markup=KB_TIKLASH_CONTACT)
@dp.callback_query(StateFilter(RaqamTiklash.contact_method), F.data == "ctm_username")
async def tiklash_ct_username(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    username = callback.from_user.username
    contact = f"@{username}" if username else "Username topilmadi, telefon kiriting"
    await state.update_data(contact=contact)
    await state.set_state(RaqamTiklash.confirm)
    data = await state.get_data()
    text = (
        f"📩 <b>Raqam tiklash so'rovini tasdiqlash:</b>\n\n"
        f"📶 Operator: {data['operator']}\n"
        f"📱 Tiklanadigan raqam: {data['number']}\n"
        f"📞 Bog'lanish usuli: {contact}\n\n"
        f"<i>Bu ma'lumotlar to'g'ri va to'liqmi? Agar ha bo'lsa, so'rov adminga yuboriladi va ko'rib chiqiladi.</i>"
    )
    await safe_edit_or_send(callback, text, KB_TIKLASH_CONFIRM)
@dp.callback_query(StateFilter(RaqamTiklash.contact_method), F.data == "ctm_text")
async def tiklash_ct_text(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamTiklash.contact_text)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Bog'lanish usuliga qaytish", callback_data="back_tiklash_ctm")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish uchun telefon raqamini kiriting:</b>\n\n", markup)
@dp.callback_query(F.data == "back_tiklash_ctm")
async def back_tiklash_ctm(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamTiklash.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>", KB_TIKLASH_CONTACT)
@dp.message(StateFilter(RaqamTiklash.contact_text))
async def tiklash_ct_text_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    phone = message.text.strip()
    if not re.fullmatch(r"\+998\d{9}", phone):
        await message.reply("❌ Telefon raqami +998 bilan boshlanishi va 12 xonali raqam bo'lishi kerak. Masalan: +99891.....66. Qayta kiriting.", reply_markup=get_cancel_kb("back_tiklash_ctm"))
        return
    await state.update_data(contact=phone)
    await state.set_state(RaqamTiklash.confirm)
    data = await state.get_data()
    text = (
        f"📩 <b>Raqam tiklash so'rovini tasdiqlash:</b>\n\n"
        f"📶 Operator: {data['operator']}\n"
        f"📱 Tiklanadigan raqam: {data['number']}\n"
        f"📞 Bog'lanish usuli: {phone}\n\n"
        f"<i>Bu ma'lumotlar to'g'ri va to'liqmi? Agar ha bo'lsa, so'rov adminga yuboriladi va ko'rib chiqiladi.</i>"
    )
    await message.answer(text, reply_markup=KB_TIKLASH_CONFIRM)
@dp.callback_query(StateFilter(RaqamTiklash.confirm), F.data == "tiklash_confirm_yes")
async def tiklash_confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    text = (
        f"📩 <b>Raqam tiklash so'rovi keldi:</b>\n\n"
        f"📶 Operator: {data['operator']}\n"
        f"📱 Tiklanadigan raqam: {data['number']}\n"
        f"📞 Bog'lanish usuli: {data['contact']}{profile_text}\n\n"
        f"🆔 Foydalanuvchi ID: {callback.from_user.id}\n\n"
        f"<i>Iltimos, bu so'rovni tez orada ko'rib chiqing va foydalanuvchiga javob bering.</i>"
    )
    await bot.send_message(ADMIN_ID, text)
    save_action({
        'type': 'raqam_tiklash',
        'chat_id': callback.from_user.id,
        'details': f"Operator: {data['operator']}, Raqam: {data['number']}, Bog'lanish: {data['contact']}"
    })
    await state.set_state(RaqamTiklash.waiting_reply)
    await safe_edit_or_send(callback, "✅ <b>Raqam tiklash so'rovingiz adminga muvaffaqiyatli yuborildi!</b>\n\nIltimos, kutib turing. So'rov ko'rib chiqilmoqda va javob tez orada keladi. Boshqa xizmatlar uchun menyudan tanlang.", get_main_menu(chat_id))
    asyncio.create_task(send_waiting_reminder(chat_id, "raqam tiklash so'rovingiz"))
    asyncio.create_task(auto_reset_state(state, chat_id, "Raqam tiklash so'rovi"))
@dp.callback_query(StateFilter(RaqamTiklash.confirm), F.data == "tiklash_confirm_no")
async def tiklash_confirm_no(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.clear()
    await safe_edit_or_send(callback, "❌ <b>Raqam tiklash so'rovi bekor qilindi.</b>\n\nAgar fikringiz o'zgarsa, 'Raqam tiklash' bo'limidan qayta boshlang.", get_main_menu(chat_id))
@dp.callback_query(F.data == "back_tiklash_number")
async def back_tiklash_number(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamTiklash.number)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Operator tanlashga qaytish", callback_data="back_tiklash_op")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "📱 <b>Raqam tiklash bosqichi 2/3:</b>\n\nTiklanishi kerak bo'lgan telefon raqamingizni kiriting.", markup)
@dp.callback_query(F.data == "back_tiklash_contact")
async def back_tiklash_contact(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamTiklash.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>", KB_TIKLASH_CONTACT)
@dp.message(StateFilter(RaqamTiklash.waiting_reply))
async def tiklash_waiting_reply(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not is_in_chat(chat_id):
        return
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    txt = f"📨 <b>Raqam tiklash so'rovi bo'yicha javob:</b>\n\n{message.text or 'Fayl yuborildi'}{profile_text}\n\n🆔 Chat ID: {chat_id}"
    try:
        if message.photo:
            await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=txt)
        elif message.document:
            await bot.send_document(ADMIN_ID, message.document.file_id, caption=txt)
        elif message.video:
            await bot.send_video(ADMIN_ID, message.video.file_id, caption=txt)
        else:
            await bot.send_message(ADMIN_ID, txt)
        await message.answer("✉️ <b>Xabaringiz adminga muvaffaqiyatli yuborildi.</b>\n\nJavobni kuting. Suhbatdan chiqish uchun chiqish tugmasini bosing.", reply_markup=KB_ADMIN_CHAT_EXIT)
        save_action({'type': 'tiklash_reply', 'chat_id': chat_id, 'details': message.text or 'media'})
    except Exception as e:
        logger.error(f"Tiklash reply xato: {e}")
        await message.answer("⚠️ Xabar yuborishda texnik xato yuz berdi. Qayta urinib ko'ring.")
# ----------------- Reklama -----------------
@dp.callback_query(F.data == "reklama")
async def reklama_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")

        return
    await state.clear()
    await state.update_data(files=[])
    await state.set_state(Reklama.ad_type)
    await safe_edit_or_send(callback, "📰 <b>Reklama xizmati:</b>\n\nReklama turini tanlang. Keyingi qadamda tafsilotlar va bog'lanish ma'lumotlarini kiritasiz. So'rov adminga yuborilgach, ko'rib chiqiladi.", KB_REKLAMA_TYPES)
@dp.callback_query(StateFilter(Reklama.ad_type), F.data.startswith("rad_"))
async def reklama_type_selected(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    ad_type = "Boshqa turdagi reklama" if callback.data == "rad_other" else f"{callback.data.split('_', 1)[1].capitalize()} reklama"
    await state.update_data(ad_type=ad_type)
    await state.set_state(Reklama.details)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Reklama turiga qaytish", callback_data="back_reklama_type")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "✍️ <b>Reklama tafsilotlarini kiriting:</b>\n\nReklama haqida batafsil ma'lumot yozing (o'lcham, rang, joylashuv talablari va h.k.). Iltimos, aniq va to'liq yozing.", markup)
@dp.callback_query(F.data == "back_reklama_type")
async def back_reklama_type(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.ad_type)
    await safe_edit_or_send(callback, "📰 <b>Reklama turini tanlang:</b>", KB_REKLAMA_TYPES)
@dp.message(StateFilter(Reklama.details))
async def reklama_details_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    details = message.text.strip()
    if not details or len(details) < 10:
        await message.reply("❌ Reklama tafsilotlari yetarlicha batafsil emas. Kamida 10 ta belgi bo'lishi va aniq ma'lumot berilishi kerak. Qayta yozing.", reply_markup=get_cancel_kb("back_reklama_type"))
        return
    await state.update_data(details=details)
    await state.set_state(Reklama.style)
    await message.answer("🎨 <b>Reklama ko'rinishini tasvirlang:</b>\n\nReklama dizayni haqida ma'lumot bering (fon rangi, shrift turi, rasmlar and h.k.). Iltimos, batafsil yozing.", reply_markup=get_cancel_kb("back_reklama_type"))
@dp.message(StateFilter(Reklama.style))
async def reklama_style_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    style = message.text.strip()
    if not style or len(style) < 5:
        await message.reply("❌ Reklama ko'rinishi haqida ma'lumot yetarlicha emas. Qayta yozing.", reply_markup=get_cancel_kb("back_reklama_type"))
        return
    await state.update_data(style=style)
    await state.set_state(Reklama.attach_choice)
    await message.answer("📎 <b>Fayl qo'shish:</b>\n\nReklama uchun rasm, video yoki hujjat fayl qo'shmoqchimisiz? (Masalan, dizayn namunasi). Ha/Yo'q tanlang.", reply_markup=KB_REKLAMA_ATTACH)
@dp.callback_query(StateFilter(Reklama.attach_choice), F.data == "rad_attach_yes")
async def reklama_attach_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.file_upload)
    await safe_edit_or_send(callback, "📤 <b>Fayl yuklash:</b>\n\nFayllarni yuboring (rasm, video yoki hujjat). Har birini alohida. Tugagach, 'Barcha fayllar yuborildi' tugmasini bosing. Virus tekshiruvi o'tkaziladi.", KB_FILE_DONE)
@dp.callback_query(StateFilter(Reklama.attach_choice), F.data == "rad_attach_no")
async def reklama_attach_no(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.update_data(files=[])
    await state.set_state(Reklama.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>\n\n.", KB_REKLAMA_CONTACT)
@dp.message(StateFilter(Reklama.file_upload))
async def reklama_file_uploaded(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    files = data.get('files', [])
    file_id, ctype = None, None
    if message.photo:
        file_id = message.photo[-1].file_id
        ctype = "photo"
    elif message.video:
        file_id = message.video.file_id
        ctype = "video"
    elif message.document:
        file_id = message.document.file_id
        ctype = "document"
    if not file_id:
        await message.answer("❌ Iltimos, faqat rasm, video yoki hujjat fayl yuboring. Qayta urinib ko'ring.", reply_markup=get_cancel_kb("back_reklama_attach"))
        return
    await show_progress(message)
    if not await check_file_for_virus(file_id, ctype):
        await message.answer("❌ Fayl virusli deb topildi yoki xavfli. Boshqa fayl yuboring yoki 'Barcha fayllar yuborildi' ni bosing.", reply_markup=get_cancel_kb("back_reklama_attach"))
        return
    files.append((ctype, file_id))
    await state.update_data(files=files)
    await message.answer("✅ Fayl muvaffaqiyatli yuklandi va tekshirildi. Yana fayl yuboring yoki tugagach 'Barcha fayllar yuborildi' ni bosing.", reply_markup=KB_FILE_DONE)
@dp.callback_query(StateFilter(Reklama.file_upload), F.data == "file_done")
async def reklama_file_done(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>", KB_REKLAMA_CONTACT)
@dp.callback_query(StateFilter(Reklama.contact_method), F.data == "rad_ctm_username")
async def reklama_ctm_username(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    username = callback.from_user.username
    contact = f"@{username}" if username else "Username topilmadi, telefon kiriting"
    await state.update_data(contact=contact)
    await state.set_state(Reklama.confirm)
    data = await state.get_data()
    text = (
        f"📰 <b>Reklama so'rovini tasdiqlash:</b>\n\n"
        f"🖼️ Reklama turi: {data['ad_type']}\n"
        f"✍️ Tafsilotlar: {data['details']}\n"
        f"🎨 Ko'rinish: {data['style']}\n"
        f"📎 Fayllar soni: {len(data.get('files', []))}\n"
        f"📞 Bog'lanish: {contact}\n\n"
        f"<i>Bu ma'lumotlar to'g'ri va to'liqmi? Agar ha bo'lsa, so'rov adminga yuboriladi va ko'rib chiqiladi.</i>"
    )
    await safe_edit_or_send(callback, text, KB_REKLAMA_CONFIRM)
@dp.callback_query(StateFilter(Reklama.contact_method), F.data == "rad_ctm_text")
async def reklama_ctm_text(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.contact_text)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Bog'lanish usuliga qaytish", callback_data="back_reklama_contact")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish uchun telefon raqamini kiriting:</b>\n\n.", markup)
@dp.callback_query(F.data == "back_reklama_contact")
async def back_reklama_contact(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>", KB_REKLAMA_CONTACT)
@dp.message(StateFilter(Reklama.contact_text))
async def reklama_contact_text_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    phone = message.text.strip()
    if not re.fullmatch(r"\+998\d{9}", phone):
        await message.reply("❌ Telefon raqami +998 bilan boshlanishi va 12 xonali raqam bo'lishi kerak. Qayta kiriting.", reply_markup=get_cancel_kb("back_reklama_contact"))
        return
    await state.update_data(contact=phone)
    await state.set_state(Reklama.confirm)
    data = await state.get_data()
    text = (
        f"📰 <b>Reklama so'rovini tasdiqlash:</b>\n\n"
        f"🖼️ Reklama turi: {data['ad_type']}\n"
        f"✍️ Tafsilotlar: {data['details']}\n"
        f"🎨 Ko'rinish: {data['style']}\n"
        f"📎 Fayllar soni: {len(data.get('files', []))}\n"
        f"📞 Bog'lanish: {phone}\n\n"
        f"<i>Bu ma'lumotlar to'g'ri va to'liqmi? Agar ha bo'lsa, so'rov adminga yuboriladi va ko'rib chiqiladi.</i>"
    )
    await message.answer(text, reply_markup=KB_REKLAMA_CONFIRM)
@dp.callback_query(StateFilter(Reklama.confirm), F.data == "rad_confirm_yes")
async def reklama_confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    text = (
        f"📰 <b>Reklama so'rovi keldi:</b>\n\n"
        f"🖼️ Reklama turi: {data['ad_type']}\n"
        f"✍️ Tafsilotlar: {data['details']}\n"
        f"🎨 Ko'rinish: {data['style']}\n"
        f"📎 Fayllar soni: {len(data.get('files', []))}\n"
        f"📞 Bog'lanish: {data['contact']}{profile_text}\n\n"
        f"🆔 Foydalanuvchi ID: {callback.from_user.id}\n\n"
        f"<i>Iltimos, bu so'rovni tez orada ko'rib chiqing va foydalanuvchiga javob bering.</i>"
    )
    await bot.send_message(ADMIN_ID, text)
    for ctype, fid in data.get('files', []):
        caption = "📎 Reklama fayli (virus tekshiruvi o'tgan)"
        try:
            if ctype == "photo":
                await bot.send_photo(ADMIN_ID, fid, caption=caption)
            elif ctype == "video":
                await bot.send_video(ADMIN_ID, fid, caption=caption)
            else:
                await bot.send_document(ADMIN_ID, fid, caption=caption)
        except Exception as e:
            logger.error(f"Fayl xato: {e}")
    save_action({
        'type': 'reklama',
        'chat_id': callback.from_user.id,
        'details': f"Turi: {data['ad_type']}, Tafsilot: {data['details']}, Ko'rinish: {data['style']}, Bog'lanish: {data['contact']}"
    })
    await state.set_state(Reklama.waiting_reply)
    await safe_edit_or_send(callback, "✅ <b>Reklama so'rovingiz adminga muvaffaqiyatli yuborildi!</b>\n\nIltimos, kutib turing. So'rov ko'rib chiqilmoqda va javob tez orada keladi. Boshqa xizmatlar uchun menyudan tanlang.", get_main_menu(chat_id))
    asyncio.create_task(send_waiting_reminder(chat_id, "reklama so'rovingiz"))
    asyncio.create_task(auto_reset_state(state, chat_id, "Reklama so'rovi"))
@dp.callback_query(StateFilter(Reklama.confirm), F.data == "rad_confirm_edit")
async def reklama_confirm_edit(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(Reklama.contact_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tahrirlash:</b>", KB_REKLAMA_CONTACT)
@dp.message(StateFilter(Reklama.waiting_reply))
async def reklama_waiting_reply(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not is_in_chat(chat_id):
        return
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    txt = f"📨 <b>Reklama so'rovi bo'yicha javob:</b>\n\n{message.text or 'Fayl yuborildi'}{profile_text}\n\n🆔 Chat ID: {chat_id}"
    try:
        if message.photo:
            await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=txt)
        elif message.document:
            await bot.send_document(ADMIN_ID, message.document.file_id, caption=txt)
        elif message.video:
            await bot.send_video(ADMIN_ID, message.video.file_id, caption=txt)
        else:
            await bot.send_message(ADMIN_ID, txt)
        await message.answer("✉️ <b>Xabaringiz adminga muvaffaqiyatli yuborildi.</b>\n\nJavobni kuting. Suhbatdan chiqish uchun chiqish tugmasini bosing.", reply_markup=KB_ADMIN_CHAT_EXIT)
        save_action({'type': 'reklama_reply', 'chat_id': chat_id, 'details': message.text or 'media'})
    except Exception as e:
        logger.error(f"Reklama reply xato: {e}")
        await message.answer("⚠️ Xabar yuborishda texnik xato yuz berdi. Qayta urinib ko'ring.")
# ----------------- Buyurtma -----------------
@dp.callback_query(F.data == "buyurtma")
async def buyurtma_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    # NEW: require profile
    profile = get_chat_profile(chat_id)
    if not profile:
        await safe_edit_or_send(callback, "❗ Yangi raqam buyurtma qilishdan oldin profil ma'lumotlaringizni to'ldiring. Iltimos profil bo'limiga o'ting.", KB_PROFIL_CONSENT)
        return
    await state.clear()
    await state.update_data(files=[])
    await state.set_state(RaqamBuyurtma.mahalla)
    await safe_edit_or_send(callback, "🆕 <b>Yangi raqam buyurtma xizmati:</b>\n\nYangi raqam olish uchun mahallangizni tanlang. Keyingi qadamlar: ma'lumot, operator, joylashuv va bog'lanish.", kb_mahalla_page(0))
@dp.callback_query(StateFilter(RaqamBuyurtma.mahalla), F.data.startswith("mah_page_"))
async def mahalla_page_nav(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    try:
        page = int(callback.data.split("_")[-1])
    except ValueError:
        page = 0
    await safe_edit_or_send(callback, "🆕 <b>Mahalla tanlash:</b>\n\nQuyidagi sahifadan mahallangizni tanlang.", kb_mahalla_page(page))
@dp.callback_query(StateFilter(RaqamBuyurtma.mahalla), F.data.startswith("mah_sel_"))
async def mahalla_selected(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    try:
        idx = int(callback.data.split("_")[-1])
        if idx < 0 or idx >= len(MAHALLALAR):
            await safe_edit_or_send(callback, "❌ Noto'g'ri mahalla tanlandi. Iltimos, qayta tanlang.", kb_mahalla_page(0))
            return
        mah = MAHALLALAR[idx]
        await state.update_data(mahalla=mah)
        await state.set_state(RaqamBuyurtma.data)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Mahalla tanlashga qaytish", callback_data="back_buyurtma_mah")],
            [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
        ])
        await safe_edit_or_send(callback, "🆕 <b>Raqam buyurtma bosqichi 2/5:</b>\n\nYangi raqam turi haqida qo'shimcha ma'lumot kiriting (masalan: sizga qanday raqam kerak va qay usulda olmoqchisiz). Iltimos, aniq yozing.", markup)
    except Exception as e:
        logger.exception(f"mahalla_selected xato: {e}")
        await safe_edit_or_send(callback, "❌ Mahalla tanlashda xato yuz berdi. Qayta urinib ko'ring.", kb_mahalla_page(0))
@dp.message(StateFilter(RaqamBuyurtma.data))
async def buyurtma_data_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    malumot = message.text.strip()
    if not malumot or len(malumot) < 10:
        await message.reply("❌ Qo'shimcha ma'lumot yetarlicha batafsil emas. Kamida 10 ta belgi bo'lishi kerak. Batafsilroq Qayta yozing.", reply_markup=get_cancel_kb("back_buyurtma_mah"))
        return
    await state.update_data(malumot=malumot)
    await state.set_state(RaqamBuyurtma.operator)
    await message.answer("📶 <b>Raqam buyurtma bosqichi 3/5:</b>\n\nQaysi operator raqamini xohlaysiz? Tanlang.", reply_markup=KB_BUYURTMA_OPERATOR)
@dp.callback_query(StateFilter(RaqamBuyurtma.operator), F.data.startswith("bop_"))
async def buyurtma_operator_selected(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    operator = callback.data.split("_", 1)[1]
    await state.update_data(operator=operator)
    await state.set_state(RaqamBuyurtma.file_choice)
    await safe_edit_or_send(callback, "📎 <b>Raqam buyurtma bosqichi 4/5:</b>\n\nBuyurtmaga qo'shimcha fayl (masalan, hujjat) qo'shmoqchimisiz? Tanlang.", yes_no_kb("file_yes", "file_no", "back_buyurtma_op"))
@dp.callback_query(F.data == "back_buyurtma_op")
async def back_buyurtma_op(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.operator)
    await safe_edit_or_send(callback, "📶 <b>Operator tanlash:</b>", KB_BUYURTMA_OPERATOR)
@dp.callback_query(StateFilter(RaqamBuyurtma.file_choice), F.data == "file_yes")
async def buyurtma_file_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.file_upload)
    await safe_edit_or_send(callback, "📤 <b>Fayl yuklash:</b>\n\nFayllarni yuboring (hujjat yoki rasm). Har birini alohida. Tugagach, 'Barcha fayllar yuborildi' ni bosing. Virus tekshiruvi o'tkaziladi.", KB_FILE_DONE)
@dp.callback_query(StateFilter(RaqamBuyurtma.file_choice), F.data == "file_no")
async def buyurtma_file_no(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.update_data(files=[])
    await state.set_state(RaqamBuyurtma.location)
    await callback.message.answer("📍 <b>Raqam buyurtma bosqichi 5/5:</b>\n\nJoylashuvni ulashing (GPS orqali). Bu mahalla tasdiqlash uchun kerak.", reply_markup=KB_SHARE_LOCATION)
@dp.callback_query(F.data == "back_buyurtma_file_choice")
async def back_buyurtma_file_choice(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.file_choice)
    await safe_edit_or_send(callback, "📎 <b>Fayl qo'shish:</b>", yes_no_kb("file_yes", "file_no", "back_buyurtma_op"))
@dp.message(StateFilter(RaqamBuyurtma.file_upload))
async def buyurtma_file_uploaded(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    files = data.get('files', [])
    ctype = None
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
        ctype = "photo"
    elif message.video:
        file_id = message.video.file_id
        ctype = "video"
    elif message.document:
        file_id = message.document.file_id
        ctype = "document"
    if file_id is None:
        await message.answer("❌ Iltimos, faqat rasm, video yoki hujjat yuboring. Qayta urinib ko'ring.", reply_markup=get_cancel_kb("back_buyurtma_file_choice"))
        return
    await show_progress(message)
    if not await check_file_for_virus(file_id, ctype):
        await message.answer("❌ Fayl virusli yoki xavfli deb topildi. Boshqa fayl yuboring.", reply_markup=get_cancel_kb("back_buyurtma_file_choice"))
        return
    files.append((ctype, file_id))
    await state.update_data(files=files)
    await message.answer("✅ Fayl muvaffaqiyatli yuklandi va tekshirildi. Yana fayl yuboring yoki tugagach tugmani bosing.", reply_markup=KB_FILE_DONE)
@dp.callback_query(StateFilter(RaqamBuyurtma.file_upload), F.data == "file_done")
async def buyurtma_file_done(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("📍 <b>Joylashuvni ulashing:</b>", reply_markup=KB_SHARE_LOCATION)
@dp.message(StateFilter(RaqamBuyurtma.location), F.location)
async def buyurtma_location_received(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    location = {'lat': message.location.latitude, 'lon': message.location.longitude}
    await state.update_data(location=location)
    data = await state.get_data()
    profile = get_chat_profile(chat_id)
    if profile:
        await state.update_data(phone=profile.get('telefon', 'Kiritilmagan'))
        await state.set_state(RaqamBuyurtma.confirm)
        await send_buyurtma_preview(message, state)
    else:
        await state.set_state(RaqamBuyurtma.phone_method)
        await message.answer("📞 <b>Bog'lanish usulini tanlang:</b>\n\n.", reply_markup=KB_BUYURTMA_PHONE)
@dp.callback_query(StateFilter(RaqamBuyurtma.phone_method), F.data == "phm_username")
async def buyurtma_phone_username(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    username = callback.from_user.username
    contact = f"@{username}" if username else "Username topilmadi, telefon kiriting"
    await state.update_data(phone=contact)
    await state.set_state(RaqamBuyurtma.confirm)
    await send_buyurtma_preview(callback, state)
@dp.callback_query(StateFilter(RaqamBuyurtma.phone_method), F.data == "phm_text")
async def buyurtma_phone_text(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.phone_text)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Bog'lanish usuliga qaytish", callback_data="back_buyurtma_phone")],
        [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_service")]
    ])
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish uchun telefon raqamini kiriting:</b>\n\n.", markup)
@dp.callback_query(F.data == "back_buyurtma_phone")
async def back_buyurtma_phone(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.phone_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tanlang:</b>", KB_BUYURTMA_PHONE)
@dp.message(StateFilter(RaqamBuyurtma.phone_text))
async def buyurtma_phone_text_entered(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    phone = message.text.strip()
    if not re.fullmatch(r"\+998\d{9}", phone):
        await message.reply("❌ Telefon raqami +998 bilan boshlanishi va 12 xonali raqam bo'lishi kerak. Qayta kiriting.", reply_markup=get_cancel_kb("back_buyurtma_phone"))
        return
    await state.update_data(phone=phone)
    await state.set_state(RaqamBuyurtma.confirm)
    await send_buyurtma_preview(message, state)
@dp.callback_query(StateFilter(RaqamBuyurtma.confirm), F.data == "confirm_edit")
async def confirm_edit(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    await state.set_state(RaqamBuyurtma.phone_method)
    await safe_edit_or_send(callback, "📞 <b>Bog'lanish usulini tahrirlash:</b>", KB_BUYURTMA_PHONE)
@dp.callback_query(StateFilter(RaqamBuyurtma.confirm), F.data == "confirm_yes")
async def confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    data = await state.get_data()
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    loc = data.get('location', {})
    lat = loc.get('lat', 'N/A')
    lon = loc.get('lon', 'N/A')
    maps_link = f"https://maps.google.com/?q={lat},{lon}" if lat != 'N/A' else "Joylashuv berilmagan"
    txt = (
        f"📩 <b>Yangi raqam buyurtma so'rovi keldi:</b>\n\n"
        f"🏘️ Mahalla: {data['mahalla']}\n"
        f"📝 Qo'shimcha ma'lumot: {data['malumot']}\n"
        f"📶 Operator: {data['operator']}\n"
        f"📍 Joylashuv: {maps_link}\n"
        f"📞 Bog'lanish usuli: {data['phone']}{profile_text}\n\n"
        f"🆔 Foydalanuvchi ID: {chat_id}\n\n"
        f"<i>Iltimos, bu so'rovni tez orada ko'rib chiqing va foydalanuvchiga javob bering.</i>"
    )
    await bot.send_message(ADMIN_ID, txt)
    for ctype, fid in data.get('files', []):
        caption = "📎 Buyurtma fayli (virus tekshiruvi o'tgan)"
        try:
            if ctype == "photo":
                await bot.send_photo(ADMIN_ID, fid, caption=caption)
            elif ctype == "video":
                await bot.send_video(ADMIN_ID, fid, caption=caption)
            else:
                await bot.send_document(ADMIN_ID, fid, caption=caption)
        except Exception as e:
            logger.error(f"Fayl xato: {e}")
    order_data = data.copy()
    order_data['chat_id'] = chat_id
    order_id = save_order(order_data)
    await bot.send_message(ADMIN_ID, f"<b>Buyurtma ID:</b> {order_id}\n\nBu ID orqali buyurtmani kuzatib borishingiz mumkin.")
    save_action({'type': 'raqam_buyurtma', 'chat_id': chat_id, 'details': f"Mahalla: {data['mahalla']}, Operator: {data['operator']}, Ma'lumot: {data['malumot']}"})
    asyncio.create_task(send_reminder(order_id))
    await state.set_state(RaqamBuyurtma.waiting_reply)
    await safe_edit_or_send(callback, "✅ <b>Raqam buyurtma so'rovingiz adminga muvaffaqiyatli yuborildi!</b>\n\nIltimos, kutib turing. So'rov ko'rib chiqilmoqda va javob tez orada keladi. Boshqa xizmatlar uchun menyudan tanlang.", get_main_menu(chat_id))
    asyncio.create_task(send_waiting_reminder(chat_id, "raqam buyurtma so'rovingiz"))
    asyncio.create_task(auto_reset_state(state, chat_id, "Raqam buyurtma so'rovi"))
@dp.message(StateFilter(RaqamBuyurtma.waiting_reply))
async def buyurtma_waiting_reply(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not is_in_chat(chat_id):
        return
    profile = get_chat_profile(chat_id)
    profile_text = f"\n👤 Ism: {profile.get('ism_familya', '')} | 📞 Telefon: {profile.get('telefon', '')} | 🏙️ Tuman/Mahalla: {profile.get('tuman_mahalla', '')}" if profile else ""
    txt = f"📨 <b>Raqam buyurtma so'rovi bo'yicha javob:</b>\n\n{message.text or 'Fayl yuborildi'}{profile_text}\n\n🆔 Chat ID: {chat_id}"
    try:
        if message.photo:
            await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption=txt)
        elif message.document:
            await bot.send_document(ADMIN_ID, message.document.file_id, caption=txt)
        elif message.video:
            await bot.send_video(ADMIN_ID, message.video.file_id, caption=txt)
        else:
            await bot.send_message(ADMIN_ID, txt)
        await message.answer("✉️ <b>Xabaringiz adminga muvaffaqiyatli yuborildi.</b>\n\nJavobni kuting. Suhbatdan chiqish uchun chiqish tugmasini bosing.", reply_markup=KB_ADMIN_CHAT_EXIT)
        save_action({'type': 'buyurtma_reply', 'chat_id': chat_id, 'details': message.text or 'media'})
    except Exception as e:
        logger.error(f"Buyurtma reply xato: {e}")
        await message.answer("⚠️ Xabar yuborishda texnik xato yuz berdi. Qayta urinib ko'ring.")
# ----------------- Admin orders -----------------
@dp.callback_query(F.data == "admin_orders", F.from_user.id == ADMIN_ID)
async def admin_orders(callback: types.CallbackQuery, state: FSMContext):
    orders = get_orders()
    if not orders:
        text = "📋 <b>Barcha buyurtmalar:</b>\n\nHozircha buyurtma yo'q."
    else:
        text = "📋 <b>Barcha buyurtmalar (so'nggi 10 ta):</b>\n\n"
        for oid, odata in list(orders.items())[:10]:
            ts = datetime.fromtimestamp(odata['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
            malumot = odata.get('malumot', 'Qisqa')[:50] + '...' if len(odata.get('malumot', '')) > 50 else odata.get('malumot', 'N/A')
            text += f"🆔 ID {oid} ({ts}): Operator - {odata.get('operator', 'N/A')}, Mahalla - {odata.get('mahalla', 'N/A')}, Ma'lumot - {malumot}\n"
    await safe_edit_or_send(callback, text, get_main_menu(ADMIN_ID))
@dp.callback_query(F.data == "admin_stats", F.from_user.id == ADMIN_ID)
async def admin_stats(callback: types.CallbackQuery, state: FSMContext):
    total = get_total_chats()
    avg = get_average_rating()
    stats = get_service_stats()
    text = f"📊 <b>Bot statistikasi:</b>\n\n👥 Jami ro'yxatdan o'tgan foydalanuvchilar: {total}\n"
    if avg > 0:
        text += f"🌟 O'rtacha baho (chat uchun): {avg}/5\n"
    text += f"\n📈 Xizmatlar bo'yicha buyurtmalar (oxirgi 24 soat):\n{chr(10).join([f'{k}: {v} ta' for k, v in stats.items()])}"
    await safe_edit_or_send(callback, text, get_main_menu(ADMIN_ID))
@dp.callback_query(F.data == "admin_users", F.from_user.id == ADMIN_ID)
async def admin_users(callback: types.CallbackQuery, state: FSMContext):
    """Show first page (page=0) of users (10 per page)."""
    await _send_admin_users_page(callback, page=0)

@dp.callback_query(F.data.startswith("admin_users_page_"), F.from_user.id == ADMIN_ID)
async def admin_users_page(callback: types.CallbackQuery, state: FSMContext):
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 0
    await _send_admin_users_page(callback, page=page)

async def _send_admin_users_page(obj, page: int = 0):
    per_page = 10
    offset = page * per_page
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM chats")
        total = cursor.fetchone()[0] or 0
        cursor.execute("SELECT chat_id, profile, last_active FROM chats ORDER BY last_active DESC LIMIT ? OFFSET ?", (per_page, offset))
        rows = cursor.fetchall()
    if not rows:
        await safe_edit_or_send(obj, "📋 Foydalanuvchilar ro'yxati bo'sh.", get_main_menu(ADMIN_ID))
        admin_last_user_list.pop(ADMIN_ID, None)
        return
    users = []
    text = f"👥 <b>Foydalanuvchilar (sahifa {page+1}, jami: {total} ta):</b>\n\n"
    for idx, row in enumerate(rows, start=1):
        cid = row[0]
        profile_json = row[1]
        display = None
        username = None
        try:
            chat = await bot.get_chat(cid)
            full_name = getattr(chat, "full_name", None)
            if not full_name:
                parts = []
                if getattr(chat, "first_name", None):
                    parts.append(chat.first_name)
                if getattr(chat, "last_name", None):
                    parts.append(chat.last_name)
                full_name = " ".join(parts).strip() if parts else None
            username = f"@{chat.username}" if getattr(chat, "username", None) else None
            if full_name:
                display = f"{full_name} {username or ''}".strip()
        except Exception as e:
            logger.debug(f"bot.get_chat failed for {cid}: {e}")
        if not display and profile_json:
            try:
                prof = json.loads(profile_json)
                display = prof.get('ism_familya') or prof.get('telefon') or None
            except Exception:
                display = None
        if not display:
            display = f"User {cid}"
        users.append({'id': cid, 'name': display, 'username': username or ''})
        text += f"{idx}. {display} (ID: {cid})\n"
    # navigatsiya klaviaturasi
    kb_rows = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin_users_page_{page-1}"))
    if (offset + per_page) < total:
        nav.append(InlineKeyboardButton(text="➡️ Keyingi", callback_data=f"admin_users_page_{page+1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
# oxirgi ko'rsatilgan sahifani saqlang
    admin_last_user_list[ADMIN_ID] = {'page': page, 'users': users, 'total': total}
    text += "\n❗ Tanlangan tartib raqamini yuboring (masalan: 1) — bot tanlangan foydalanuvchi uchun amallarni ko'rsatadi."
    await safe_edit_or_send(obj, text, kb)

# ----------------- Administrator tomonidan bloklangan foydalanuvchilar (bir xil xatti-harakatlarni saqlaydi, lekin oxirgi ro'yxatni saqlaydi) -----------------
@dp.callback_query(F.data == "admin_blocked", F.from_user.id == ADMIN_ID)
async def admin_blocked(callback: types.CallbackQuery, state: FSMContext):
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, profile, last_active FROM chats WHERE banned = 1 ORDER BY last_active DESC")
        rows = cursor.fetchall()
    if not rows:
        await safe_edit_or_send(callback, "🚫 Hozircha bloklangan foydalanuvchilar yo'q.", get_main_menu(ADMIN_ID))
        admin_last_user_list.pop(ADMIN_ID, None)
        return
    users = []
    text = f"🚫 <b>Bloklangan foydalanuvchilar ({len(rows)}):</b>\n\n"
    for idx, row in enumerate(rows, start=1):
        cid = row[0]
        profile_json = row[1]
        display = None
        username = None
     # Telegram foydalanuvchi nomi/ismini olishga harakat qiling
        try:
            chat = await bot.get_chat(cid)
            full_name = getattr(chat, "full_name", None)
            if not full_name:
                parts = []
                if getattr(chat, "first_name", None):
                    parts.append(chat.first_name)
                if getattr(chat, "last_name", None):
                    parts.append(chat.last_name)
                full_name = " ".join(parts).strip() if parts else None
            username = f"@{chat.username}" if getattr(chat, "username", None) else None
            if full_name:
                display = f"{full_name} {username or ''}".strip()
        except Exception as e:
            logger.debug(f"bot.get_chat failed for blocked user {cid}: {e}")
        if not display and profile_json:
            try:
                prof = json.loads(profile_json)
                display = prof.get('ism_familya') or prof.get('telefon') or None
            except Exception:
                display = None
        if not display:
            display = f"User {cid}"
        users.append({'id': cid, 'name': display, 'username': username or ''})
        text += f"{idx}. {display} (ID: {cid})\n"
    admin_last_user_list[ADMIN_ID] = {'page': 0, 'users': users, 'total': len(users)}
    text += "\n❗ Tanlangan tartib raqamini yuboring (masalan: 1) — bot tanlangan foydalanuvchi uchun blokdan ochish tugmasini chiqaradi."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")]])
    await safe_edit_or_send(callback, text, kb)

# ----------------- Administrator uchun bitta raqamli ishlov beruvchi -> oxirgi ko'rsatilgan ro'yxatda ishlaydi -----------------
@dp.message(F.chat.id == ADMIN_ID, F.text.regexp(r"^\d+$"))
async def admin_user_action_by_number(message: types.Message):
    try:
        idx = int(message.text.strip()) - 1
        entry = admin_last_user_list.get(ADMIN_ID)
        if not entry:
            await message.answer("❌ Avval ro'yxatni oching (Foydalanuvchilar yoki Bloklanganlar).")
            return
        users = entry.get('users', [])
        if idx < 0 or idx >= len(users):
            await message.answer("❌ Bunday tartib raqami foydalanuvchi topilmadi. Ro'yxatni qayta ko'ring.")
            return
        user = users[idx]
        chat_id = user['id']
        name = user['name']
        if is_banned(chat_id):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🔓 {name} blokdan chiqarish", callback_data=f"admin_unblock_{chat_id}")],
                [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")]
            ])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💬 {name} bilan chat ochish", callback_data=f"admin_chat_with_{chat_id}")],
                [InlineKeyboardButton(text=f"🚫 {name} ni bloklash", callback_data=f"admin_block_{chat_id}")],
                [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")]
            ])
        await message.answer(f"👤 Foydalanuvchi: {name} (ID: {chat_id})\n\nQuyidagi harakatni tanlang:", reply_markup=kb)
      # tasodifiy qayta ishlatmaslik uchun oxirgi ko'rsatilgan ro'yxatni tozalang
        admin_last_user_list.pop(ADMIN_ID, None)
    except Exception as e:
        logger.error(f"admin numeric action error: {e}")
        await message.answer("❌ Xato yuz berdi. Qayta urinib ko'ring.")

# ----------------- Blokdan chiqarish ishlovchisi (mavjud xatti-harakatlarni saqlaydi) -----------------
@dp.callback_query(F.data.startswith("admin_unblock_"), F.from_user.id == ADMIN_ID)
async def admin_unblock_user(callback: types.CallbackQuery, state: FSMContext):
    try:
        chat_id = int(callback.data.split("_")[-1])
        set_banned(chat_id, False)
        set_in_chat(chat_id, False)
        await callback.message.edit_text(f"✅ Foydalanuvchi {chat_id} blokdan olindi va chatga ruxsat berildi.")
        try:
            await bot.send_message(chat_id, "✅ Sizning blokingiz olib tashlandi. Endi botdan foydalanishingiz mumkin.", reply_markup=get_main_menu(chat_id))
        except Exception:
            pass
        save_action({'type': 'admin_unblock', 'chat_id': chat_id, 'details': 'Unblocked by admin'})
    except Exception as e:
        logger.error(f"Unblock xato: {e}")
        await callback.answer("❌ Blokni ochishda xato yuz berdi.", show_alert=True)

# ----------------- Admin Panel  -----------------
@dp.callback_query(F.data == "admin_panel", F.from_user.id == ADMIN_ID)
async def admin_panel(callback: types.CallbackQuery, state: FSMContext):
    """
    Show admin panel keyboard to authorized admin.
    This handler was missing and caused "Update ... is not handled" when admin pressed the Admin panel button.
    """
    try:
        await safe_edit_or_send(callback, "👨‍💻 <b>Admin panel:</b>\n\nQuyidagi bo'limlardan birini tanlang. Statistika va ro'yxatlar real vaqtda yangilanadi.", KB_ADMIN_PANEL)
    except Exception as e:
        logger.exception(f"admin_panel handler error: {e}")
        await callback.answer("❌ Admin panelni ochishda xato yuz berdi.", show_alert=True)

# ----------------- Yangi: administrator foydalanuvchi va administrator bloklarini ishlovchilar bilan suhbatni boshlaydi -----------------
@dp.callback_query(F.data.startswith("admin_chat_with_"), F.from_user.id == ADMIN_ID)
async def admin_chat_with_user(callback: types.CallbackQuery, state: FSMContext):
    """
    Admin admin_users ro'yxatidan foydalanuvchi bilan chat ochadi.
    """
    try:
        target_id = int(callback.data.split("_")[-1])
        set_in_chat(target_id, True)
        # record that admin is actively chatting with target
        admin_chat_targets[ADMIN_ID] = target_id

       # administratorni xabardor qilish (xabarni tahrirlash)
        await callback.message.edit_text(f"✅ Admin {target_id} bilan chat boshlandi. Endi admin tomonidan yuboriladigan xabarlar shu foydalanuvchiga yo'naltiriladi.")
        # foydalanuvchini xabardor qilish
        try:
            await bot.send_message(target_id, "📞 Admin siz bilan chat boshladi. Endi savolingizni yozing. Suhbatdan chiqish uchun 'Admin chatdan chiqish' tugmasini bosing.", reply_markup=KB_ADMIN_CHAT_EXIT)
        except Exception:
           # foydalanuvchi botni bloklagan bo‘lishi yoki xabar qabul qila olmasligi mumkin
            logger.warning(f"admin_chat_with_user: foydalanuvchi {target_id} ga xabar yuborilmadi.")
        # aniqlik uchun adminni shaxsiy xabarda ham xabardor qilish
        try:
            await bot.send_message(ADMIN_ID, f"📞 Siz {target_id} ID li foydalanuvchi bilan chatni boshladingiz.\n\nFoydalanuvchi javob berganida u avtomatik adminga yuboriladi.", reply_markup=KB_ADMIN_CHAT_EXIT)
        except Exception:
            pass
        save_action({'type': 'admin_chat_start', 'chat_id': ADMIN_ID, 'details': f"Chat with {target_id}"})
    except Exception as e:
        logger.exception(f"admin_chat_with_user xato: {e}")
        await callback.answer("❌ Chatni boshlashda xato yuz berdi.", show_alert=True)

# New: user requests admin chat -> send admin a confirmation request with inline buttons
@dp.callback_query(F.data == "admin_chat")
async def user_request_admin_chat(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if is_banned(chat_id):
        await callback.answer("❌ Siz botdan bloklangansiz. Admin bilan bog'lanish mumkin emas.", show_alert=True)
        return
    # build profile/context for admin
    profile = get_chat_profile(chat_id) or {}
    prof_text = (
        f'👤 Ism: {profile.get("ism_familya", "Noma\\'lum")} \n'
        f'📞 Telefon: {profile.get("telefon", "N/A")}\n'
        f'📍 Tuman/Mahalla: {profile.get("tuman_mahalla", "N/A")}\n'
    )
    user_name = callback.from_user.full_name or callback.from_user.username or f"User {chat_id}"
    # inline keyboard for admin to accept/decline
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlayman (chatni boshlash)", callback_data=f"admin_accept_chat_{chat_id}")],
        [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"admin_decline_chat_{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="back_main")]
    ])
    # notify admin
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📩 Adminga so'rov:\n\n{user_name} (ID: {chat_id}) siz bilan chat boshlamoqchi.\n\n{prof_text}\n\n<b>So'rovni tasdiqlaysizmi?</b>",
            reply_markup=kb
        )
    except Exception as e:
        logger.exception(f"Failed to send admin chat request to admin: {e}")
        await safe_edit_or_send(callback, "❌ Texnik xato: adminga so'rov yuborilmadi. Qayta urinib ko'ring.", get_main_menu(chat_id))
        return

    # confirm to user that request sent
    await safe_edit_or_send(callback, "📨 So'rovingiz adminga yuborildi. Admin javobini kuting.", get_main_menu(chat_id))
    save_action({'type': 'admin_chat_request', 'chat_id': chat_id, 'details': 'User requested admin chat'})

# New: admin accepts the user chat request
@dp.callback_query(F.data.startswith("admin_accept_chat_"), F.from_user.id == ADMIN_ID)
async def admin_accept_chat(callback: types.CallbackQuery, state: FSMContext):
    try:
        target_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("❌ Noto'g'ri foydalanuvchi ID.", show_alert=True)
        return
    # set mapping and flags
    admin_chat_targets[ADMIN_ID] = target_id
    set_in_chat(target_id, True)
    # edit admin's message to reflect acceptance
    try:
        await callback.message.edit_text(f"✅ Siz {target_id} bilan chatni tasdiqladingiz. Chat boshlandi.")
    except Exception:
        pass
    # notify admin (private) and user
    try:
        await bot.send_message(ADMIN_ID, f"📞 Chat boshlandi: foydalanuvchi {target_id} bilan endi suhbatdasiz.", reply_markup=KB_ADMIN_CHAT_EXIT)
    except Exception:
        pass
    try:
        await bot.send_message(target_id, "📞 Admin so'rovingizni qabul qildi. Chat boshlandi. Endi savolingizni yozing. Suhbatdan chiqish uchun 'Admin chatdan chiqish' tugmasini bosing.", reply_markup=KB_ADMIN_CHAT_EXIT)
    except Exception:
        logger.warning(f"Could not notify user {target_id} about accepted admin chat.")
    save_action({'type': 'admin_chat_accepted', 'chat_id': ADMIN_ID, 'details': f"Accepted chat with {target_id}"})
    await callback.answer("✅ Chat boshlandi va foydalanuvchiga xabar yuborildi.", show_alert=True)

# New: admin declines the user chat request
@dp.callback_query(F.data.startswith("admin_decline_chat_"), F.from_user.id == ADMIN_ID)
async def admin_decline_chat(callback: types.CallbackQuery, state: FSMContext):
    try:
        target_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("❌ Noto'g'ri foydalanuvchi ID.", show_alert=True)
        return
    try:
        await callback.message.edit_text(f"❌ Siz {target_id} uchun chat so'rovini rad etdingiz.")
    except Exception:
        pass
    try:
        await bot.send_message(target_id, "❌ Admin sizning chat so'rovingizni rad etdi. Keyinroq qayta urinib ko'ring.", reply_markup=get_main_menu(target_id))
    except Exception:
        logger.warning(f"Could not notify user {target_id} about declined admin chat.")
    save_action({'type': 'admin_chat_declined', 'chat_id': ADMIN_ID, 'details': f"Declined chat with {target_id}"})
    await callback.answer("❌ So'rov rad etildi va foydalanuvchiga xabar yuborildi.", show_alert=True)

# ----------------- Admin broadcast (send to all non-banned users) -----------------
@dp.callback_query(F.data == "admin_broadcast", F.from_user.id == ADMIN_ID)
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    try:
        await state.set_state(AdminBroadcast.waiting)
        await safe_edit_or_send(callback, "📢 Iltimos, e'lon (matn, rasm, video yoki fayl) yuboring. Barcha foydalanuvchilarga yuboriladi. Bekor qilish uchun /start yoki Bekor qilish tugmasidan foydalaning.")
    except Exception as e:
        logger.exception(f"admin_broadcast_start xato: {e}")
        await callback.answer("❌ E'lon yuborishni boshlashda xato yuz berdi.", show_alert=True)

@dp.message(StateFilter(AdminBroadcast.waiting), F.chat.id == ADMIN_ID)
async def admin_broadcast_receive(message: types.Message, state: FSMContext):
    try:
        success, total = await broadcast_message(message)
        await message.answer(f"📣 E'lon yuborildi: {success}/{total} ta foydalanuvchiga yetkazildi.")
    except Exception as e:
        logger.exception(f"admin_broadcast_receive xato: {e}")
        await message.answer("❌ E'lon yuborishda xato yuz berdi.")
    finally:
        await state.clear()

# ----------------- Require profile before using main services -----------------
# For Raqam tiklash
@dp.callback_query(F.data == "tiklash")
async def tiklash_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    # NEW: require profile
    profile = get_chat_profile(chat_id)
    if not profile:
        await safe_edit_or_send(callback, "❗ Ushbu xizmatdan foydalanish uchun avval profil ma'lumotlaringizni to'ldiring. Iltimos, profil bo'limiga o'ting va ma'lumotlarni saqlang.", KB_PROFIL_CONSENT)
        return
    await state.clear()
    await state.set_state(RaqamTiklash.operator)
    await safe_edit_or_send(callback, "📱 <b>Raqam tiklash xizmati:</b>\n\nYo'qolgan yoki bloklangan raqamingizni tiklash uchun operatorni tanlang. Keyingi qadamda raqam va bog'lanish usulini kiritasiz.", KB_TIKLASH_OPERATOR)

# For Reklama
@dp.callback_query(F.data == "reklama")
async def reklama_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    # NEW: require profile
    profile = get_chat_profile(chat_id)
    if not profile:
        await safe_edit_or_send(callback, "❗ Reklama yuborish uchun avval profil ma'lumotlaringizni to'ldiring. Iltimos profilni to'ldiring.", KB_PROFIL_CONSENT)
        return
    await state.clear()
    await state.update_data(files=[])
    await state.set_state(Reklama.ad_type)
    await safe_edit_or_send(callback, "📰 <b>Reklama xizmati:</b>\n\nReklama turini tanlang. Keyingi qadamda tafsilotlar va bog'lanish ma'lumotlarini kiritasiz. So'rov adminga yuborilgach, ko'rib chiqiladi.", KB_REKLAMA_TYPES)

# For Buyurtma
@dp.callback_query(F.data == "buyurtma")
async def buyurtma_start(callback: types.CallbackQuery, state: FSMContext):
    chat_id = callback.message.chat.id
    ensure_chat_exists(chat_id)
    update_chat_activity(chat_id)
    if not await is_working_hours():
        await callback.message.answer("⏰ Bot ish vaqti: 07:00-24:00. Ertaga urinib ko'ring.")
        return
    # NEW: require profile
    profile = get_chat_profile(chat_id)
    if not profile:
        await safe_edit_or_send(callback, "❗ Yangi raqam buyurtma qilishdan oldin profil ma'lumotlaringizni to'ldiring. Iltimos profil bo'limiga o'ting.", KB_PROFIL_CONSENT)
        return
    await state.clear()
    await state.update_data(files=[])
    await state.set_state(RaqamBuyurtma.mahalla)
    await safe_edit_or_send(callback, "🆕 <b>Yangi raqam buyurtma xizmati:</b>\n\nYangi raqam olish uchun mahallangizni tanlang. Keyingi qadamlar: ma'lumot, operator, joylashuv va bog'lanish.", kb_mahalla_page(0))

async def main():
    logger.info("Bot ishga tushmoqda...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.exception(f"Botda ishlashda xato yuz berdi: {e}")
    finally:
        try:
            await bot.close()
        except Exception as e:
            logger.debug(f"Botni yopishda xato: {e}")
        logger.info("Bot to'xtatildi.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot foydalanuvchi tomonidan to'xtatildi (KeyboardInterrupt).")
    except Exception as e:
        logger.exception(f"Botni ishga tushirishda fatal xato: {e}")