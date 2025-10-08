# upload_bot.py
import sqlite3
import time
import logging
import secrets
from typing import (Dict, Any, List)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, ConversationHandler, filters
)

# ---------- CONFIG ----------
UPLOAD_BOT_TOKEN = "7986735755:AAHQ5Ke7TI9uBxcYivDpib5pNzOmebGdZSY"
MAIN_CHANNEL_ID = "-1003104322226"  # or numeric -100...
PASSWORD = "test"
PASSWORD_VALID_SECONDS = 24 * 3600
DB_PATH = "tg_content.db"
# ----------------------------

# Conversation states
(
    STATE_PASSWORD,
    STATE_THUMBNAIL,
    STATE_DESCRIPTION,
    STATE_OPTION,
    STATE_MEDIA_UPLOAD,
    STATE_TEXT_UPLOAD,
    STATE_TOKEN_REQUIRE,
    STATE_CONFIRM_TOKEN
) = range(8)


# In-memory upload sessions; short-lived. If you restart the bot you lose in-progress sessions.
sessions: Dict[int, Dict[str, Any]] = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db():
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
    conn.commit()
    conn.close()


def user_is_authed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_auth FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    last_auth = row[0]
    return (time.time() - last_auth) <= PASSWORD_VALID_SECONDS


def set_user_auth(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = int(time.time())
    c.execute("INSERT OR REPLACE INTO users(user_id,last_auth) VALUES(?,?)", (user_id, now))
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


def count_media_for_session(session: Dict[str, Any]) -> Dict[str, int]:
    # session['media_list'] is list of dicts: {'file_id':..., 'media_type':...}
    photos = sum(1 for m in session.get("media_list", []) if m["media_type"] == "photo")
    videos = sum(1 for m in session.get("media_list", []) if m["media_type"] == "video")
    docs = sum(1 for m in session.get("media_list", []) if m["media_type"] not in ("photo", "video"))
    return {"photos": photos, "videos": videos, "other": docs}


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Upload Bot.\n"
        "To upload content, send /upload\n"
        "You must enter password 'test' which remains valid for 24 hours."
    )


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_is_authed(user_id):
        # skip password entry, proceed to thumbnail
        sessions[user_id] = {"uploader_id": user_id, "media_list": []}
        await update.message.reply_text("Password already validated for 24 hours. Please send the thumbnail image now (as a photo).")
        return STATE_THUMBNAIL
    else:
        await update.message.reply_text("Please enter the password to begin upload:")
        return STATE_PASSWORD


async def password_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    if text.strip() == PASSWORD:
        set_user_auth(user_id)
        sessions[user_id] = {"uploader_id": user_id, "media_list": []}
        await update.message.reply_text("Password accepted for 24 hours. Now send the thumbnail image (photo).")
        return STATE_THUMBNAIL
    else:
        await update.message.reply_text("Wrong password. Send /upload to try again.")
        return ConversationHandler.END


async def thumbnail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.photo:
        # pick largest size
        photo = update.message.photo[-1]
        file_id = photo.file_id
        session = sessions.setdefault(user_id, {"uploader_id": user_id, "media_list": []})
        session["thumb_file_id"] = file_id
        await update.message.reply_text("Thumbnail saved. Now send the description text message.")
        return STATE_DESCRIPTION
    else:
        await update.message.reply_text("Please send a photo to be used as thumbnail.")
        return STATE_THUMBNAIL


async def description_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""
    if not text.strip():
        await update.message.reply_text("Please send a non-empty description.")
        return STATE_DESCRIPTION
    session = sessions.get(user_id)
    session["description"] = text.strip()

    # present 4 options
    keyboard = [
        [InlineKeyboardButton("1) Upload from phone", callback_data="opt_upload_phone")],
        [InlineKeyboardButton("2) Forward media", callback_data="opt_forward")],
        [InlineKeyboardButton("3) Upload URL / Text only", callback_data="opt_url_text")],
        [InlineKeyboardButton("4) Cancel", callback_data="opt_cancel")]
    ]
    await update.message.reply_text("Choose how you want to add content (or Cancel):", reply_markup=InlineKeyboardMarkup(keyboard))
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
        # for URL/Text only: ask them to send the text or URL now
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
    """Receive phone-uploaded media or forwarded media depending on session."""
    user_id = update.effective_user.id
    session = sessions.get(user_id)
    if not session:
        await update.message.reply_text("No active upload session. Send /upload to start.")
        return ConversationHandler.END

    # If user chose URL/Text only, here we don't accept media
    if session.get("is_text_only"):
        await update.message.reply_text("You selected URL/Text. Send the text/URL now (or /cancel).")
        return STATE_MEDIA_UPLOAD

    # Accept photos, videos, animations, documents
    added = False

    if update.message.photo:
        # store the largest size
        photo = update.message.photo[-1]
        session["media_list"].append({"file_id": photo.file_id, "file_unique_id": photo.file_unique_id, "media_type": "photo", "is_forwarded": 1 if update.message.forward_from or update.message.forward_from_chat else 0})
        added = True

    if update.message.video:
        vid = update.message.video
        session["media_list"].append({"file_id": vid.file_id, "file_unique_id": vid.file_unique_id, "media_type": "video", "is_forwarded": 1 if update.message.forward_from or update.message.forward_from_chat else 0})
        added = True

    if update.message.document:
        doc = update.message.document
        session["media_list"].append({"file_id": doc.file_id, "file_unique_id": doc.file_unique_id, "media_type": "document", "is_forwarded": 1 if update.message.forward_from or update.message.forward_from_chat else 0})
        added = True

    if added:
        counts = count_media_for_session(session)
        await update.message.reply_text(f"Saved media. Current counts — Photos: {counts['photos']}, Videos: {counts['videos']}, Other: {counts['other']}. When finished send /done or /cancel.")
    else:
        await update.message.reply_text("No supported media found in that message. Send photo/video/document, or /done when finished.")
    return STATE_MEDIA_UPLOAD


async def url_text_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle URL/Text-only option: user sends the content text or URL."""
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
    # proceed to token requirement question
    return await ask_token_requirement(update, context)


async def done_receiving_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When user done adding media (or forwarded), ask about token requirement."""
    user_id = update.effective_user.id
    session = sessions.get(user_id)
    if not session:
        await update.message.reply_text("No active session. Send /upload to start.")
        return ConversationHandler.END

    # if no media and not text only:
    if not session.get("is_text_only") and not session.get("media_list"):
        await update.message.reply_text("You didn't add any media. Use /cancel to reset or add media.")
        return STATE_MEDIA_UPLOAD

    return await ask_token_requirement(update, context)


async def ask_token_requirement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask whether the content requires a watch token."""
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("Yes — requires token", callback_data="tok_yes")],
        [InlineKeyboardButton("No — free (no token)", callback_data="tok_no")],
        [InlineKeyboardButton("Cancel upload", callback_data="opt_cancel")]
    ]
    await update.message.reply_text("Does this content require a watch token?", reply_markup=InlineKeyboardMarkup(keyboard))
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
    # save content into DB
    thumbnail = session.get("thumb_file_id")
    description = session.get("description", "")
    is_text_only = 1 if session.get("is_text_only") else 0
    content_id = save_content_to_db(user_id, thumbnail, description, is_text_only, requires_token)

    # Add media items if any
    for m in session.get("media_list", []):
        add_media_item(content_id, m["file_id"], m.get("file_unique_id", ""), m["media_type"], m.get("is_forwarded", 0))

    # If text-only, also save that as a special media row (optional) or store URL text in description (we stored it in description)
    if is_text_only:
        # put the url/text into description if not already
        url_text = session.get("url_text", "")
        if url_text:
            # append or replace description
            description_to_save = f"{description}\n\n[URL/TEXT]\n{url_text}"
            # update content description
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE content SET description = ? WHERE content_id = ?", (description_to_save, content_id))
            conn.commit()
            conn.close()

    # Build and post to main channel
    counts = count_media_for_session(session)
    summary = f"Photos: {counts['photos']} | Videos: {counts['videos']}"
    
    # Create inline keyboard: Watch button linking to viewer bot
    viewer_bot_username = context.bot.username.replace("upload", "viewer") if "upload" in context.bot.username else "<VIEWER_BOT_USERNAME>"
    # Prefer to use configured viewer username instead - set as environment variable or replace below:
    viewer_bot_username = "Viewvideos10bot"  # REPLACE with your viewer bot username e.g. MyViewerBot
    watch_link = f"https://t.me/{viewer_bot_username}?start=content_{content_id}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("▶ Watch Video", url=watch_link)]])

    # Post to main channel: send thumbnail + description + summary + watch button
    # Use send_photo with thumbnail and caption
    caption = f"{session.get('description','')}\n\n{summary}\n\nToken: {'Required' if requires_token else 'Not required'}"
    try:
        sent = await context.bot.send_photo(chat_id=MAIN_CHANNEL_ID, photo=thumbnail, caption=caption, reply_markup=kb)
        # Save main_channel_message_id
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

    await query.edit_message_text(f"Content posted to main channel as content_id {content_id}.\nWatch link: {watch_link}\nUpload finished.")
    sessions.pop(user_id, None)
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await update.message.reply_text("Upload cancelled and session reset.")
    return ConversationHandler.END


# small helper to allow editing viewer bot username easily:
def set_viewer_bot_username_in_code(username: str):
    # NOT used programmatically: just a reminder to replace the placeholder above.
    pass


def main():
    init_db()
    app = ApplicationBuilder().token(UPLOAD_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("upload", cmd_upload)],
        states={
            STATE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, password_text)],
            STATE_THUMBNAIL: [MessageHandler(filters.PHOTO & ~filters.COMMAND, thumbnail_handler),
                              CommandHandler("cancel", cancel_command)],
            STATE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_handler),
                                CommandHandler("cancel", cancel_command)],
            STATE_OPTION: [CallbackQueryHandler(option_pressed)],
            STATE_MEDIA_UPLOAD: [
                MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL & ~filters.COMMAND, media_receiver),
                MessageHandler(filters.TEXT & ~filters.COMMAND, url_text_receive),  # used for url/text option
                CommandHandler("done", done_receiving_media),
                CommandHandler("cancel", cancel_command)
            ],
            STATE_CONFIRM_TOKEN: [CallbackQueryHandler(token_choice_callback)]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # also handle callback queries (option buttons) globally
    app.add_handler(CallbackQueryHandler(option_pressed, pattern="^opt_"))
    app.add_handler(CallbackQueryHandler(token_choice_callback, pattern="^tok_"))

    logger.info("Upload Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
