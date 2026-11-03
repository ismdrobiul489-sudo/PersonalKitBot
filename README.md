# Private Telegram Utility Bot

A high-performance, lightweight, private Telegram utility bot built with Python (`python-telegram-bot` v20+ async) and designed for low-resource environments (Linux VPS, Google Cloud Shell, BotKeep worker slots).

The bot serializes heavy background jobs, restricts incoming files to 20 MB, enforces strict owner/allowlist access control, and automatically cleans up temporary files.

---

## 🌟 Key Features & Modules

### 📬 1. Disposable Temporary Email & OTP Receiver (`/tempmail`)
- **100% Free & Unlimited**: Powered by the Mail.tm REST API (no API key required).
- **Persistent Vault (`tempmail_vault.json`)**: Automatically saves generated accounts so your emails survive bot restarts.
- **VPS Reset / Wiped Server Protection**: Displays the account **Email** and **Password** immediately with one-tap copy formatting, plus a dedicated `/tempmail restore <email> <password>` command to restore any mailbox if your server is ever reinstalled.
- **Interactive UI**:
  - `🔄 Check Inbox`: Checks for incoming mail and extracts 4–8 digit verification codes / OTPs using smart regex.
  - `🆕 New Email`: Generates a fresh temporary email with instant credentials.
  - `📜 Saved Emails (X)`: Lists your previously created mailboxes with one-click switching.
  - `🏷️ Set Label`: Gives mailboxes memorable names (e.g. *Canva*, *Discord*, *ChatGPT*).
  - `🔐 Backup Key`: Directly outputs full email credentials and the one-line restore command.
  - `🗑️ Close Active` / `Delete from Vault`: Safe mailbox lifecycle management.
- **Custom Username Creation**: `/tempmail custom <name>` (e.g., `/tempmail custom robiservice`).

---

### 🛡️ 2. Privacy & Metadata / EXIF Toolkit (`privacy.py`)
Triggered automatically on uploaded images or media via the `🛡️ Metadata Tools` button:
- **View Metadata**: Extracts camera make/model, capture date, software, and GPS coordinates with a clickable Google Maps link.
- **Delete EXIF**: Purges 100% of EXIF tags, GPS coordinates, and camera profiles for total privacy.
- **Customise / Spoof Metadata**:
  - Disguise camera model as *Sony Alpha 7 IV*, *iPhone 15 Pro Max*, or *Canon EOS R5*.
  - Inject custom photographer / author copyright tags.
  - Reset capture timestamps to the current date and time.

---

### 🎙️ 3. Groq Whisper Subtitles & Transcription
- **Word-Level Chunking**: Generates subtitle cues chunked into strictly 3–4 words per cue using Groq Whisper `timestamp_granularities=["word", "segment"]`.
- **Interactive Delivery**:
  - Inline preview of the first 400 characters.
  - Format buttons: `📄 Plain Text (.txt)`, `📋 SubRip (.srt)`, `🌐 WebVTT (.vtt)`, and `📦 All Formats (.zip)`.

---

### 🤖 4. Voice & AI Integration
- **Voice-to-AI**: Automatically transcribes incoming Telegram voice notes and responds with intelligent LLM insights (toggle on/off with `/voiceai`).
- **Natural Text-to-Speech (`/tts`)**:
  - Automatic language detection (English, Bengali, Hindi, Arabic, Urdu, etc.).
  - Interactive inline voice picker for Female and Male voices (`edge-tts`).
- **AI Text Operations**: Summarize, polish grammar, analyze sentiments, and translate with an interactive 8-language target picker.

---

### 🖼️ 5. Image & Graphic Utilities
- **Format Conversion**: JPG, PNG, WebP, animated GIF.
- **Resolution Resizing**: 1080p, 720p, Square, 50%, 25%.
- **OCR Text Extraction**: Supports English, Bengali (`tesseract-ocr-ben`), Hindi, and Arabic.
- **Watermarking**: Interactive chat prompt to apply custom text watermarks.
- **Stickers**: One-click Telegram sticker converter (512×512 transparent WebP).

---

### 📑 6. PDF Power Tools
- **Split**: Safely split PDFs up to 80 pages into a downloadable ZIP archive.
- **Compress**: High-efficiency compression via `pikepdf`.
- **Images**: Export the first 10 pages as high-quality image renders.
- **Merge**: Combine multiple PDFs into a single document (`/mergepdf`).
- **Password Protection**: Encrypt PDFs with `/protect <password>`.
- **Password Unlock**: Decrypt protected PDFs with `/unlock <password>`.
- **Rotate**: 90° clockwise page rotation.

---

### 🎬 7. Audio & Video Processing
- **Audio Extraction**: Convert any video file or voice note to MP3.
- **Speed Adjustment**: Select playback factor (0.75x, 1.25x, 1.5x, 2.0x, 3.0x) via buttons or `/speed <factor>`.
- **Precision Trimming**: Interactive chat input for timestamps (e.g. `00:05 00:20` or `5 20`).
- **GIF Maker**: Convert video clips into animated GIFs with duration pickers (3s, 5s, 8s).

---

### 🧰 8. Everyday Utilities
- **Currency Converter**: Live rates via `/fx <amount> <from> <to>` (e.g. `/fx 100 USD BDT`).
- **Smart Reminders**: Asynchronous scheduled alerts via `/remind <time> <message>` (e.g. `/remind 30m Take medicine`).
- **Article Reader**: Extract clean readable text from web pages via `/read <URL>`.
- **QR Codes**: Generate QR images from text/links, or scan uploaded QR codes.
- **Unit & Time Converter**: Fast conversions via `/convert <amount> <from> <to>`.
- **ZIP Archives**: Create and safely extract ZIP files.
- **YouTube Metadata**: Inspect public video metadata and captions (downloads disabled).

---

## ⌨️ Command Reference

| Command | Usage Example | Description |
| :--- | :--- | :--- |
| `/tempmail` | `/tempmail` | Show active disposable email or generate a new one |
| `/tempmail history` | `/tempmail history` | View all saved disposable mailboxes and passwords |
| `/tempmail custom` | `/tempmail custom robiservice` | Create a disposable email with custom username |
| `/tempmail restore` | `/tempmail restore email pass` | Restore an old mailbox after a VPS reset |
| `/tts` | `/tts Hello world` | Text-to-Speech with Male/Female voice selector |
| `/voiceai` | `/voiceai` | Toggle automatic Voice-to-AI mode (ON/OFF) |
| `/remind` | `/remind 45m Submit report` | Set a background timer reminder |
| `/fx` | `/fx 150 USD EUR` | Convert currencies using real-time rates |
| `/read` | `/read https://example.com/post` | Extract clean article content from a web page |
| `/protect` | `/protect MySecret123` | Set PDF encryption password |
| `/unlock` | `/unlock MySecret123` | Remove PDF password encryption |
| `/speed` | `/speed 1.5` | Set audio/video playback speed multiplier |
| `/myid` | `/myid` | Get your numeric Telegram user ID |
| `/help` | `/help` | Display interactive help menu |

### Owner-Only Access Controls:
| Command | Description |
| :--- | :--- |
| `/access` or `/allowlist` | List approved Telegram user IDs |
| `/allow <user_id>` | Grant bot access to a user |
| `/deny <user_id>` | Revoke bot access from a user |

---

## ⚙️ Environment Configuration

Create a `.env` file in the root directory (refer to `.env.example`):

```ini
# Required
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"
OWNER_USER_ID=123456789

# AI & Transcription
GROQ_API_KEY="gsk_..."
NVIDIA_API_KEY=""                # Optional fallback LLM provider
OCR_LANG="eng+ben"               # Tesseract language code (requires tesseract-ocr-ben)

# Access Control
ALLOWED_USER_IDS="123456789"
ALLOWLIST_FILE="allowlist.json"
```

---

## 🚀 Installation & Deployment

### 1. System Prerequisites (Debian/Ubuntu/VPS)
```bash
sudo apt-get update
sudo apt-get install -y ffmpeg tesseract-ocr poppler-utils ghostscript
# Optional Bengali language support for OCR:
sudo apt-get install -y tesseract-ocr-ben
```

### 2. Python Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Start the Bot
```bash
python3 main.py
```

### 4. Background Execution on VPS (systemd or tmux)
To keep the bot running 24/7 on your server:

**Using `tmux` (Fastest):**
```bash
tmux new -s bot
source .venv/bin/activate
python3 main.py
# Press Ctrl+B then D to detach
```

**Using `systemd` (Recommended for production):**
Create `/etc/systemd/system/personal-kit-bot.service`:
```ini
[Unit]
Description=Personal Kit Telegram Bot
After=network.target

[Service]
Type=simple
User=ismdrobiul488
WorkingDirectory=/home/ismdrobiul488/PersonalKitBot
ExecStart=/home/ismdrobiul488/PersonalKitBot/.venv/bin/python3 main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable personal-kit-bot
sudo systemctl start personal-kit-bot
```

---

## 🔒 Security & Privacy
- **Strict Authorization**: Unauthorized users are silently blocked.
- **Isolated Storage**: Each user's uploaded files are processed in isolated per-user directories (`user_files/<user_id>`).
- **Credential Safety**: `tempmail_vault.json`, `allowlist.json`, `.env`, and cookies are automatically excluded from Git commits via `.gitignore`.
- **Zero Heavy Models**: Does not load multi-gigabyte neural networks into RAM; runs smoothly on 512 MB – 1 GB RAM VPS instances.
