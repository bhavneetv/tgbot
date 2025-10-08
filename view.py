# viewer_bot.py
import sqlite3
import time
import logging
import secrets
from typing import List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)



# ---------- CONFIG ----------
VIEWER_BOT_TOKEN = "8413595718:AAEI8yJAcDt22VbzASEpNR_aJNMXrMscdGk"
TOKEN = "8413595718:AAEI8yJAcDt22VbzASEpNR_aJNMXrMscdGk"
DB_PATH = "tg_content.db"
TOKEN_TTL = 24 * 3600  # 24 hours
SHORTENER_CALLBACK_SECRET = "a_changeable_secret"  # used if you implement webhook callbacks from shortener
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # (Tables should already exist if upload bot ran, but ensure existence)
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
    conn.commit()
    conn.close()


def fetch_content(content_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT content_id, uploader_id, thumb_file_id, description, is_text_only, requires_token, created_at FROM content WHERE content_id = ?", (content_id,))
    row = c.fetchone()
    conn.close()
    return row


def fetch_media_items(content_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT file_id, media_type FROM media_items WHERE content_id = ? ORDER BY media_id ASC", (content_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def user_is_vip(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


def check_user_has_valid_token(user_id: int, content_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    c.execute("""SELECT token FROM tokens WHERE user_id = ? AND content_id = ? AND expires_at > ?""", (user_id, content_id, now))
    row = c.fetchone()
    conn.close()
    return bool(row)


def issue_token_to_user(user_id: int, content_id: int) -> str:
    token = secrets.token_urlsafe(10)
    now = int(time.time())
    expires = now + TOKEN_TTL
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tokens(token, user_id, content_id, issued_at, expires_at) VALUES(?,?,?,?,?)", (token, user_id, content_id, now, expires))
    conn.commit()
    conn.close()
    return token


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start and deep-links like /start content_<id>"""
    user = update.effective_user
    args = context.args  # deep-link params
    if not args:
        await update.message.reply_text("Welcome to Viewer Bot. Use /help for usage.")
        return

    param = args[0]
    if not param.startswith("content_"):
        await update.message.reply_text("Invalid start parameter.")
        return

    try:
        content_id = int(param.split("_", 1)[1])
    except Exception:
        await update.message.reply_text("Invalid content id.")
        return

    row = fetch_content(content_id)
    if not row:
        await update.message.reply_text("Content not found.")
        return

    _, uploader_id, thumb_file_id, description, is_text_only, requires_token, created_at = row

    # VIP bypass or content no token required or user has valid token:
    if is_text_only:
        # Display the description/text directly.
        await update.message.reply_text(f"Content (text):\n\n{description}")
        return

    if not requires_token or user_is_vip(user.id) or check_user_has_valid_token(user.id, content_id):
        # Send the media
        media_rows = fetch_media_items(content_id)
        if not media_rows:
            await update.message.reply_text("No media found for this content.")
            return

        # group into InputMedia list
        media_group = []
        for file_id, media_type in media_rows:
            if media_type == "photo":
                media_group.append({"type": "photo", "media": file_id})
            elif media_type == "video":
                media_group.append({"type": "video", "media": file_id})
            else:
                # for other types, we can send as document
                media_group.append({"type": "document", "media": file_id})

        # For safety, send up to 10 items as an album or sequentially
        # python-telegram-bot requires using send_media_group for albums of photos/videos; we will chunk by 10
        # But for simplicity, send them sequentially:
        await update.message.reply_text("Access granted. Sending media...")
        for m in media_group:
            if m["type"] == "photo":
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=m["media"])
            elif m["type"] == "video":
                await context.bot.send_video(chat_id=update.effective_chat.id, video=m["media"])
            else:
                await context.bot.send_document(chat_id=update.effective_chat.id, document=m["media"])
        return

    # otherwise: requires token and user doesn't have it
    # Provide shortener link to get token. The shortener should redirect back to a URL you control or directly to Telegram deep link once completed.
    # We'll provide two options: (1) simulate token issuance locally via /gettoken (for testing)
    # (2) real integration: replace create_shortener_link(...) to call exe.io or your shortener API and configure a landing endpoint to mark the token as issued.
    short_link_sim = f"To get access, click this link (simulated): https://t.me/{context.bot.username}?start=gettoken_{content_id}\n\nOr use /gettoken_{content_id} in this bot (testing)."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Get Token (simulate)", callback_data=f"gettok_{content_id}")]])
    await update.message.reply_text("This content requires a watch token. You don't have a valid token.\n\nOptions:\n- Use an ad-shortener flow (configure in the bot) to obtain a token automatically.\n- For testing, press the button below to get a simulated token (bypasses ad-shortening).", reply_markup=kb)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data and data.startswith("gettok_"):
        try:
            content_id = int(data.split("_", 1)[1])
        except:
            await query.edit_message_text("Invalid request.")
            return
        token = issue_token_to_user(user.id, content_id)
        await query.edit_message_text(f"Token granted (simulated). Valid for 24 hours.\nYou can now re-open the content link or press /start content_{content_id}. Token: {token}")
        return


async def simulated_gettoken_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for simulated deep-link like: /start gettoken_<contentid>
       (This is just for local testing in absence of a real shortener)."""
    args = context.args
    if not args:
        await update.message.reply_text("No simulated token requested.")
        return
    param = args[0]
    if param.startswith("gettoken_"):
        try:
            content_id = int(param.split("_", 1)[1])
        except:
            await update.message.reply_text("Invalid content id.")
            return
        token = issue_token_to_user(update.effective_user.id, content_id)
        await update.message.reply_text(f"Simulated token issued for content {content_id}. Token valid for 24 hours.")
        # optionally redirect user to content start:
        await update.message.reply_text(f"Now open the content: https://t.me/{context.bot.username}?start=content_{content_id}")
        return

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me the deep-link from the main channel's Watch button, or /start content_<id>.\nVIP users bypass token checks.\nAdmins can issue tokens manually.")


def main():
    init_db()
    app = ApplicationBuilder().token(VIEWER_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("gettoken", simulated_gettoken_start))  # dev testing

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, help_cmd))
    app.add_handler(MessageHandler(filters.COMMAND, help_cmd))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    logger.info("Viewer Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
