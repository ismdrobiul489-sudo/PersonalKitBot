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
import html
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
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv

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
    buttons: list[tuple[str, str]] = [("File info", "do:file:info"), ("SHA-256", "do:file:hash")]
    if mime.startswith("image/") or ext in {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff"}:
        buttons += [
            ("Resize", "do:img:resize"), ("Compress", "do:img:compress"), ("Center crop", "do:img:crop"),
            ("Rotate 90°", "do:img:rotate"), ("OCR text", "do:img:ocr"),
            ("Image → PDF", "do:img:pdf"), ("Convert to JPG", "do:img:jpg"),
            ("Convert to PNG", "do:img:png"), ("Convert to WebP", "do:img:webp"),
            ("Read QR", "do:qr:read"),
        ]
    elif mime == "application/pdf" or ext == "pdf":
        buttons += [
            ("Extract PDF text", "do:pdf:text"), ("Rotate pages", "do:pdf:rotate"),
            ("Split pages", "do:pdf:split"), ("Compress PDF", "do:pdf:compress"),
            ("PDF pages → images", "do:pdf:images"),
        ]
    elif mime.startswith("audio/") or ext in {"mp3", "wav", "m4a", "aac", "ogg", "flac", "opus"}:
        buttons += [("Convert to MP3", "do:media:mp3"), ("Compress audio", "do:media:compress"), ("Transcribe", "do:media:transcribe")]
    elif mime.startswith("video/") or ext in {"mp4", "mov", "mkv", "webm", "avi", "m4v"}:
        buttons += [("Extract MP3 audio", "do:media:extract"), ("Compress video", "do:media:vcompress"), ("Transcribe audio", "do:media:transcribe")]
    elif ext == "zip" or mime == "application/zip":
        buttons += [("Unzip files", "do:zip:unzip")]
    elif ext == "json":
        buttons += [("Clean/format text", "do:text:clean"), ("Count text", "do:text:count"), ("Pretty JSON", "do:text:json")]
    elif ext in {"csv", "tsv"}:
        buttons += [("Clean/format text", "do:text:clean"), ("Count text", "do:text:count"), ("CSV/TSV → JSON", "do:text:csvjson")]
    elif ext in {"txt", "md", "log"} or mime.startswith("text/"):
        buttons += [("Clean/format text", "do:text:clean"), ("Count text", "do:text:count"), ("URL encode", "do:text:encode"), ("URL decode", "do:text:decode"), ("Pretty JSON", "do:text:json")]
    buttons.append(("ZIP this file", "do:zip:zip"))
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
        "/help — show this help\n\n"
        "Send a video URL for metadata and available captions only; the bot never downloads the video. "
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
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("Usage: /allow <positive numeric Telegram user ID>")
        return
    user_id = int(context.args[0])
    if user_id == OWNER_USER_ID:
        await update.effective_message.reply_text("The owner is permanently authorized and does not need to be added.")
        return
    if user_id in ALLOWED_USERS:
        await update.effective_message.reply_text(f"Telegram user {user_id} is already on the allow list.")
        return
    ALLOWED_USERS.add(user_id)
    try:
        save_allowed_users()
    except OSError:
        ALLOWED_USERS.remove(user_id)
        log.exception("Could not persist allow-list addition")
        await update.effective_message.reply_text("Could not save the allow list; no access change was made.")
        return
    await update.effective_message.reply_text(f"Access granted to Telegram user {user_id}.")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await owner_only(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("Usage: /deny <positive numeric Telegram user ID>")
        return
    user_id = int(context.args[0])
    if user_id == OWNER_USER_ID:
        await update.effective_message.reply_text("The owner is permanent and cannot be removed from access.")
        return
    if user_id not in ALLOWED_USERS:
        await update.effective_message.reply_text(f"Telegram user {user_id} is not on the allow list.")
        return
    ALLOWED_USERS.remove(user_id)
    try:
        save_allowed_users()
    except OSError:
        ALLOWED_USERS.add(user_id)
        log.exception("Could not persist allow-list removal")
        await update.effective_message.reply_text("Could not save the allow list; no access change was made.")
        return
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
    if not await authorized(update):
        return
    item = update.message.audio or update.message.voice or update.message.video_note
    tg_file = await context.bot.get_file(item.file_id)
    ext = Path(getattr(item, "file_name", "") or "audio.ogg").suffix or ".ogg"
    path = await store_upload(update, context, tg_file, getattr(item, "file_name", None) or f"audio_{item.file_unique_id}{ext}", item.file_size)
    if path:
        await update.message.reply_text("Audio received. Choose a tool:", reply_markup=make_keyboard(action_buttons(path)))


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    item = update.message.video or update.message.animation
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


def process_image(src: Path, action: str, outdir: Path) -> tuple[Optional[Path], Optional[str]]:
    image = ImageOps.exif_transpose(Image.open(src))
    if action == "ocr":
        lang = os.getenv("OCR_LANG", "eng")
        return None, pytesseract.image_to_string(image, lang=lang).strip() or "No text detected."
    if action == "resize":
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
            completion = client.chat.completions.create(
                model=os.getenv("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile"),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1800,
            )
            return completion.choices[0].message.content or "No response."
        except Exception as exc:
            errors.append(f"Groq: {exc}")
    if nvidia_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=nvidia_key, base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"))
            model = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1800,
            )
            return completion.choices[0].message.content or "No response."
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
            timestamp_granularities=["segment"],
        )
    transcript = getattr(result, "text", "") or ""
    raw_segments = getattr(result, "segments", None) or []
    segments = []
    for segment in raw_segments:
        if hasattr(segment, "model_dump"):
            segment = segment.model_dump()
        elif not isinstance(segment, dict):
            segment = vars(segment)
        segments.append(segment)
    return transcript, segments


async def do_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE, src: Path) -> None:
    await update.effective_message.reply_text("Transcribing…")
    async with MAX_HEAVY_JOBS:
        text, segments = await asyncio.to_thread(groq_transcribe, src)
    outdir = user_dir(update.effective_user.id)
    transcript_path = outdir / f"{time.time_ns()}_{src.stem}_transcript.txt"
    transcript_path.write_text(text, encoding="utf-8")
    await send_output(update.effective_message, transcript_path, "Transcript (text).")
    if segments:
        srt_path = outdir / f"{time.time_ns()}_{src.stem}.srt"
        vtt_path = outdir / f"{time.time_ns()}_{src.stem}.vtt"
        srt_lines = []
        vtt_lines = ["WEBVTT", ""]
        for index, segment in enumerate(segments, 1):
            start = float(segment.get("start", 0))
            end = float(segment.get("end", start))
            caption = str(segment.get("text", "")).strip()
            srt_lines.extend([str(index), f"{subtitle_timestamp(start)} --> {subtitle_timestamp(end)}", caption, ""])
            vtt_lines.extend([f"{subtitle_timestamp(start, vtt=True)} --> {subtitle_timestamp(end, vtt=True)}", caption, ""])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")
        await send_output(update.effective_message, srt_path, "SRT subtitles.")
        await send_output(update.effective_message, vtt_path, "WebVTT subtitles.")


def yt_metadata_and_captions(url: str, outdir: Path) -> tuple[str, Optional[Path]]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed.") from exc
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "extract_flat": False}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
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
            "quiet": True, "no_warnings": True, "skip_download": True,
            "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": [lang], "subtitlesformat": "vtt/best",
            "outtmpl": str(caption_dir / "%(id)s.%(ext)s"), "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])
            captions = list(caption_dir.glob("*.vtt")) + list(caption_dir.glob("*.srt"))
            if captions:
                caption_path = outdir / f"{time.time_ns()}_youtube_captions{captions[0].suffix}"
                shutil.copy2(captions[0], caption_path)
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


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
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
            ("Count", "do:text:count"), ("Clean whitespace", "do:text:clean"),
            ("URL encode", "do:text:encode"), ("URL decode", "do:text:decode"),
            ("AI summarize", "do:ai:summarize"), ("AI polish", "do:ai:polish"),
            ("AI analyze", "do:ai:analyze"), ("Translate to Bengali", "do:ai:translate"),
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
        target = os.getenv("AI_TARGET_LANGUAGE", "Bengali")
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
    if parts[0] == "do" and len(parts) == 3 and parts[1] in {"text", "ai"}:
        group, action = parts[1], parts[2]
        text = context.user_data.get("last_text")
        if not text and group == "text":
            raw = context.user_data.get("last_file")
            if raw and Path(raw).exists():
                try:
                    text = await asyncio.to_thread(extract_text, Path(raw))
                except Exception as exc:
                    await query.message.reply_text(f"Could not read text from that file: {str(exc)[:600]}")
                    return
        if not text:
            await query.message.reply_text("Send text or upload a supported text file first.")
            return
        if group == "ai":
            context.user_data["ai_task"] = action
            action = "ai"
        try:
            await do_text_action(update, context, action, text)
        except Exception as exc:
            log.exception("Text callback failed")
            await query.message.reply_text(f"Operation failed: {str(exc)[:700]}")
        return
    if len(parts) != 3 or parts[0] != "do":
        await query.message.reply_text("Unknown action.")
        return
    _, group, action = parts
    src_raw = context.user_data.get("last_file")
    src = Path(src_raw) if src_raw else None
    if not src or not src.exists():
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
                out, _ = await asyncio.to_thread(process_pdf, src, action, outdir)
                await send_output(query.message, out, "PDF processed.")
                return
            if group == "media":
                if action == "transcribe":
                    # release semaphore before helper acquires it
                    pass
                elif action in {"mp3", "compress", "extract", "vcompress"}:
                    pass
                else:
                    raise ValueError("Unknown media operation.")
        if group == "media" and action == "transcribe":
            await do_transcribe(update, context, src)
            return
        if group == "media":
            out = outdir / (f"{src.stem}_{action}.mp4" if action == "vcompress" else f"{src.stem}_{action}.mp3")
            if action in {"mp3", "compress"}:
                args = ["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-b:a", "96k" if action == "compress" else "192k", str(out)]
            elif action == "extract":
                args = ["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-q:a", "4", str(out)]
            else:
                args = ["-i", str(src), "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k", str(out)]
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
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /ai summarize|polish|analyze|translate <text>")
        return
    task = context.args[0].lower()
    text = " ".join(context.args[1:])
    if task not in {"summarize", "polish", "analyze", "translate"}:
        await update.effective_message.reply_text("Choose summarize, polish, analyze, or translate.")
        return
    context.user_data["last_text"] = text
    context.user_data["ai_task"] = task
    await do_text_action(update, context, "ai", text)


async def cmd_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    raw = context.user_data.get("last_file")
    if not raw or not Path(raw).exists():
        await update.effective_message.reply_text("Upload an audio or video file first (maximum 20 MB).")
        return
    try:
        await do_transcribe(update, context, Path(raw))
    except Exception as exc:
        log.exception("Transcription failed")
        await update.effective_message.reply_text(f"Transcription failed: {str(exc)[:800]}")


async def cmd_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    raw = context.user_data.get("last_file")
    if not raw or not Path(raw).exists():
        await update.effective_message.reply_text("Upload audio or video first, then use /trim <start> <end>, e.g. /trim 00:05 00:20")
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /trim <start> <end>, e.g. /trim 00:05 00:20")
        return
    src = Path(raw)
    out = user_dir(update.effective_user.id) / f"{src.stem}_trimmed{src.suffix or '.mp4'}"
    try:
        async with MAX_HEAVY_JOBS:
            await asyncio.to_thread(ffmpeg_run, ["-ss", context.args[0], "-to", context.args[1], "-i", str(src), "-c", "copy", str(out)])
        await send_output(update.effective_message, out, "Trimmed media.")
    except Exception as exc:
        log.exception("Trim failed")
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
    try:
        if len(context.args) != 2:
            raise ValueError("Usage: /convert <value> <unit>, e.g. /convert 10 km")
        result = convert_units(float(context.args[0]), context.args[1])
        await update.effective_message.reply_text(result)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))


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
