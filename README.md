# Private Telegram Utility Bot Prototype

A private long-polling Telegram bot intended for one BotKeep worker slot. The configured owner is always authorized and cannot be removed through bot commands; the owner can dynamically grant or revoke access for other Telegram user IDs. The bot limits incoming files to 20 MB, serializes heavy jobs, and removes temporary files after about 30 minutes.

Use Python 3.10 or newer.

## Run locally

From this directory:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg tesseract-ocr poppler-utils ghostscript
# Optional Bengali OCR data: sudo apt-get install -y tesseract-ocr-ben
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

The `.env.example` already contains the owner's permanent Telegram user ID. Set `TELEGRAM_BOT_TOKEN` before starting. If you run the bot under a different owner, replace `OWNER_USER_ID` with that account's numeric Telegram ID. Only the owner can use `/allow`, `/deny`, `/access`, and `/allowlist`.

```bash
python main.py
```

On BotKeep, install the system tools using its supported system-package mechanism, install Python requirements with `pip install -r requirements.txt`, set the same environment variables in its settings, and use `python3 main.py` as the long-running worker command. Keep `ALLOWLIST_FILE` on persistent storage so changes survive restarts. Do not commit `.env` or paste API tokens into public logs.

## Included in this prototype

The bot presents file-matched inline actions for images, PDFs, audio/video, ZIPs, and text/data files. It includes image resizing/compression/conversion/rotation/center-crop/OCR and image-to-PDF; PDF text extraction, rotation, compression, page splitting, first-ten-page image export, and multi-file merging; audio/video conversion, audio extraction, compression, trimming, Groq Whisper transcription with SRT/VTT sidecars; ZIP creation and safe extraction; QR creation/reading; file info/SHA-256; text cleanup/counting/URL encoding and decoding/JSON formatting/CSV-to-JSON conversion; unit and time conversion; AI summarize/polish/analyze/translate; and YouTube metadata plus accessible captions only. Video downloads are disabled.

## Required settings

- `TELEGRAM_BOT_TOKEN`: token for the Telegram bot.
- `OWNER_USER_ID`: the owner's positive numeric Telegram user ID. The owner is always authorized and `/deny` cannot remove them.
- `ALLOWED_USER_IDS`: optional comma-separated initial IDs. Imported only if the persistent allow-list file does not exist.
- `ALLOWLIST_FILE`: path to the JSON file used to persist dynamic access changes; defaults to `allowlist.json` in the current working directory.
- `GROQ_API_KEY`: needed for transcription; used for AI text if supplied.
- `NVIDIA_API_KEY`: optional fallback AI provider, with model/base URL configurable.
- `OCR_LANG`: Tesseract language code; `eng+ben` requires Bengali OCR data to be installed.

When authorized as the owner, use `/access` or `/allowlist` to view approved IDs, `/allow <user_id>` to grant access, and `/deny <user_id>` to revoke it. The owner ID is held separately from the mutable list and remains authorized even if the JSON file is edited. This is a prototype, not yet connected to BotKeep or Telegram. Keep the BotKeep slot within its shared resource limits; the bot allows only one heavy operation at a time. Bot API file downloads and outgoing files are capped at 20 MB. PDF-to-image export is limited to the first 10 pages, and PDF splitting to 80 pages per operation.
