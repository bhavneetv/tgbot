# video_viewer_bot.py
import sqlite3, time, secrets, requests
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

BOT_TOKEN = "8413595718:AAEI8yJAcDt22VbzASEpNR_aJNMXrMscdGk"
SHORTENER_API = "c204899d0187dc988e3d368d21038fbf82789531"  # For monetized link

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def shorten_link(long_url):
    try:
        r = requests.get("https://exe.io/api", params={"api": SHORTENER_API, "url": long_url})
        data = r.json()
        return data.get("shortenedUrl", long_url)
    except:
        return long_url

def validate_token(token):
    conn = sqlite3.connect("content.db")
    c = conn.cursor()
    c.execute("SELECT * FROM tokens WHERE token=? AND expires_at>?", (token, int(time.time())))
    data = c.fetchone()
    conn.close()
    return data

@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    args = msg.get_args()
    if args.startswith("token_"):
        token = args.split("_",1)[1]
        data = validate_token(token)
        if not data:
            # Token expired → monetized short link
            new_link = shorten_link(f"https://t.me/{(await bot.get_me()).username}?start=token_{secrets.token_urlsafe(8)}")
            return await msg.answer(f"⛔ Token expired!\nGet a new one here:\n{new_link}")

        _, content_id, _ = data
        # Fetch content
        conn = sqlite3.connect("content.db")
        c = conn.cursor()
        c.execute("SELECT * FROM contents WHERE id=?", (content_id,))
        content = c.fetchone()
        conn.close()

        if content:
            _, title, desc, thumbnail, ctype, items, _, _ = content
            await msg.answer_photo(thumbnail, caption=f"🎬 {desc}")
            for i in eval(items):
                if ctype=="video":
                    await msg.answer_video(i)
                else:
                    await msg.answer(f"🔗 {i}")
    else:
        await msg.answer("👋 Welcome! You need a valid token link to view content.")

executor.start_polling(dp)
