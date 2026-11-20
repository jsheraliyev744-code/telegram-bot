
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
import imageio_ffmpeg

from flask import Flask

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.request import HTTPXRequest

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ==================================================
# SETTINGS
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_SIZE = 45 * 1024 * 1024
DB_FILE = "users.db"

SEARCH_LIMIT = 15
PAGE_SIZE = 10
MAX_DOWNLOADS = 3

PORT = int(os.getenv("PORT", "10000"))

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

web_app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

DOMAINS = {
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "pinterest.com",
    "pin.it",
}

# ==================================================
# DATABASE
# ==================================================

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


# ==================================================
# LANGUAGES
# ==================================================

TEXTS = {
    "uz": {
        "welcome": (
            "👋 Assalomu alaykum!\n\n"
            "🎵 Qo'shiq nomini yozing.\n"
            "🎬 Yoki video havolasini yuboring.\n\n"
            "YouTube, Instagram, TikTok va Pinterest qo'llab-quvvatlanadi."
        ),
        "searching": "⏳ Qo'shiq qidirilmoqda...",
        "downloading": "⏳ Yuklanmoqda, kuting...",
        "results": "🎵 Qidiruv natijalari:\n\n",
        "not_found": "⚠️ Hech narsa topilmadi.",
        "invalid": "❌ Bu havola qo'llab-quvvatlanmaydi.",
        "video_error": "⚠️ Videoni yuklab bo'lmadi. Boshqa havolani sinab ko'ring.",
        "song_error": "⚠️ Qo'shiqni yuklab bo'lmadi.",
        "large": "⚠️ Fayl 45 MB dan katta.",
        "cancelled": "❌ Qidiruv bekor qilindi.",
        "choose": "🌍 Tilni tanlang:",
        "selected": "✅ O'zbek tili tanlandi!",
        "help": (
            "📌 Botdan foydalanish:\n\n"
            "1. Qo'shiq nomini yozing.\n"
            "2. Kerakli raqamni bosing.\n"
            "3. MP3 faylni oling.\n\n"
            "Yoki video havolasini yuboring.\n\n"
            "/start - boshlash\n"
            "/language - til\n"
            "/help - yordam"
        ),
    },

    "ru": {
        "welcome": (
            "👋 Здравствуйте!\n\n"
            "🎵 Напишите название песни.\n"
            "🎬 Или отправьте ссылку на видео."
        ),
        "searching": "⏳ Ищу песню...",
        "downloading": "⏳ Загружаю, подождите...",
        "results": "🎵 Результаты поиска:\n\n",
        "not_found": "⚠️ Ничего не найдено.",
        "invalid": "❌ Эта ссылка не поддерживается.",
        "video_error": "⚠️ Не удалось скачать видео.",
        "song_error": "⚠️ Не удалось скачать песню.",
        "large": "⚠️ Файл больше 45 МБ.",
        "cancelled": "❌ Поиск отменён.",
        "choose": "🌍 Выберите язык:",
        "selected": "✅ Русский язык выбран!",
        "help": (
            "Отправьте ссылку или название песни.\n"
            "/start - начало\n"
            "/language - язык\n"
            "/help - помощь"
        ),
    },

    "en": {
        "welcome": (
            "👋 Hello!\n\n"
            "🎵 Type a song name.\n"
            "🎬 Or send a video link."
        ),
        "searching": "⏳ Searching for a song...",
        "downloading": "⏳ Downloading, please wait...",
        "results": "🎵 Search results:\n\n",
        "not_found": "⚠️ Nothing found.",
        "invalid": "❌ Unsupported link.",
        "video_error": "⚠️ Video download failed.",
        "song_error": "⚠️ Song download failed.",
        "large": "⚠️ File is larger than 45 MB.",
        "cancelled": "❌ Search cancelled.",
        "choose": "🌍 Choose language:",
        "selected": "✅ English selected!",
        "help": (
            "Send a video link or song name.\n"
            "/start - start\n"
            "/language - language\n"
            "/help - help"
        ),
    },
}


# ==================================================
# KEYBOARDS
# ==================================================

def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ])


def results_keyboard(results, page):
    start = page * PAGE_SIZE
    current = results[start:start + PAGE_SIZE]
    rows = []

    for row_start in range(0, len(current), 5):
        row = []

        for i in range(row_start, min(row_start + 5, len(current))):
            index = start + i

            row.append(
                InlineKeyboardButton(
                    str(i + 1),
                    callback_data=f"song_pick_{index}"
                )
            )

        rows.append(row)

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton("⬅️", callback_data="song_prev")
        )

    navigation.append(
        InlineKeyboardButton("❌", callback_data="song_cancel")
    )

    if start + PAGE_SIZE < len(results):
        navigation.append(
            InlineKeyboardButton("➡️", callback_data="song_next")
        )

    rows.append(navigation)

    return InlineKeyboardMarkup(rows)


# ==================================================
# URL HELPERS
# ==================================================

def extract_url(text):
    match = re.search(r"https?://[^\s<>]+", text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)']\"}")


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


# ==================================================
# DOWNLOAD HELPERS
# ==================================================

def find_file(folder, video_id):
    files = [
        f for f in Path(folder).glob(video_id + ".*")
        if f.is_file()
        and not f.name.endswith((".part", ".ytdl"))
    ]

    if not files:
        raise FileNotFoundError("Downloaded file not found")

    return max(files, key=lambda f: f.stat().st_size)


def download_video(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_SIZE,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 256,
        "http_chunk_size": 10 * 1024 * 1024,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        file_path = find_file(
            folder,
            str(info["id"])
        )

    if file_path.stat().st_size > MAX_SIZE:
        raise ValueError("large")

    return file_path, info.get("title", "Video")


# ==================================================
# SONG SEARCH
# ==================================================

def search_songs(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 15,
        "retries": 2,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            f"ytsearch{SEARCH_LIMIT}:{query}",
            download=False
        )

    results = []

    for item in (info or {}).get("entries", []) or []:
        if not item or not item.get("id"):
            continue

        video_id = str(item["id"])

        results.append({
            "id": video_id,
            "title": str(item.get("title") or "Unknown")[:180],
            "url": (
                item.get("webpage_url")
                or f"https://www.youtube.com/watch?v={video_id}"
            ),
            "duration": item.get("duration"),
        })

    return results


# ==================================================
# SONG DOWNLOAD
# ==================================================

def download_song(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s",
        ),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_SIZE,
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 256,
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        video_id = str(info["id"])
        expected = Path(folder) / (video_id + ".mp3")

        if expected.exists():
            file_path = expected
        else:
            file_path = find_file(folder, video_id)

    if file_path.stat().st_size > MAX_SIZE:
        raise ValueError("large")

    return (
        file_path,
        info.get("title", "Audio"),
        info.get("uploader", "")
    )


# ==================================================
# TEXT HELPERS
# ==================================================

def duration_text(seconds):
    if not seconds:
        return ""

    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def make_results_text(results, page, language):
    start = page * PAGE_SIZE
    current = results[start:start + PAGE_SIZE]

    lines = [TEXTS[language]["results"]]

    for index, item in enumerate(current, 1):
        duration = duration_text(item.get("duration"))
        suffix = f" {duration}" if duration else ""
        title = item["title"].replace("\n", " ")

        lines.append(f"{index}. {title}{suffix}")

    return "\n".join(lines)


# ==================================================
# COMMANDS
# ==================================================

async def start(update, context):
    user_id = update.effective_user.id
    language = get_language(user_id)

    if not language:
        await update.effective_message.reply_text(
            "🌍 Tilni tanlang / Choose language:",
            reply_markup=language_keyboard()
        )
        return

    await update.effective_message.reply_text(
        TEXTS[language]["welcome"]
    )


async def language_command(update, context):
    language = get_language(update.effective_user.id) or "uz"

    await update.effective_message.reply_text(
        TEXTS[language]["choose"],
        reply_markup=language_keyboard()
    )


async def help_command(update, context):
    language = get_language(update.effective_user.id) or "uz"

    await update.effective_message.reply_text(
        TEXTS[language]["help"]
    )


async def language_callback(update, context):
    query = update.callback_query
    await query.answer()

    language = query.data.replace("lang_", "", 1)

    if language not in TEXTS:
        return

    save_language(query.from_user.id, language)

    await query.edit_message_text(
        TEXTS[language]["selected"]
        + "\n\n"
        + TEXTS[language]["welcome"]
    )


# ==================================================
# STATUS HELPER
# ==================================================

async def delete_status(status):
    try:
        await status.delete()
    except TelegramError:
        pass


# ==================================================
# MAIN MESSAGE HANDLER
# ==================================================

async def handle_message(update, context):
    message = update.effective_message
    user_id = update.effective_user.id

    language = get_language(user_id) or "uz"
    message_text = (message.text or "").strip()

    url = extract_url(message_text)
    semaphore = context.application.bot_data["semaphore"]

    # VIDEO DOWNLOAD

    if url:
        if not valid_url(url):
            await message.reply_text(
                TEXTS[language]["invalid"]
            )
            return

        status = await message.reply_text(
            TEXTS[language]["downloading"]
        )

        try:
            async with semaphore:
                with tempfile.TemporaryDirectory() as folder:
                    file_path, title = await asyncio.to_thread(
                        download_video,
                        url,
                        folder
                    )

                    with open(file_path, "rb") as media:
                        if file_path.suffix.lower() == ".mp4":
                            await message.reply_video(
                                video=media,
                                caption=("🎬 " + title)[:900],
                                supports_streaming=True,
                                read_timeout=180,
                                write_timeout=180,
                                connect_timeout=30,
                                pool_timeout=60,
                            )
                        else:
                            await message.reply_document(
                                document=media,
                                filename=file_path.name,
                                caption=("🎬 " + title)[:900],
                                read_timeout=180,
                                write_timeout=180,
                                connect_timeout=30,
                                pool_timeout=60,
                            )

            await delete_status(status)

        except ValueError:
            await status.edit_text(
                TEXTS[language]["large"]
            )

        except Exception:
            logger.exception("Video download failed")

            await status.edit_text(
                TEXTS[language]["video_error"]
            )

        return

    # SONG SEARCH

    if len(message_text) < 2:
        await message.reply_text(
            TEXTS[language]["not_found"]
        )
        return

    status = await message.reply_text(
        TEXTS[language]["searching"]
    )

    try:
        results = await asyncio.to_thread(
            search_songs,
            message_text
        )

        if not results:
            await delete_status(status)

            await message.reply_text(
                TEXTS[language]["not_found"]
            )
            return

        context.user_data["song_results"] = results
        context.user_data["song_page"] = 0

        await delete_status(status)

        await message.reply_text(
            make_results_text(results, 0, language),
            reply_markup=results_keyboard(results, 0),
            disable_web_page_preview=True,
        )

    except Exception:
        logger.exception("Song search failed")

        await delete_status(status)

        await message.reply_text(
            TEXTS[language]["not_found"]
        )


# ==================================================
# SONG BUTTON HANDLER
# ==================================================

async def song_callback(update, context):
    query = update.callback_query

    language = get_language(query.from_user.id) or "uz"
    data = query.data

    results = context.user_data.get("song_results", [])

    if data == "song_cancel":
        await query.answer()

        context.user_data.pop("song_results", None)
        context.user_data.pop("song_page", None)

        await query.edit_message_text(
            TEXTS[language]["cancelled"]
        )
        return

    if not results:
        await query.answer(
            "Qidiruv eskirgan. Qayta qidiring.",
            show_alert=True
        )
        return

    page = int(context.user_data.get("song_page", 0))

    if data == "song_prev":
        await query.answer()

        page = max(0, page - 1)
        context.user_data["song_page"] = page

        await query.edit_message_text(
            make_results_text(results, page, language),
            reply_markup=results_keyboard(results, page),
            disable_web_page_preview=True,
        )
        return

    if data == "song_next":
        await query.answer()

        max_page = (len(results) - 1) // PAGE_SIZE
        page = min(max_page, page + 1)

        context.user_data["song_page"] = page

        await query.edit_message_text(
            make_results_text(results, page, language),
            reply_markup=results_keyboard(results, page),
            disable_web_page_preview=True,
        )
        return

    match = re.fullmatch(r"song_pick_(\d+)", data)

    if not match:
        await query.answer()
        return

    index = int(match.group(1))

    if index < 0 or index >= len(results):
        await query.answer(
            "Natija topilmadi.",
            show_alert=True
        )
        return

    await query.answer()

    selected = results[index]

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except TelegramError:
        pass

    status = await query.message.reply_text(
        TEXTS[language]["downloading"]
    )

    semaphore = context.application.bot_data["semaphore"]

    try:
        async with semaphore:
            with tempfile.TemporaryDirectory() as folder:
                file_path, title, performer = await asyncio.to_thread(
                    download_song,
                    selected["url"],
                    folder
                )

                with open(file_path, "rb") as audio:
                    await query.message.reply_audio(
                        audio=audio,
                        title=title[:64],
                        performer=performer[:64] if performer else None,
                        filename="song.mp3",
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=60,
                    )

        await delete_status(status)

    except ValueError:
        await status.edit_text(
            TEXTS[language]["large"]
        )

    except Exception:
        logger.exception("Song download failed")

        await status.edit_text(
            TEXTS[language]["song_error"]
        )


# ==================================================
# RENDER WEB SERVER
# ==================================================

@web_
