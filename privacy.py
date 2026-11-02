"""
Privacy Cleaner & Metadata (EXIF) Stripper Module for Telegram Utility Bot.
Supports:
1. View Metadata (Camera, Date, Software, GPS location map).
2. Delete All Metadata (100% purge, anonymous).
3. Customise Metadata (Disguise camera model, custom author/artist, fake timestamp).
"""

import datetime
import logging
import subprocess
from pathlib import Path
from typing import Optional

from PIL import ExifTags, Image, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("privacy")


def extract_gps_coords(gps_info: dict) -> tuple[Optional[str], Optional[str]]:
    """Parse GPS coordinates from EXIF GPSInfo tags into lat/lon strings and Google Maps URL."""
    try:
        lat_ref = gps_info.get(1) or gps_info.get("GPSLatitudeRef") or "N"
        lat = gps_info.get(2) or gps_info.get("GPSLatitude")
        lon_ref = gps_info.get(3) or gps_info.get("GPSLongitudeRef") or "E"
        lon = gps_info.get(4) or gps_info.get("GPSLongitude")

        if lat and lon:
            def to_degrees(val):
                if isinstance(val, (tuple, list)) and len(val) >= 3:
                    return float(val[0]) + float(val[1]) / 60.0 + float(val[2]) / 3600.0
                return float(val)

            lat_num = to_degrees(lat)
            if str(lat_ref).upper() == "S":
                lat_num = -lat_num

            lon_num = to_degrees(lon)
            if str(lon_ref).upper() == "W":
                lon_num = -lon_num

            human_str = f"{abs(lat_num):.4f}° {lat_ref}, {abs(lon_num):.4f}° {lon_ref}"
            maps_url = f"https://maps.google.com/?q={lat_num:.6f},{lon_num:.6f}"
            return human_str, maps_url
    except Exception:
        pass
    return None, None


def inspect_image_exif(src: Path) -> dict:
    """Inspect and extract all sensitive EXIF metadata from an image."""
    found: dict = {}
    try:
        with Image.open(src) as img:
            exif = img.getexif()
            if not exif:
                return found

            for tag_id, value in exif.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if tag_name == "Make":
                    found["make"] = str(value).strip()
                elif tag_name == "Model":
                    found["model"] = str(value).strip()
                elif tag_name in {"DateTime", "DateTimeOriginal", "DateTimeDigitized"}:
                    found["datetime"] = str(value).strip()
                elif tag_name == "Software":
                    found["software"] = str(value).strip()
                elif tag_name == "Artist":
                    found["artist"] = str(value).strip()

            # GPS tag: 34853
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo) if hasattr(exif, "get_ifd") else None
            if gps_ifd:
                coords, maps_link = extract_gps_coords(gps_ifd)
                if coords:
                    found["gps"] = coords
                    found["maps_url"] = maps_link
    except Exception as exc:
        log.debug("Could not read EXIF tags from %s: %s", src, exc)
    return found


def inspect_media_metadata(src: Path) -> dict:
    """Inspect metadata tags of an audio or video file using FFprobe."""
    found: dict = {}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format_tags=title,artist,album,creation_time,encoder,date,location",
            "-of", "default=noprint_wrappers=1",
            str(src),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    tag_name = k.replace("TAG:", "").strip()
                    if v.strip():
                        found[tag_name] = v.strip()
    except Exception as exc:
        log.debug("FFprobe inspection failed: %s", exc)
    return found


def format_metadata_report(found: dict, is_media: bool = False) -> str:
    """Format extracted metadata into an organized, readable report."""
    if not found:
        return (
            "🛡️ **Metadata Inspection Report:**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ **No sensitive metadata detected!**\n"
            "This file does not contain embedded GPS coordinates, camera models, or timestamps.\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

    lines = [
        "🛡️ **Embedded Metadata Inspection:**",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if not is_media:
        camera = f"{found.get('make', '')} {found.get('model', '')}".strip()
        if camera:
            lines.append(f"📷 **Camera/Device:** `{camera}`")
        if found.get("datetime"):
            lines.append(f"📅 **Capture Date:** `{found['datetime']}`")
        if found.get("gps"):
            lines.append(f"📍 **GPS Location:** `{found['gps']}`")
        if found.get("maps_url"):
            lines.append(f"🗺️ **Google Maps:** [Open Location]({found['maps_url']})")
        if found.get("artist"):
            lines.append(f"👤 **Author/Artist:** `{found['artist']}`")
        if found.get("software"):
            lines.append(f"💻 **Software:** `{found['software']}`")
    else:
        for k, v in found.items():
            lines.append(f"📌 **{k.capitalize()}:** `{v}`")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚠️ _This private information is publicly visible when you share this file._")
    return "\n".join(lines)


def make_metadata_menu(group: str = "img") -> InlineKeyboardMarkup:
    """Build the primary 3-action menu: View, Delete, Customise."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁️ View Metadata", callback_data=f"do:meta:view:{group}"),
            InlineKeyboardButton("🗑️ Delete All", callback_data=f"do:meta:delete:{group}"),
        ],
        [
            InlineKeyboardButton("✏️ Customise / Disguise", callback_data=f"do:meta:cust_menu:{group}"),
        ],
    ])


def make_customise_menu(group: str = "img") -> InlineKeyboardMarkup:
    """Build customization sub-menu (Fake DSLR, Custom Author, Reset Date)."""
    if group == "img":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📷 Disguise as Sony A7 IV", callback_data="do:meta:set:dslr"),
                InlineKeyboardButton("📱 Disguise as iPhone 15 Pro", callback_data="do:meta:set:iphone"),
            ],
            [
                InlineKeyboardButton("👤 Set Custom Author…", callback_data="do:meta:set:ask_author"),
                InlineKeyboardButton("📅 Reset Date to Today", callback_data="do:meta:set:today"),
            ],
            [
                InlineKeyboardButton("🔙 Back to Metadata Menu", callback_data="do:meta:menu:img"),
            ],
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Set Title & Artist…", callback_data="do:meta:set:ask_media"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Metadata Menu", callback_data="do:meta:menu:media"),
        ],
    ])


def strip_image_metadata(src: Path, outdir: Path) -> tuple[Path, dict]:
    """Completely remove all EXIF metadata, ICC profiles, and comments from an image."""
    found_info = inspect_image_exif(src)

    with Image.open(src) as img:
        img_transposed = ImageOps.exif_transpose(img)
        clean_img = Image.new(img_transposed.mode, img_transposed.size)
        clean_img.putdata(list(img_transposed.getdata()))

        fmt = (img.format or "JPEG").upper()
        if fmt in {"JPEG", "JPG"}:
            ext, save_fmt = ".jpg", "JPEG"
        elif fmt == "PNG":
            ext, save_fmt = ".png", "PNG"
        elif fmt == "WEBP":
            ext, save_fmt = ".webp", "WEBP"
        else:
            ext, save_fmt = src.suffix or ".jpg", fmt

        out_path = outdir / f"{src.stem}_clean{ext}"

        if save_fmt == "JPEG":
            clean_img.convert("RGB").save(out_path, "JPEG", quality=95, optimize=True)
        else:
            clean_img.save(out_path, save_fmt, optimize=True)

    return out_path, found_info


def strip_media_metadata(src: Path, outdir: Path) -> Path:
    """Losslessly strip all metadata tags and timestamps from audio/video in milliseconds."""
    out_path = outdir / f"{src.stem}_clean{src.suffix or '.mp4'}"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map_metadata", "-1",
        "-c", "copy",
        str(out_path),
    ]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg metadata strip failed: {(result.stderr or '')[-500:]}")
    return out_path


def customize_image_metadata(
    src: Path,
    outdir: Path,
    make: str = "Sony",
    model: str = "ILCE-7M4",
    author: str = "Anonymous",
    date_str: Optional[str] = None,
) -> Path:
    """Create a clean image with custom/faked EXIF tags and zero GPS data."""
    if not date_str:
        date_str = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")

    with Image.open(src) as img:
        img_transposed = ImageOps.exif_transpose(img)
        clean_img = Image.new(img_transposed.mode, img_transposed.size)
        clean_img.putdata(list(img_transposed.getdata()))

        exif = clean_img.getexif()
        exif[271] = str(make)
        exif[272] = str(model)
        exif[305] = "Adobe Photoshop 2026"
        exif[306] = str(date_str)
        exif[315] = str(author)

        out_path = outdir / f"{src.stem}_custom.jpg"
        clean_img.convert("RGB").save(out_path, "JPEG", quality=95, exif=exif, optimize=True)
        return out_path


def customize_media_metadata(src: Path, outdir: Path, title: str = "Title", artist: str = "Creator") -> Path:
    """Losslessly set custom title and artist metadata tags on audio/video."""
    out_path = outdir / f"{src.stem}_custom{src.suffix or '.mp4'}"
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map_metadata", "-1",
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-c", "copy",
        str(out_path),
    ]
    subprocess.run(args, capture_output=True, text=True, timeout=120)
    return out_path
