"""Telegram image + audio to MP4 bot.

The bot keeps one pending image and one pending audio file per Telegram user.
Once both files are present, MoviePy renders an MP4 with the image displayed
for the full duration of the audio and sends it back to the same chat.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from urllib.request import urlopen
from weakref import WeakValueDictionary

from flask import Flask
from moviepy import AudioFileClip, ImageClip
from PIL import Image, ImageOps
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

LOGGER = logging.getLogger(__name__)

MAX_AUDIO_SECONDS = 15 * 60
PENDING_MEDIA_TTL_SECONDS = 60 * 60
MEDIA_CLEANUP_INTERVAL_SECONDS = 5 * 60
DEFAULT_HEALTH_PORT = 5000
HEALTH_PING_INTERVAL_SECONDS = 4 * 60
HEALTH_PING_TIMEOUT_SECONDS = 10
VIDEO_FPS = 24
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ALLOWED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".wav",
    ".webm",
}

health_app = Flask("telegram_media_bot")


@health_app.get("/")
def health_root() -> tuple[str, int]:
    return "Telegram Media Bot is running\n", 200


@health_app.get("/healthz")
def health_check() -> tuple[dict[str, str], int]:
    return {"status": "ok", "service": "telegram-media-bot"}, 200


class HealthServer:
    """Small HTTP server for uptime monitors, running beside Telegram polling."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.server = None
        self.thread: Thread | None = None

    def start(self) -> None:
        from werkzeug.serving import make_server

        self.server = make_server(
            "0.0.0.0",
            self.port,
            health_app,
            threaded=True,
        )
        self.thread = Thread(
            target=self.server.serve_forever,
            name="health-server",
            daemon=True,
        )
        self.thread.start()
        LOGGER.info("Health server listening on port %s", self.port)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        LOGGER.info("Health server stopped")


def ping_health_endpoint(url: str) -> int:
    """Make one bounded request to the local Flask health endpoint."""

    with urlopen(url, timeout=HEALTH_PING_TIMEOUT_SECONDS) as response:
        response.read(256)
        return response.status


async def self_ping_health_loop(port: int) -> None:
    """Keep the local health server active with a request every four minutes."""

    health_url = f"http://127.0.0.1:{port}/healthz"
    LOGGER.info(
        "Started health self-ping task: %s every %s seconds",
        health_url,
        HEALTH_PING_INTERVAL_SECONDS,
    )
    while True:
        try:
            status = await asyncio.to_thread(ping_health_endpoint, health_url)
            if status == 200:
                LOGGER.info("Health self-ping succeeded: HTTP %s", status)
            else:
                LOGGER.warning("Health self-ping returned HTTP %s", status)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Health self-ping failed for %s", health_url)
        await asyncio.sleep(HEALTH_PING_INTERVAL_SECONDS)


@dataclass
class UserMedia:
    """Files waiting to be combined for one Telegram user."""

    directory: Path
    image_path: Path | None = None
    audio_path: Path | None = None
    processing: bool = False
    last_activity: float = 0.0


pending_media: dict[int, UserMedia] = {}
user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def log_user_interaction(update: Update, handler_name: str) -> None:
    """Write non-secret Telegram user details to the console log."""

    user = update.effective_user
    if user is None:
        return

    LOGGER.info(
        "%s | User First Name: %s | Username: %s | User ID: %s",
        handler_name,
        user.first_name or "<none>",
        f"@{user.username}" if user.username else "<none>",
        user.id,
    )


def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_locks[user_id] = lock
    return lock


def get_or_create_media(user_id: int) -> UserMedia:
    media = pending_media.get(user_id)
    if media is None:
        media = UserMedia(
            directory=Path(tempfile.mkdtemp(prefix=f"media-bot-{user_id}-")),
            last_activity=time.monotonic(),
        )
        pending_media[user_id] = media
    else:
        media.last_activity = time.monotonic()
    return media


def cleanup_media(user_id: int) -> None:
    media = pending_media.pop(user_id, None)
    if media is not None:
        shutil.rmtree(media.directory, ignore_errors=True)


def cleanup_all_media() -> None:
    for user_id in list(pending_media):
        cleanup_media(user_id)


async def stale_media_cleanup_loop() -> None:
    """Remove abandoned uploads and their temporary directories periodically."""

    while True:
        await asyncio.sleep(MEDIA_CLEANUP_INTERVAL_SECONDS)
        now = time.monotonic()
        for user_id, media in list(pending_media.items()):
            if media.processing or now - media.last_activity < PENDING_MEDIA_TTL_SECONDS:
                continue

            lock = get_user_lock(user_id)
            async with lock:
                current = pending_media.get(user_id)
                if (
                    current is media
                    and not current.processing
                    and time.monotonic() - current.last_activity
                    >= PENDING_MEDIA_TTL_SECONDS
                ):
                    LOGGER.info("Removing abandoned media for user %s", user_id)
                    cleanup_media(user_id)


def has_allowed_extension(filename: str | None, allowed: set[str]) -> bool:
    return bool(filename and Path(filename).suffix.lower() in allowed)


def is_image_document(message) -> bool:
    document = message.document
    if document is None:
        return False
    content_type = (document.mime_type or "").lower()
    return content_type.startswith("image/") or has_allowed_extension(
        document.file_name, ALLOWED_IMAGE_EXTENSIONS
    )


def is_audio_document(message) -> bool:
    document = message.document
    if document is None:
        return False
    content_type = (document.mime_type or "").lower()
    return content_type.startswith("audio/") or has_allowed_extension(
        document.file_name, ALLOWED_AUDIO_EXTENSIONS
    )


async def download_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    destination: Path,
    file_id: str,
) -> None:
    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(destination))


async def maybe_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    user_id = user.id
    lock = get_user_lock(user_id)
    media: UserMedia | None = None

    async with lock:
        media = pending_media.get(user_id)
        if media is None or media.image_path is None or media.audio_path is None:
            return
        if media.processing:
            return
        media.processing = True
        image_path = media.image_path
        audio_path = media.audio_path
        output_path = media.directory / "combined.mp4"

    try:
        await message.reply_text("I have both files. Rendering your video now.")
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
        )
        duration = await asyncio.to_thread(
            render_video, image_path, audio_path, output_path
        )
        if duration > MAX_AUDIO_SECONDS:
            raise ValueError(
                f"Audio is longer than the {MAX_AUDIO_SECONDS // 60}-minute limit."
            )

        with output_path.open("rb") as video_file:
            await message.reply_video(
                video=video_file,
                supports_streaming=True,
                caption="Your video is ready.",
                read_timeout=120,
                write_timeout=120,
            )
    except ValueError as exc:
        await message.reply_text(str(exc))
    except asyncio.CancelledError:
        LOGGER.warning("Video render cancelled for user %s", user_id)
        raise
    except Exception:
        LOGGER.exception("Failed to render video for user %s", user_id)
        with suppress(Exception):
            await message.reply_text(
                "I couldn't render those files. Please try a different image or audio file."
            )
    finally:
        async with lock:
            if pending_media.get(user_id) is media:
                cleanup_media(user_id)


def render_video(image_path: Path, audio_path: Path, output_path: Path) -> float:
    """Render a still image with a full-length audio track and return duration."""

    audio_clip = None
    image_clip = None
    normalized_image_path = image_path.parent / "normalized-image.png"
    try:
        audio_clip = AudioFileClip(str(audio_path))
        duration = float(audio_clip.duration or 0)
        if duration <= 0:
            raise ValueError("The audio file has no playable duration.")
        if duration > MAX_AUDIO_SECONDS:
            raise ValueError(
                f"Audio is longer than the {MAX_AUDIO_SECONDS // 60}-minute limit."
            )

        with Image.open(image_path) as source_image:
            normalized_image = source_image.convert("RGB")
            width, height = normalized_image.size
            if width % 2 or height % 2:
                normalized_image = ImageOps.expand(
                    normalized_image,
                    border=(0, 0, width % 2, height % 2),
                    fill="black",
                )
            normalized_image.save(normalized_image_path, format="PNG")

        image_clip = (
            ImageClip(str(normalized_image_path))
            .with_duration(duration)
            .with_audio(audio_clip)
        )
        image_clip.write_videofile(
            str(output_path),
            fps=VIDEO_FPS,
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )
        return duration
    finally:
        if image_clip is not None:
            image_clip.close()
        if audio_clip is not None:
            audio_clip.close()
        normalized_image_path.unlink(missing_ok=True)


async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user_interaction(update, "Image handler")
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    async with get_user_lock(user.id):
        media = get_or_create_media(user.id)
        if media.processing:
            await message.reply_text("Your previous video is still being rendered.")
            return

        if message.photo:
            image = message.photo[-1]
            destination = media.directory / "image.jpg"
            await download_file(update, context, destination, image.file_id)
        elif message.document and is_image_document(message):
            extension = Path(message.document.file_name or ".img").suffix.lower()
            destination = media.directory / f"image{extension or '.img'}"
            await download_file(update, context, destination, message.document.file_id)
        else:
            return

        media.image_path = destination
        media.last_activity = time.monotonic()

    await message.reply_text(
        "Image received. Now send the audio file, or send another image to replace it."
    )
    await maybe_render(update, context)


async def receive_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user_interaction(update, "Audio handler")
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    audio = message.audio or message.voice
    document = message.document if audio is None else None
    if audio is None and not is_audio_document(message):
        return

    async with get_user_lock(user.id):
        media = get_or_create_media(user.id)
        if media.processing:
            await message.reply_text("Your previous video is still being rendered.")
            return

        if audio is not None:
            extension = Path(getattr(audio, "file_name", "") or "").suffix.lower()
            if not extension:
                extension = ".ogg" if message.voice else ".audio"
            destination = media.directory / f"audio{extension}"
            file_id = audio.file_id
        else:
            extension = Path(document.file_name or "").suffix.lower()
            destination = media.directory / f"audio{extension or '.audio'}"
            file_id = document.file_id

        await download_file(update, context, destination, file_id)
        media.audio_path = destination
        media.last_activity = time.monotonic()

    await message.reply_text(
        "Audio received. Now send the image, or send another audio file to replace it."
    )
    await maybe_render(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user_interaction(update, "Start handler")
    if update.effective_message:
        await update.effective_message.reply_text(
            "Send me one image and one audio file. I’ll turn them into an MP4 "
            "with the image displayed for the entire audio track and send it back.\n\n"
            "You can send the files in either order. Use /reset to start over."
        )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_user_interaction(update, "Reset handler")
    user = update.effective_user
    if user is None or update.effective_message is None:
        return
    async with get_user_lock(user.id):
        media = pending_media.get(user.id)
        if media is not None and media.processing:
            await update.effective_message.reply_text(
                "Your video is currently rendering; I can't reset it yet."
            )
            return
        cleanup_media(user.id)
    await update.effective_message.reply_text(
        "Your pending files were cleared. Send a new image and audio file."
    )


async def unsupported_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    log_user_interaction(update, "Unsupported-message handler")
    if update.effective_message:
        await update.effective_message.reply_text(
            "Please send one image and one audio file. Use /start for instructions."
        )


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    LOGGER.error("Unhandled Telegram update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with suppress(Exception):
            await update.effective_message.reply_text(
                "Something went wrong while handling that message. Please try again."
            )


async def post_init(application: Application) -> None:
    application.bot_data["media_cleanup_task"] = asyncio.create_task(
        stale_media_cleanup_loop()
    )
    LOGGER.info("Started abandoned-media cleanup task")
    health_port = int(application.bot_data.get("health_port", DEFAULT_HEALTH_PORT))
    application.bot_data["health_ping_task"] = asyncio.create_task(
        self_ping_health_loop(health_port)
    )


async def post_shutdown(application: Application) -> None:
    for task_key in ("media_cleanup_task", "health_ping_task"):
        task = application.bot_data.pop(task_key, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    cleanup_all_media()
    LOGGER.info("Cleaned up temporary media")


def build_application(token: str) -> Application:
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, receive_image)
    )
    application.add_handler(
        MessageHandler(
            filters.AUDIO
            | filters.VOICE
            | filters.Document.AUDIO
            | filters.Document.ALL,
            receive_audio,
        )
    )
    application.add_handler(MessageHandler(filters.ALL, unsupported_message))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Add the bot token to the project's secrets."
        )

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    # Telegram API URLs contain the bot token. Never emit HTTP request URLs
    # to workflow logs, even when the application log level is verbose.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    raw_port = os.environ.get("PORT", str(DEFAULT_HEALTH_PORT))
    try:
        health_port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError(f"Invalid PORT value: {raw_port!r}") from exc
    if health_port <= 0:
        raise RuntimeError(f"PORT must be positive, got {health_port}")

    LOGGER.info("Starting Telegram media bot")
    application = build_application(token)
    application.bot_data["health_port"] = health_port
    health_server = HealthServer(health_port)
    health_server.start()
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    finally:
        health_server.stop()


if __name__ == "__main__":
    main()