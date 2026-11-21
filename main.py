
import os
import re
import uuid
import asyncio
import logging
import tempfile

from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ==========================================
# CONFIG
# ==========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

MAX_CONCURRENT = 3
MAX_FILE_SIZE = 48 * 1024 * 1024

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# ==========================================
# LANGUAGES
# ==========================================

TEXTS = {
    "uz": {
        "welcome": "👋 Assalomu alaykum!\n\n🎬 Video yuklovchi bot\n🎵 Musiqa qidirish bot\n\n📥 Video havolasini yuboring yoki qo'shiq nomini yozing.",
        "video": "🎬 Video yuklash",
        "music": "🎵 Musiqa qidirish",
        "language": "🌐 Tilni o'zgartirish",
        "choose": "🌐 Tilni tanlang:",
        "waiting": "⏳ Yuklanmoqda...",
        "searching": "🔎 Musiqa qidirilmoqda...",
        "not_found": "❌ Hech narsa topilmadi.",
        "error": "❌ Xatolik yuz berdi. Qayta urinib ko'ring.",
        "done": "✅ Tayyor!",
        "send_link": "🔗 Video havolasini yuboring.",
        "send_song": "🎵 Qo'shiq nomini yozing:",
        "audio": "🎵 Videodan MP3 olish",
        "unsupported": "❌ Bu havola qo'llab-quvvatlanmaydi.",
        "too_large": "❌ Fayl hajmi juda katta.",
        "choose_song": "🎶 Qo'shiqni tanlang:",
    },

    "ru": {
        "welcome": "👋 Добро пожаловать!\n\n🎬 Бот для скачивания видео\n🎵 Поиск музыки\n\n📥 Отправьте ссылку или название песни.",
        "video": "🎬 Скачать видео",
        "music": "🎵 Найти музыку",
        "language": "🌐 Изменить язык",
        "choose": "🌐 Выберите язык:",
        "waiting": "⏳ Загрузка...",
        "searching": "🔎 Ищу музыку...",
        "not_found": "❌ Ничего не найдено.",
        "error": "❌ Произошла ошибка.",
        "done": "✅ Готово!",
        "send_link": "🔗 Отправьте ссылку на видео.",
        "send_song": "🎵 Напишите название песни:",
        "audio": "🎵 Скачать MP3 из видео",
        "unsupported": "❌ Ссылка не поддерживается.",
        "too_large": "❌ Файл слишком большой.",
        "choose_song": "🎶 Выберите песню:",
    },

    "en": {
        "welcome": "👋 Welcome!\n\n🎬 Video downloader bot\n🎵 Music search bot\n\n📥 Send a video link or song name.",
        "video": "🎬 Download video",
        "music": "🎵 Search music",
        "language": "🌐 Change language",
        "choose": "🌐 Choose your language:",
        "waiting": "⏳ Downloading...",
        "searching": "🔎 Searching music...",
        "not_found": "❌ Nothing found.",
        "error": "❌ An error occurred.",
        "done": "✅ Done!",
        "send_link": "🔗 Send a video link.",
        "send_song": "🎵 Enter a song name:",
        "audio": "🎵 Extract MP3 from video",
        "unsupported": "❌ Unsupported link.",
        "too_large": "❌ File is too large.",
        "choose_song": "🎶 Choose a song:",
    },
}


def lang(context):
    return context.user_data.get("lang", "uz")


def tr(context, key):
    return TEXTS[lang(context)][key]


# ==========================================
# KEYBOARDS
# ==========================================

def main_keyboard(context):
    return ReplyKeyboardMarkup(
        [
            [tr(context, "video"), tr(context, "music")],
            [tr(context, "language")],
        ],
        resize_keyboard=True,
    )


def language_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz"),
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        ],
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ],
    ])


# ==========================================
# URL VALIDATION
# ==========================================

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "pinterest.com",
    "pin.it",
)


def valid_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and any(
                host == domain or host.endswith("." + domain)
                for domain in SUPPORTED_DOMAINS
            )
        )
    except Exception:
        return False


# ==========================================
# YT-DLP SETTINGS
# ==========================================

def base_options(folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    }

    cookies = os.getenv("COOKIES_FILE")

    if cookies and Path(cookies).is_file():
        options["cookiefile"] = cookies

    return options


# ==========================================
# VIDEO DOWNLOAD
# ==========================================

def download_video_sync(url, folder):
    formats = [
        "best[height<=720]/best",
        "best",
        "bestvideo*+bestaudio/best",
        "worst/best",
    ]

    last_error = None

    for selected_format in formats:
        options = base_options(folder)

        options.update({
            "format": selected_format,
            "merge_output_format": "mp4",
        })

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

                if not info:
                    raise RuntimeError("Video not found")

                title = info.get("title", "Video")

            files = [
                p for p in Path(folder).iterdir()
                if p.is_file()
                and not p.name.endswith(
                    (".part", ".ytdl", ".json")
                )
            ]

            if not files:
                raise FileNotFoundError("Video file not found")

            file_path = max(
                files,
                key=lambda p: p.stat().st_size,
            )

            return str(file_path), title

        except Exception as error:
            last_error = error

            logger.warning(
                "Format %s failed: %s",
                selected_format,
                error,
            )

    raise RuntimeError(
        f"All video formats failed: {last_error}"
    )


# ==========================================
# AUDIO DOWNLOAD
# ==========================================

def download_audio_sync(url, folder):
    options = base_options(folder)

    options.update({
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    })

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("Audio not found")

        title = info.get("title", "Music")

    files = list(Path(folder).glob("*.mp3"))

    if not files:
        raise FileNotFoundError(
            "MP3 file not found. Check FFmpeg."
        )

    file_path = max(
        files,
        key=lambda p: p.stat().st_size,
    )

    return str(file_path), title


# ==========================================
# MUSIC SEARCH
# ==========================================

def search_music_sync(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        data = ydl.extract_info(
            "ytsearch10:" + query,
            download=False,
        )

    results = []

    for item in data.get("entries", []) or []:
        if not item:
            continue

        video_url = item.get("webpage_url")

        if not video_url:
            video_url = item.get("url")

        if not video_url or not str(video_url).startswith("http"):
            video_id = item.get("id")

            if video_id:
                video_url = (
                    "https://www.youtube.com/watch?v=" + video_id
                )

        if not video_url:
            continue

        results.append({
            "title": item.get("title", "Unknown"),
            "url": video_url,
        })

    return results[:10]


# ==========================================
# ASYNC DOWNLOAD
# ==========================================

async def process_download(url, audio=False):
    temp = tempfile.TemporaryDirectory(prefix="songbot_")

    try:
        async with semaphore:
            loop = asyncio.get_running_loop()

            if audio:
                result = await loop.run_in_executor(
                    None,
                    download_audio_sync,
                    url,
                    temp.name,
                )
            else:
                result = await loop.run_in_executor(
                    None,
                    download_video_sync,
                    url,
                    temp.name,
                )

        return temp, result

    except Exception:
        temp.cleanup()
        raise


# ==========================================
# START
# ==========================================

async def start(update, context):
    await update.message.reply_text(
        tr(context, "welcome"),
        reply_markup=main_keyboard(context),
    )


# ==========================================
# LANGUAGE
# ==========================================

async def language(update, context):
    await update.message.reply_text(
        tr(context, "choose"),
        reply_markup=language_keyboard(),
    )


async def language_callback(update, context):
    query = update.callback_query
    await query.answer()

    selected = query.data.replace("lang_", "")

    if selected not in TEXTS:
        return

    context.user_data["lang"] = selected

    await query.edit_message_text(
        TEXTS[selected]["welcome"]
    )

    await query.message.reply_text(
        TEXTS[selected]["done"],
        reply_markup=main_keyboard(context),
    )


# ==========================================
# VIDEO HANDLER
# ==========================================

async def handle_video(url, update, context):
    waiting = await update.message.reply_text(
        tr(context, "waiting")
    )

    temp = None

    try:
        temp, result = await process_download(url, audio=False)

        file_path, title = result

        size = os.path.getsize(file_path)

        if size > MAX_FILE_SIZE:
            await waiting.edit_text(tr(context, "too_large"))
            return

        with open(file_path, "rb") as video:
            await update.message.reply_video(
                video=video,
                caption="🎬 " + title[:900],
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )

        key = uuid.uuid4().hex[:12]

        context.user_data.setdefault("jobs", {})[key] = url

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    tr(context, "audio"),
                    callback_data="aud_" + key,
                )
            ]
        ])

        await update.message.reply_text(
            tr(context, "done"),
            reply_markup=keyboard,
        )

    except Exception:
        logger.exception("Video download error")

        await update.message.reply_text(tr(context, "error"))

    finally:
        if temp:
            temp.cleanup()

        try:
            await waiting.delete()
        except Exception:
            pass


# ==========================================
# VIDEO TO MP3
# ==========================================

async def audio_callback(update, context):
    query = update.callback_query
    await query.answer()

    key = query.data.replace("aud_", "")

    url = context.user_data.get("jobs", {}).pop(key, None)

    if not url:
        await query.message.reply_text(tr(context, "error"))
        return

    waiting = await query.message.reply_text(
        tr(context, "waiting")
    )

    temp = None

    try:
        temp, result = await process_download(url, audio=True)

        file_path, title = result

        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            await query.message.reply_text(tr(context, "too_large"))
            return

        with open(file_path, "rb") as audio:
            await query.message.reply_audio(
                audio=audio,
                title=title[:64],
                filename="music.mp3",
                read_timeout=120,
                write_timeout=120,
            )

    except Exception:
        logger.exception("Audio extraction error")
        await query.message.reply_text(tr(context, "error"))

    finally:
        if temp:
            temp.cleanup()

        try:
            await waiting.delete()
        except Exception:
            pass


# ==========================================
# MUSIC SEARCH
# ==========================================

async def handle_search(query_text, update, context):
    waiting = await update.message.reply_text(
        tr(context, "searching")
    )

    try:
        loop = asyncio.get_running_loop()

        async with semaphore:
            results = await loop.run_in_executor(
                None,
                search_music_sync,
                query_text,
            )

        if not results:
            await waiting.edit_text(tr(context, "not_found"))
            return

        buttons = []
        jobs = {}

        for index, item in enumerate(results, 1):
            key = uuid.uuid4().hex[:10]

            jobs[key] = item["url"]

            buttons.append([
                InlineKeyboardButton(
                    f"🎵 {index}. {item['title'][:45]}",
                    callback_data="song_" + key,
                )
            ])

        context.user_data["song_jobs"] = jobs

        await waiting.edit_text(
            tr(context, "choose_song"),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception:
        logger.exception("Music search error")
        await waiting.edit_text(tr(context, "error"))


# ==========================================
# SONG DOWNLOAD
# ==========================================

async def song_callback(update, context):
    query = update.callback_query
    await query.answer()

    key = query.data.replace("song_", "")

    url = context.user_data.get("song_jobs", {}).pop(key, None)

    if not url:
        await query.message.reply_text(tr(context, "error"))
        return

    waiting = await query.message.reply_text(
        tr(context, "waiting")
    )

    temp = None

    try:
        temp, result = await process_download(url, audio=True)

        file_path, title = result

        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            await query.message.reply_text(tr(context, "too_large"))
            return

        with open(file_path, "rb") as audio:
            await query.message.reply_audio(
                audio=audio,
                title=title[:64],
                filename="song.mp3",
                read_timeout=120,
                write_timeout=120,
            )

    except Exception:
        logger.exception("Song download error")
        await query.message.reply_text(tr(context, "error"))

    finally:
        if temp:
            temp.cleanup()

        try:
            await waiting.delete()
        except Exception:
            pass


# ==========================================
# MESSAGE HANDLER
# ==========================================

async def message_handler(update, context):
    if not update.message or not update.message.text:
        return

    message = update.message.text.strip()

    if message == tr(context, "video"):
        await update.message.reply_text(tr(context, "send_link"))
        return

    if message == tr(context, "music"):
        await update.message.reply_text(tr(context, "send_song"))
        return

    if message == tr(context, "language"):
        await language(update, context)
        return

    match = re.search(r"https?://\S+", message)

    if match:
        url = match.group(0).rstrip(".,!?)]}")

        if not valid_url(url):
            await update.message.reply_text(tr(context, "unsupported"))
            return

        await handle_video(url, update, context)
        return

    if len(message) < 2:
        await update.message.reply_text(tr(context, "send_song"))
        return

    if len(message) > 150:
        await update.message.reply_text(tr(context, "error"))
        return

    await handle_search(message, update, context)


# ==========================================
# ERROR HANDLER
# ==========================================

async def error_handler(update, context):
    logger.error(
        "Unexpected error: %s",
        context.error,
        exc_info=context.error,
    )


# ==========================================
# MAIN
# ==========================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing!"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(8)
        .connection_pool_size(16)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("language", language)
    )

    application.add_handler(
        CallbackQueryHandler(
            language_callback,
            pattern=r"^lang_",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            audio_callback,
            pattern=r"^aud_",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            song_callback,
            pattern=r"^song_",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("BOT STARTED SUCCESSFULLY!")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
