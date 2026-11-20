
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_SIZE = 45 * 1024 * 1024  # Telegram bot API upload limit safety margin
DB_FILE = "users.db"
SEARCH_LIMIT = 20
PAGE_SIZE = 10
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("telegram_downloader")

DOMAINS = {
    "youtube.com", "youtu.be", "m.youtube.com",
    "instagram.com",
    "tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "pinterest.com", "pin.it",
}

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# =========================================================
# DATABASE / LANGUAGES
# =========================================================

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
            "SELECT language FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row[0] if row else None


def save_language(user_id, language):
    with sqlite3.connect(DB_FILE) as db:
        db.execute("""
            INSERT INTO users(user_id, language)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET language = excluded.language
        """, (user_id, language))
        db.commit()


TEXTS = {
    "uz": {
        "welcome": (
            "👋 Assalomu alaykum!\n\n"
            "🎬 Video havolasini yuboring yoki qo'shiq nomini yozing.\n\n"
            "📥 YouTube, Instagram, TikTok va Pinterest havolalari "
            "qo'llab-quvvatlanadi.\n"
            "🎵 Qo'shiq nomini yozsangiz, qidiruv natijalari chiqadi."
        ),
        "choose": "🌍 Tilni tanlang:",
        "selected": "✅ O'zbek tili tanlandi!",
        "searching": "⏳ Qidirilmoqda...",
        "downloading": "⏳ Yuklanmoqda, kuting...",
        "results": "🎵 Qidiruv natijalari:\n\n",
        "not_found": "⚠️ Hech narsa topilmadi. Boshqa nom yozib ko'ring.",
        "invalid": "❌ Bu havola qo'llab-quvvatlanmaydi.",
        "video_error": "⚠️ Videoni yuklab bo'lmadi. Boshqa havolani sinab ko'ring.",
        "song_error": "⚠️ Qo'shiqni yuklab bo'lmadi. Boshqasini tanlang.",
        "large": "⚠️ Fayl 45 MB dan katta.",
        "cancelled": "❌ Qidiruv yopildi.",
        "help": (
            "📌 Foydalanish:\n"
            "• Video havolasini yuboring.\n"
            "• Yoki qo'shiq nomini yozing.\n"
            "• Natijadan raqamni bosing.\n\n"
            "/start — boshlash\n"
            "/language — tilni o'zgartirish\n"
            "/help — yordam"
        ),
    },
    "ru": {
        "welcome": (
            "👋 Здравствуйте!\n\n"
            "🎬 Отправьте ссылку на видео или напишите название песни.\n"
            "Поддерживаются YouTube, Instagram, TikTok и Pinterest."
        ),
        "choose": "🌍 Выберите язык:",
        "selected": "✅ Выбран русский язык!",
        "searching": "⏳ Ищу...",
        "downloading": "⏳ Загружаю, подождите...",
        "results": "🎵 Результаты поиска:\n\n",
        "not_found": "⚠️ Ничего не найдено. Попробуйте другое название.",
        "invalid": "❌ Эта ссылка не поддерживается.",
        "video_error": "⚠️ Не удалось скачать видео. Попробуйте другую ссылку.",
        "song_error": "⚠️ Не удалось скачать песню. Выберите другую.",
        "large": "⚠️ Файл больше 45 МБ.",
        "cancelled": "❌ Поиск закрыт.",
        "help": "Отправьте ссылку на видео или название песни.\n/start — начать\n/language — язык\n/help — помощь",
    },
    "en": {
        "welcome": (
            "👋 Hello!\n\n"
            "🎬 Send a video link or type a song name.\n"
            "YouTube, Instagram, TikTok and Pinterest links are supported."
        ),
        "choose": "🌍 Choose a language:",
        "selected": "✅ English selected!",
        "searching": "⏳ Searching...",
        "downloading": "⏳ Downloading, please wait...",
        "results": "🎵 Search results:\n\n",
        "not_found": "⚠️ Nothing found. Try another title.",
        "invalid": "❌ This link is not supported.",
        "video_error": "⚠️ Could not download the video. Try another link.",
        "song_error": "⚠️ Could not download the song. Choose another one.",
        "large": "⚠️ File is larger than 45 MB.",
        "cancelled": "❌ Search closed.",
        "help": "Send a video link or type a song name.\n/start — start\n/language — language\n/help — help",
    },
}


def language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ])


# =========================================================
# URL HELPERS
# =========================================================

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
        return any(host == domain or host.endswith("." + domain) for domain in DOMAINS)
    except Exception:
        return False


# =========================================================
# YT-DLP HELPERS (RUN IN WORKER THREADS)
# =========================================================

def find_downloaded_file(folder, video_id, preferred_path=None):
    if preferred_path:
        candidate = Path(preferred_path)
        if candidate.exists():
            return candidate

    matches = list(Path(folder).glob(f"{video_id}.*"))
    matches = [p for p in matches if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if matches:
        return max(matches, key=lambda p: p.stat().st_size)
    raise FileNotFoundError("Downloaded file was not found")


def download_video(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "concurrent_fragment_downloads": 4,
        "max_filesize": MAX_SIZE,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = find_downloaded_file(
            folder,
            str(info.get("id", "")),
            info.get("filepath") or ydl.prepare_filename(info),
        )

    if file_path.stat().st_size > MAX_SIZE:
        raise ValueError("large")

    return file_path, info.get("title") or "Video"


def search_songs(query):
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch",
        "socket_timeout": 15,
        "retries": 1,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{query}", download=False)

    results = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        video_id = entry.get("id")
        title = entry.get("title") or "Nomsiz qo'shiq"
        url = entry.get("webpage_url") or entry.get("url")

        if not url or not str(url).startswith("http"):
            if video_id:
                url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                continue

        results.append({
            "id": str(video_id or ""),
            "title": str(title)[:180],
            "url": str(url),
            "duration": entry.get("duration"),
        })

    return results


def download_song(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "%(id)s.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "max_filesize": MAX_SIZE,
        "ffmpeg_location": FFMPEG_PATH,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = str(info.get("id", ""))
        expected = Path(folder) / f"{video_id}.mp3"
        if expected.exists():
            file_path = expected
        else:
            file_path = find_downloaded_file(folder, video_id)

    if file_path.stat().st_size > MAX_SIZE:
        raise ValueError("large")

    return file_path, info.get("title") or "Audio", info.get("uploader") or ""


# =========================================================
# SEARCH RESULTS KEYBOARD
# =========================================================

def duration_text(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def results_keyboard(results, page):
    start = page * PAGE_SIZE
    current = results[start:start + PAGE_SIZE]
    rows = []

    # 5 buttons per row, numbered 1-10 on each page
    for row_start in range(0, len(current), 5):
        row = []
        for local_index in range(row_start, min(row_start + 5, len(current))):
            global_index = start + local_index
            row.append(InlineKeyboardButton(
                str(local_index + 1),
                callback_data=f"song_pick_{global_index}",
            ))
        rows.append(row)

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("⬅️", callback_data="song_page_prev"))
    navigation.append(InlineKeyboardButton("❌", callback_data="song_cancel"))
    if start + PAGE_SIZE < len(results):
        navigation.append(InlineKeyboardButton("➡️", callback_data="song_page_next"))
    rows.append(navigation)

    return InlineKeyboardMarkup(rows)


def make_results_text(results, page, language):
    start = page * PAGE_SIZE
    current = results[start:start + PAGE_SIZE]
    lines = [TEXTS[language]["results"]]

    for index, item in enumerate(current, start=1):
        duration = duration_text(item.get("duration"))
        suffix = f" — {duration}" if duration else ""
        title = item["title"].replace("\n", " ")
        lines.append(f"{index}. {title}{suffix}")

    return "\n".join(lines)


# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    language = get_language(user_id)

    if not language:
        await update.effective_message.reply_text(
            "🌍 Welcome! Xush kelibsiz! Добро пожаловать!\n\n"
            "Tilni tanlang / Choose language / Выберите язык:",
            reply_markup=language_keyboard(),
        )
        return

    await update.effective_message.reply_text(TEXTS[language]["welcome"])


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🌐 " + TEXTS[get_language(update.effective_user.id) or "uz"]["choose"],
        reply_markup=language_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    language = get_language(update.effective_user.id) or "uz"
    await update.effective_message.reply_text(TEXTS[language]["help"])


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    language = query.data.replace("lang_", "")
    if language not in TEXTS:
        return

    save_language(query.from_user.id, language)
    await query.edit_message_text(
        TEXTS[language]["selected"] + "\n\n" + TEXTS[language]["welcome"]
    )


# =========================================================
# MAIN MESSAGE HANDLER
# =========================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id
    language = get_language(user_id) or "uz"
    text = (message.text or "").strip()

    url = extract_url(text)

    # If the user sent a URL, handle it as a video download.
    if url:
        if not valid_url(url):
            await message.reply_text(TEXTS[language]["invalid"])
            return

        status = await message.reply_text(TEXTS[language]["downloading"])

        try:
            async with DOWNLOAD_SEMAPHORE:
                with tempfile.TemporaryDirectory() as folder:
                    file_path, title = await asyncio.to_thread(download_video, url, folder)

                    with open(file_path, "rb") as media:
                        if file_path.suffix.lower() == ".mp4":
                            await message.reply_video(
                                video=media,
                                caption=("🎬 " + title)[:900],
                                supports_streaming=True,
                                read_timeout=180,
                                write_timeout=180,
                                connect_timeout=30,
                                pool_timeout=30,
                            )
                        else:
                            await message.reply_document(
                                document=media,
                                filename=file_path.name,
                                caption=("🎬 " + title)[:900],
                                read_timeout=180,
                                write_timeout=180,
                                connect_timeout=30,
                                pool_timeout=30,
                            )

            await status.delete()

        except ValueError:
            await status.edit_text(TEXTS[language]["large"])
        except Exception:
            logger.exception("Video download failed")
            try:
                await status.edit_text(TEXTS[language]["video_error"])
            except TelegramError:
                await message.reply_text(TEXTS[language]["video_error"])
        return

    # No URL: treat the text as a song search query.
    if len(text) < 2:
        await message.reply_text(TEXTS[language]["not_found"])
        return

    status = await message.reply_text(TEXTS[language]["searching"])

    try:
        results = await asyncio.to_thread(search_songs, text)

        if not results:
            await status.delete()
            await message.reply_text(TEXTS[language]["not_found"])
            return

        context.user_data["song_results"] = results
        context.user_data["song_page"] = 0

        # Remove the ⏳ searching message before showing the results.
        await status.delete()

        await message.reply_text(
            make_results_text(results, 0, language),
            reply_markup=results_keyboard(results, 0),
            disable_web_page_preview=True,
        )

    except Exception:
        logger.exception("Song search failed")
        try:
            await status.delete()
        except TelegramError:
            pass
        await message.reply_text(TEXTS[language]["not_found"])


# =========================================================
# SONG BUTTON CALLBACKS
# =========================================================

async def song_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    language = get_language(query.from_user.id) or "uz"
    data = query.data or ""

    if data == "song_cancel":
        await query.answer()
        context.user_data.pop("song_results", None)
        context.user_data.pop("song_page", None)
        try:
            await query.edit_message_text(TEXTS[language]["cancelled"])
        except TelegramError:
            pass
        return

    results = context.user_data.get("song_results", [])
    if not results:
        await query.answer("Qidiruv eskirgan. Qo'shiq nomini qayta yuboring.", show_alert=True)
        return

    if data == "song_page_prev":
        await query.answer()
        page = max(0, int(context.user_data.get("song_page", 0)) - 1)
        context.user_data["song_page"] = page
        await query.edit_message_text(
            make_results_text(results, page, language),
            reply_markup=results_keyboard(results, page),
            disable_web_page_preview=True,
        )
        return

    if data == "song_page_next":
        await query.answer()
        max_page = max(0, (len(results) - 1) // PAGE_SIZE)
        page = min(max_page, int(context.user_data.get("song_page", 0)) + 1)
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
        await query.answer("Natija topilmadi.", show_alert=True)
        return

    await query.answer()
    selected = results[index]

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    status = await query.message.reply_text(TEXTS[language]["downloading"])

    try:
        async with DOWNLOAD_SEMAPHORE:
            with tempfile.TemporaryDirectory() as folder:
                file_path, title, performer = await asyncio.to_thread(
                    download_song, selected["url"], folder
                )

                with open(file_path, "rb") as audio:
                    await query.message.reply_audio(
                        audio=audio,
                        title=title[:64],
                        performer=performer[:64] if performer else None,
                        filename=(title[:80] + ".mp3"),
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

        await status.delete()

    except ValueError:
        await status.edit_text(TEXTS[language]["large"])
    except Exception:
        logger.exception("Song download failed")
        try:
            await status.edit_text(TEXTS[language]["song_error"])
        except TelegramError:
            await query.message.reply_text(TEXTS[language]["song_error"])


# =========================================================
# ERROR HAN
