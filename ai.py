#!/usr/bin/env python3
"""
Telegram Upload+View Bot — webhook variant (single-file)

Drop into Render as a web service. Uses webhooks (no polling) to avoid `telegram.error.Conflict`.
Keeps upload/view/token logic from your previous code, adapted for webhook handlers.

Usage:
 - Set environment variables (see top of file / README).
 - Deploy to Render as a web service.
 - Configure Uptime Robot to ping https://<your-render-hostname>/ every 5 minutes.

Notes:
 - If you have multiple instances/replicas, ensure only one sets the webhook (control with SET_WEBHOOK=1).
 - This file intentionally avoids polling entirely.
"""
import os
import logging
import time
import secrets
import sqlite3
import urllib.parse
import asyncio
from typing import Dict, Any, List, Optional

from flask import Flask, request, jsonify
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

import aiohttp

# ------------------------------
# Config (env)
# ------------------------------
UPLOAD_BOT_TOKEN = os.environ.get("UPLOAD_BOT_TOKEN", "7986735755:AAHQ5Ke7TI9uBxcYivDpib5pNzOmebGdZSY").strip()
if not UPLOAD_BOT_TOKEN:
    raise RuntimeError("UPLOAD_BOT_TOKEN must be provided in environment")

MAIN_CHANNEL_ID = os.environ.get("MAIN_CHANNEL_ID", "-1003104322226").strip()
if not MAIN_CHANNEL_ID:
    raise RuntimeError("MAIN_CHANNEL_ID must be provided in environment")

PASSWORD = os.environ.get("UPLOAD_PASSWORD", "test")
PASSWORD_VALID_SECONDS = int(os.environ.get("PASSWORD_VALID_SECONDS", 24 * 3600))
DB_PATH = os.environ.get("DB_PATH", "tg_content.db")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
EXEIO_API_KEY = os.environ.get("EXEIO_API_KEY", "").strip()
EXEIO_API_ENDPOINT = os.environ.get("EXEIO_API_ENDPOINT", "https://exe.io/api")
# If you prefer to provide full webhook URL directly, set WEBHOOK_URL
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
PORT = int(os.environ.get("PORT", 8080))
SET_WEBHOOK = os.environ.get("SET_WEBHOOK", "1").strip() == "1"
content_protection = os.environ.get("CONTENT_PROTECTION", "1").strip() != "0"

# ------------------------------
# Conversation states
# ------------------------------
(
    STATE_PASSWORD,
    STATE_THUMBNAIL,
    STATE_DESCRIPTION,
    STATE_OPTION,
    STATE_MEDIA_UPLOAD,
    STATE_TEXT_UPLOAD,
    STATE_TOKEN_REQUIRE,
    STATE_CONFIRM_TOKEN,
) = range(8)

sessions: Dict[int, Dict[str, Any]] = {}

# ------------------------------
# Logging
# ------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------
# DB helpers (same schema as your original)
# ------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        last_auth INTEGER,
        is_vip INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS content(
        content_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uploader_id INTEGER,
        thumb_file_id TEXT,
        description TEXT,
        is_text_only INTEGER DEFAULT 0,
        requires_token INTEGER DEFAULT 0,
        created_at INTEGER,
        main_channel_message_id INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS media_items(
        media_id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_id INTEGER,
        file_id TEXT,
        file_unique_id TEXT,
        media_type TEXT,
        is_forwarded INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        content_id INTEGER,
        issued_at INTEGER,
        expires_at INTEGER,
        is_used INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shortener_requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shortener_url TEXT,
        token TEXT,
        status TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()

def load_password_from_db():
    global PASSWORD
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = 'password'")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            PASSWORD = row[0]
            logger.info("Loaded PASSWORD from DB.")
            return
    except Exception:
        logger.exception("Failed to read password from DB; using env/default.")
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("password", PASSWORD))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to init password in DB.")

def set_password_in_db(new_pass: str):
    global PASSWORD
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("password", new_pass))
    conn.commit()
    conn.close()
    PASSWORD = new_pass

def user_is_authed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_auth, is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    last_auth, is_vip = row
    if is_vip:
        return True
    return (time.time() - last_auth) <= PASSWORD_VALID_SECONDS

def set_user_auth(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    c.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    is_vip = row[0] if row else 0
    c.execute("INSERT OR REPLACE INTO users(user_id,last_auth,is_vip) VALUES(?,?,?)", (user_id, now, is_vip))
    conn.commit()
    conn.close()

def set_user_vip(user_id: int, is_vip: int = 1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_auth FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    last_auth = row[0] if row else 0
    c.execute("INSERT OR REPLACE INTO users(user_id,last_auth,is_vip) VALUES(?,?,?)", (user_id, last_auth, is_vip))
    conn.commit()
    conn.close()

def save_content_to_db(uploader_id: int, thumb_file_id: str, description: str, is_text_only: int, requires_token: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    c.execute("""INSERT INTO content(uploader_id, thumb_file_id, description, is_text_only, requires_token, created_at)
                 VALUES(?,?,?,?,?,?)""", (uploader_id, thumb_file_id, description, is_text_only, requires_token, now))
    content_id = c.lastrowid
    conn.commit()
    conn.close()
    return content_id

def add_media_item(content_id: int, file_id: str, file_unique_id: str, media_type: str, is_forwarded: int = 0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO media_items(content_id, file_id, file_unique_id, media_type, is_forwarded)
                 VALUES(?,?,?,?,?)""", (content_id, file_id, file_unique_id, media_type, is_forwarded))
    conn.commit()
    conn.close()

def get_content(content_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT content_id, uploader_id, thumb_file_id, description, is_text_only, requires_token, created_at, main_channel_message_id FROM content WHERE content_id = ?", (content_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    keys = ["content_id", "uploader_id", "thumb_file_id", "description", "is_text_only", "requires_token", "created_at", "main_channel_message_id"]
    content = dict(zip(keys, row))
    c.execute("SELECT media_id, file_id, file_unique_id, media_type, is_forwarded FROM media_items WHERE content_id = ? ORDER BY media_id ASC", (content_id,))
    media_rows = c.fetchall()
    content["media_items"] = [
        {"media_id": r[0], "file_id": r[1], "file_unique_id": r[2], "media_type": r[3], "is_forwarded": r[4]} for r in media_rows
    ]
    conn.close()
    return content

def create_token_for_user(user_id: int, content_id: int) -> str:
    token = secrets.token_hex(4)
    now = int(time.time())
    expires = now + 24 * 3600
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO tokens(token,user_id,content_id,issued_at,expires_at)
        VALUES(?,?,?,?,?)
    """, (token, user_id, content_id, now, expires))
    conn.commit()
    conn.close()
    return token

def get_valid_token(token: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token,user_id,content_id,issued_at,expires_at FROM tokens WHERE token = ?", (token,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["token", "user_id", "content_id", "issued_at", "expires_at"]
    data = dict(zip(keys, row))
    now = int(time.time())
    if data["expires_at"] < now:
        return None
    return data

def mark_token_used(token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Mark as used (set is_used=1)
    c.execute("UPDATE tokens SET is_used = 1 WHERE token = ?", (token,))
    conn.commit()
    conn.close()

def record_shortener_request(short_url: str, token: str, status: str = "done"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO shortener_requests(shortener_url, token, status) VALUES(?,?,?)", (short_url, token, status))
    conn.commit()
    conn.close()

# ------------------------------
# UI helpers (same as before)
# ------------------------------
def count_media_for_session(session: Dict[str, Any]) -> Dict[str, int]:
    photos = sum(1 for m in session.get("media_list", []) if m["media_type"] == "photo")
    videos = sum(1 for m in session.get("media_list", []) if m["media_type"] == "video")
    docs = sum(1 for m in session.get("media_list", []) if m["media_type"] not in ("photo", "video"))
    return {"photos": photos, "videos": videos, "other": docs}

def kb_upload_options_with_emoji():
    keyboard = [
        [InlineKeyboardButton("🖼️ Upload from phone", callback_data="opt_upload_phone")],
        [InlineKeyboardButton("🔁 Forward media", callback_data="opt_forward")],
        [InlineKeyboardButton("🔗 Upload URL / Text only", callback_data="opt_url_text")],
        [InlineKeyboardButton("❌ Cancel", callback_data="opt_cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)

def kb_token_choice_with_emoji():
    keyboard = [
        [InlineKeyboardButton("🎟️ Yes — requires token", callback_data="tok_yes")],
        [InlineKeyboardButton("✅ No — free (no token)", callback_data="tok_no")],
        [InlineKeyboardButton("❌ Cancel upload", callback_data="opt_cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)

def kb_watch_button_with_emoji(watch_link: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch Video", url=watch_link)]])

def kb_get_token_button_with_emoji(content_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎟️ Get Token", callback_data=f"gettok_{content_id}")]])

# ------------------------------
# exe.io shortener helper (async)
# ------------------------------
async def exeio_shorten_long_url(long_url: str) -> Optional[str]:
    if not EXEIO_API_KEY:
        return None
    try:
        encoded = urllib.parse.quote(long_url, safe='')
        api = f"{EXEIO_API_ENDPOINT}?api={EXEIO_API_KEY}&url={encoded}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api, timeout=10) as resp:
                # many shortener APIs return json; adapt as needed
                try:
                    data = await resp.json()
                    # attempt common fields
                    for key in ("shortenedUrl","short","url"):
                        if data.get(key):
                            return data.get(key)
                    # fallback: if API returns string
                    if isinstance(data, str) and data.startswith("http"):
                        return data
                except Exception:
                    text = await resp.text()
                    if text.startswith("http"):
                        return text.strip()
        return None
    except Exception:
        logger.exception("Shortener failed")
        return None

# ------------------------------
# Handlers (mostly preserved, adapted for webhook style)
# ------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args:
        payload = args[0]
        if payload.startswith("content_"):
            try:
                content_id = int(payload.split("_", 1)[1])
            except Exception:
                await update.effective_chat.send_message("Invalid content link.")
                return
            await handle_view_content(update, context, content_id)
            return
        if payload.startswith("token_"):
            token = payload.split("_", 1)[1]
            await handle_token_start(update, context, token)
            return
    await update.message.reply_text(
        "Welcome. Use /upload to post content (password required).\nIf you have a content link, open it to view."
    )

async def handle_view_content(update: Update, context: ContextTypes.DEFAULT_TYPE, content_id: int):
    user = update.effective_user
    user_id = user.id
    content = get_content(content_id)
    if not content:
        await update.effective_chat.send_message("Content not found.")
        return
    requires_token = bool(content.get("requires_token"))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    is_vip = bool(row[0]) if row else False
    conn.close()
    if not requires_token or is_vip:
        await send_content_media(update, context, content)
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    c.execute(
        "SELECT token,expires_at,is_used FROM tokens WHERE user_id = ? AND content_id = ? ORDER BY issued_at DESC LIMIT 1",
        (user_id, content_id),
    )
    row = c.fetchone()
    conn.close()
    has_valid = False
    token_for_user = None
    if row:
        token_val, expires_at, is_used = row
        if (not is_used) and expires_at >= now:
            has_valid = True
            token_for_user = token_val
    if has_valid:
        mark_token_used(token_for_user)
        await send_content_media(update, context, content)
        return
    kb = kb_get_token_button_with_emoji(content_id)
    await update.effective_chat.send_message(
        "🔒 This content requires a token to watch. Tokens are valid for 24 hours and are one-time-use. Tap below to get your token.", reply_markup=kb
    )

async def handle_token_start(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    user = update.effective_user
    user_id = user.id
    t = get_valid_token(token)
    if not t:
        await update.effective_chat.send_message("❌ Token invalid or expired.")
        return
    if t["user_id"] != user_id:
        await update.effective_chat.send_message("❌ Token doesn't belong to you.")
        return
    mark_token_used(token)
    content = get_content(t["content_id"])
    if not content:
        await update.effective_chat.send_message("Content not found.")
        return
    await send_content_media(update, context, content)

async def send_content_media(update: Update, context: ContextTypes.DEFAULT_TYPE, content: Dict[str, Any]):
    chat = update.effective_chat
    desc = content.get("description", "")
    requires_token = bool(content.get("requires_token"))
    label = "🔒 Token: Required" if requires_token else "🟢 Free"
    caption_intro = f"{desc}\n\n{label}"
    media_items = content.get("media_items", [])
    medias = []
    for i, m in enumerate(media_items):
        caption_text = caption_intro if i == 0 else None
        if m["media_type"] == "photo":
            medias.append(InputMediaPhoto(media=m["file_id"], caption=caption_text))
        elif m["media_type"] == "video":
            medias.append(InputMediaVideo(media=m["file_id"], caption=caption_text))
        else:
            pass
    try:
        if medias:
            if len(medias) == 1:
                if isinstance(medias[0], InputMediaPhoto):
                    await chat.send_photo(photo=medias[0].media, caption=medias[0].caption, protect_content=content_protection)
                else:
                    await chat.send_video(video=medias[0].media, caption=medias[0].caption, protect_content=content_protection)
            else:
                await chat.send_media_group(media=medias[:10], protect_content=content_protection)
        else:
            thumb = content.get("thumb_file_id")
            if thumb:
                await chat.send_photo(photo=thumb, caption=caption_intro, protect_content=content_protection)
            else:
                await chat.send_message(caption_intro)
        for m in media_items:
            if m["media_type"] not in ("photo", "video"):
                await chat.send_document(document=m["file_id"], protect_content=content_protection)
    except Exception as e:
        logger.exception("Failed to send media: %s", e)
        await chat.send_message("Failed to send media. The file ids may be invalid or the bot lacks access.")

# --- Upload flow handlers (mostly unchanged) ---
async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    is_vip = bool(row[0]) if row else False
    conn.close()
    if is_vip:
        sessions[user_id] = {"uploader_id": user_id, "media_list": []}
        await update.message.reply_text("🌟 VIP detected — you can upload now. Send the thumbnail image (photo).")
        return STATE_THUMBNAIL
    if user_is_authed(user_id):
        sessions[user_id] = {"uploader_id": user_id, "media_list": []}
        await update.message.reply_text("🔓 Password validated. Please send the thumbnail image now (photo).")
        return STATE_THUMBNAIL
    else:
        await update.message.reply_text("Please enter the password to begin upload:")
        return STATE_PASSWORD

async def password_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == PASSWORD:
        set_user_auth(user_id)
        sessions[user_id] = {"uploader_id": user_id, "media_list": []}
        await update.message.reply_text("✅ Password accepted for 24 hours. Now send the thumbnail image (photo).")
        return STATE_THUMBNAIL
    else:
        await update.message.reply_text("❌ Wrong password. Send /upload to try again.")
        return ConversationHandler.END

async def thumbnail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        session = sessions.setdefault(user_id, {"uploader_id": user_id, "media_list": []})
        session["thumb_file_id"] = file_id
        await update.message.reply_text("🖼️ Thumbnail saved. Now send the description text message.")
        return STATE_DESCRIPTION
    else:
        await update.message.reply_text("Please send a photo to be used as thumbnail.")
        return STATE_THUMBNAIL

async def description_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Please send a non-empty description.")
        return STATE_DESCRIPTION
    session = sessions.get(user_id)
    session["description"] = text
    await update.message.reply_text("Choose how you want to add content (or Cancel):", reply_markup=kb_upload_options_with_emoji())
    return STATE_OPTION

async def option_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data == "opt_cancel":
        sessions.pop(user_id, None)
        await query.edit_message_text("Upload canceled and session reset.")
        return ConversationHandler.END
    if data == "opt_url_text":
        await query.edit_message_text("Send the URL or text that will be saved as the content (no media).")
        session = sessions.setdefault(user_id, {"uploader_id": user_id, "media_list": []})
        session["is_text_only"] = True
        return STATE_MEDIA_UPLOAD
    if data == "opt_forward":
        await query.edit_message_text("Now forward the media messages from any chat to me. When done, send /done .")
        session = sessions.setdefault(user_id, {"uploader_id": user_id, "media_list": []})
        session["expect_forward"] = True
        return STATE_MEDIA_UPLOAD
    if data == "opt_upload_phone":
        await query.edit_message_text("Now send photos/videos/documents from your phone. When finished, send /done .")
        session = sessions.setdefault(user_id, {"uploader_id": user_id, "media_list": []})
        session["expect_forward"] = False
        return STATE_MEDIA_UPLOAD
    await query.edit_message_text("Unknown option.")
    return ConversationHandler.END

async def media_receiver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = sessions.get(user_id)
    if not session:
        await update.message.reply_text("No active upload session. Send /upload to start.")
        return ConversationHandler.END
    if session.get("is_text_only"):
        await update.message.reply_text("You selected URL/Text. Send the text/URL now (or /cancel).")
        return STATE_MEDIA_UPLOAD
    added = False
    if update.message.photo:
        photo = update.message.photo[-1]
        session["media_list"].append({"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "media_type": "photo", "is_forwarded": 1 if getattr(update.message, "forward_from", None) or getattr(update.message, "forward_from_chat", None) else 0})
        added = True
    if update.message.video:
        vid = update.message.video
        session["media_list"].append({"file_id": vid.file_id, "file_unique_id": vid.file_unique_id, "media_type": "video", "is_forwarded": 1 if getattr(update.message, "forward_from", None) or getattr(update.message, "forward_from_chat", None) else 0})
        added = True
    if update.message.document:
        doc = update.message.document
        session["media_list"].append({"file_id": doc.file_id, "file_unique_id": doc.file_unique_id, "media_type": "document", "is_forwarded": 1 if getattr(update.message, "forward_from", None) or getattr(update.message, "forward_from_chat", None) else 0})
        added = True
    if added:
        counts = count_media_for_session(session)
        await update.message.reply_text(f"Saved media. Current counts — 🖼 Photos: {counts['photos']}, 🎬 Videos: {counts['videos']}, 📁 Other: {counts['other']}. When finished send /done or /cancel.")
    else:
        await update.message.reply_text("No supported media found in that message. Send photo/video/document, or /done when finished.")
    return STATE_MEDIA_UPLOAD

async def url_text_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = sessions.get(user_id)
    if not session or not session.get("is_text_only"):
        await update.message.reply_text("No URL/Text upload session active. Use /upload to start.")
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Please send a non-empty URL or text.")
        return STATE_MEDIA_UPLOAD
    session["url_text"] = text
    return await ask_token_requirement(update, context)

async def done_receiving_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = sessions.get(user_id)
    if not session:
        await update.message.reply_text("No active session. Send /upload to start.")
        return ConversationHandler.END
    if not session.get("is_text_only") and not session.get("media_list"):
        await update.message.reply_text("You didn't add any media. Use /cancel to reset or add media.")
        return STATE_MEDIA_UPLOAD
    return await ask_token_requirement(update, context)

async def ask_token_requirement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text("Does this content require a watch token?", reply_markup=kb_token_choice_with_emoji())
    return STATE_CONFIRM_TOKEN

async def token_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data == "opt_cancel":
        sessions.pop(user_id, None)
        await query.edit_message_text("Upload canceled and session reset.")
        return ConversationHandler.END
    requires_token = 1 if data == "tok_yes" else 0
    session = sessions.get(user_id)
    thumbnail = session.get("thumb_file_id")
    description = session.get("description", "")
    is_text_only = 1 if session.get("is_text_only") else 0
    content_id = save_content_to_db(user_id, thumbnail, description, is_text_only, requires_token)
    for m in session.get("media_list", []):
        add_media_item(content_id, m["file_id"], m.get("file_unique_id", ""), m["media_type"], m.get("is_forwarded", 0))
    if is_text_only:
        url_text = session.get("url_text", "")
        if url_text:
            description_to_save = f"{description}\n\n[URL/TEXT]\n{url_text}"
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE content SET description = ? WHERE content_id = ?", (description_to_save, content_id))
            conn.commit()
            conn.close()
    counts = count_media_for_session(session)
    summary = f"🖼 Photos: {counts['photos']} | 🎬 Videos: {counts['videos']}"
    bot_username = (context.bot.username or "").lstrip("@")
    watch_link = f"https://t.me/{bot_username}?start=content_{content_id}"
    kb = kb_watch_button_with_emoji(watch_link)
    caption = f"{session.get('description','')}\n\n{summary}\n\n{'🔒 Token: Required' if requires_token else '🟢 Free'}"
    try:
        sent = await context.bot.send_photo(chat_id=MAIN_CHANNEL_ID, photo=thumbnail, caption=caption, reply_markup=kb)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE content SET main_channel_message_id = ? WHERE content_id = ?", (sent.message_id, content_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to post to main channel: %s", e)
        await query.edit_message_text(f"Saved content (id {content_id}) but failed to post to MAIN CHANNEL. Error: {e}")
        sessions.pop(user_id, None)
        return ConversationHandler.END
    await query.edit_message_text(f"✅ Content posted to main channel as content_id {content_id}.\nWatch link: {watch_link}\nUpload finished.")
    sessions.pop(user_id, None)
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await update.message.reply_text("Upload cancelled and session reset.")
    return ConversationHandler.END

async def callback_get_token_exeio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("gettok_"):
        await query.edit_message_text("Unknown action.")
        return
    try:
        content_id = int(data.split("_", 1)[1])
    except Exception:
        await query.edit_message_text("Invalid content id.")
        return
    user_id = query.from_user.id
    token = create_token_for_user(user_id, content_id)
    bot_username = (context.bot.username or "").lstrip("@")
    long_watch_link = f"https://t.me/{bot_username}?start=token_{token}"
    short_link = await exeio_shorten_long_url(long_watch_link)
    if short_link:
        record_shortener_request(short_link, token, status="created")
        await query.edit_message_text(
            "🎟️ *Token Generated Successfully!*\n\n"
            "To unlock this content, click below 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Get Access", url=short_link)]]),
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text(
            "⚠️ Shortener failed, but your token is active.\n\nClick below to open directly 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶ Watch Now", url=long_watch_link)]]),
        )

# Admin
async def cmd_addvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Only admins can manage VIPs.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addvip <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user id")
        return
    set_user_vip(uid, 1)
    await update.message.reply_text(f"User {uid} marked as VIP.")

async def cmd_delvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Only admins can manage VIPs.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /delvip <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user id")
        return
    set_user_vip(uid, 0)
    await update.message.reply_text(f"User {uid} removed from VIPs.")

async def cmd_changepass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Only admins can change the upload password.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /changepass <new_password>")
        return
    newpass = context.args[0].strip()
    if not newpass:
        await update.message.reply_text("Password cannot be empty.")
        return
    try:
        set_password_in_db(newpass)
        await update.message.reply_text("🔒 Upload password changed successfully and saved.")
    except Exception as e:
        logger.exception("Failed to change password.")
        await update.message.reply_text(f"Failed to change password: {e}")

async def cmd_myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_auth,is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ You are not authenticated and not a VIP. Use /upload to start and provide password.")
        return
    last_auth, is_vip = row
    if is_vip:
        await update.message.reply_text("🌟 You are a VIP user. You can upload and view token-protected content without tokens.")
        return
    remaining = max(0, int(PASSWORD_VALID_SECONDS - (time.time() - last_auth)))
    hrs = remaining // 3600
    mins = (remaining % 3600) // 60
    secs = remaining % 60
    await update.message.reply_text(f"⏳ Password valid for another {hrs}h {mins}m {secs}s.")

# ------------------------------
# Application & Webhook wiring
# ------------------------------
# Flask for health checks + webhook entry
flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def home():
    return "✅ Bot is alive (webhook)."

# We'll start the telegram Application and attach its webhook route via Flask's WSGI integration below.

# Build the Application with standard handlers setup
def build_application() -> Application:
    app = ApplicationBuilder().token(UPLOAD_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload)],
        states={
            STATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, password_text)],
            STATE_THUMBNAIL: [MessageHandler(filters.PHOTO & ~filters.COMMAND, thumbnail_handler), CommandHandler("cancel", cancel_command)],
            STATE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_handler), CommandHandler("cancel", cancel_command)],
            STATE_OPTION: [CallbackQueryHandler(option_pressed)],
            STATE_MEDIA_UPLOAD: [
                MessageHandler((filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, media_receiver),
                MessageHandler(filters.TEXT & ~filters.COMMAND, url_text_receive),
                CommandHandler("done", done_receiving_media),
                CommandHandler("cancel", cancel_command),
            ],
            STATE_CONFIRM_TOKEN: [CallbackQueryHandler(token_choice_callback)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(option_pressed, pattern="^opt_"))
    app.add_handler(CallbackQueryHandler(token_choice_callback, pattern="^tok_"))
    app.add_handler(CallbackQueryHandler(callback_get_token_exeio, pattern="^gettok_"))
    app.add_handler(CommandHandler("addvip", cmd_addvip))
    app.add_handler(CommandHandler("delvip", cmd_delvip))
    app.add_handler(CommandHandler("changepass", cmd_changepass))
    app.add_handler(CommandHandler("myinfo", cmd_myinfo))

    return app

# We'll mount the telegram Application's webhook endpoint inside Flask.
# Use /webhook/<token> path (keeps it secretish). Flask receives POST JSON, converts to Update and passes to app.process_update.

TELEGRAM_WEBHOOK_PATH = f"/webhook/{UPLOAD_BOT_TOKEN.split(':')[0]}"

# Create global app
application = build_application()

@flask_app.route(TELEGRAM_WEBHOOK_PATH, methods=["POST"])
def telegram_webhook_entry():
    if request.headers.get("content-type", "").startswith("application/json"):
        data = request.get_json(force=True)
    else:
        data = request.get_data(as_text=True)
        try:
            import json
            data = json.loads(data)
        except Exception:
            logger.warning("Webhook received non-json body")
            return "bad request", 400
    # create Update and schedule processing on asyncio loop
    try:
        update = Update.de_json(data, application.bot)
    except Exception:
        logger.exception("Failed to parse update")
        return "bad request", 400
    # Use asyncio.create_task to process the update asynchronously, but ensure event loop is running
    try:
        # If running under same event loop, use create_task
        loop = asyncio.get_event_loop()
        # schedule processing
        loop.create_task(application.process_update(update))
    except RuntimeError:
        # no running loop — run synchronously (fallback)
        asyncio.run(application.process_update(update))
    return "ok", 200

# ------------------------------
# Startup helpers: set webhook on startup (if requested)
# ------------------------------
async def set_webhook_if_needed():
    if not SET_WEBHOOK:
        logger.info("SET_WEBHOOK not enabled: skipping webhook set")
        return
    if WEBHOOK_URL:
        webhook = WEBHOOK_URL
    else:
        if not RENDER_EXTERNAL_HOSTNAME:
            raise RuntimeError("RENDER_EXTERNAL_HOSTNAME not set and WEBHOOK_URL not provided")
        webhook = f"https://{RENDER_EXTERNAL_HOSTNAME}{TELEGRAM_WEBHOOK_PATH}"
    # ensure webhook is HTTPS and looks sane
    logger.info("Setting webhook to %s", webhook)
    try:
        await application.bot.set_webhook(url=webhook)
        logger.info("Webhook set successfully")
    except Exception:
        logger.exception("Failed to set webhook")

# ------------------------------
# Main entry — init DB, load password, start flask + telegram event loop
# ------------------------------
def main():
    init_db()
    load_password_from_db()

    # Start the Telegram Application background (initialization)
    async def start_async():
        await application.initialize()
        await application.start()
        await set_webhook_if_needed()
        # Do NOT call application.updater.start_polling() — we rely on webhook only
        logger.info("Telegram Application initialized and running (webhook mode).")
        # Keep the application running until process exit (we don't call stop here).
    # Run the telegram app initialization in background thread / task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(start_async())
    except Exception:
        logger.exception("Failed to start telegram application")
        raise

    # Run flask (blocking). Render will use gunicorn/uvicorn; but for simple Render web service, Flask's built-in server is enough.
    # If you prefer a production WSGI server, configure Render to run via gunicorn pointing to this file.
    logger.info("Starting Flask webserver on port %d", PORT)
    # Run Flask app — this call blocks the process and handles incoming webhook POSTs
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
