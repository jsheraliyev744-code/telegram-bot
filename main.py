
import os
import re
import sqlite3
import asyncio
import logging
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ==============================
# SETTINGS
# ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE = 45 * 1024 * 1024
DB_FILE = "users.db"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

DOMAINS = [
    "youtube.com", "youtu.be",
    "instagram.com",
    "tiktok.com", "vm.tiktok.com",
    "vt.tiktok.com",
    "pinterest.com", "pin.it"
]

# ==============================
# DATABASE
# ==============================

def init_db():
    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                language TEXT DEFAULT 'uz'
            )
        """)
        db.commit()


def get_language(user_id):
    with sqlite3.connect(DB_FILE) as db:
        row = db.execute(
            "SELECT language FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    return row[0] if row else None


def save_language(user_id, language):
    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            INSERT INTO users(user_id, language)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET language=excluded.language
        """, (user_id, language))
        db.commit()


# ==============================
# LANGUAGES
# ==============================

TEXTS = {
    "uz": {
        "welcome": (
            "👋 Assalomu alaykum!\n\n"
            "📥 Men sizga video yuklab beruvchi botman.\n\n"
            "🔗 YouTube, Instagram, TikTok yoki "
            "Pinterest havolasini yuboring.\n\n"
            "🎬 Videoni ovozi bilan yuborishga harakat qilaman."
        ),
        "selected": "✅ O'zbek tili tanlandi!",
        "send": "🔗 Endi video havolasini yuboring.",
        "loading": "⏳ Video yuklanmoqda, kuting...",
        "done": "✅ Video tayyor!",
        "invalid": "❌ Iltimos, to'g'ri video havolasini yuboring.",
        "error": "⚠️ Videoni yuklab bo'lmadi. Boshqa havolani sinab ko'ring.",
        "large": "⚠️ Video hajmi 45 MB dan katta."
    },

    "ru": {
        "welcome": (
            "👋 Здравствуйте!\n\n"
            "📥 Я бот для загрузки видео.\n\n"
            "🔗 Отправьте ссылку YouTube, Instagram, "
            "TikTok или Pinterest."
        ),
        "selected": "✅ Русский язык выбран!",
        "send": "🔗 Теперь отправьте ссылку на видео.",
        "loading": "⏳ Загружаю видео, подождите...",
        "done": "✅ Видео готово!",
        "invalid": "❌ Отправьте правильную ссылку.",
        "error": "⚠️ Не удалось загрузить видео. Попробуйте другую ссылку.",
        "large": "⚠️ Видео слишком большое. Максимум 45 МБ."
    },

    "en": {
        "welcome": (
            "👋 Hello!\n\n"
            "📥 I am a video downloader bot.\n\n"
            "🔗 Send a YouTube, Instagram, TikTok "
            "or Pinterest link."
        ),
        "selected": "✅ English language selected!",
        "send": "🔗 Now send a video link.",
        "loading": "⏳ Downloading video, please wait...",
        "done": "✅ Your video is ready!",
        "invalid": "❌ Please send a valid video link.",
        "error": "⚠️ Could not download the video. Try another link.",
        "large": "⚠️ Video is too large. Maximum 45 MB."
    }
}


def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ])


# ==============================
# START
# ==============================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    language = get_language(user_id)

    if not language:
        await update.effective_message.reply_text(
            "🌍 Welcome! Xush kelibsiz! Добро пожаловать!\n\n"
            "Tilni tanlang / Choose language / Выберите язык:",
            reply_markup=language_keyboard()
        )
        return

    await update.effective_message.reply_text(
        TEXTS[language]["welcome"]
    )


# ==============================
# LANGUAGE
# ==============================

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🌐 Tilni tanlang / Choose language / Выберите язык:",
        reply_markup=language_keyboard()
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    language = query.data.replace("lang_", "")

    if language not in TEXTS:
        return

    save_language(query.from_user.id, language)

    await query.edit_message_text(
        TEXTS[language]["selected"] + "\n\n" +
        TEXTS[language]["send"]
    )


# ==============================
# CHECK URL
# ==============================

def valid_url(url):
    try:
        parsed = urlparse(url.strip())

        if parsed.scheme not in ("http", "https"):
            return False

        host = (parsed.hostname or "").lower()

        return any(
            host == domain or host.endswith("." + domain)
            for domain in DOMAINS
        )

    except Exception:
        return False


# ==============================
# DOWNLOAD VIDEO
# ==============================

def download_video(url, folder):

    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "max_filesize": MAX_SIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 60,
        "retries": 3,
        "fragment_retries": 3
    }

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(url, download=True)

        file_path = Path(
            info.get("filepath") or ydl.prepare_filename(info)
        )

        if not file_path.exists():
            files = list(Path(folder).glob(info["id"] + ".*"))

            if not files:
                raise FileNotFoundError("Video file not found")

            file_path = files[0]

        if file_path.stat().st_size > MAX_SIZE:
            raise ValueError("large")

        return file_path, info.get("title", "Video")


# ==============================
# HANDLE LINKS
# ==============================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.effective_message
    user_id = update.effective_user.id

    language = get_language(user_id) or "uz"
    text = message.text or ""

    match = re.search(r"https?://[^\s]+", text)

    if not match:
        await message.reply_text(TEXTS[language]["invalid"])
        return

    url = match.group(0).rstrip(".,!?)']")

    if not valid_url(url):
        await message.reply_text(TEXTS[language]["invalid"])
        return

    status = await message.reply_text(
        TEXTS[language]["loading"]
    )

    try:

        with tempfile.TemporaryDirectory() as folder:

            file_path, title = await asyncio.to_thread(
                download_video,
                url,
                folder
            )

            with open(file_path, "rb") as video:

                await message.reply_video(
                    video=video,
                    caption="🎬 " + title[:900],
                    supports_streaming=True,
                    read_timeout=180,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60
                )

        await status.edit_text(
            TEXTS[language]["done"]
        )

    except ValueError:
        await status.edit_text(
            TEXTS[language]["large"]
        )

    except Exception:
        logger.exception("Video download failed")

        await status.edit_text(
            TEXTS[language]["error"]
        )


# ==============================
# HELP
# ==============================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    language = get_language(update.effective_user.id) or "uz"

    messages = {
        "uz": (
            "📌 Botdan foydalanish:\n"
            "1. Video havolasini yuboring.\n"
            "2. Yuklanishini kuting.\n"
            "3. Videoni qabul qiling.\n\n"
            "/language - tilni o'zgartirish"
        ),

        "ru": (
            "📌 Как пользоваться ботом:\n"
            "1. Отправьте ссылку.\n"
            "2. Дождитесь загрузки.\n"
            "3. Получите видео.\n\n"
            "/language - изменить язык"
        ),

        "en": (
            "📌 How to use:\n"
            "1. Send a video link.\n"
            "2. Wait for download.\n"
            "3. Receive your video.\n\n"
            "/language - change language"
        )
    }

    await update.effective_message.reply_text(
        messages[language]
    )


# ==============================
# ERROR HANDLER
# ==============================

async def error_handler(update, context):
    logger.error(
        "Telegram update error: %s",
        context.error,
        exc_info=context.error
    )


# ==============================
# FLASK
# ==============================

@app.route("/")
def home():
    return "Telegram Downloader Bot is running!", 200


@app.route("/health")
def health():
    return "OK", 200


# ==============================
# RUN BOT
# ==============================

def run_bot():

    init_db()

    request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=60,
        read_timeout=120,
        write_timeout=180,
        pool_timeout=60
    )

    updates_request = HTTPXRequest(
        connection_pool_size=5,
        connect_timeout=60,
        read_timeout=120,
        write_timeout=60,
        pool_timeout=60
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("language", language_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            language_callback,
            pattern=r"^lang_(uz|ru|en)$"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Telegram bot is starting...")

    application.run_polling(
        drop_pending_updates=False,
        timeout=30,
        bootstrap_retries=-1,
        stop_signals=None
    )


# ==============================
# MAIN
# ==============================

def run_flask():

    port = int(os.environ.get("PORT", 10000))

    logger.info("Starting Flask server on port %s", port)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


if __name__ == "__main__":

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing! Add it in Render Environment."
        )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-server"
    )

    flask_thread.start()

    logger.info("Starting Telegram bot...")

    run_bot()
