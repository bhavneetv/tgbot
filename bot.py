# admin_upload_bot.py
import sqlite3, secrets, time
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = "7986735755:AAHQ5Ke7TI9uBxcYivDpib5pNzOmebGdZSY"   # Replace with your Admin Bot token
ADMIN_ID =  6233731222
           # Your Telegram ID
VIDEO_VIEWER_BOT_USERNAME = "Viewvideos10bot"  # Your Video Viewer Bot username

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ------------------ DATABASE ------------------
def init_db():
    conn = sqlite3.connect("content.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS contents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        thumbnail TEXT,
        type TEXT,
        items TEXT,
        require_token INTEGER,
        created_at INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tokens (
        token TEXT,
        content_id INTEGER,
        expires_at INTEGER
    )""")
    conn.commit()
    conn.close()

init_db()

# ------------------ STATE ------------------
upload_state = {}

# ------------------ CANCEL BUTTON ------------------
cancel_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
cancel_keyboard.add(KeyboardButton("❌ Cancel"))

# ------------------ /upload ------------------
@dp.message_handler(commands=['upload'])
async def upload_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("❌ Only admin can upload.")
    upload_state[msg.from_user.id] = {"items": []}
    await msg.answer("🖼 Send thumbnail image.", reply_markup=cancel_keyboard)

# ------------------ CANCEL HANDLER ------------------
@dp.message_handler(lambda m: m.text == "❌ Cancel" and m.from_user.id in upload_state)
async def cancel(msg: types.Message):
    del upload_state[msg.from_user.id]
    await msg.answer("🛑 Upload cancelled.", reply_markup=types.ReplyKeyboardRemove())

# ------------------ GET THUMBNAIL ------------------
@dp.message_handler(content_types=['photo'])
async def get_thumbnail(msg: types.Message):
    if msg.from_user.id in upload_state and 'thumbnail' not in upload_state[msg.from_user.id]:
        upload_state[msg.from_user.id]['thumbnail'] = msg.photo[-1].file_id
        await msg.answer("📝 Send description text.", reply_markup=cancel_keyboard)

# ------------------ GET DESCRIPTION ------------------
@dp.message_handler(lambda m: m.from_user.id in upload_state and 'desc' not in upload_state[m.from_user.id])
async def get_desc(msg: types.Message):
    upload_state[msg.from_user.id]['desc'] = msg.text
    await msg.answer("📦 Send type: 'video' or 'link'.", reply_markup=cancel_keyboard)

# ------------------ GET TYPE ------------------
@dp.message_handler(lambda m: m.from_user.id in upload_state and 'type' not in upload_state[m.from_user.id])
async def get_type(msg: types.Message):
    t = msg.text.lower()
    if t not in ['video', 'link']:
        return await msg.answer("Type must be 'video' or 'link'.")
    upload_state[msg.from_user.id]['type'] = t
    upload_state[msg.from_user.id]['stage'] = 'items'
    await msg.answer("📤 Send 1–8 videos or links. Send one by one. Type 'done' when finished.", reply_markup=cancel_keyboard)

# ------------------ COLLECT ITEMS ------------------
@dp.message_handler(lambda m: m.from_user.id in upload_state and upload_state[m.from_user.id].get('stage') == 'items', content_types=['video', 'text'])
async def collect_items(msg: types.Message):
    data = upload_state[msg.from_user.id]
    
    # Handle done
    if msg.text and msg.text.lower() == 'done':
        if len(data['items']) == 0:
            return await msg.answer("⚠️ You must add at least 1 item.")
        await msg.answer("🔒 Require token? (yes/no)", reply_markup=cancel_keyboard)
        data['stage'] = 'token'
        return
    
    # Handle video
    if data['type'] == 'video':
        if msg.video:
            if msg.forward_from_chat:
                copied = await bot.copy_message(chat_id=msg.chat.id, from_chat_id=msg.forward_from_chat.id, message_id=msg.message_id)
                file_id = copied.video.file_id
            else:
                file_id = msg.video.file_id
            data['items'].append(file_id)
        else:
            await msg.answer("⚠️ Send a valid video or forward a video.")
            return
    # Handle link
    elif data['type'] == 'link':
        if msg.text and msg.text.startswith("http"):
            data['items'].append(msg.text)
        else:
            await msg.answer("⚠️ Send a valid link starting with http.")
            return
    
    if len(data['items']) >= 8:
        await msg.answer("✅ Maximum 8 items reached. Type 'done' to continue.")

# ------------------ TOKEN STAGE ------------------
@dp.message_handler(lambda m: m.from_user.id in upload_state and upload_state[m.from_user.id].get('stage') == 'token')
async def require_token(msg: types.Message):
    require = 1 if msg.text.lower() == 'yes' else 0
    d = upload_state[msg.from_user.id]
    
    # Save content in DB
    conn = sqlite3.connect("content.db")
    c = conn.cursor()
    c.execute("INSERT INTO contents(title, description, thumbnail, type, items, require_token, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              ("New Content", d['desc'], d['thumbnail'], d['type'], str(d['items']), require, int(time.time())))
    conn.commit()
    cid = c.lastrowid
    conn.close()
    
    # Generate token
    token = secrets.token_urlsafe(8)
    expires_at = int(time.time()) + 86400
    conn = sqlite3.connect("content.db")
    c = conn.cursor()
    c.execute("INSERT INTO tokens(token, content_id, expires_at) VALUES (?, ?, ?)", (token, cid, expires_at))
    conn.commit()
    conn.close()
    
    # Generate pre-made template with inline button
    watch_link = f"https://t.me/{VIDEO_VIEWER_BOT_USERNAME}?start=token_{token}"
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("▶ Watch Video", url=watch_link))
    
    await msg.answer_photo(d['thumbnail'], caption=f"🎬 {d['desc']}\n\nSend this message to your channel.", reply_markup=keyboard)
    
    del upload_state[msg.from_user.id]
