"""
Temporary Email (Disposable Mailbox / OTP Receiver) Module for Telegram Utility Bot.
Uses the 100% free Mail.tm REST API (no API key required).
Supports persistent email vault to save, label, switch, and restore old disposable addresses anytime.
Includes manual backup credentials (email + password) for VPS-reset protection.
"""

import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
try:
    from telegram import CopyTextButton
except ImportError:
    CopyTextButton = None
from telegram.ext import ContextTypes

log = logging.getLogger("tempmail")

BASE_URL = "https://api.mail.tm"
TIMEOUT = 14.0
VAULT_FILE = Path(__file__).resolve().parent / "tempmail_vault.json"


# ══════════════════════════════════════════════════════════════════════════════
# 1. PERSISTENT VAULT STORAGE (tempmail_vault.json)
# ══════════════════════════════════════════════════════════════════════════════

def load_vault() -> dict:
    """Load persistent tempmail vault from disk safely."""
    if not VAULT_FILE.exists():
        return {}
    try:
        with open(VAULT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Failed to read tempmail_vault.json: %s", exc)
        return {}


def save_vault(data: dict) -> None:
    """Save persistent tempmail vault atomically to disk."""
    tmp_path = VAULT_FILE.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, VAULT_FILE)
    except Exception as exc:
        log.error("Failed to write tempmail_vault.json: %s", exc)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def get_user_vault(user_id: int | str) -> list[dict]:
    """Retrieve all saved mailbox accounts for a specific Telegram user."""
    vault = load_vault()
    return vault.get(str(user_id), [])


def save_account_to_vault(
    user_id: int | str,
    address: str,
    password: str,
    account_id: str = "",
    label: str = "",
) -> dict:
    """Save or update a mailbox account in the persistent vault."""
    vault = load_vault()
    uid = str(user_id)
    accounts = vault.get(uid, [])

    # Check if address already exists in vault
    for acc in accounts:
        if acc.get("address", "").lower() == address.lower():
            acc["password"] = password
            if account_id:
                acc["account_id"] = account_id
            if label:
                acc["label"] = label
            save_vault(vault)
            return acc

    # New account entry
    short_id = secrets.token_hex(4)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = {
        "id": short_id,
        "address": address,
        "password": password,
        "account_id": account_id,
        "label": label or "",
        "created_at": now_str,
    }
    accounts.insert(0, entry)  # latest first
    vault[uid] = accounts
    save_vault(vault)
    return entry


def get_account_by_id(user_id: int | str, acc_id: str) -> Optional[dict]:
    """Find a saved mailbox by its short identifier or exact address."""
    accounts = get_user_vault(user_id)
    for acc in accounts:
        if acc.get("id") == acc_id or acc.get("address", "").lower() == acc_id.lower():
            return acc
    return None


def update_account_label(user_id: int | str, address_or_id: str, new_label: str) -> bool:
    """Update label for a saved email address."""
    vault = load_vault()
    uid = str(user_id)
    accounts = vault.get(uid, [])
    found = False
    for acc in accounts:
        if acc.get("address", "").lower() == address_or_id.lower() or acc.get("id") == address_or_id:
            acc["label"] = new_label.strip()
            found = True
            break
    if found:
        save_vault(vault)
    return found


def remove_account_from_vault(user_id: int | str, acc_id_or_addr: str) -> bool:
    """Delete an email address permanently from user's saved vault."""
    vault = load_vault()
    uid = str(user_id)
    accounts = vault.get(uid, [])
    initial_len = len(accounts)
    filtered = [
        acc for acc in accounts
        if acc.get("id") != acc_id_or_addr and acc.get("address", "").lower() != acc_id_or_addr.lower()
    ]
    if len(filtered) != initial_len:
        vault[uid] = filtered
        save_vault(vault)
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# 2. OTP EXTRACTION UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def extract_otp(text: str) -> Optional[str]:
    """Extract a 4 to 8 digit verification/OTP code from email body text."""
    if not text:
        return None

    # Handle split formats like 123-456 or 123 456
    split_match = re.search(
        r"(?:code|otp|verification|pin|password|token|is)[:\s]+([0-9]{3})[-\s]([0-9]{3})\b",
        text,
        re.IGNORECASE,
    )
    if split_match:
        return f"{split_match.group(1)}{split_match.group(2)}"

    # Look for explicit keyword patterns like "code: 123456", "OTP: 1234", "is 123456"
    keyword_match = re.search(
        r"(?:code|otp|verification|pin|password|token|is)[:\s]+([0-9]{4,8})\b",
        text,
        re.IGNORECASE,
    )
    if keyword_match:
        return keyword_match.group(1)

    # Standalone 4-8 digit numbers
    candidates = re.findall(r"\b[0-9]{4,8}\b", text)
    if candidates:
        filtered = [c for c in candidates if not (len(c) == 4 and 1990 <= int(c) <= 2035)]
        if filtered:
            return filtered[0]
        return candidates[0]
    return None


def extract_verification_links(text: str, html_content: Optional[str | list] = None) -> list[str]:
    """
    Extract verification/confirmation/activation URLs from email body (plain text & HTML).
    Prioritizes verification-specific action links and filters out unhelpful social/unsubscribe links.
    """
    found_links: list[str] = []
    seen = set()

    def add_link(url: str):
        clean_url = re.sub(r'[\s>\)\]\.,;:\'"]+$', '', url.strip())
        if clean_url.startswith(("http://", "https://")) and clean_url not in seen:
            lower = clean_url.lower()
            if any(bad in lower for bad in [
                "unsubscribe", "privacy", "terms", "optout", "opt-out",
                "twitter.com", "facebook.com", "instagram.com", "linkedin.com", "youtube.com"
            ]):
                return
            seen.add(clean_url)
            found_links.append(clean_url)

    # 1. Search HTML <a> tags for verification-related anchor texts or hrefs
    raw_html = ""
    if isinstance(html_content, list):
        raw_html = " ".join(str(item) for item in html_content)
    elif isinstance(html_content, str):
        raw_html = html_content

    if raw_html:
        a_tags = re.findall(
            r'<a\s+[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
            raw_html,
            re.IGNORECASE | re.DOTALL,
        )
        for href, anchor_text in a_tags:
            combined = f"{href} {anchor_text}".lower()
            if any(kw in combined for kw in [
                "verify", "verification", "confirm", "confirmation", "activate",
                "activation", "validate", "login", "magic", "token", "click here", "sign up", "signup"
            ]):
                add_link(href)

    # 2. Search plain text for URLs containing verification keywords
    if text:
        urls = re.findall(r'https?://[^\s<>"\']+', text)
        for u in urls:
            lower = u.lower()
            if any(kw in lower for kw in ["verify", "confirm", "activate", "token", "auth", "magic", "validate"]):
                add_link(u)

    # 3. Fallback: if no verification-specific URL matched yet, check all HTML hrefs
    if not found_links and raw_html:
        hrefs = re.findall(r'href=["\'](https?://[^"\']+)["\']', raw_html, re.IGNORECASE)
        for h in hrefs:
            add_link(h)

    # 4. Fallback: if still no links, take any plain text URL (up to 3)
    if not found_links and text:
        urls = re.findall(r'https?://[^\s<>"\']+', text)
        for u in urls:
            add_link(u)

    return found_links[:3]


async def safe_send_text(
    target_msg,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "HTML",
) -> None:
    """Send text safely to Telegram with automatic fallback to plain text if entity parsing fails."""
    if not target_msg:
        return
    try:
        await target_msg.reply_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.warning("Formatted Telegram send failed (%s), falling back to raw plain text", exc)
        plain = re.sub(r"<[^>]+>", "", text)
        try:
            await target_msg.reply_text(
                plain,
                parse_mode=None,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as fallback_exc:
            log.exception("Plain text fallback send also failed: %s", fallback_exc)


# ══════════════════════════════════════════════════════════════════════════════
# 3. MAIL.TM REST API CLIENT
# ══════════════════════════════════════════════════════════════════════════════

async def get_available_domain() -> str:
    """Fetch the first active available domain from Mail.tm."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        dom_resp = await client.get(f"{BASE_URL}/domains")
        dom_resp.raise_for_status()
        dom_data = dom_resp.json()
        members = dom_data.get("hydra:member") or []
        if not members:
            raise RuntimeError("No active domains available from Mail.tm right now.")
        return members[0]["domain"]


async def login_mailbox(address: str, password: str) -> dict:
    """Authenticate with Mail.tm using address & password and return fresh JWT token."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/token",
            json={"address": address, "password": password},
        )
        if resp.status_code == 401:
            raise RuntimeError("Invalid credentials or account expired on Mail.tm.")
        resp.raise_for_status()
        data = resp.json()
        return {
            "address": address,
            "password": password,
            "token": data.get("token"),
            "account_id": data.get("id"),
        }


async def create_mailbox(custom_username: Optional[str] = None) -> dict:
    """Create a new temporary email inbox on Mail.tm and acquire a JWT token."""
    domain = await get_available_domain()

    if custom_username:
        clean_user = re.sub(r"[^a-zA-Z0-9_\.-]", "", custom_username).lower()
        if len(clean_user) < 3:
            clean_user = f"{clean_user}_{secrets.token_hex(2)}"
        username = clean_user[:25]
    else:
        username = f"usr_{secrets.token_hex(4)}"

    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Step 1: Register account
        reg_resp = await client.post(
            f"{BASE_URL}/accounts",
            json={"address": address, "password": password},
        )
        if reg_resp.status_code == 422:
            # Address already taken; fallback to random suffix
            address = f"{username}_{secrets.token_hex(2)}@{domain}"
            reg_resp = await client.post(
                f"{BASE_URL}/accounts",
                json={"address": address, "password": password},
            )
        reg_resp.raise_for_status()
        account_id = reg_resp.json().get("id")

        # Step 2: Login to obtain JWT Bearer token
        token_resp = await client.post(
            f"{BASE_URL}/token",
            json={"address": address, "password": password},
        )
        token_resp.raise_for_status()
        token = token_resp.json().get("token")

        return {
            "address": address,
            "password": password,
            "token": token,
            "account_id": account_id,
        }


async def fetch_messages_safe(account: dict) -> list[dict]:
    """Retrieve incoming email list with automatic token refresh on 401."""
    token = account.get("token")
    address = account.get("address", "")
    password = account.get("password", "")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await client.get(f"{BASE_URL}/messages", headers=headers)

        # If token expired, auto re-login
        if resp.status_code == 401 and address and password:
            log.info("Token expired for %s, re-authenticating...", address)
            login_data = await login_mailbox(address, password)
            token = login_data["token"]
            account["token"] = token
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get(f"{BASE_URL}/messages", headers=headers)

        resp.raise_for_status()
        return resp.json().get("hydra:member") or []


async def fetch_message_detail_safe(account: dict, message_id: str) -> dict:
    """Retrieve full email content with automatic token refresh on 401."""
    token = account.get("token")
    address = account.get("address", "")
    password = account.get("password", "")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await client.get(f"{BASE_URL}/messages/{message_id}", headers=headers)

        if resp.status_code == 401 and address and password:
            login_data = await login_mailbox(address, password)
            token = login_data["token"]
            account["token"] = token
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get(f"{BASE_URL}/messages/{message_id}", headers=headers)

        resp.raise_for_status()
        return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
# 4. KEYBOARDS & UI BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def make_copy_link_button(link: str, label: str = "📋 Copy Link") -> InlineKeyboardButton:
    """
    Build a 1-tap Copy Button for a verification link.
    If Telegram Bot API 7.8 CopyTextButton is supported and link length <= 256,
    uses Telegram's native client-side clipboard copy button.
    Otherwise, returns an inline button with callback to send the link in a tap-to-copy code block.
    """
    if CopyTextButton is not None and len(link) <= 256:
        try:
            return InlineKeyboardButton(label, copy_text=CopyTextButton(text=link))
        except (TypeError, ValueError):
            pass
    return InlineKeyboardButton(label, callback_data="do:tempmail:copylink")


def make_tempmail_keyboard(
    has_messages: bool = False,
    latest_msg_id: Optional[str] = None,
    saved_count: int = 0,
    verify_link: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Build interactive buttons for Temp Mail management."""
    keyboard = []

    # If verification link is detected, show 1-tap Copy Link button at the top
    if verify_link and verify_link.startswith(("http://", "https://")):
        keyboard.append([
            make_copy_link_button(verify_link, "📋 Copy Link"),
        ])

    keyboard.append([
        InlineKeyboardButton("🔄 Check Inbox", callback_data="do:tempmail:check"),
        InlineKeyboardButton("🆕 New Email", callback_data="do:tempmail:new"),
    ])
    if has_messages and latest_msg_id:
        keyboard.append([
            InlineKeyboardButton("📖 Read Latest Email", callback_data=f"do:tempmail:read:{latest_msg_id}"),
        ])

    history_text = f"📜 Saved Emails ({saved_count})" if saved_count > 0 else "📜 Saved Emails"
    keyboard.append([
        InlineKeyboardButton(history_text, callback_data="do:tempmail:history"),
        InlineKeyboardButton("🏷️ Set Label", callback_data="do:tempmail:setlabel"),
    ])
    keyboard.append([
        InlineKeyboardButton("🔐 Backup Key", callback_data="do:tempmail:creds"),
        InlineKeyboardButton("🗑️ Close Active", callback_data="do:tempmail:close"),
    ])
    return InlineKeyboardMarkup(keyboard)


def make_history_keyboard(user_id: int | str) -> InlineKeyboardMarkup:
    """Build inline keyboard listing user's saved email accounts."""
    accounts = get_user_vault(user_id)
    keyboard = []

    # Display up to 10 most recent saved emails
    for idx, acc in enumerate(accounts[:10]):
        acc_id = acc.get("id") or acc.get("address")
        label = acc.get("label") or acc.get("address", "").split("@")[0]
        # Shorten button text to fit nicely on mobile
        btn_text = f"📧 {label[:18]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"do:tempmail:switch:{acc_id}")])

    keyboard.append([
        InlineKeyboardButton("➕ New Email", callback_data="do:tempmail:new"),
        InlineKeyboardButton("📬 Active Mail", callback_data="do:tempmail:show_active"),
    ])
    if accounts:
        keyboard.append([
            InlineKeyboardButton("🗑️ Delete from Vault", callback_data="do:tempmail:delmenu"),
        ])
    return InlineKeyboardMarkup(keyboard)


def make_delete_menu_keyboard(user_id: int | str) -> InlineKeyboardMarkup:
    """Build keyboard to pick which saved email to permanently delete from vault."""
    accounts = get_user_vault(user_id)
    keyboard = []
    for acc in accounts[:10]:
        acc_id = acc.get("id") or acc.get("address")
        label = acc.get("label") or acc.get("address", "").split("@")[0]
        btn_text = f"❌ Remove {label[:16]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"do:tempmail:delconfirm:{acc_id}")])

    keyboard.append([
        InlineKeyboardButton("🔙 Back to Saved List", callback_data="do:tempmail:history"),
    ])
    return InlineKeyboardMarkup(keyboard)


def format_active_mailbox_msg(account: dict, saved_count: int = 0) -> str:
    """Format active mailbox description message with full credentials for manual backup."""
    address = account.get("address", "Unknown")
    password = account.get("password", "")
    label = account.get("label") or "_None (Tap 🏷️ Set Label to add)_"
    created_at = account.get("created_at") or "Just now"

    lines = [
        "📬 **Your Disposable Temporary Email:**\n",
        f"📧 **Email:** `{address}`",
    ]
    if password:
        lines.append(f"🔑 **Password:** `{password}`")

    lines.extend([
        f"\n🏷️ **Label / Name:** {label}",
        f"📅 **Added:** {created_at}\n",
        "💡 _Tap on Email or Password above to copy._",
        "When an OTP or confirmation arrives, tap **🔄 Check Inbox** below!\n",
        "💾 **Manual Backup (VPS Reset Protection):**",
        "Save this email & password in your notes! Even if your VPS resets or gets reinstalled, you can restore this mailbox anytime with:",
        f"`/tempmail restore {address} {password}`",
    ])
    return "\n".join(lines)


def format_history_msg(user_id: int | str) -> str:
    """Format list of saved mailboxes."""
    accounts = get_user_vault(user_id)
    if not accounts:
        return (
            "📭 **No Saved Temporary Emails Found**\n\n"
            "You haven't generated or saved any disposable emails yet.\n"
            "Tap **➕ New Email** to generate one!"
        )

    lines = [
        f"📜 **Your Saved Temporary Email Vault ({len(accounts)}):**\n",
        "You can switch between these mailboxes anytime to receive future OTPs or verification links:\n",
    ]
    for idx, acc in enumerate(accounts[:10], start=1):
        label_part = f"🏷️ *{acc.get('label')}* — " if acc.get("label") else ""
        lines.append(f"{idx}. {label_part}`{acc.get('address')}`")
        if acc.get("password"):
            lines.append(f"   🔑 Password: `{acc.get('password')}`")
        lines.append(f"   🕒 Added: {acc.get('created_at', 'N/A')}\n")

    lines.append("👉 *Tap any email button below to restore & switch to it:*")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 5. COMMAND & CALLBACK HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_tempmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle /tempmail command:
    - /tempmail : show active email or load/create one
    - /tempmail history / list : list saved vault emails
    - /tempmail restore <email> <password> : restore external account
    - /tempmail custom <name> : create with custom username
    """
    if not update.effective_message or not update.effective_user:
        return

    user_id = update.effective_user.id
    user_data = context.user_data or {}
    args = context.args or []

    # Sub-command: /tempmail history or /tempmail list
    if args and args[0].lower() in ("history", "list", "vault"):
        msg_text = format_history_msg(user_id)
        await update.effective_message.reply_text(
            msg_text,
            parse_mode="Markdown",
            reply_markup=make_history_keyboard(user_id),
        )
        return

    # Sub-command: /tempmail restore <email> <password>
    if args and args[0].lower() in ("restore", "login") and len(args) >= 3:
        target_addr = args[1].strip()
        target_pass = args[2].strip()
        await update.effective_message.reply_text(f"⏳ Logging in to `{target_addr}` on Mail.tm…", parse_mode="Markdown")
        try:
            account = await login_mailbox(target_addr, target_pass)
            # Save into persistent vault
            entry = save_account_to_vault(user_id, target_addr, target_pass, account.get("account_id", ""))
            account["id"] = entry.get("id")
            account["label"] = entry.get("label", "")
            account["created_at"] = entry.get("created_at", "")
            if context.user_data is not None:
                context.user_data["tempmail"] = account

            saved_count = len(get_user_vault(user_id))
            await update.effective_message.reply_text(
                f"✅ **Mailbox Restored Successfully!**\n\n"
                f"{format_active_mailbox_msg(account, saved_count)}",
                parse_mode="Markdown",
                reply_markup=make_tempmail_keyboard(saved_count=saved_count),
            )
        except Exception as exc:
            log.exception("Manual restore failed")
            await update.effective_message.reply_text(f"❌ Failed to restore mailbox: {str(exc)[:400]}")
        return

    # Sub-command: /tempmail custom <username>
    if args and args[0].lower() in ("custom", "new") and len(args) >= 2:
        custom_name = args[1].strip()
        await update.effective_message.reply_text(f"⏳ Creating custom email with username `{custom_name}`…", parse_mode="Markdown")
        try:
            account = await create_mailbox(custom_username=custom_name)
            entry = save_account_to_vault(
                user_id,
                account["address"],
                account["password"],
                account.get("account_id", ""),
                label=custom_name,
            )
            account["id"] = entry.get("id")
            account["label"] = entry.get("label", custom_name)
            account["created_at"] = entry.get("created_at", "")
            if context.user_data is not None:
                context.user_data["tempmail"] = account

            saved_count = len(get_user_vault(user_id))
            await update.effective_message.reply_text(
                format_active_mailbox_msg(account, saved_count),
                parse_mode="Markdown",
                reply_markup=make_tempmail_keyboard(saved_count=saved_count),
            )
        except Exception as exc:
            log.exception("Custom tempmail creation failed")
            await update.effective_message.reply_text(f"❌ Failed to create custom email: {str(exc)[:400]}")
        return

    # Default /tempmail:
    active_mail = user_data.get("tempmail")
    vault_accounts = get_user_vault(user_id)

    # If no active email in memory, check if user has saved ones in vault
    if not active_mail or not active_mail.get("token"):
        if vault_accounts:
            # Auto-restore the most recent saved email
            latest_acc = vault_accounts[0]
            try:
                login_data = await login_mailbox(latest_acc["address"], latest_acc["password"])
                active_mail = {
                    "address": latest_acc["address"],
                    "password": latest_acc["password"],
                    "token": login_data["token"],
                    "account_id": login_data.get("account_id") or latest_acc.get("account_id", ""),
                    "id": latest_acc.get("id"),
                    "label": latest_acc.get("label", ""),
                    "created_at": latest_acc.get("created_at", ""),
                }
                if context.user_data is not None:
                    context.user_data["tempmail"] = active_mail
            except Exception as exc:
                log.warning("Could not auto-login to saved email %s: %s", latest_acc.get("address"), exc)
                active_mail = None

    # If still none, create a fresh one and save it
    if not active_mail or not active_mail.get("token"):
        await update.effective_message.reply_text("⏳ Generating a fresh temporary email address…")
        try:
            active_mail = await create_mailbox()
            entry = save_account_to_vault(
                user_id,
                active_mail["address"],
                active_mail["password"],
                active_mail.get("account_id", ""),
            )
            active_mail["id"] = entry.get("id")
            active_mail["label"] = entry.get("label", "")
            active_mail["created_at"] = entry.get("created_at", "")
            if context.user_data is not None:
                context.user_data["tempmail"] = active_mail
            vault_accounts = get_user_vault(user_id)
        except Exception as exc:
            log.exception("Temp mail creation failed")
            await update.effective_message.reply_text(f"❌ Could not create temporary email: {str(exc)[:400]}")
            return

    saved_count = len(vault_accounts)
    await update.effective_message.reply_text(
        format_active_mailbox_msg(active_mail, saved_count),
        parse_mode="Markdown",
        reply_markup=make_tempmail_keyboard(saved_count=saved_count),
    )


async def handle_tempmail_callback(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    extra_id: Optional[str] = None,
) -> None:
    """Handle all inline button callbacks for Temp Mail."""
    user_id = query.from_user.id
    user_data = context.user_data or {}
    active_mail = user_data.get("tempmail")
    vault_accounts = get_user_vault(user_id)

    # ── ACTION: Copy Verification Link ─────────────────────────────────────
    if action == "copylink":
        link = context.user_data.get("tempmail_last_verify_link") if context.user_data else None
        if not link and query.message:
            # Fallback extraction from message text
            target_text = query.message.text or query.message.caption or ""
            extracted = extract_verification_links(target_text)
            if extracted:
                link = extracted[0]

        if link:
            await query.answer("📋 Tap below to copy link!")
            if query.message:
                await safe_send_text(
                    query.message,
                    f"🔗 <b>Verification Link (Tap to Copy):</b>\n\n"
                    f"<code>{html.escape(link)}</code>\n\n"
                    f"💡 <i>Tip: Tap on the link block above to copy and paste anywhere.</i>",
                    parse_mode="HTML",
                )
        else:
            await query.answer("❌ No verification link found.", show_alert=True)
        return

    # ── ACTION: Show History Vault ──────────────────────────────────────────
    if action == "history":
        msg_text = format_history_msg(user_id)
        if query.message:
            await query.message.reply_text(
                msg_text,
                parse_mode="Markdown",
                reply_markup=make_history_keyboard(user_id),
            )
        return

    # ── ACTION: Switch to a Saved Email ─────────────────────────────────────
    if action == "switch" and extra_id:
        acc = get_account_by_id(user_id, extra_id)
        if not acc:
            if query.message:
                await query.message.reply_text("❌ Email not found in your saved vault.")
            return

        address = acc["address"]
        password = acc["password"]
        if query.message:
            await query.message.reply_text(f"⏳ Restoring mailbox `{address}`…", parse_mode="Markdown")

        try:
            login_data = await login_mailbox(address, password)
            new_active = {
                "address": address,
                "password": password,
                "token": login_data["token"],
                "account_id": login_data.get("account_id") or acc.get("account_id", ""),
                "id": acc.get("id"),
                "label": acc.get("label", ""),
                "created_at": acc.get("created_at", ""),
            }
            if context.user_data is not None:
                context.user_data["tempmail"] = new_active

            # Automatically check messages for restored email
            messages = await fetch_messages_safe(new_active)
            has_messages = bool(messages)
            latest_id = messages[0]["id"] if has_messages else None

            label_esc = f" (🏷️ <b>{html.escape(acc.get('label'))}</b>)" if acc.get("label") else ""
            status_text = (
                f"✅ <b>Restored & Active Mailbox:</b>\n\n"
                f"📧 <b>Email:</b> <code>{html.escape(address)}</code>{label_esc}\n"
                f"🔑 <b>Password:</b> <code>{html.escape(password)}</code>\n\n"
            )
            top_link = None
            if messages:
                latest = messages[0]
                sender = latest.get("from", {}).get("address", "Unknown")
                subject = latest.get("subject", "(No Subject)")
                intro = latest.get("intro", "")
                otp_code = None
                verify_links = []
                try:
                    detail = await fetch_message_detail_safe(new_active, latest["id"])
                    full_body = detail.get("text") or intro or ""
                    html_data = detail.get("html")
                    otp_code = extract_otp(full_body)
                    verify_links = extract_verification_links(full_body, html_data)
                except Exception:
                    pass

                status_text += (
                    f"📩 <b>Latest Incoming Email:</b>\n"
                    f"📌 <b>From:</b> <code>{html.escape(sender)}</code>\n"
                    f"📌 <b>Subject:</b> {html.escape(subject)}\n"
                )
                if otp_code:
                    status_text += f"\n🔑 <b>Detected OTP/Code:</b> <code>{html.escape(otp_code)}</code>\n"
                if verify_links:
                    top_link = verify_links[0]
                    status_text += (
                        f"\n🔗 <b>Detected Verification Link:</b>\n"
                        f"👉 <code>{html.escape(top_link)}</code>\n"
                        f"💡 <i>Tip: Tap on the link or use the copy button to copy.</i>\n"
                    )
            else:
                status_text += "📭 Inbox is currently empty. Tap <b>🔄 Check Inbox</b> when an OTP arrives."

            if top_link and context.user_data is not None:
                context.user_data["tempmail_last_verify_link"] = top_link

            if query.message:
                await safe_send_text(
                    query.message,
                    status_text,
                    reply_markup=make_tempmail_keyboard(
                        has_messages=has_messages,
                        latest_msg_id=latest_id,
                        saved_count=len(vault_accounts),
                        verify_link=top_link,
                    ),
                    parse_mode="HTML",
                )
        except Exception as exc:
            log.exception("Failed to switch mailbox")
            if query.message:
                await query.message.reply_text(
                    f"❌ Could not restore this mailbox: {str(exc)[:400]}\n\n"
                    f"The address may have been purged on Mail.tm servers.",
                    reply_markup=make_history_keyboard(user_id),
                )
        return

    # ── ACTION: Generate New Email ──────────────────────────────────────────
    if action == "new":
        if query.message:
            await query.message.reply_text("⏳ Generating a new disposable email address…")
        try:
            new_mail = await create_mailbox()
            entry = save_account_to_vault(
                user_id,
                new_mail["address"],
                new_mail["password"],
                new_mail.get("account_id", ""),
            )
            new_mail["id"] = entry.get("id")
            new_mail["label"] = entry.get("label", "")
            new_mail["created_at"] = entry.get("created_at", "")
            if context.user_data is not None:
                context.user_data["tempmail"] = new_mail

            saved_count = len(get_user_vault(user_id))
            if query.message:
                await query.message.reply_text(
                    format_active_mailbox_msg(new_mail, saved_count),
                    parse_mode="Markdown",
                    reply_markup=make_tempmail_keyboard(saved_count=saved_count),
                )
        except Exception as exc:
            log.exception("New tempmail generation failed")
            if query.message:
                await query.message.reply_text(f"❌ Failed to create new email: {str(exc)[:400]}")
        return

    # ── ACTION: Show Delete Menu ────────────────────────────────────────────
    if action == "delmenu":
        if query.message:
            await query.message.reply_text(
                "🗑️ **Delete a Saved Email from Vault:**\n\n"
                "Select which mailbox you want to permanently remove from your saved history:",
                parse_mode="Markdown",
                reply_markup=make_delete_menu_keyboard(user_id),
            )
        return

    # ── ACTION: Confirm Delete from Vault ───────────────────────────────────
    if action == "delconfirm" and extra_id:
        acc = get_account_by_id(user_id, extra_id)
        addr_name = acc.get("address", extra_id) if acc else extra_id
        removed = remove_account_from_vault(user_id, extra_id)

        # If it was the active mailbox, clear it from memory
        if active_mail and (active_mail.get("id") == extra_id or active_mail.get("address") == addr_name):
            if context.user_data is not None:
                context.user_data.pop("tempmail", None)

        if query.message:
            if removed:
                await query.message.reply_text(
                    f"🗑️ Removed `{addr_name}` from your saved email vault.",
                    parse_mode="Markdown",
                    reply_markup=make_history_keyboard(user_id),
                )
            else:
                await query.message.reply_text("Email not found in vault.")
        return

    # ── ACTION: Set Label / Name ────────────────────────────────────────────
    if action == "setlabel":
        if not active_mail or not active_mail.get("address"):
            if query.message:
                await query.message.reply_text("No active temporary email found. Send /tempmail first.")
            return
        if context.user_data is not None:
            context.user_data["awaiting_tempmail_label"] = True
        if query.message:
            await query.message.reply_text(
                f"🏷️ **Set a Label for this Email:**\n`{active_mail['address']}`\n\n"
                f"Reply with a short label (e.g. `Canva`, `ChatGPT`, `Facebook`, `Discord`, `Bank`) "
                f"so you can easily recognize it later in your saved history.",
                parse_mode="Markdown",
            )
        return

    # ── ACTION: Show Credentials for Manual Backup ──────────────────────────
    if action == "creds":
        if not active_mail or not active_mail.get("address"):
            if query.message:
                await query.message.reply_text("No active temporary email found. Send /tempmail first.")
            return
        addr = active_mail.get("address", "")
        pwd = active_mail.get("password", "")
        lbl = active_mail.get("label") or "Disposable Mailbox"
        creds_text = (
            f"🔐 **Manual Backup Credentials ({lbl}):**\n\n"
            f"📧 **Address:**\n`{addr}`\n\n"
            f"🔑 **Password:**\n`{pwd}`\n\n"
            f"📌 **One-Click Restore Command:**\n"
            f"`/tempmail restore {addr} {pwd}`\n\n"
            f"💡 _Tap on Email, Password, or Restore command above to copy._\n"
            f"Save this in your personal notes or Telegram Saved Messages! "
            f"Even if your VPS or server resets, you can restore this mailbox anytime."
        )
        if query.message:
            await query.message.reply_text(creds_text, parse_mode="Markdown")
        return

    # ── ACTION: Close Active Mailbox ────────────────────────────────────────
    if action == "close":
        if context.user_data is not None:
            context.user_data.pop("tempmail", None)
        if query.message:
            await query.message.reply_text(
                "📪 Active temporary mailbox closed.\n\n"
                "💡 _Don't worry! Your email is safely preserved in your **📜 Saved Emails** vault and can be restored anytime._",
                parse_mode="Markdown",
            )
        return

    # ── ACTION: Show Active Mailbox View ────────────────────────────────────
    if action == "show_active":
        if not active_mail or not active_mail.get("token"):
            if query.message:
                await query.message.reply_text("No active temporary email right now. Send /tempmail to view or create one.")
            return
        saved_count = len(get_user_vault(user_id))
        if query.message:
            await query.message.reply_text(
                format_active_mailbox_msg(active_mail, saved_count),
                parse_mode="Markdown",
                reply_markup=make_tempmail_keyboard(saved_count=saved_count),
            )
        return

    # ── Ensure active mailbox exists for check/read ─────────────────────────
    if not active_mail or not active_mail.get("token"):
        if query.message:
            await query.message.reply_text("No active temporary email found. Send /tempmail to create or restore one.")
        return

    address = active_mail["address"]

    # ── ACTION: Check Inbox ─────────────────────────────────────────────────
    if action == "check":
        try:
            messages = await fetch_messages_safe(active_mail)
            saved_count = len(get_user_vault(user_id))

            if not messages:
                if query.message:
                    await safe_send_text(
                        query.message,
                        f"📭 <b>Inbox is currently empty</b> for:\n<code>{html.escape(address)}</code>\n\n"
                        f"⏳ No emails received yet. Please wait a few moments and tap <b>🔄 Check Inbox</b> again.",
                        reply_markup=make_tempmail_keyboard(has_messages=False, saved_count=saved_count),
                        parse_mode="HTML",
                    )
                return

            latest = messages[0]
            latest_id = latest["id"]
            sender = latest.get("from", {}).get("address", "Unknown sender")
            subject = latest.get("subject", "(No Subject)")
            intro = latest.get("intro", "")

            # Fetch full message to parse OTP and verification links
            otp_code = None
            verify_links = []
            try:
                detail = await fetch_message_detail_safe(active_mail, latest_id)
                full_body = detail.get("text") or intro or ""
                html_data = detail.get("html")
                otp_code = extract_otp(full_body)
                verify_links = extract_verification_links(full_body, html_data)
            except Exception:
                pass

            report = [
                f"📩 <b>Incoming Email Received!</b> (Total: {len(messages)})",
                "━━━━━━━━━━━━━━━━━━━━",
                f"📌 <b>From:</b> <code>{html.escape(sender)}</code>",
                f"📌 <b>Subject:</b> {html.escape(subject)}",
            ]
            if otp_code:
                report.append(f"\n🔑 <b>Detected Verification / OTP Code:</b>\n👉 <code>{html.escape(otp_code)}</code>\n")
            if verify_links:
                first_link = verify_links[0]
                report.append(f"\n🔗 <b>Detected Verification / Action Link:</b>\n👉 <code>{html.escape(first_link)}</code>\n")
                if len(verify_links) > 1:
                    report.append("📎 <i>Additional links found:</i>")
                    for extra_l in verify_links[1:]:
                        report.append(f"• <code>{html.escape(extra_l)}</code>")
                    report.append("")
                report.append("💡 <i>Tip: Tap on any link or button to copy it.</i>\n")
            if intro:
                report.append(f"📝 <b>Preview:</b>\n{html.escape(intro[:320])}")
            report.append("━━━━━━━━━━━━━━━━━━━━")

            verify_btn_link = verify_links[0] if (verify_links and verify_links[0].startswith(("http://", "https://"))) else None
            if verify_btn_link and context.user_data is not None:
                context.user_data["tempmail_last_verify_link"] = verify_btn_link

            if query.message:
                await safe_send_text(
                    query.message,
                    "\n".join(report),
                    reply_markup=make_tempmail_keyboard(
                        has_messages=True,
                        latest_msg_id=latest_id,
                        saved_count=saved_count,
                        verify_link=verify_btn_link,
                    ),
                    parse_mode="HTML",
                )
        except Exception as exc:
            log.exception("Tempmail inbox check failed")
            if query.message:
                await safe_send_text(query.message, f"❌ Failed to check inbox: {str(exc)[:400]}", parse_mode=None)
        return

    # ── ACTION: Read Full Message ───────────────────────────────────────────
    if action == "read" and extra_id:
        try:
            detail = await fetch_message_detail_safe(active_mail, extra_id)
            sender = detail.get("from", {}).get("address", "Unknown")
            subject = detail.get("subject", "(No Subject)")
            date = detail.get("createdAt", "")[:19].replace("T", " ")
            body = (detail.get("text") or "No plain text content available.").strip()
            html_data = detail.get("html")

            otp_code = extract_otp(body)
            verify_links = extract_verification_links(body, html_data)
            primary_link = verify_links[0] if verify_links else None
            if primary_link and context.user_data is not None:
                context.user_data["tempmail_last_verify_link"] = primary_link

            header = [
                "📧 <b>Full Email Message</b>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"👤 <b>From:</b> <code>{html.escape(sender)}</code>",
                f"📅 <b>Date:</b> {html.escape(date)}",
                f"📋 <b>Subject:</b> {html.escape(subject)}",
            ]
            if otp_code:
                header.append(f"\n🔑 <b>Detected OTP:</b> <code>{html.escape(otp_code)}</code>")
            if verify_links:
                header.append(f"\n🔗 <b>Detected Verification Link:</b>\n👉 <code>{html.escape(primary_link)}</code>")
                if len(verify_links) > 1:
                    header.append("📎 <i>Other links in message:</i>")
                    for extra_l in verify_links[1:]:
                        header.append(f"• <code>{html.escape(extra_l)}</code>")
                header.append("💡 <i>Tip: Tap on the link or use the copy button to copy.</i>")
            header.append("━━━━━━━━━━━━━━━━━━━━\n")

            header_str = "\n".join(header)
            # Safe body truncation before escaping to avoid broken HTML tags
            max_body_chars = max(500, 3500 - len(header_str))
            if len(body) > max_body_chars:
                trimmed_body = body[:max_body_chars] + "\n\n… [Truncated due to Telegram message length limit]"
            else:
                trimmed_body = body

            safe_body = html.escape(trimmed_body)
            full_text = header_str + safe_body

            read_buttons = []
            if primary_link and primary_link.startswith(("http://", "https://")):
                read_buttons.append([make_copy_link_button(primary_link, "📋 Copy Link")])
            read_buttons.append([InlineKeyboardButton("🔄 Back to Active Mail", callback_data="do:tempmail:show_active")])

            read_markup = InlineKeyboardMarkup(read_buttons)
            if query.message:
                await safe_send_text(query.message, full_text, reply_markup=read_markup, parse_mode="HTML")
        except Exception as exc:
            log.exception("Read message detail failed")
            if query.message:
                await safe_send_text(query.message, f"❌ Failed to read message: {str(exc)[:400]}", parse_mode=None)
        return
