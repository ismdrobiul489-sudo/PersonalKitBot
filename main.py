#!/usr/bin/env python3
"""Private Telegram utility bot prototype.

The configured owner is always authorized; the owner manages the persistent allow list.
Temporary files are kept under WORK_DIR and removed after 30 minutes.
YouTube URLs are inspected for metadata/captions only; video downloads are disabled.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional

import cv2
import filetype
import qrcode
import pytesseract
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pypdf import PdfReader, PdfWriter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv
from tempmail import cmd_tempmail, handle_tempmail_callback
from privacy import (
    customize_image_metadata,
    customize_media_metadata,
    format_metadata_report,
    inspect_image_exif,
    inspect_media_metadata,
    make_customise_menu,
    make_metadata_menu,
    strip_image_metadata,
    strip_media_metadata,
)

try:
    import pikepdf
except ImportError:  # PDF compression is optional if pikepdf is unavailable.
    pikepdf = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("private-telegram-utility-bot")
load_dotenv()

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
TEMP_TTL_SECONDS = 30 * 60
MAX_HEAVY_JOBS = asyncio.Semaphore(1)
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/telegram_utility_bot"))
ALLOWLIST_FILE = Path(os.getenv("ALLOWLIST_FILE", "allowlist.json"))
OWNER_USER_ID: Optional[int] = None
ALLOWED_USERS: set[int] = set()


def parse_user_ids(raw: str, setting_name: str) -> set[int]:
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            if not item.isdigit() or int(item) <= 0:
                raise ValueError(f"{setting_name} must contain positive numeric Telegram user IDs.")
            ids.add(int(item))
    return ids


def save_allowed_users() -> None:
    ALLOWLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ALLOWLIST_FILE.with_suffix(ALLOWLIST_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(sorted(ALLOWED_USERS), indent=2) + "\n", encoding="utf-8")
    temporary.replace(ALLOWLIST_FILE)


def initialize_access_control() -> None:
    """Load the immutable owner ID and the persistent, owner-managed allow list."""
    global OWNER_USER_ID, ALLOWED_USERS
    raw_owner = os.getenv("OWNER_USER_ID", "").strip()
    if raw_owner and (not raw_owner.isdigit() or int(raw_owner) <= 0):
        raise SystemExit("OWNER_USER_ID must be a positive numeric Telegram user ID.")
    OWNER_USER_ID = int(raw_owner) if raw_owner else None
    seed_users = parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""), "ALLOWED_USER_IDS")
    seed_users.discard(OWNER_USER_ID)

    if ALLOWLIST_FILE.exists():
        try:
            stored = json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Could not read allow-list file {ALLOWLIST_FILE}: {exc}") from exc
        if not isinstance(stored, list) or any(
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0 for user_id in stored
        ):
            raise SystemExit(f"Allow-list file {ALLOWLIST_FILE} must contain a JSON list of positive numeric IDs.")
        ALLOWED_USERS = set(stored)
        ALLOWED_USERS.discard(OWNER_USER_ID)
    else:
        ALLOWED_USERS = seed_users
        save_allowed_users()


def user_dir(user_id: int) -> Path:
    path = WORK_DIR / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(name: str) -> str:
    clean = Path(name or "upload.bin").name
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", clean).strip("._")
    return (clean or "upload.bin")[:120]


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def detect_type(path: Path) -> tuple[str, str]:
    with path.open("rb") as file_handle:
        kind = filetype.guess(file_handle.read(261))
    if kind:
        return kind.mime, kind.extension.lower()
    ext = path.suffix.lower().lstrip(".")
    mime_by_ext = {
        "pdf": "application/pdf", "zip": "application/zip", "txt": "text/plain",
        "csv": "text/csv", "json": "application/json", "mp3": "audio/mpeg",
        "wav": "audio/wav", "m4a": "audio/mp4", "ogg": "audio/ogg",
        "mp4": "video/mp4", "mov": "video/quicktime", "mkv": "video/x-matroska",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return mime_by_ext.get(ext, "application/octet-stream"), ext


def make_keyboard(buttons: list[tuple[str, str]], row_size: int = 2) -> InlineKeyboardMarkup:
    rows = []
    for index in range(0, len(buttons), row_size):
        rows.append([InlineKeyboardButton(label, callback_data=data) for label, data in buttons[index:index + row_size]])
    return InlineKeyboardMarkup(rows)


async def authorized(update: Update) -> bool:
    user = update.effective_user
    if not user or OWNER_USER_ID is None or (user.id != OWNER_USER_ID and user.id not in ALLOWED_USERS):
        if update.effective_message:
            reason = (
                "The owner is not configured yet. Set OWNER_USER_ID to the ID shown by /myid."
                if OWNER_USER_ID is None
                else "This is a private bot. Your Telegram account is not on its access list."
            )
            await update.effective_message.reply_text(reason)
        return False
    return True


async def owner_only(update: Update) -> bool:
    if update.effective_user and update.effective_user.id == OWNER_USER_ID:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Only the bot owner can manage the allow list.")
    return False


def action_buttons(path: Path) -> list[tuple[str, str]]:
    mime, ext = detect_type(path)
    buttons: list[tuple[str, str]] = [("ℹ️ File Info", "do:file:info"), ("🔐 SHA-256", "do:file:hash")]
    if mime.startswith("image/") or ext in {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff"}:
        buttons += [
            ("📐 Resize", "do:img:resize"), ("🗜️ Compress", "do:img:compress"), ("✂️ Crop", "do:img:crop"),
            ("🔄 Rotate", "do:img:rotate"), ("🔍 OCR Text", "do:img:ocr"),
            ("📄 → PDF", "do:img:pdf"), ("🖼️ → JPG", "do:img:jpg"),
            ("🖼️ → PNG", "do:img:png"), ("🖼️ → WebP", "do:img:webp"),
            ("📷 Read QR", "do:qr:read"),
            ("🎭 Sticker", "do:img:sticker"),
            ("💧 Watermark", "do:img:watermark"),
            ("🛡️ Metadata Tools", "do:meta:menu:img"),
        ]
    elif mime == "application/pdf" or ext == "pdf":
        buttons += [
            ("📝 Extract Text", "do:pdf:text"), ("🔄 Rotate Pages", "do:pdf:rotate"),
            ("✂️ Split Pages", "do:pdf:split"), ("🗜️ Compress", "do:pdf:compress"),
            ("🖼️ Pages → Images", "do:pdf:images"),
            ("🔒 Password protect", "do:pdf:protect"), ("🔓 Remove password", "do:pdf:unlock"),
        ]
    elif mime.startswith("audio/") or ext in {"mp3", "wav", "m4a", "aac", "ogg", "flac", "opus"}:
        buttons += [("🎵 → MP3", "do:media:mp3"), ("🗜️ Compress", "do:media:compress"), ("🎙️ Transcribe", "do:media:transcribe"), ("✂️ Trim", "do:media:trim"), ("⏩ Speed up", "do:media:speed"), ("🛡️ Metadata Tools", "do:meta:menu:media")]
    elif mime.startswith("video/") or ext in {"mp4", "mov", "mkv", "webm", "avi", "m4v"}:
        buttons += [("🎵 Extract Audio", "do:media:extract"), ("🗜️ Compress", "do:media:vcompress"), ("🎙️ Transcribe", "do:media:transcribe"), ("🎬 Make GIF", "do:media:gif"), ("✂️ Trim", "do:media:trim"), ("⏩ Speed up", "do:media:speed"), ("🛡️ Metadata Tools", "do:meta:menu:media")]
    elif ext == "zip" or mime == "application/zip":
        buttons += [("📂 Unzip", "do:zip:unzip")]
    elif ext == "json":
        buttons += [("🧹 Clean Text", "do:text:clean"), ("📊 Word Count", "do:text:count"), ("📋 Pretty JSON", "do:text:json")]
    elif ext in {"csv", "tsv"}:
        buttons += [("🧹 Clean Text", "do:text:clean"), ("📊 Word Count", "do:text:count"), ("🔄 CSV/TSV → JSON", "do:text:csvjson")]
    elif ext in {"txt", "md", "log"} or mime.startswith("text/"):
        buttons += [("🧹 Clean Text", "do:text:clean"), ("📊 Word Count", "do:text:count"), ("🔤 URL Encode", "do:text:encode"), ("🔓 URL Decode", "do:text:decode"), ("📋 Pretty JSON", "do:text:json")]
    buttons.append(("📦 ZIP File", "do:zip:zip"))
    return buttons


async def remove_later(path: Path) -> None:
    await asyncio.sleep(TEMP_TTL_SECONDS)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:
        log.exception("Could not remove temporary path %s", path)


async def store_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file, filename: str, size: Optional[int]) -> Optional[Path]:
    if size is not None and size > MAX_DOWNLOAD_BYTES:
        await update.effective_message.reply_text("That file is over the 20 MB Telegram Bot API download limit. Please send a smaller file.")
        return None
    folder = user_dir(update.effective_user.id)
    old = context.user_data.get("last_file")
    context.user_data.pop("last_text", None)
    merge_mode = bool(context.user_data.get("merge_mode"))
    if old and not merge_mode:
        try:
            Path(old).unlink(missing_ok=True)
        except OSError:
            pass
    target = folder / f"{time.time_ns()}_{safe_name(filename)}"
    await tg_file.download_to_drive(custom_path=str(target))
    if target.stat().st_size > MAX_DOWNLOAD_BYTES:
        target.unlink(missing_ok=True)
        await update.effective_message.reply_text("That file exceeds the 20 MB download limit.")
        return None
    context.user_data["last_file"] = str(target)
    if merge_mode:
        context.user_data.setdefault("merge_files", []).append(str(target))
    asyncio.create_task(remove_later(target))
    return target


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    await update.effective_message.reply_text(
        "Private Utility Bot is ready. Send a photo, audio, video, PDF, ZIP, text/data file, or a video/channel URL. "
        "I’ll show matching tools. Use /help for commands. Temporary files are deleted after 30 minutes."
    )


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    help_text = (
        "Commands:\n"
        "/myid — show your numeric Telegram user ID\n"
        "/qr <text> — create a QR image\n"
        "/ai <summarize|polish|analyze|translate> <text> — AI text help\n"
        "/transcribe — transcribe the last uploaded audio/video\n"
        "/trim <start> <end> — trim the last audio/video, e.g. /trim 00:05 00:20\n"
        "/mergepdf — start collecting PDFs; /finishmerge to merge; /cancelmerge to stop\n"
        "/convert <value> <unit> — convert km↔mi, kg↔lb, C↔F, cm↔in\n"
        "/tts <text> — text-to-speech (detects language & shows voice buttons)\n"
        "/remind <duration> <message> — set a reminder, e.g. /remind 30m Take medicine\n"
        "/fx <amount> <from> <to> — currency converter, e.g. /fx 100 USD BDT\n"
        "/read <URL> — extract article text from a webpage\n"
        "/protect <password> — set PDF password, then send PDF\n"
        "/unlock <password> — set unlock password, then send encrypted PDF\n"
        "/speed <factor> — set speed (0.5–4.0), then use ⏩ Speed up button\n"
        "/voiceai — toggle voice-to-AI mode (default: ON)\n"
        "/tempmail — disposable email to receive OTP/verification codes\n"
        "/help — show this help\n\n"
        "Send a video URL for metadata and available captions only; the bot never downloads the video. "
        "Voice messages are auto-transcribed and answered by AI (toggle with /voiceai). "
        "Max upload size: 20 MB. Access is restricted to the owner and approved user IDs."
    )
    if update.effective_user and update.effective_user.id == OWNER_USER_ID:
        help_text += (
            "\n\nOwner access controls:\n"
            "/access or /allowlist — show the current allow list\n"
            "/allow <user_id> — grant access\n"
            "/deny <user_id> — revoke access"
        )
    await update.effective_message.reply_text(help_text)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_message:
        await update.effective_message.reply_text(f"Your Telegram user ID is: {user.id}")


async def cmd_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    allowed = sorted(ALLOWED_USERS)
    entries = "\n".join(str(user_id) for user_id in allowed) or "(empty)"
    await update.effective_message.reply_text(
        f"Permanent owner: {OWNER_USER_ID} (always authorized)\n"
        f"Additional allow-list IDs:\n{entries}\n\n"
        "Use /allow <user_id> to add someone or /deny <user_id> to remove them."
    )


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /allow <positive numeric Telegram user ID>")
        return
    user_id = int(args[0])
    if user_id == OWNER_USER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("The owner is permanently authorized and does not need to be added.")
        return
    if user_id in ALLOWED_USERS:
        if update.effective_message:
            await update.effective_message.reply_text(f"Telegram user {user_id} is already on the allow list.")
        return
    ALLOWED_USERS.add(user_id)
    try:
        save_allowed_users()
    except OSError:
        ALLOWED_USERS.remove(user_id)
        log.exception("Could not persist allow-list addition")
        if update.effective_message:
            await update.effective_message.reply_text("Could not save the allow list; no access change was made.")
        return
    if update.effective_message:
        await update.effective_message.reply_text(f"Access granted to Telegram user {user_id}.")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    args = context.args or []
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /deny <positive numeric Telegram user ID>")
        return
    user_id = int(args[0])
    if user_id == OWNER_USER_ID:
        if update.effective_message:
            await update.effective_message.reply_text("The owner is permanent and cannot be removed from access.")
        return
    if user_id not in ALLOWED_USERS:
        if update.effective_message:
            await update.effective_message.reply_text(f"Telegram user {user_id} is not on the allow list.")
        return
    ALLOWED_USERS.remove(user_id)
    try:
        save_allowed_users()
    except OSError:
        ALLOWED_USERS.add(user_id)
        log.exception("Could not persist allow-list removal")
        if update.effective_message:
            await update.effective_message.reply_text("Could not save the allow list; no access change was made.")
        return
    if update.effective_message:
        await update.effective_message.reply_text(f"Access revoked for Telegram user {user_id}.")


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    path = await store_upload(update, context, tg_file, f"photo_{photo.file_unique_id}.jpg", photo.file_size)
    if path:
        await update.message.reply_text("Image received. Choose a tool:", reply_markup=make_keyboard(action_buttons(path)))


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    doc = update.message.document
    if context.user_data.get("merge_mode"):
        if Path(doc.file_name or "").suffix.lower() != ".pdf" and doc.mime_type != "application/pdf":
            await update.message.reply_text("PDF merge mode is on. Please send PDF files only, then use /finishmerge.")
            return
        tg_file = await context.bot.get_file(doc.file_id)
        path = await store_upload(update, context, tg_file, doc.file_name or "merge.pdf", doc.file_size)
        if path:
            count = len(context.user_data.get("merge_files", []))
            await update.message.reply_text(f"Added PDF {count}. Send another PDF or use /finishmerge to merge (minimum 2).")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    path = await store_upload(update, context, tg_file, doc.file_name or "document.bin", doc.file_size)
    if path:
        mime, ext = detect_type(path)
        await update.message.reply_text(
            f"Received {path.name} ({mime}, {format_size(path.stat().st_size)}). Choose a tool:",
            reply_markup=make_keyboard(action_buttons(path)),
        )


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update) or not update.message:
        return
    item = update.message.audio or update.message.voice or update.message.video_note
    if not item:
        return
    # Route voice messages to voice-to-AI when the mode is enabled
    user_data = context.user_data or {}
    if update.message.voice and user_data.get("voice_ai_mode", True):
        await on_voice_ai(update, context)
        return
    tg_file = await context.bot.get_file(item.file_id)
    ext = Path(getattr(item, "file_name", "") or "audio.ogg").suffix or ".ogg"
    path = await store_upload(update, context, tg_file, getattr(item, "file_name", None) or f"audio_{item.file_unique_id}{ext}", item.file_size)
    if path:
        await update.message.reply_text("Audio received. Choose a tool:", reply_markup=make_keyboard(action_buttons(path)))


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update) or not update.message:
        return
    item = update.message.video or update.message.animation
    if not item:
        return
    tg_file = await context.bot.get_file(item.file_id)
    ext = Path(getattr(item, "file_name", "") or "video.mp4").suffix or ".mp4"
    path = await store_upload(update, context, tg_file, getattr(item, "file_name", None) or f"video_{item.file_unique_id}{ext}", item.file_size)
    if path:
        await update.message.reply_text("Video received. Choose a tool:", reply_markup=make_keyboard(action_buttons(path)))


async def send_output(message, path: Path, caption: str = "Done") -> None:
    if not path.exists():
        await message.reply_text("The result was not created.")
        return
    if path.stat().st_size > MAX_OUTPUT_BYTES:
        await message.reply_text("The result is over 20 MB and cannot be sent through the standard Telegram Bot API. Try a smaller input or lower-quality output.")
        return
    asyncio.create_task(remove_later(path))
    await message.reply_document(document=str(path), caption=caption)


def ffmpeg_run(args: list[str], timeout: int = 240) -> None:
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or "FFmpeg failed")[-1200:])


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".json":
        return json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2)
    if ext in {".txt", ".csv", ".tsv", ".md", ".log"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError("This action supports PDF, TXT, CSV, TSV, Markdown, LOG, or JSON files.")


def process_image(src: Path, action: str, outdir: Path, **kwargs) -> tuple[Optional[Path], Optional[str]]:
    image = ImageOps.exif_transpose(Image.open(src))
    if action == "ocr":
        lang = kwargs.get("ocr_lang") or os.getenv("OCR_LANG", "eng")
        return None, pytesseract.image_to_string(image, lang=lang).strip() or "No text detected."
    if action == "resize":
        target = kwargs.get("resize_target", "1080p")
        if target == "1080p":
            image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        elif target == "720p":
            image.thumbnail((1280, 720), Image.Resampling.LANCZOS)
        elif target == "square":
            side = min(image.size)
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            image = image.crop((left, top, left + side, top + side))
            image.thumbnail((1080, 1080), Image.Resampling.LANCZOS)
        elif target == "50pct":
            image = image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)
        elif target == "25pct":
            image = image.resize((max(1, image.width // 4), max(1, image.height // 4)), Image.Resampling.LANCZOS)
        else:
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        out = outdir / f"{src.stem}_resized.png"
        image.save(out, optimize=True)
    elif action == "crop":
        width, height = image.size
        side = min(width, height)
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side))
        out = outdir / f"{src.stem}_cropped.png"
        image.save(out, optimize=True)
    elif action == "compress":
        out = outdir / f"{src.stem}_compressed.jpg"
        image.convert("RGB").save(out, "JPEG", quality=72, optimize=True)
    elif action == "rotate":
        out = outdir / f"{src.stem}_rotated.jpg"
        image.rotate(-90, expand=True).convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    elif action == "pdf":
        out = outdir / f"{src.stem}.pdf"
        image.convert("RGB").save(out, "PDF", resolution=150)
    elif action in {"jpg", "png", "webp"}:
        out = outdir / f"{src.stem}.{action}"
        if action == "jpg":
            image.convert("RGB").save(out, "JPEG", quality=92, optimize=True)
        else:
            image.save(out, action.upper(), optimize=True)
    elif action == "sticker":
        # Telegram sticker format: 512×512 transparent WebP
        image = image.convert("RGBA")
        width, height = image.size
        scale = 512 / max(width, height)
        new_w, new_h = int(width * scale), int(height * scale)
        image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        paste_x = (512 - new_w) // 2
        paste_y = (512 - new_h) // 2
        canvas.paste(image, (paste_x, paste_y), image)
        out = outdir / f"{src.stem}_sticker.webp"
        canvas.save(out, "WEBP", quality=90)
    elif action == "watermark":
        image = image.convert("RGBA")
        txt_layer = Image.new("RGBA", image.size, (255, 255, 255, 0))
        draw = ImageDraw.Draw(txt_layer)
        watermark_text = kwargs.get("watermark_text") or os.getenv("WATERMARK_TEXT", "CONFIDENTIAL")
        # Use a reasonable font size based on image dimensions
        font_size = max(20, min(image.size) // 12)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (IOError, OSError):
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), watermark_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (image.size[0] - tw) // 2
        y = (image.size[1] - th) // 2
        draw.text((x, y), watermark_text, fill=(255, 255, 255, 110), font=font)
        watermarked = Image.alpha_composite(image, txt_layer).convert("RGB")
        out = outdir / f"{src.stem}_watermark.jpg"
        watermarked.save(out, "JPEG", quality=90, optimize=True)
    else:
        raise ValueError("Unknown image operation.")
    return out, None


def process_pdf(src: Path, action: str, outdir: Path) -> tuple[Optional[Path], Optional[str]]:
    if action == "text":
        reader = PdfReader(str(src))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        return None, text[:50000] or "No extractable text found."
    if action == "rotate":
        reader = PdfReader(str(src))
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(90)
            writer.add_page(page)
        out = outdir / f"{src.stem}_rotated.pdf"
        with out.open("wb") as f:
            writer.write(f)
        return out, None
    if action == "split":
        reader = PdfReader(str(src))
        if len(reader.pages) > 80:
            raise ValueError("For safety, splitting is limited to 80 pages at a time.")
        archive = outdir / f"{src.stem}_pages.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, page in enumerate(reader.pages, 1):
                writer = PdfWriter()
                writer.add_page(page)
                buf = io.BytesIO()
                writer.write(buf)
                zf.writestr(f"page_{index:03}.pdf", buf.getvalue())
        return archive, None
    if action == "compress":
        if pikepdf is None:
            raise RuntimeError("Install pikepdf to compress PDF files.")
        out = outdir / f"{src.stem}_compressed.pdf"
        with pikepdf.open(str(src)) as pdf:
            pdf.save(str(out), compress_streams=True, recompress_flate=True)
        return out, None
    if action == "images":
        from pdf2image import convert_from_path
        pages = convert_from_path(str(src), first_page=1, last_page=10, dpi=110)
        if not pages:
            raise ValueError("No PDF pages could be rendered.")
        archive = outdir / f"{src.stem}_images.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, page in enumerate(pages, 1):
                buf = io.BytesIO()
                page.save(buf, format="PNG", optimize=True)
                zf.writestr(f"page_{index:03}.png", buf.getvalue())
        return archive, None
    if action == "protect":
        password = os.getenv("PDF_PASSWORD", "1234")
        reader = PdfReader(str(src))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)
        out = outdir / f"{src.stem}_protected.pdf"
        with out.open("wb") as f:
            writer.write(f)
        return out, f"PDF protected with password. Password: {password}\nTip: Use /protect <password> to set a custom password."
    if action == "unlock":
        password = os.getenv("PDF_PASSWORD", "1234")
        try:
            reader = PdfReader(str(src))
            if reader.is_encrypted:
                reader.decrypt(password)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            out = outdir / f"{src.stem}_unlocked.pdf"
            with out.open("wb") as f:
                writer.write(f)
            return out, None
        except Exception as exc:
            raise ValueError(f"Could not unlock PDF. Wrong password or unsupported encryption: {exc}") from exc
    raise ValueError("Unknown PDF operation.")


def safely_unzip(src: Path, outdir: Path) -> tuple[Path, list[Path]]:
    target_dir = outdir / f"{src.stem}_contents"
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(src) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > 100:
            raise ValueError("For safety, ZIP extraction is limited to 100 files.")
        total = sum(member.file_size for member in members)
        if total > 100 * 1024 * 1024:
            raise ValueError("The uncompressed ZIP contents exceed the 100 MB safety limit.")
        for member in members:
            output = (target_dir / member.filename).resolve()
            if target_dir.resolve() not in output.parents:
                raise ValueError("This ZIP contains an unsafe path and was not extracted.")
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src_file, output.open("wb") as dst_file:
                shutil.copyfileobj(src_file, dst_file)
            extracted.append(output)
    return target_dir, extracted


def ai_response(prompt: str) -> str:
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
    system = "You are a concise personal utility assistant. Be accurate, preserve meaning, and do not invent facts."
    errors = []
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            configured = os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-120b").strip()
            candidates = [m.strip() for m in configured.split(",") if m.strip()]
            for fallback in ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"):
                if fallback not in candidates:
                    candidates.append(fallback)
            groq_last_exc = None
            for model_name in candidates:
                try:
                    completion = client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        temperature=0.2,
                        max_tokens=1800,
                    )
                    return completion.choices[0].message.content or "No response."
                except Exception as exc:
                    groq_last_exc = exc
                    err_text = str(exc).lower()
                    if any(term in err_text for term in ("404", "model_not_found", "does not exist", "not have access", "decommissioned")):
                        continue
                    break
            if groq_last_exc is not None:
                errors.append(f"Groq: {groq_last_exc}")
        except Exception as exc:
            errors.append(f"Groq: {exc}")
    if nvidia_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=nvidia_key, base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"))
            configured_nv = os.getenv("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct").strip()
            nv_candidates = [m.strip() for m in configured_nv.split(",") if m.strip()]
            for fallback in ("meta/llama-3.1-70b-instruct", "meta/llama-3.1-8b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"):
                if fallback not in nv_candidates:
                    nv_candidates.append(fallback)
            nv_last_exc = None
            for model_name in nv_candidates:
                try:
                    completion = client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        temperature=0.2,
                        max_tokens=1800,
                    )
                    return completion.choices[0].message.content or "No response."
                except Exception as exc:
                    nv_last_exc = exc
                    err_text = str(exc).lower()
                    if any(term in err_text for term in ("404", "410", "model_not_found", "not found", "decommissioned")):
                        continue
                    break
            if nv_last_exc is not None:
                errors.append(f"NVIDIA: {nv_last_exc}")
        except Exception as exc:
            errors.append(f"NVIDIA: {exc}")
    if not groq_key and not nvidia_key:
        return "AI tools need GROQ_API_KEY or NVIDIA_API_KEY configured in the environment."
    raise RuntimeError("AI service failed: " + "; ".join(errors))


def subtitle_timestamp(seconds: float, vtt: bool = False) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def chunk_words(words: list[dict], max_words: int = 3, max_duration: float = 2.0) -> list[dict]:
    """Group word-level timestamps into short 3-4 word subtitle chunks (ideal for Shorts/Reels)."""
    chunks: list[dict] = []
    current_words: list[dict] = []
    current_word_count = 0
    last_end = 0.0

    for item in words:
        raw_w = str(item.get("word", "")).strip()
        if not raw_w:
            continue
        w_count = len(raw_w.split())
        current_words.append(item)
        current_word_count += w_count
        start_time = float(current_words[0].get("start", 0.0))
        end_time = float(item.get("end", start_time + 0.3))
        duration = end_time - start_time
        has_punctuation = raw_w.endswith((".", "?", "!", "।", ";", "—"))

        # Flush when reaching target word count (3-4 words), max duration, or punctuation
        if current_word_count >= max_words or duration >= max_duration or (current_word_count >= 2 and has_punctuation):
            chunk_text = " ".join(str(w.get("word", "")).strip() for w in current_words).strip()
            if chunk_text:
                c_start = max(start_time, last_end)
                c_end = max(end_time, c_start + 0.25)
                chunks.append({
                    "start": round(c_start, 3),
                    "end": round(c_end, 3),
                    "text": chunk_text,
                })
                last_end = c_end
            current_words = []
            current_word_count = 0

    if current_words:
        chunk_text = " ".join(str(w.get("word", "")).strip() for w in current_words).strip()
        if chunk_text:
            start_time = float(current_words[0].get("start", 0.0))
            end_time = float(current_words[-1].get("end", start_time + 0.3))
            c_start = max(start_time, last_end)
            c_end = max(end_time, c_start + 0.25)
            # If trailing chunk is only 1 word and previous chunk has <= 3 words, merge to keep 3-4 words
            if current_word_count == 1 and chunks and len(chunks[-1]["text"].split()) <= 3:
                chunks[-1]["text"] = f"{chunks[-1]['text']} {chunk_text}"
                chunks[-1]["end"] = round(max(chunks[-1]["end"], c_end), 3)
            else:
                chunks.append({
                    "start": round(c_start, 3),
                    "end": round(c_end, 3),
                    "text": chunk_text,
                })

    return chunks


def chunk_segment_by_words(segment: dict, max_words: int = 3) -> list[dict]:
    """Fallback: divide a long segment into short 3-4 word subtitle chunks with interpolated timestamps."""
    start = float(segment.get("start", 0.0))
    end = float(segment.get("end", start + 1.0))
    text = str(segment.get("text", "")).strip()
    words = text.split()
    if not words:
        return []
    if len(words) <= max_words + 1:
        return [{"start": round(start, 3), "end": round(max(end, start + 0.3), 3), "text": text}]

    total_words = len(words)
    total_time = max(0.5, end - start)
    time_per_word = total_time / total_words
    chunks: list[dict] = []

    i = 0
    while i < total_words:
        remaining = total_words - i
        if remaining == 4:
            take = 4
        elif remaining <= max_words:
            take = remaining
        else:
            take = max_words

        group = words[i:i + take]
        c_start = start + (i * time_per_word)
        c_end = start + ((i + len(group)) * time_per_word)
        if i + len(group) >= total_words:
            c_end = end

        chunks.append({
            "start": round(c_start, 3),
            "end": round(max(c_end, c_start + 0.25), 3),
            "text": " ".join(group),
        })
        i += take

    return chunks


def groq_transcribe(path: Path) -> tuple[str, list[dict]]:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Transcription needs GROQ_API_KEY configured in the environment.")
    from groq import Groq
    client = Groq(api_key=key)
    with path.open("rb") as audio_file:
        result = client.audio.transcriptions.create(
            file=(path.name, audio_file.read()),
            model=os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"),
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    transcript = getattr(result, "text", "") or ""

    raw_words = []
    if isinstance(result, dict):
        raw_words = result.get("words") or []
    elif hasattr(result, "words"):
        raw_words = getattr(result, "words") or []

    words = []
    for w in raw_words:
        if isinstance(w, dict):
            word_text = str(w.get("word", "")).strip()
            start = float(w.get("start", 0.0))
            end = float(w.get("end", start + 0.3))
        else:
            word_text = str(getattr(w, "word", "")).strip()
            start = float(getattr(w, "start", 0.0))
            end = float(getattr(w, "end", start + 0.3))
        if word_text:
            words.append({"word": word_text, "start": start, "end": end})

    raw_segments = []
    if isinstance(result, dict):
        raw_segments = result.get("segments") or []
    elif hasattr(result, "segments"):
        raw_segments = getattr(result, "segments") or []

    segments = []
    for segment in raw_segments:
        if isinstance(segment, dict):
            text = str(segment.get("text", "")).strip()
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start + 1.0))
        else:
            text = str(getattr(segment, "text", "")).strip()
            start = float(getattr(segment, "start", 0.0))
            end = float(getattr(segment, "end", start + 1.0))
        if text:
            segments.append({"text": text, "start": start, "end": end})

    # Build short 3-4 word subtitle segments
    try:
        max_chunk_words = int(os.getenv("SUBTITLE_MAX_WORDS", "3"))
    except ValueError:
        max_chunk_words = 3

    if words:
        sub_segments = chunk_words(words, max_words=max_chunk_words)
    else:
        sub_segments = []
        for seg in segments:
            sub_segments.extend(chunk_segment_by_words(seg, max_words=max_chunk_words))

    return transcript, sub_segments


async def do_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE, src: Path) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Transcribing…")
    async with MAX_HEAVY_JOBS:
        text, segments = await asyncio.to_thread(groq_transcribe, src)
    # Store transcript data for on-demand format delivery
    if context.user_data is not None:
        context.user_data["last_transcript_text"] = text
        context.user_data["last_transcript_segments"] = segments
    preview = text[:400] + ("…" if len(text) > 400 else "")
    format_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Plain Text", callback_data="do:transcript:txt"),
            InlineKeyboardButton("📋 SRT (CapCut)", callback_data="do:transcript:srt"),
        ],
        [
            InlineKeyboardButton("🌐 WebVTT", callback_data="do:transcript:vtt"),
            InlineKeyboardButton("📦 All Formats", callback_data="do:transcript:all"),
        ],
    ])
    if update.effective_message:
        await update.effective_message.reply_text(
            f"✅ Transcription complete!\n\n📝 Preview:\n{preview}\n\nChoose a format to download:",
            reply_markup=format_keyboard,
        )


def yt_metadata_and_captions(url: str, outdir: Path) -> tuple[str, Optional[Path]]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed.") from exc

    base_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        },
    }

    cookies_env = os.getenv("YOUTUBE_COOKIES_FILE", "cookies.txt").strip()
    if cookies_env and Path(cookies_env).exists():
        base_opts["cookiefile"] = str(Path(cookies_env).resolve())

    proxy = os.getenv("YOUTUBE_PROXY", "").strip()
    if proxy:
        base_opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        err_msg = str(exc)
        if "confirm you’re not a bot" in err_msg or "Sign in" in err_msg:
            raise RuntimeError(
                "YouTube has blocked this server IP (Anti-bot check). "
                "To fix this, export a 'cookies.txt' file from your browser into the bot folder "
                "or configure YOUTUBE_COOKIES_FILE in your .env."
            ) from exc
        raise

    title = info.get("title", "Unknown title")
    uploader = info.get("uploader") or info.get("channel") or "Unknown channel"
    duration = info.get("duration")
    description = (info.get("description") or "").strip().replace("\n", " ")[:700]
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    caption_path = None
    # Download caption sidecars only; yt-dlp is explicitly prevented from downloading media.
    languages = list(subtitles) or list(automatic)
    if languages:
        lang = next((key for key in languages if key.startswith(("en", "bn"))), languages[0])
        caption_dir = Path(tempfile.mkdtemp(prefix="ytcaps_", dir=str(outdir)))
        options = {
            **base_opts,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt/best",
            "outtmpl": str(caption_dir / "%(id)s.%(ext)s"),
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
            captions = list(caption_dir.glob("*.vtt")) + list(caption_dir.glob("*.srt"))
            if captions:
                caption_path = outdir / f"{time.time_ns()}_youtube_captions{captions[0].suffix}"
                shutil.copy2(captions[0], caption_path)
        except Exception as exc:
            log.warning("Could not download YouTube captions: %s", exc)
        finally:
            shutil.rmtree(caption_dir, ignore_errors=True)
    duration_text = f"{int(duration) // 60} min {int(duration) % 60} sec" if duration else "Unknown"
    report = f"Title: {title}\nChannel/uploader: {uploader}\nDuration: {duration_text}\nURL: {url}"
    if description:
        report += f"\nDescription: {description}"
    report += "\n\nVideo download is disabled; only metadata and, when available, captions were accessed."
    return report, caption_path


def is_youtube_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"))


def process_pdf_with_password(src: Path, action: str, outdir: Path, password: str) -> tuple[Optional[Path], Optional[str]]:
    """Run PDF protect or unlock using the given password directly (not from env)."""
    if action == "protect":
        reader = PdfReader(str(src))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)
        out = outdir / f"{src.stem}_protected.pdf"
        with out.open("wb") as f:
            writer.write(f)
        return out, f"🔒 PDF protected with password: {password}"
    if action == "unlock":
        try:
            reader = PdfReader(str(src))
            if reader.is_encrypted:
                reader.decrypt(password)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            out = outdir / f"{src.stem}_unlocked.pdf"
            with out.open("wb") as f:
                writer.write(f)
            return out, None
        except Exception as exc:
            raise ValueError(f"Could not unlock PDF. Wrong password or unsupported encryption: {exc}") from exc
    raise ValueError("Unknown PDF password operation.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    # Check if we're awaiting a PDF password from the user
    user_data = context.user_data or {}
    awaiting_pdf_action = user_data.get("awaiting_pdf_action")
    if awaiting_pdf_action:
        password = text
        if context.user_data is not None:
            context.user_data["pdf_password"] = password
            context.user_data.pop("awaiting_pdf_action", None)
        src_raw = user_data.get("pdf_pending_file")
        if src_raw and Path(src_raw).exists():
            src = Path(src_raw)
            outdir = user_dir(update.effective_user.id)
            if context.user_data is not None:
                context.user_data.pop("pdf_pending_file", None)
            try:
                if awaiting_pdf_action == "protect":
                    out, msg = await asyncio.to_thread(process_pdf_with_password, src, "protect", outdir, password)
                    await send_output(update.message, out, msg or f"🔒 PDF protected with password: {password}")
                elif awaiting_pdf_action == "unlock":
                    out, _ = await asyncio.to_thread(process_pdf_with_password, src, "unlock", outdir, password)
                    await send_output(update.message, out, "🔓 PDF unlocked successfully.")
            except Exception as exc:
                log.exception("PDF password operation failed")
                await update.message.reply_text(f"Operation failed: {str(exc)[:700]}")
        else:
            await update.message.reply_text(f"✅ Password saved. Now send a PDF and tap the appropriate button.")
        return

    # Check if we're awaiting custom watermark text
    if user_data.get("awaiting_watermark"):
        watermark_text = text
        if context.user_data is not None:
            context.user_data.pop("awaiting_watermark", None)
            context.user_data["watermark_text"] = watermark_text
        src_raw = user_data.get("watermark_pending_file") or user_data.get("last_file")
        if src_raw and Path(src_raw).exists():
            src = Path(src_raw)
            outdir = user_dir(update.effective_user.id)
            if context.user_data is not None:
                context.user_data.pop("watermark_pending_file", None)
            try:
                out, _ = await asyncio.to_thread(process_image, src, "watermark", outdir, watermark_text=watermark_text)
                await send_output(update.message, out, f"💧 Watermark applied: '{watermark_text}'")
            except Exception as exc:
                log.exception("Watermark operation failed")
                await update.message.reply_text(f"Watermark failed: {str(exc)[:600]}")
        else:
            await update.message.reply_text(f"✅ Watermark text saved as '{watermark_text}'. Now send an image and tap 💧 Watermark.")
        return

    # Check if we're awaiting trim timestamps (e.g. 00:05 00:20 or 5 25)
    if user_data.get("awaiting_trim"):
        trim_range = text.split()
        if context.user_data is not None:
            context.user_data.pop("awaiting_trim", None)
        if len(trim_range) != 2:
            await update.message.reply_text("Invalid trim range. Format: `00:05 00:20` or seconds `5 20`.")
            return
        start_ts, end_ts = trim_range[0], trim_range[1]
        src_raw = user_data.get("trim_pending_file") or user_data.get("last_file")
        if src_raw and Path(src_raw).exists():
            src = Path(src_raw)
            outdir = user_dir(update.effective_user.id)
            if context.user_data is not None:
                context.user_data.pop("trim_pending_file", None)
            out = outdir / f"{src.stem}_trimmed{src.suffix or '.mp4'}"
            try:
                await update.message.reply_text(f"✂️ Trimming from {start_ts} to {end_ts}…")
                async with MAX_HEAVY_JOBS:
                    await asyncio.to_thread(ffmpeg_run, ["-ss", start_ts, "-to", end_ts, "-i", str(src), "-c", "copy", str(out)])
                await send_output(update.message, out, f"✂️ Trimmed ({start_ts} → {end_ts}).")
            except Exception as exc:
                log.exception("Trim failed")
                await update.message.reply_text(f"Trim failed: {str(exc)[:700]}")
        else:
            await update.message.reply_text("Please upload an audio or video file first.")
        return

    # Check if we're awaiting custom metadata author
    if user_data.get("awaiting_meta_author"):
        author_text = text
        if context.user_data is not None:
            context.user_data.pop("awaiting_meta_author", None)
        src_raw = user_data.get("meta_pending_file") or user_data.get("last_file")
        if src_raw and Path(src_raw).exists():
            src = Path(src_raw)
            outdir = user_dir(update.effective_user.id)
            if context.user_data is not None:
                context.user_data.pop("meta_pending_file", None)
            try:
                out = await asyncio.to_thread(customize_image_metadata, src, outdir, author=author_text)
                await send_output(update.message, out, f"✏️ Custom metadata applied! Author: `{author_text}`")
            except Exception as exc:
                log.exception("Custom metadata failed")
                await update.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
        else:
            await update.message.reply_text("Please upload an image first.")
        return

    # Check if we're awaiting custom media metadata
    if user_data.get("awaiting_meta_media"):
        parts_media = text.split("-", 1)
        title = parts_media[0].strip()
        artist = parts_media[1].strip() if len(parts_media) > 1 else "Creator"
        if context.user_data is not None:
            context.user_data.pop("awaiting_meta_media", None)
        src_raw = user_data.get("meta_pending_file") or user_data.get("last_file")
        if src_raw and Path(src_raw).exists():
            src = Path(src_raw)
            outdir = user_dir(update.effective_user.id)
            if context.user_data is not None:
                context.user_data.pop("meta_pending_file", None)
            try:
                out = await asyncio.to_thread(customize_media_metadata, src, outdir, title, artist)
                await send_output(update.message, out, f"✏️ Custom media metadata applied! Title: `{title}`, Artist: `{artist}`")
            except Exception as exc:
                log.exception("Custom media metadata failed")
                await update.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
        else:
            await update.message.reply_text("Please upload a media file first.")
        return

    context.user_data["last_text"] = text
    url_match = re.search(r"https?://\S+", text)
    if url_match and is_youtube_url(url_match.group(0).rstrip(".,)")):
        await update.message.reply_text("Checking public metadata and captions only…")
        try:
            outdir = user_dir(update.effective_user.id)
            report, captions = await asyncio.to_thread(yt_metadata_and_captions, url_match.group(0).rstrip(".,)"), outdir)
            await update.message.reply_text(report[:4000])
            if captions:
                await send_output(update.message, captions, "Available captions (VTT/SRT).")
            else:
                await update.message.reply_text("No accessible captions were found for this URL.")
        except Exception as exc:
            log.exception("YouTube metadata/caption lookup failed")
            await update.message.reply_text(f"Could not inspect that URL: {str(exc)[:800]}")
        return
    await update.message.reply_text(
        "Text received. Choose an operation:",
        reply_markup=make_keyboard([
            ("📊 Word Count", "do:text:count"), ("🧹 Clean Whitespace", "do:text:clean"),
            ("🔤 URL Encode", "do:text:encode"), ("🔓 URL Decode", "do:text:decode"),
            ("📝 AI Summarize", "do:ai:summarize"), ("✨ AI Polish", "do:ai:polish"),
            ("🔍 AI Analyze", "do:ai:analyze"), ("🌐 AI Translate", "do:ai:translate:select"),
            ("🔊 Text-to-Speech", "do:tts:select"),
        ]),
    )


async def do_text_action(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, text: str) -> None:
    if action == "count":
        words = re.findall(r"\S+", text)
        result = f"Characters: {len(text)}\nWords: {len(words)}\nLines: {len(text.splitlines())}\nBytes (UTF-8): {len(text.encode('utf-8'))}"
    elif action == "clean":
        result = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()
    elif action == "encode":
        result = urllib.parse.quote(text, safe="")
    elif action == "decode":
        result = urllib.parse.unquote(text)
    elif action == "json":
        result = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    elif action == "csvjson":
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel_tab if "\t" in text[:4096] else csv.excel
        records = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        result = json.dumps(records, ensure_ascii=False, indent=2)
    elif action == "ai":
        task = context.user_data.get("ai_task", "summarize")
        target = context.user_data.get("translate_target_lang") or os.getenv("AI_TARGET_LANGUAGE", "Bengali")
        prompts = {
            "summarize": "Summarize the following text clearly:\n\n" + text,
            "polish": "Polish the following text while preserving its meaning and language:\n\n" + text,
            "analyze": "Analyze the following text and give concise, useful observations:\n\n" + text,
            "translate": f"Translate the following text into {target}, preserving meaning:\n\n" + text,
        }
        async with MAX_HEAVY_JOBS:
            result = await asyncio.to_thread(ai_response, prompts.get(task, prompts["summarize"]))
    else:
        raise ValueError("Unknown text operation.")
    if len(result) <= 3900:
        await update.effective_message.reply_text(result)
    else:
        out = user_dir(update.effective_user.id) / f"{time.time_ns()}_text_result.txt"
        out.write_text(result, encoding="utf-8")
        await send_output(update.effective_message, out, "Text result")


VOICE_CATALOG = {
    "bn": {
        "name": "বাংলা (Bengali)",
        "flag": "🇧🇩",
        "voices": [
            ("🇧🇩 নবানিতা ♀", "bn-BD-NabanitaNeural"),
            ("🇧🇩 প্রদীপ ♂", "bn-BD-PradeepNeural"),
            ("🇮🇳 তানিশা ♀", "bn-IN-TanishaaNeural"),
            ("🇮🇳 ভাস্কর ♂", "bn-IN-BashkarNeural"),
        ],
    },
    "en": {
        "name": "English",
        "flag": "🇺🇸",
        "voices": [
            ("🇺🇸 Aria ♀", "en-US-AriaNeural"),
            ("🇺🇸 Jenny ♀", "en-US-JennyNeural"),
            ("🇺🇸 Michelle ♀", "en-US-MichelleNeural"),
            ("🇺🇸 Guy ♂", "en-US-GuyNeural"),
            ("🇺🇸 Davis ♂", "en-US-DavisNeural"),
            ("🇺🇸 Jason ♂", "en-US-JasonNeural"),
            ("🇬🇧 Sonia ♀", "en-GB-SoniaNeural"),
            ("🇬🇧 Ryan ♂", "en-GB-RyanNeural"),
            ("🇦🇺 Natasha ♀", "en-AU-NatashaNeural"),
            ("🇦🇺 William ♂", "en-AU-WilliamNeural"),
        ],
    },
    "hi": {
        "name": "हिन्दी (Hindi)",
        "flag": "🇮🇳",
        "voices": [
            ("🇮🇳 Swara ♀", "hi-IN-SwaraNeural"),
            ("🇮🇳 Ananya ♀", "hi-IN-AnanyaNeural"),
            ("🇮🇳 Madhur ♂", "hi-IN-MadhurNeural"),
            ("🇮🇳 Rohit ♂", "hi-IN-RohitNeural"),
            ("🇮🇳 Kavya ♀", "hi-IN-KavyaNeural"),
            ("🇮🇳 Aarav ♂", "hi-IN-AaravNeural"),
        ],
    },
    "ar": {
        "name": "العربية (Arabic)",
        "flag": "🇸🇦",
        "voices": [
            ("🇸🇦 Zariyah ♀", "ar-SA-ZariyahNeural"),
            ("🇸🇦 Hamed ♂", "ar-SA-HamedNeural"),
            ("🇪🇬 Salma ♀", "ar-EG-SalmaNeural"),
            ("🇪🇬 Shakir ♂", "ar-EG-ShakirNeural"),
            ("🇦🇪 Fatima ♀", "ar-AE-FatimaNeural"),
            ("🇦🇪 Hamdan ♂", "ar-AE-HamdanNeural"),
        ],
    },
    "ur": {
        "name": "اردو (Urdu)",
        "flag": "🇵🇰",
        "voices": [
            ("🇵🇰 Uzma ♀", "ur-PK-UzmaNeural"),
            ("🇵🇰 Asad ♂", "ur-PK-AsadNeural"),
        ],
    },
    "fr": {
        "name": "Français (French)",
        "flag": "🇫🇷",
        "voices": [
            ("🇫🇷 Denise ♀", "fr-FR-DeniseNeural"),
            ("🇫🇷 Eloise ♀", "fr-FR-EloiseNeural"),
            ("🇫🇷 Henri ♂", "fr-FR-HenriNeural"),
            ("🇫🇷 Alain ♂", "fr-FR-AlainNeural"),
        ],
    },
    "de": {
        "name": "Deutsch (German)",
        "flag": "🇩🇪",
        "voices": [
            ("🇩🇪 Katja ♀", "de-DE-KatjaNeural"),
            ("🇩🇪 Seraphina ♀", "de-DE-SeraphinaNeural"),
            ("🇩🇪 Conrad ♂", "de-DE-ConradNeural"),
            ("🇩🇪 Killian ♂", "de-DE-KillianNeural"),
        ],
    },
    "es": {
        "name": "Español (Spanish)",
        "flag": "🇪🇸",
        "voices": [
            ("🇪🇸 Elvira ♀", "es-ES-ElviraNeural"),
            ("🇪🇸 Ximena ♀", "es-ES-XimenaNeural"),
            ("🇪🇸 Alvaro ♂", "es-ES-AlvaroNeural"),
            ("🇲🇽 Dalia ♀", "es-MX-DaliaNeural"),
            ("🇲🇽 Jorge ♂", "es-MX-JorgeNeural"),
        ],
    },
    "tr": {
        "name": "Türkçe (Turkish)",
        "flag": "🇹🇷",
        "voices": [
            ("🇹🇷 Emel ♀", "tr-TR-EmelNeural"),
            ("🇹🇷 Ahmet ♂", "tr-TR-AhmetNeural"),
        ],
    },
    "ru": {
        "name": "Русский (Russian)",
        "flag": "🇷🇺",
        "voices": [
            ("🇷🇺 Svetlana ♀", "ru-RU-SvetlanaNeural"),
            ("🇷🇺 Dariya ♀", "ru-RU-DariyaNeural"),
            ("🇷🇺 Dmitry ♂", "ru-RU-DmitryNeural"),
        ],
    },
    "zh": {
        "name": "中文 (Chinese)",
        "flag": "🇨🇳",
        "voices": [
            ("🇨🇳 Xiaoxiao ♀", "zh-CN-XiaoxiaoNeural"),
            ("🇨🇳 Xiaoyi ♀", "zh-CN-XiaoyiNeural"),
            ("🇨🇳 Yunjian ♂", "zh-CN-YunjianNeural"),
            ("🇨🇳 Yunxi ♂", "zh-CN-YunxiNeural"),
        ],
    },
    "ja": {
        "name": "日本語 (Japanese)",
        "flag": "🇯🇵",
        "voices": [
            ("🇯🇵 Nanami ♀", "ja-JP-NanamiNeural"),
            ("🇯🇵 Keita ♂", "ja-JP-KeitaNeural"),
            ("🇯🇵 Aoi ♀", "ja-JP-AoiNeural"),
            ("🇯🇵 Daichi ♂", "ja-JP-DaichiNeural"),
        ],
    },
    "ko": {
        "name": "한국어 (Korean)",
        "flag": "🇰🇷",
        "voices": [
            ("🇰🇷 Sun-Hi ♀", "ko-KR-SunHiNeural"),
            ("🇰🇷 InJoon ♂", "ko-KR-InJoonNeural"),
            ("🇰🇷 Hyunsu ♂", "ko-KR-HyunsuNeural"),
        ],
    },
    "pt": {
        "name": "Português (Portuguese)",
        "flag": "🇧🇷",
        "voices": [
            ("🇧🇷 Francisca ♀", "pt-BR-FranciscaNeural"),
            ("🇧🇷 Thalita ♀", "pt-BR-ThalitaNeural"),
            ("🇧🇷 Antonio ♂", "pt-BR-AntonioNeural"),
            ("🇵🇹 Raquel ♀", "pt-PT-RaquelNeural"),
            ("🇵🇹 Duarte ♂", "pt-PT-DuarteNeural"),
        ],
    },
    "it": {
        "name": "Italiano (Italian)",
        "flag": "🇮🇹",
        "voices": [
            ("🇮🇹 Elsa ♀", "it-IT-ElsaNeural"),
            ("🇮🇹 Isabella ♀", "it-IT-IsabellaNeural"),
            ("🇮🇹 Diego ♂", "it-IT-DiegoNeural"),
        ],
    },
}


def detect_text_language(text: str) -> str:
    """Detect language from Unicode script ranges. Returns ISO 639-1 code."""
    if not text or not text.strip():
        return "en"
    sample = text[:500]  # Use first 500 chars for speed

    counts: dict[str, int] = {
        "bn": len(re.findall(r"[\u0980-\u09FF]", sample)),   # Bengali script
        "hi": len(re.findall(r"[\u0900-\u097F]", sample)),   # Devanagari (Hindi)
        "ar": len(re.findall(r"[\u0600-\u06FF]", sample)),   # Arabic script
        "ur": len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF]", sample)),  # Urdu (extends Arabic)
        "zh": len(re.findall(r"[\u4E00-\u9FFF\u3400-\u4DBF]", sample)),  # CJK (Chinese)
        "ja": len(re.findall(r"[\u3040-\u30FF]", sample)),   # Hiragana/Katakana (Japanese)
        "ko": len(re.findall(r"[\uAC00-\uD7AF]", sample)),   # Hangul (Korean)
        "ru": len(re.findall(r"[\u0400-\u04FF]", sample)),   # Cyrillic (Russian)
        "tr": 0,   # Turkish uses Latin; detected by keyword heuristic below
        "fr": 0,   # French uses Latin; detected by keyword heuristic below
        "de": 0,   # German uses Latin; detected by keyword heuristic below
        "es": 0,   # Spanish uses Latin; detected by keyword heuristic below
        "pt": 0,   # Portuguese uses Latin
        "it": 0,   # Italian uses Latin
        "en": len(re.findall(r"[a-zA-Z]", sample)),
    }

    # --- Boost Arabic vs Urdu ---
    # Urdu uses extended Arabic block; if more extended chars → Urdu
    ar_basic = len(re.findall(r"[\u0600-\u06FF]", sample))
    ur_ext = len(re.findall(r"[\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", sample))
    if ar_basic > 0 and ur_ext > ar_basic * 0.3:
        counts["ur"] = ar_basic + ur_ext
        counts["ar"] = max(0, counts["ar"] - ur_ext)

    # --- Boost Hindi vs Bengali (both Brahmic scripts but different blocks) ---
    # They use different Unicode ranges so the regex already separates them well.

    # --- Latin-script languages: use common unique words/characters as hints ---
    lower = sample.lower()
    # French: accented chars é, è, ê, à, ù, ç, î, ô
    fr_score = len(re.findall(r"[éèêàùçîôœæ]", sample))
    # German: ä, ö, ü, ß
    de_score = len(re.findall(r"[äöüß]", sample)) * 3
    # Spanish: ñ, ¿, ¡
    es_score = len(re.findall(r"[ñ¿¡]", sample)) * 4
    # Portuguese: ã, õ, ê, â  (shares some with French)
    pt_score = len(re.findall(r"[ãõâ]", sample)) * 4
    # Turkish: ğ, ş, ı, İ
    tr_score = len(re.findall(r"[ğşıİ]", sample)) * 4
    # Italian: common words
    it_score = len(re.findall(r"\b(il|la|di|che|un|per|non|si|con|ho)\b", lower)) * 2

    counts["fr"] = fr_score
    counts["de"] = de_score
    counts["es"] = es_score
    counts["pt"] = pt_score
    counts["tr"] = tr_score
    counts["it"] = it_score

    # Non-Latin scripts take absolute priority
    non_latin = {k: v for k, v in counts.items() if k not in {"en", "fr", "de", "es", "pt", "tr", "it"}}
    best_non_latin = max(non_latin, key=lambda k: non_latin[k])
    if non_latin[best_non_latin] >= 5:  # threshold: at least 5 script characters
        return best_non_latin

    # For Latin-based: pick highest scoring language
    latin_langs = {k: counts[k] for k in ("en", "fr", "de", "es", "pt", "tr", "it")}
    best_latin = max(latin_langs, key=lambda k: latin_langs[k])
    if latin_langs[best_latin] > 0:
        return best_latin

    return "en"  # safe default


def make_tts_keyboard(lang_code: str, text: str = "") -> InlineKeyboardMarkup:
    """Build voice selection buttons — shows ONLY the detected language's voices first,
    then offers a quick switch to the most common alternative languages."""
    buttons: list[tuple[str, str]] = []

    # Primary: voices for detected language
    lang_info = VOICE_CATALOG.get(lang_code, VOICE_CATALOG["en"])
    for label, voice_name in lang_info["voices"]:
        buttons.append((label, f"do:tts:{voice_name}"))

    # Secondary row: language switcher buttons (skip detected lang, pick top 3 alternatives)
    alt_order = ["bn", "en", "hi", "ar", "ur", "fr", "de", "es", "zh", "ja", "ko", "ru", "tr", "pt", "it"]
    alts_shown = 0
    for alt in alt_order:
        if alt == lang_code or alts_shown >= 3:
            continue
        info = VOICE_CATALOG.get(alt)
        if info:
            # Use flag + short name to save space
            short = info["name"].split("(")[0].strip()[:10]
            buttons.append((f"{info['flag']} {short}", f"do:tts:lang:{alt}"))
            alts_shown += 1

    return make_keyboard(buttons, row_size=2)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if (
        not query.from_user
        or (
            query.from_user.id != OWNER_USER_ID
            and query.from_user.id not in ALLOWED_USERS
        )
    ):
        await query.answer("Not authorized", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    user_data = context.user_data or {}
    # ── Temp Mail callbacks ──────────────────────────────────────────────────────
    if parts[0] == "do" and parts[1] == "tempmail" and len(parts) >= 3:
        sub_action = parts[2]
        extra_id = parts[3] if len(parts) > 3 else None
        await handle_tempmail_callback(query, context, sub_action, extra_id)
        return

    # ── Transcript format selector ───────────────────────────────────────────────
    if parts[0] == "do" and len(parts) == 3 and parts[1] == "transcript":
        fmt = parts[2]
        transcript_text = user_data.get("last_transcript_text", "")
        segments = user_data.get("last_transcript_segments", [])
        if not transcript_text and not segments:
            if query.message:
                await query.message.reply_text("No transcript found. Please transcribe a file first.")
            return
        outdir = user_dir(query.from_user.id)
        stem = f"{time.time_ns()}_transcript"
        try:
            if fmt in {"txt", "all"}:
                txt_path = outdir / f"{stem}.txt"
                txt_path.write_text(transcript_text, encoding="utf-8")
                asyncio.create_task(remove_later(txt_path))
                if query.message:
                    await query.message.reply_document(document=str(txt_path), caption="📄 Plain Text transcript")
            if fmt in {"srt", "all"} and segments:
                srt_lines = []
                for index, segment in enumerate(segments, 1):
                    start = float(segment.get("start", 0))
                    end = float(segment.get("end", start + 0.5))
                    if end <= start:
                        end = start + 0.5
                    caption = str(segment.get("text", "")).strip()
                    srt_lines.extend([str(index), f"{subtitle_timestamp(start)} --> {subtitle_timestamp(end)}", caption, ""])
                srt_path = outdir / f"{stem}.srt"
                srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
                asyncio.create_task(remove_later(srt_path))
                if query.message:
                    await query.message.reply_document(document=str(srt_path), caption="📋 SRT subtitles (CapCut)")
            if fmt in {"vtt", "all"} and segments:
                vtt_lines = ["WEBVTT", ""]
                for segment in segments:
                    start = float(segment.get("start", 0))
                    end = float(segment.get("end", start + 0.5))
                    if end <= start:
                        end = start + 0.5
                    caption = str(segment.get("text", "")).strip()
                    vtt_lines.extend([f"{subtitle_timestamp(start, vtt=True)} --> {subtitle_timestamp(end, vtt=True)}", caption, ""])
                vtt_path = outdir / f"{stem}.vtt"
                vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
                asyncio.create_task(remove_later(vtt_path))
                if query.message:
                    await query.message.reply_document(document=str(vtt_path), caption="🌐 WebVTT subtitles")
        except Exception as exc:
            log.exception("Transcript format delivery failed")
            if query.message:
                await query.message.reply_text(f"Failed to send transcript: {str(exc)[:600]}")
        return
    # ── Speed factor selector ────────────────────────────────────────────────────
    if parts[0] == "do" and parts[1] == "speed" and len(parts) == 3:
        try:
            factor = float(parts[2])
        except ValueError:
            if query.message:
                await query.message.reply_text("Invalid speed value.")
            return
        if context.user_data is not None:
            context.user_data["speed_factor"] = factor
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the file again; temporary files expire after 30 minutes.")
            return
        outdir = user_dir(query.from_user.id)
        atempo = min(max(factor, 0.5), 4.0)
        out = outdir / f"{src.stem}_speed{atempo}x{src.suffix or '.mp4'}"
        mime, _ = detect_type(src)
        if mime.startswith("video/"):
            setpts = f"{1.0/atempo:.4f}"
            args = ["-i", str(src), "-filter_complex", f"[0:v]setpts={setpts}*PTS[v];[0:a]atempo={atempo}[a]", "-map", "[v]", "-map", "[a]", "-preset", "veryfast", str(out)]
        else:
            args = ["-i", str(src), "-filter:a", f"atempo={atempo}", str(out)]
        try:
            if query.message:
                await query.message.reply_text(f"⏩ Applying {atempo}x speed…")
            async with MAX_HEAVY_JOBS:
                await asyncio.to_thread(ffmpeg_run, args)
            await send_output(query.message, out, f"⏩ Speed adjusted to {atempo}x.")
        except Exception as exc:
            log.exception("Speed operation failed")
            if query.message:
                await query.message.reply_text(f"Speed operation failed: {str(exc)[:700]}")
        return
    if parts[0] == "do" and len(parts) >= 3 and parts[1] == "tts":
        action_or_voice = parts[2]
        text = user_data.get("tts_text") or user_data.get("last_text")
        if not text:
            raw = user_data.get("last_file")
            if raw and Path(raw).exists():
                try:
                    text = await asyncio.to_thread(extract_text, Path(raw))
                except Exception:
                    pass
        if not text:
            if query.message:
                await query.message.reply_text("Send text or upload a text file first.")
            return
        if action_or_voice == "select":
            lang_code = detect_text_language(text)
            lang_info = VOICE_CATALOG.get(lang_code, VOICE_CATALOG["en"])
            flag = lang_info["flag"]
            lang_name = lang_info["name"]
            if query.message:
                await query.message.reply_text(
                    f"🔍 Detected language: {flag} {lang_name}\n\n"
                    f"🗣️ Choose a voice (bottom row = switch language):",
                    reply_markup=make_tts_keyboard(lang_code, text),
                )
            return

        # ── Language switcher: do:tts:lang:<code> ──────────────────────────────
        if action_or_voice == "lang" and len(parts) == 4:
            switch_lang = parts[3]
            lang_info = VOICE_CATALOG.get(switch_lang, VOICE_CATALOG["en"])
            flag = lang_info["flag"]
            lang_name = lang_info["name"]
            if query.message:
                await query.message.reply_text(
                    f"{flag} Switched to {lang_name}. Choose a voice:",
                    reply_markup=make_tts_keyboard(switch_lang, text),
                )
            return

        voice_name = action_or_voice
        if query.message:
            await query.message.reply_text("🔊 Generating speech audio…")
        try:
            import edge_tts
        except ImportError:
            if query.message:
                await query.message.reply_text("Install edge-tts first: pip install edge-tts")
            return
        outdir = user_dir(query.from_user.id)
        out = outdir / f"{time.time_ns()}_tts.mp3"
        try:
            async with MAX_HEAVY_JOBS:
                communicate = edge_tts.Communicate(text[:5000], voice_name)
                await communicate.save(str(out))
            if query.message:
                await query.message.reply_voice(voice=str(out), caption=f"🔊 Voice: {voice_name}")
            asyncio.create_task(remove_later(out))
        except Exception as exc:
            log.exception("TTS generation failed")
            if query.message:
                await query.message.reply_text(f"TTS failed: {str(exc)[:600]}")
        return

    # ── Dynamic AI Translation Language Selector ─────────────────────────────────
    if parts[0] == "do" and parts[1] == "ai" and len(parts) >= 3 and parts[2] == "translate":
        if len(parts) == 3 or parts[3] == "select":
            if query.message:
                await query.message.reply_text(
                    "🌐 Select Target Language for AI Translation:",
                    reply_markup=make_keyboard([
                        ("🇧🇩 বাংলা (Bengali)", "do:ai:translate:Bengali"),
                        ("🇺🇸 English", "do:ai:translate:English"),
                        ("🇸🇦 العربية (Arabic)", "do:ai:translate:Arabic"),
                        ("🇮🇳 हिन्दी (Hindi)", "do:ai:translate:Hindi"),
                        ("🇪🇸 Español (Spanish)", "do:ai:translate:Spanish"),
                        ("🇫🇷 Français (French)", "do:ai:translate:French"),
                        ("🇩🇪 Deutsch (German)", "do:ai:translate:German"),
                        ("🇷🇺 Русский (Russian)", "do:ai:translate:Russian"),
                    ], row_size=2)
                )
            return
        target_lang = parts[3]
        if context.user_data is not None:
            context.user_data["translate_target_lang"] = target_lang
            context.user_data["ai_task"] = "translate"
        text = user_data.get("last_text")
        if not text:
            raw = user_data.get("last_file")
            if raw and Path(raw).exists():
                try:
                    text = await asyncio.to_thread(extract_text, Path(raw))
                except Exception:
                    pass
        if not text:
            if query.message:
                await query.message.reply_text("Send text or upload a text file first.")
            return
        if query.message:
            await query.message.reply_text(f"🌐 Translating to {target_lang}…")
        await do_text_action(update, context, "ai", text)
        return

    # ── Dynamic Image Resize Target Selector & Handler ───────────────────────────
    if parts[0] == "do" and parts[1] == "img" and parts[2] == "resize":
        if len(parts) == 3:
            if query.message:
                await query.message.reply_text(
                    "📐 Select target resolution or ratio:",
                    reply_markup=make_keyboard([
                        ("📱 1080p FHD", "do:img:resize:1080p"),
                        ("🖥️ 720p HD", "do:img:resize:720p"),
                        ("🔲 1:1 Square (1080×1080)", "do:img:resize:square"),
                        ("📉 50% Half Size", "do:img:resize:50pct"),
                        ("📉 25% Quarter Size", "do:img:resize:25pct"),
                    ], row_size=2)
                )
            return
        if len(parts) == 4:
            target = parts[3]
            src_raw = user_data.get("last_file")
            src = Path(src_raw) if src_raw else None
            if not src or not src.exists():
                if query.message:
                    await query.message.reply_text("Please upload the image again.")
                return
            outdir = user_dir(query.from_user.id)
            if query.message:
                await query.message.reply_text(f"📐 Resizing image ({target})…")
            try:
                out, _ = await asyncio.to_thread(process_image, src, "resize", outdir, resize_target=target)
                await send_output(query.message, out, f"📐 Resized ({target}).")
            except Exception as exc:
                log.exception("Resize failed")
                if query.message:
                    await query.message.reply_text(f"Resize failed: {str(exc)[:600]}")
            return

    # ── Dynamic OCR Language Selector & Handler ──────────────────────────────────
    if parts[0] == "do" and parts[1] == "img" and parts[2] == "ocr":
        if query.message:
            await query.message.reply_text(
                "🔍 Select OCR Language:",
                reply_markup=make_keyboard([
                    ("🇬🇧 English", "do:ocr:eng"),
                    ("🇧🇩 বাংলা (Bengali)", "do:ocr:ben"),
                    ("🌐 English + বাংলা", "do:ocr:eng+ben"),
                    ("🇮🇳 हिन्दी (Hindi)", "do:ocr:hin"),
                    ("🇸🇦 العربية (Arabic)", "do:ocr:ara"),
                ], row_size=2)
            )
        return

    if parts[0] == "do" and parts[1] == "ocr" and len(parts) == 3:
        lang = parts[2]
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the image again.")
            return
        outdir = user_dir(query.from_user.id)
        if query.message:
            await query.message.reply_text(f"🔍 Reading text with language `{lang}`…")
        try:
            _, result = await asyncio.to_thread(process_image, src, "ocr", outdir, ocr_lang=lang)
            text_out = (result or "No text detected.").strip()
            if len(text_out) <= 3900:
                await query.message.reply_text(f"🔍 Extracted Text ({lang}):\n\n{text_out}")
            else:
                txt_file = outdir / f"{src.stem}_ocr_{lang}.txt"
                txt_file.write_text(text_out, encoding="utf-8")
                await send_output(query.message, txt_file, f"🔍 Extracted Text ({lang})")
        except Exception as exc:
            log.exception("OCR failed")
            if query.message:
                await query.message.reply_text(f"OCR failed: {str(exc)[:600]}")
        return

    # ── Dynamic Image Watermark Options ──────────────────────────────────────────
    if parts[0] == "do" and parts[1] == "img" and parts[2] == "watermark":
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the image again.")
            return
        if query.message:
            await query.message.reply_text(
                "💧 Watermark Options:\n\n"
                "• Tap **Default** for 'CONFIDENTIAL'\n"
                "• Or tap **Custom Text** to type your brand, name, or @channel:",
                reply_markup=make_keyboard([
                    ("⚡ Default (CONFIDENTIAL)", "do:wm:default"),
                    ("✏️ Custom Watermark Text…", "do:wm:ask"),
                ], row_size=1)
            )
        return

    if parts[0] == "do" and parts[1] == "wm" and len(parts) == 3:
        sub_action = parts[2]
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the image again.")
            return
        outdir = user_dir(query.from_user.id)
        if sub_action == "default":
            try:
                out, _ = await asyncio.to_thread(process_image, src, "watermark", outdir, watermark_text="CONFIDENTIAL")
                await send_output(query.message, out, "💧 Watermark applied ('CONFIDENTIAL').")
            except Exception as exc:
                log.exception("Watermark failed")
                if query.message:
                    await query.message.reply_text(f"Watermark failed: {str(exc)[:600]}")
        elif sub_action == "ask":
            if context.user_data is not None:
                context.user_data["awaiting_watermark"] = True
                context.user_data["watermark_pending_file"] = str(src)
            if query.message:
                await query.message.reply_text("💧 Please send the text you want as watermark:")
        return

    # ── Dynamic Audio / Video Trim ───────────────────────────────────────────────
    if parts[0] == "do" and parts[1] == "media" and parts[2] == "trim":
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the file again.")
            return
        if context.user_data is not None:
            context.user_data["awaiting_trim"] = True
            context.user_data["trim_pending_file"] = str(src)
        if query.message:
            await query.message.reply_text(
                "✂️ Send the start and end timestamp to trim:\n\n"
                "Examples:\n"
                "• `00:05 00:20`\n"
                "• `10 45` (in seconds)"
            )
        return

    # ── Dynamic Video to GIF Duration Selector ───────────────────────────────────
    if parts[0] == "do" and parts[1] == "media" and parts[2] == "gif":
        if query.message:
            await query.message.reply_text(
                "🎬 Select GIF Duration:",
                reply_markup=make_keyboard([
                    ("⏱️ First 5 sec", "do:gif:5"),
                    ("⏱️ First 10 sec", "do:gif:10"),
                    ("⏱️ First 15 sec", "do:gif:15"),
                ], row_size=3)
            )
        return

    if parts[0] == "do" and parts[1] == "gif" and len(parts) == 3:
        duration_sec = parts[2]
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None
        if not src or not src.exists():
            if query.message:
                await query.message.reply_text("Please upload the file again.")
            return
        outdir = user_dir(query.from_user.id)
        out = outdir / f"{src.stem}.gif"
        args = ["-i", str(src), "-t", duration_sec, "-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0", str(out)]
        if query.message:
            await query.message.reply_text(f"🎬 Creating {duration_sec}s animated GIF…")
        try:
            async with MAX_HEAVY_JOBS:
                await asyncio.to_thread(ffmpeg_run, args)
            await send_output(query.message, out, f"🎬 Animated GIF ({duration_sec}s).")
        except Exception as exc:
            log.exception("GIF conversion failed")
            if query.message:
                await query.message.reply_text(f"GIF failed: {str(exc)[:600]}")
        return

    # ── Privacy & Metadata Toolkit (View / Delete / Customise) ───────────────────
    if parts[0] == "do" and parts[1] == "meta":
        sub_cmd = parts[2]
        group = parts[3] if len(parts) > 3 else "img"
        src_raw = user_data.get("last_file")
        src = Path(src_raw) if src_raw else None

        if sub_cmd == "menu":
            if not src or not src.exists():
                if query.message:
                    await query.message.reply_text("Please upload the file again.")
                return
            if query.message:
                await query.message.reply_text(
                    "🛡️ **Privacy & Metadata Toolkit:**\n\n"
                    "Select an option:\n"
                    "• **👁️ View Metadata** — Inspect camera, date, software, and GPS location\n"
                    "• **🗑️ Delete All** — 100% anonymous, instant purge of all metadata\n"
                    "• **✏️ Customise** — Disguise as DSLR/iPhone, custom author, or reset date",
                    parse_mode="Markdown",
                    reply_markup=make_metadata_menu(group),
                )
            return

        if sub_cmd == "view":
            if not src or not src.exists():
                if query.message:
                    await query.message.reply_text("Please upload the file again.")
                return
            if group == "img":
                info = await asyncio.to_thread(inspect_image_exif, src)
                report = format_metadata_report(info, is_media=False)
            else:
                info = await asyncio.to_thread(inspect_media_metadata, src)
                report = format_metadata_report(info, is_media=True)
            if query.message:
                await query.message.reply_text(
                    report,
                    parse_mode="Markdown",
                    disable_web_page_preview=False,
                    reply_markup=make_metadata_menu(group),
                )
            return

        if sub_cmd == "delete":
            if not src or not src.exists():
                if query.message:
                    await query.message.reply_text("Please upload the file again.")
                return
            outdir = user_dir(query.from_user.id)
            if group == "img":
                if query.message:
                    await query.message.reply_text("🗑️ Purging all image metadata, device info, and GPS coordinates…")
                try:
                    out, _ = await asyncio.to_thread(strip_image_metadata, src, outdir)
                    await send_output(query.message, out, "✅ All EXIF & GPS metadata deleted! 100% anonymous.")
                except Exception as exc:
                    log.exception("Image metadata strip failed")
                    if query.message:
                        await query.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
            else:
                if query.message:
                    await query.message.reply_text("🗑️ Purging all media metadata and author tags…")
                try:
                    out = await asyncio.to_thread(strip_media_metadata, src, outdir)
                    await send_output(query.message, out, "✅ All media metadata deleted! 100% anonymous.")
                except Exception as exc:
                    log.exception("Media metadata strip failed")
                    if query.message:
                        await query.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
            return

        if sub_cmd == "cust_menu":
            if query.message:
                await query.message.reply_text(
                    "✏️ **Customise & Disguise Metadata:**\n\n"
                    "Choose how you want to disguise or customize this file's metadata:",
                    parse_mode="Markdown",
                    reply_markup=make_customise_menu(group),
                )
            return

        if sub_cmd == "set":
            setting = parts[3] if len(parts) > 3 else ""
            if not src or not src.exists():
                if query.message:
                    await query.message.reply_text("Please upload the file again.")
                return
            outdir = user_dir(query.from_user.id)

            if setting == "dslr":
                if query.message:
                    await query.message.reply_text("🎭 Disguising image as Sony Alpha 7 IV (DSLR)…")
                try:
                    out = await asyncio.to_thread(customize_image_metadata, src, outdir, "Sony", "ILCE-7M4", "Pro Photographer")
                    await send_output(query.message, out, "🎭 Disguised: Camera set to Sony Alpha 7 IV, GPS removed.")
                except Exception as exc:
                    if query.message:
                        await query.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
                return

            if setting == "iphone":
                if query.message:
                    await query.message.reply_text("🎭 Disguising image as Apple iPhone 15 Pro…")
                try:
                    out = await asyncio.to_thread(customize_image_metadata, src, outdir, "Apple", "iPhone 15 Pro", "Apple User")
                    await send_output(query.message, out, "🎭 Disguised: Camera set to iPhone 15 Pro, GPS removed.")
                except Exception as exc:
                    if query.message:
                        await query.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
                return

            if setting == "today":
                now_str = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")
                try:
                    out = await asyncio.to_thread(customize_image_metadata, src, outdir, date_str=now_str)
                    await send_output(query.message, out, f"📅 Date/Time reset to current time: `{now_str}`")
                except Exception as exc:
                    if query.message:
                        await query.message.reply_text(f"❌ Failed: {str(exc)[:600]}")
                return

            if setting == "ask_author":
                if context.user_data is not None:
                    context.user_data["awaiting_meta_author"] = True
                    context.user_data["meta_pending_file"] = str(src)
                if query.message:
                    await query.message.reply_text("👤 Please send the Author/Artist name you want embedded in the image:")
                return

            if setting == "ask_media":
                if context.user_data is not None:
                    context.user_data["awaiting_meta_media"] = True
                    context.user_data["meta_pending_file"] = str(src)
                if query.message:
                    await query.message.reply_text("🎬 Send the custom title and artist:\nFormat: `Title - Artist` (e.g. `My Video - Rob`)")
                return
        return

    if parts[0] == "do" and len(parts) == 3 and parts[1] in {"text", "ai"}:
        group, action = parts[1], parts[2]
        text = user_data.get("last_text")
        if not text and group == "text":
            raw = user_data.get("last_file")
            if raw and Path(raw).exists():
                try:
                    text = await asyncio.to_thread(extract_text, Path(raw))
                except Exception as exc:
                    if query.message:
                        await query.message.reply_text(f"Could not read text from that file: {str(exc)[:600]}")
                    return
        if not text:
            if query.message:
                await query.message.reply_text("Send text or upload a supported text file first.")
            return
        if group == "ai":
            if context.user_data is not None:
                context.user_data["ai_task"] = action
            action = "ai"
        try:
            await do_text_action(update, context, action, text)
        except Exception as exc:
            log.exception("Text callback failed")
            if query.message:
                await query.message.reply_text(f"Operation failed: {str(exc)[:700]}")
        return
    if len(parts) != 3 or parts[0] != "do":
        if query.message:
            await query.message.reply_text("Unknown action.")
        return
    _, group, action = parts
    src_raw = user_data.get("last_file")
    src = Path(src_raw) if src_raw else None
    if not src or not src.exists():
        if query.message:
            await query.message.reply_text("Please upload the file again; temporary files expire after 30 minutes.")
        return
    outdir = user_dir(query.from_user.id)
    try:
        async with MAX_HEAVY_JOBS:
            if group == "file" and action == "info":
                mime, ext = detect_type(src)
                await query.message.reply_text(f"Name: {src.name}\nType: {mime}\nExtension: {ext or 'unknown'}\nSize: {format_size(src.stat().st_size)}")
                return
            if group == "file" and action == "hash":
                digest = await asyncio.to_thread(lambda: hashlib.sha256(src.read_bytes()).hexdigest())
                await query.message.reply_text(f"SHA-256:\n{digest}")
                return
            if group == "img":
                if action == "ocr":
                    _, result = await asyncio.to_thread(process_image, src, action, outdir)
                    await query.message.reply_text((result or "No text found.")[:3900])
                    return
                out, _ = await asyncio.to_thread(process_image, src, action, outdir)
                await send_output(query.message, out, "Image processed.")
                return
            if group == "qr" and action == "read":
                image = cv2.imread(str(src))
                if image is None:
                    raise ValueError("Could not read this image.")
                result, points, _ = cv2.QRCodeDetector().detectAndDecode(image)
                await query.message.reply_text(result or "No QR code detected.")
                return
            if group == "pdf":
                if action == "text":
                    _, result = await asyncio.to_thread(process_pdf, src, action, outdir)
                    if len(result or "") <= 3900:
                        await query.message.reply_text(result or "No text found.")
                    else:
                        out = outdir / f"{src.stem}_text.txt"
                        out.write_text(result or "", encoding="utf-8")
                        await send_output(query.message, out, "Extracted PDF text.")
                    return
                if action == "protect":
                    password = user_data.get("pdf_password")
                    if not password:
                        # No password set — prompt user to send one as a text message
                        if context.user_data is not None:
                            context.user_data["awaiting_pdf_action"] = "protect"
                            context.user_data["pdf_pending_file"] = str(src)
                        if query.message:
                            await query.message.reply_text(
                                "🔒 Please send the password as a text message. "
                                "Your next text message will be used as the PDF password. "
                                "Then tap the button again.\n\n"
                                "Alternatively, use /protect <password> to pre-set a password."
                            )
                        return
                    out, msg = await asyncio.to_thread(process_pdf_with_password, src, "protect", outdir, password)
                    if out:
                        await send_output(query.message, out, msg or f"🔒 PDF protected with password: {password}")
                    else:
                        if query.message:
                            await query.message.reply_text(msg or "🔒 PDF protected.")
                    return
                if action == "unlock":
                    password = user_data.get("pdf_password")
                    if not password:
                        # No password set — prompt user to send one as a text message
                        if context.user_data is not None:
                            context.user_data["awaiting_pdf_action"] = "unlock"
                            context.user_data["pdf_pending_file"] = str(src)
                        if query.message:
                            await query.message.reply_text(
                                "🔓 Please send the password as a text message. "
                                "Your next text message will be used as the PDF unlock password. "
                                "Then tap the button again.\n\n"
                                "Alternatively, use /unlock <password> to pre-set a password."
                            )
                        return
                    out, _ = await asyncio.to_thread(process_pdf_with_password, src, "unlock", outdir, password)
                    await send_output(query.message, out, "🔓 PDF unlocked successfully.")
                    return
                out, _ = await asyncio.to_thread(process_pdf, src, action, outdir)
                await send_output(query.message, out, "PDF processed.")
                return
            if group == "media":
                if action == "transcribe":
                    # release semaphore before helper acquires it
                    pass
                elif action in {"mp3", "compress", "extract", "vcompress", "gif", "speed", "trim"}:
                    pass
                else:
                    raise ValueError("Unknown media operation.")
        if group == "media" and action == "transcribe":
            await do_transcribe(update, context, src)
            return
        if group == "media":
            if action == "gif":
                out = outdir / f"{src.stem}.gif"
                args = ["-i", str(src), "-t", "10", "-vf", "fps=12,scale=480:-1:flags=lanczos", "-loop", "0", str(out)]
            elif action == "speed":
                speed_factor = user_data.get("speed_factor")
                if speed_factor is None:
                    # No speed set — show speed selector keyboard
                    speed_keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("🐢 0.75x", callback_data="do:speed:0.75"),
                            InlineKeyboardButton("🚶 1.25x", callback_data="do:speed:1.25"),
                        ],
                        [
                            InlineKeyboardButton("🚀 1.5x", callback_data="do:speed:1.5"),
                            InlineKeyboardButton("⚡ 2.0x", callback_data="do:speed:2.0"),
                            InlineKeyboardButton("🔥 3.0x", callback_data="do:speed:3.0"),
                        ],
                    ])
                    if query.message:
                        await query.message.reply_text("⏩ Choose a playback speed:", reply_markup=speed_keyboard)
                    return
                speed = speed_factor
                atempo = min(max(speed, 0.5), 4.0)
                out = outdir / f"{src.stem}_speed{atempo}x{src.suffix or '.mp4'}"
                mime, ext = detect_type(src)
                if mime.startswith("video/"):
                    setpts = f"{1.0/atempo:.4f}"
                    args = ["-i", str(src), "-filter_complex", f"[0:v]setpts={setpts}*PTS[v];[0:a]atempo={atempo}[a]", "-map", "[v]", "-map", "[a]", "-preset", "veryfast", str(out)]
                else:
                    args = ["-i", str(src), "-filter:a", f"atempo={atempo}", str(out)]
            elif action in {"mp3", "compress"}:
                out = outdir / f"{src.stem}_{action}.mp3"
                args = ["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-b:a", "96k" if action == "compress" else "192k", str(out)]
            elif action == "extract":
                out = outdir / f"{src.stem}_{action}.mp3"
                args = ["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-q:a", "4", str(out)]
            else:
                out = outdir / f"{src.stem}_{action}.mp4"
                args = ["-i", str(src), "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k", str(out)]
            async with MAX_HEAVY_JOBS:
                await asyncio.to_thread(ffmpeg_run, args)
            await send_output(query.message, out, "Media processed.")
            return
        if group == "zip" and action == "zip":
            out = outdir / f"{src.name}.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(src, arcname=src.name)
            await send_output(query.message, out, "ZIP archive created.")
            return
        if group == "zip" and action == "unzip":
            folder, files = await asyncio.to_thread(safely_unzip, src, outdir)
            if not files:
                await query.message.reply_text("The archive is empty.")
            else:
                await query.message.reply_text(f"Extracted {len(files)} files. Sending files individually (maximum 10).")
                for item in files[:10]:
                    await send_output(query.message, item, item.name)
            return
        if group == "text":
            text = await asyncio.to_thread(extract_text, src)
            await do_text_action(update, context, action, text)
            return
        await query.message.reply_text("That action is not available for this file.")
    except Exception as exc:
        log.exception("Action %s failed", data)
        await query.message.reply_text(f"Operation failed: {str(exc)[:900]}")


async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    value = " ".join(context.args).strip()
    if not value:
        await update.effective_message.reply_text("Usage: /qr text or URL")
        return
    image = qrcode.make(value)
    out = user_dir(update.effective_user.id) / f"{time.time_ns()}_qr.png"
    image.save(out)
    await send_output(update.effective_message, out, "QR code")


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    args = context.args or []
    if len(args) < 2:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /ai summarize|polish|analyze|translate <text>")
        return
    task = args[0].lower()
    text = " ".join(args[1:])
    if task not in {"summarize", "polish", "analyze", "translate"}:
        if update.effective_message:
            await update.effective_message.reply_text("Choose summarize, polish, analyze, or translate.")
        return
    context.user_data["last_text"] = text
    context.user_data["ai_task"] = task
    await do_text_action(update, context, "ai", text)


async def cmd_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    raw = (context.user_data or {}).get("last_file")
    if not raw or not Path(raw).exists():
        if update.effective_message:
            await update.effective_message.reply_text("Upload an audio or video file first (maximum 20 MB).")
        return
    try:
        await do_transcribe(update, context, Path(raw))
    except Exception as exc:
        log.exception("Transcription failed")
        if update.effective_message:
            await update.effective_message.reply_text(f"Transcription failed: {str(exc)[:800]}")


async def cmd_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    raw = (context.user_data or {}).get("last_file")
    if not raw or not Path(raw).exists():
        if update.effective_message:
            await update.effective_message.reply_text("Upload audio or video first, then use /trim <start> <end>, e.g. /trim 00:05 00:20")
        return
    args = context.args or []
    if len(args) != 2:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /trim <start> <end>, e.g. /trim 00:05 00:20")
        return
    src = Path(raw)
    out = user_dir(update.effective_user.id) / f"{src.stem}_trimmed{src.suffix or '.mp4'}"
    try:
        async with MAX_HEAVY_JOBS:
            await asyncio.to_thread(ffmpeg_run, ["-ss", args[0], "-to", args[1], "-i", str(src), "-c", "copy", str(out)])
        await send_output(update.effective_message, out, "Trimmed media.")
    except Exception as exc:
        log.exception("Trim failed")
        if update.effective_message:
            await update.effective_message.reply_text(f"Trim failed: {str(exc)[:800]}")


async def cmd_mergepdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    for raw in context.user_data.get("merge_files", []):
        Path(raw).unlink(missing_ok=True)
    context.user_data["merge_mode"] = True
    context.user_data["merge_files"] = []
    context.user_data.pop("last_text", None)
    await update.effective_message.reply_text("PDF merge mode started. Send at least two PDF files, then use /finishmerge. Use /cancelmerge to stop.")


async def cmd_finishmerge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    files = [Path(raw) for raw in context.user_data.get("merge_files", []) if Path(raw).exists()]
    if len(files) < 2:
        await update.effective_message.reply_text(f"I have {len(files)} PDF file(s). Send at least two, then use /finishmerge.")
        return
    try:
        writer = PdfWriter()
        for path in files:
            for page in PdfReader(str(path)).pages:
                writer.add_page(page)
        out = user_dir(update.effective_user.id) / "merged.pdf"
        with out.open("wb") as output:
            writer.write(output)
        context.user_data["merge_mode"] = False
        context.user_data["merge_files"] = []
        await send_output(update.effective_message, out, f"Merged {len(files)} PDFs.")
        for path in files:
            path.unlink(missing_ok=True)
    except Exception as exc:
        log.exception("PDF merge failed")
        await update.effective_message.reply_text(f"Could not merge PDFs: {str(exc)[:800]}")


async def cmd_cancelmerge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    for raw in context.user_data.get("merge_files", []):
        Path(raw).unlink(missing_ok=True)
    context.user_data["merge_files"] = []
    context.user_data["merge_mode"] = False
    await update.effective_message.reply_text("PDF merge canceled; temporary merge files were removed.")


def convert_units(value: float, unit: str) -> str:
    unit = unit.lower()
    conversions = {
        "km": ("mi", lambda x: x * 0.621371), "mi": ("km", lambda x: x / 0.621371),
        "kg": ("lb", lambda x: x * 2.20462), "lb": ("kg", lambda x: x / 2.20462),
        "cm": ("in", lambda x: x / 2.54), "in": ("cm", lambda x: x * 2.54),
        "c": ("f", lambda x: x * 9 / 5 + 32), "f": ("c", lambda x: (x - 32) * 5 / 9),
        "s": ("min", lambda x: x / 60), "sec": ("min", lambda x: x / 60),
        "min": ("s", lambda x: x * 60), "h": ("min", lambda x: x * 60),
        "hr": ("min", lambda x: x * 60), "d": ("h", lambda x: x * 24),
        "day": ("h", lambda x: x * 24),
    }
    if unit not in conversions:
        raise ValueError("Supported units: km, mi, kg, lb, cm, in, C, F, s, sec, min, h, hr, d, day.")
    to_unit, converter = conversions[unit]
    return f"{value:g} {unit} = {converter(value):.2f} {to_unit}"


async def cmd_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    args = context.args or []
    try:
        if len(args) != 2:
            raise ValueError("Usage: /convert <value> <unit>, e.g. /convert 10 km")
        result = convert_units(float(args[0]), args[1])
        if update.effective_message:
            await update.effective_message.reply_text(result)
    except Exception as exc:
        if update.effective_message:
            await update.effective_message.reply_text(str(exc))


# ── Feature: Text-to-Speech (TTS) ──────────────────────────────────────────────

async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect text language and present voice buttons to generate speech."""
    if not await authorized(update):
        return
    args = context.args or []
    text = " ".join(args).strip()
    user_data = context.user_data or {}
    if not text:
        last = user_data.get("last_text", "").strip()
        if last:
            text = last
        else:
            if update.effective_message:
                await update.effective_message.reply_text("Usage: /tts <text> or send text first, then /tts")
            return
    if context.user_data is not None:
        context.user_data["tts_text"] = text
        context.user_data["last_text"] = text

    lang_code = detect_text_language(text)
    lang_info = VOICE_CATALOG.get(lang_code, VOICE_CATALOG["en"])
    flag = lang_info["flag"]
    lang_name = lang_info["name"]

    if update.effective_message:
        await update.effective_message.reply_text(
            f"Detected language: {flag} {lang_name}\n\n"
            f"🗣️ Choose a voice to generate audio:",
            reply_markup=make_tts_keyboard(lang_code),
        )


# ── Feature: Reminder System ───────────────────────────────────────────────────

def parse_duration(text: str) -> Optional[int]:
    """Parse a human-friendly duration like '30m', '2h', '1h30m', '90s' into seconds."""
    pattern = re.compile(r"(?:(\d+)\s*h)?[\s]*(?:(\d+)\s*m)?[\s]*(?:(\d+)\s*s)?", re.IGNORECASE)
    match = pattern.fullmatch(text.strip())
    if not match or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


async def reminder_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fire a reminder notification."""
    job = context.job
    if not job or not job.chat_id:
        return
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"⏰ Reminder: {job.data}",
    )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a one-shot reminder: /remind 30m Take medicine"""
    if not await authorized(update):
        return
    args = context.args or []
    if len(args) < 2:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Usage: /remind <duration> <message>\n"
                "Examples: /remind 30m Take medicine\n"
                "         /remind 2h Call the office\n"
                "         /remind 1h30m Check oven"
            )
        return
    if context.job_queue is None or not update.effective_chat or not update.effective_user:
        if update.effective_message:
            await update.effective_message.reply_text("Job queue or chat is not available.")
        return
    duration_text = args[0]
    seconds = parse_duration(duration_text)
    if not seconds:
        if update.effective_message:
            await update.effective_message.reply_text("Invalid duration. Use formats like 30s, 10m, 2h, or 1h30m.")
        return
    if seconds > 86400:
        if update.effective_message:
            await update.effective_message.reply_text("Reminders are limited to 24 hours maximum.")
        return
    message = " ".join(args[1:])
    context.job_queue.run_once(
        reminder_callback,
        when=seconds,
        chat_id=update.effective_chat.id,
        data=message,
        name=f"remind_{update.effective_user.id}_{time.time_ns()}",
    )
    if seconds >= 3600:
        human = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    elif seconds >= 60:
        human = f"{seconds // 60}m {seconds % 60}s"
    else:
        human = f"{seconds}s"
    if update.effective_message:
        await update.effective_message.reply_text(f"✅ Reminder set for {human} from now: {message}")


# ── Feature: Currency Converter ────────────────────────────────────────────────

async def cmd_fx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert currencies: /fx 100 USD BDT"""
    if not await authorized(update):
        return
    args = context.args or []
    if len(args) != 3:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /fx <amount> <from_currency> <to_currency>\nExample: /fx 100 USD BDT")
        return
    try:
        amount = float(args[0])
        from_cur = args[1].upper()
        to_cur = args[2].upper()
    except ValueError:
        if update.effective_message:
            await update.effective_message.reply_text("Invalid amount. Use a number.")
        return
    try:
        import urllib.request
        api_url = f"https://open.er-api.com/v6/latest/{from_cur}"
        with urllib.request.urlopen(api_url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") != "success":
            raise ValueError(f"API error: {data.get('error-type', 'unknown')}")
        rates = data.get("rates", {})
        if to_cur not in rates:
            raise ValueError(f"Unknown currency: {to_cur}")
        converted = amount * rates[to_cur]
        rate = rates[to_cur]
        if update.effective_message:
            await update.effective_message.reply_text(
                f"💱 {amount:,.2f} {from_cur} = {converted:,.2f} {to_cur}\n"
                f"Rate: 1 {from_cur} = {rate:,.4f} {to_cur}\n"
                f"Updated: {data.get('time_last_update_utc', 'N/A')}"
            )
    except Exception as exc:
        log.exception("Currency conversion failed")
        if update.effective_message:
            await update.effective_message.reply_text(f"Currency conversion failed: {str(exc)[:600]}")


# ── Feature: Webpage Article Reader ────────────────────────────────────────────

def fetch_article_text(url: str) -> str:
    """Extract readable article text from a URL."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            raise ValueError("Could not fetch the URL.")
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
        return (text or "").strip() or "Could not extract readable text from this page."
    except ImportError:
        pass
    # Fallback: basic urllib + html parsing
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    clean = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:10000] or "Could not extract text."


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read and extract text from a webpage: /read <URL>"""
    if not await authorized(update):
        return
    args = context.args or []
    if not args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /read <URL>\nI'll extract the article text from the page.")
        return
    url = args[0].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if update.effective_message:
        await update.effective_message.reply_text("📰 Reading article…")
    try:
        async with MAX_HEAVY_JOBS:
            text = await asyncio.to_thread(fetch_article_text, url)
        if len(text) <= 3900:
            if update.effective_message:
                await update.effective_message.reply_text(text)
        else:
            out = user_dir(update.effective_user.id) / f"{time.time_ns()}_article.txt"
            out.write_text(text, encoding="utf-8")
            await send_output(update.effective_message, out, "📰 Extracted article text")
    except Exception as exc:
        log.exception("Article extraction failed")
        if update.effective_message:
            await update.effective_message.reply_text(f"Could not read article: {str(exc)[:600]}")


# ── Feature: PDF Password Protect/Unlock Commands ─────────────────────────────

async def cmd_protect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a custom password for the next PDF protect action: /protect <password>"""
    if not await authorized(update):
        return
    args = context.args or []
    if not args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /protect <password>\nThen tap '🔒 Password protect' on a PDF.")
        return
    password = " ".join(args)
    if context.user_data is not None:
        context.user_data["pdf_password"] = password
    if update.effective_message:
        masked = "*" * len(password)
        await update.effective_message.reply_text(
            f"✅ Password `{masked}` saved. Now send a PDF and tap 🔒 Password protect."
        )


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the password to unlock a PDF: /unlock <password>"""
    if not await authorized(update):
        return
    args = context.args or []
    if not args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /unlock <password>\nThen tap '🔓 Remove password' on a PDF.")
        return
    password = " ".join(args)
    if context.user_data is not None:
        context.user_data["pdf_password"] = password
    if update.effective_message:
        masked = "*" * len(password)
        await update.effective_message.reply_text(
            f"✅ Password `{masked}` saved. Now send a PDF and tap 🔓 Remove password."
        )


# ── Feature: Audio/Video Speed Command ─────────────────────────────────────────

async def cmd_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set playback speed for the next speed-up action: /speed 1.5"""
    if not await authorized(update):
        return
    args = context.args or []
    if not args:
        if update.effective_message:
            await update.effective_message.reply_text("Usage: /speed <factor>\nExamples: /speed 1.25, /speed 1.5, /speed 2.0\nThen tap '⏩ Speed up' on audio/video.")
        return
    try:
        factor = float(args[0])
        if not (0.5 <= factor <= 4.0):
            raise ValueError("Speed must be between 0.5 and 4.0")
        if context.user_data is not None:
            context.user_data["speed_factor"] = factor
        if update.effective_message:
            await update.effective_message.reply_text(f"⏩ Speed set to {factor}x. Now send audio/video and tap '⏩ Speed up'.")
    except ValueError as exc:
        if update.effective_message:
            await update.effective_message.reply_text(str(exc))


# ── Feature: Voice-to-AI ──────────────────────────────────────────────────────

async def on_voice_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """When a voice message is received, transcribe it and send it to AI for a response."""
    if not await authorized(update) or not update.message:
        return
    item = update.message.voice or update.message.audio or update.message.video_note
    if not item:
        return
    tg_file = await context.bot.get_file(item.file_id)
    ext = Path(getattr(item, "file_name", "") or "voice.ogg").suffix or ".ogg"
    path = await store_upload(update, context, tg_file, f"voice_{item.file_unique_id}{ext}", item.file_size)
    if not path:
        return

    user_data = context.user_data or {}
    voice_ai_mode = user_data.get("voice_ai_mode", True)

    if voice_ai_mode:
        await update.message.reply_text("🎙️ Listening and thinking…")
        try:
            async with MAX_HEAVY_JOBS:
                transcript, _ = await asyncio.to_thread(groq_transcribe, path)
            if not transcript.strip():
                await update.message.reply_text("Could not understand the voice message. Please try again.")
                return
            await update.message.reply_text(f"📝 You said: {transcript[:500]}")
            async with MAX_HEAVY_JOBS:
                answer = await asyncio.to_thread(ai_response, transcript)
            if len(answer) <= 3900:
                await update.message.reply_text(f"🤖 {answer}")
            else:
                out = user_dir(update.effective_user.id) / f"{time.time_ns()}_voice_ai.txt"
                out.write_text(answer, encoding="utf-8")
                await send_output(update.message, out, "🤖 AI response")
        except Exception as exc:
            log.exception("Voice-to-AI failed")
            await update.message.reply_text(f"Voice-to-AI failed: {str(exc)[:600]}")
    else:
        await update.message.reply_text("Audio received. Choose a tool:", reply_markup=make_keyboard(action_buttons(path)))


async def cmd_voiceai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle voice-to-AI mode on/off: /voiceai"""
    if not await authorized(update):
        return
    user_data = context.user_data or {}
    current = user_data.get("voice_ai_mode", True)
    if context.user_data is not None:
        context.user_data["voice_ai_mode"] = not current
    status = "ON 🟢" if not current else "OFF 🔴"
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Voice-to-AI mode is now {status}.\n"
            f"{'Voice messages will be transcribed and answered by AI.' if not current else 'Voice messages will show the standard tool menu.'}"
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled Telegram handler error", exc_info=context.error)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment or .env file.")
    initialize_access_control()
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("myid", cmd_myid))
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("help", on_help))
    application.add_handler(CommandHandler(["access", "allowlist"], cmd_allowlist))
    application.add_handler(CommandHandler("allow", cmd_allow))
    application.add_handler(CommandHandler("deny", cmd_deny))
    application.add_handler(CommandHandler("qr", cmd_qr))
    application.add_handler(CommandHandler("ai", cmd_ai))
    application.add_handler(CommandHandler("transcribe", cmd_transcribe))
    application.add_handler(CommandHandler("trim", cmd_trim))
    application.add_handler(CommandHandler("mergepdf", cmd_mergepdf))
    application.add_handler(CommandHandler("finishmerge", cmd_finishmerge))
    application.add_handler(CommandHandler("cancelmerge", cmd_cancelmerge))
    application.add_handler(CommandHandler("convert", cmd_convert))
    application.add_handler(CommandHandler("tts", cmd_tts))
    application.add_handler(CommandHandler("remind", cmd_remind))
    application.add_handler(CommandHandler("fx", cmd_fx))
    application.add_handler(CommandHandler("read", cmd_read))
    application.add_handler(CommandHandler("protect", cmd_protect))
    application.add_handler(CommandHandler("unlock", cmd_unlock))
    application.add_handler(CommandHandler("speed", cmd_speed))
    application.add_handler(CommandHandler("voiceai", cmd_voiceai))
    application.add_handler(CommandHandler("tempmail", cmd_tempmail))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, on_document))
    application.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE, on_audio))
    application.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION, on_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_error_handler(on_error)
    log.info("Starting private bot (owner configured: %s; %d allow-listed user(s))", OWNER_USER_ID is not None, len(ALLOWED_USERS))
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
