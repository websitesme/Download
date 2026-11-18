"""
Suno Music Downloader Telegram Bot
----------------------------------
A self-contained, asynchronous Telegram bot built with `python-telegram-bot` (v20+),
`httpx`, and `beautifulsoup4`.

How it works:
1. Listens for messages containing a Suno song URL (e.g., https://suno.com/song/...).
2. Fetches the public webpage HTML and extracts standard OpenGraph metadata
   (`og:audio`, `og:title`, `og:image`).
3. Validates the audio URL and downloads the MP3 file into memory (with size limits).
4. Sends the audio file directly back to the Telegram chat with title and thumbnail.

Requirements:
    pip install python-telegram-bot>=20.0 httpx beautifulsoup4
"""

import io
import logging
import os
import re
from typing import Optional, Dict
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Load environment variables from a .env file if present
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------

# Replace with your actual Telegram Bot Token from @BotFather, or set the
# TELEGRAM_BOT_TOKEN environment variable in your .env file.
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Telegram Bot API standard upload limit is 50 MB for bots.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "48"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Regex pattern to detect Suno song or share links
SUNO_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?(?:suno\.com|suno\.ai)/(?:song|s)/[a-zA-Z0-9_-]+)"
)

# Configure structured logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metadata Extraction & Download Logic
# ---------------------------------------------------------------------------


def sanitize_filename(name: str) -> str:
    """Removes characters that are invalid in filenames."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned[:100] if cleaned else "suno_track"


async def fetch_suno_metadata(url: str) -> Optional[Dict[str, str]]:
    """
    Fetches a public Suno song page and extracts OpenGraph audio, title,
    and cover image URLs.

    Args:
        url: The public Suno song URL.

    Returns:
        A dictionary containing 'title', 'audio_url', and 'image_url',
        or None if metadata could not be extracted.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract OpenGraph tags
            og_audio = soup.find("meta", property="og:audio")
            og_title = soup.find("meta", property="og:title")
            og_image = soup.find("meta", property="og:image")

            audio_url = og_audio["content"].strip() if og_audio and og_audio.get("content") else None

            # Fallback: if og:audio is missing, check if the final redirected URL
            # contains a standard UUID and construct the public CDN audio link.
            if not audio_url:
                final_url = str(response.url)
                uuid_match = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    final_url,
                    re.IGNORECASE,
                )
                if uuid_match:
                    song_id = uuid_match.group(1)
                    audio_url = f"https://cdn1.suno.ai/{song_id}.mp3"

            if not audio_url:
                logger.warning("No audio URL found for %s", url)
                return None

            raw_title = (
                og_title["content"].strip()
                if og_title and og_title.get("content")
                else "Suno AI Track"
            )
            # Clean up trailing " | Suno" from page title if present
            clean_title = re.sub(r"\s*\|\s*Suno\s*$", "", raw_title, flags=re.IGNORECASE)

            image_url = (
                og_image["content"].strip()
                if og_image and og_image.get("content")
                else None
            )

            return {
                "title": clean_title,
                "audio_url": audio_url,
                "image_url": image_url,
            }

    except httpx.HTTPStatusError as exc:
        logger.error("HTTP error while fetching Suno page (%s): %s", url, exc)
    except httpx.RequestError as exc:
        logger.error("Network error while fetching Suno page (%s): %s", url, exc)
    except Exception as exc:
        logger.exception("Unexpected error parsing Suno page (%s): %s", url, exc)

    return None


async def download_binary(url: str) -> Optional[io.BytesIO]:
    """
    Downloads a binary file (audio or thumbnail) into an in-memory BytesIO buffer,
    enforcing a maximum size limit.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_FILE_SIZE_BYTES:
                    logger.warning("File exceeds size limit: %s bytes", content_length)
                    return None

                buffer = io.BytesIO()
                downloaded = 0

                async for chunk in response.aiter_bytes(chunk_size=65536):
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE_BYTES:
                        logger.warning("Stream exceeded size limit during download.")
                        return None
                    buffer.write(chunk)

                buffer.seek(0)
                return buffer

    except Exception as exc:
        logger.error("Failed to download binary from %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    welcome_text = (
        "👋 *Welcome to the Suno Music Downloader Bot!*\n\n"
        "Send me any public Suno song link (for example:\n"
        "`https://suno.com/song/...` or `https://suno.com/s/...`)\n"
        "and I will fetch the audio track for you."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    help_text = (
        "ℹ️ *How to use this bot:*\n"
        "1. Open a public song on Suno.\n"
        "2. Copy its share link.\n"
        "3. Paste the link directly into this chat.\n\n"
        "Note: Only publicly accessible songs can be downloaded."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scans incoming text messages for Suno URLs and processes them."""
    if not update.message or not update.message.text:
        return

    match = SUNO_URL_PATTERN.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "Please send a valid Suno song URL (e.g., `https://suno.com/song/...`).",
            parse_mode="Markdown",
        )
        return

    suno_url = match.group(1)
    status_msg = await update.message.reply_text("🔍 Fetching track details from Suno...")

    # Show "uploading audio" status in Telegram chat header
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE
    )

    metadata = await fetch_suno_metadata(suno_url)
    if not metadata:
        await status_msg.edit_text(
            "❌ Could not retrieve audio from that link. "
            "Please verify the song is public and the URL is valid."
        )
        return

    title = metadata["title"]
    audio_url = metadata["audio_url"]
    image_url = metadata.get("image_url")

    await status_msg.edit_text(f"⬇️ Downloading *{title}*...", parse_mode="Markdown")

    audio_buffer = await download_binary(audio_url)
    if not audio_buffer:
        await status_msg.edit_text(
            "❌ Failed to download the audio file (it may be unavailable or exceed Telegram's 50 MB limit)."
        )
        return

    # Optionally download cover art thumbnail
    thumb_buffer = None
    if image_url:
        thumb_buffer = await download_binary(image_url)

    filename = f"{sanitize_filename(title)}.mp3"

    try:
        await update.message.reply_audio(
            audio=audio_buffer,
            filename=filename,
            title=title,
            performer="Suno AI",
            thumbnail=thumb_buffer,
            caption=f"🎵 *{title}*\n🔗 [Original Link]({suno_url})",
            parse_mode="Markdown",
        )
        await status_msg.delete()
    except Exception as exc:
        logger.error("Error sending audio via Telegram: %s", exc)
        await status_msg.edit_text("❌ An error occurred while sending the audio file.")


# ---------------------------------------------------------------------------
# Application Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    """Starts the Telegram bot using long polling."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise ValueError(
            "Please set your Telegram Bot Token in the BOT_TOKEN variable "
            "or the TELEGRAM_BOT_TOKEN environment variable."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    # Register command and message handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot is running and polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
