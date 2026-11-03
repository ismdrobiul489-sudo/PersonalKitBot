"""
Temporary Email (Disposable Mailbox / OTP Receiver) Module for Telegram Utility Bot.
Uses the 100% free Mail.tm REST API (no API key required).
Supports persistent email vault to save, label, switch, and restore old disposable addresses anytime.
Includes manual backup credentials (email + password) for VPS-reset protection.
"""

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

def make_tempmail_keyboard(
    has_messages: bool = False,
    latest_msg_id: Optional[str] = None,
    saved_count: int = 0,
) -> InlineKeyboardMarkup:
    """Build interactive buttons for Temp Mail management."""
    keyboard = [
        [
            InlineKeyboardButton("🔄 Check Inbox", callback_data="do:tempmail:check"),
            InlineKeyboardButton("🆕 New Email", callback_data="do:tempmail:new"),
        ],
    ]
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

            label_str = f" (🏷️ *{acc.get('label')}*)" if acc.get("label") else ""
            status_text = (
                f"✅ **Restored & Active Mailbox:**\n\n"
                f"📧 **Email:** `{address}`{label_str}\n"
                f"🔑 **Password:** `{password}`\n\n"
            )
            if messages:
                latest = messages[0]
                sender = latest.get("from", {}).get("address", "Unknown")
                subject = latest.get("subject", "(No Subject)")
                intro = latest.get("intro", "")
                otp_code = None
                try:
                    detail = await fetch_message_detail_safe(new_active, latest["id"])
                    otp_code = extract_otp(detail.get("text") or intro or "")
                except Exception:
                    pass

                status_text += (
                    f"📩 **Latest Incoming Email:**\n"
                    f"📌 **From:** `{sender}`\n"
                    f"📌 **Subject:** {subject}\n"
                )
                if otp_code:
                    status_text += f"\n🔑 **Detected OTP/Code:** `{otp_code}`\n"
            else:
                status_text += "📭 Inbox is currently empty. Tap **🔄 Check Inbox** when an OTP arrives."

            if query.message:
                await query.message.reply_text(
                    status_text,
                    parse_mode="Markdown",
                    reply_markup=make_tempmail_keyboard(
                        has_messages=has_messages,
                        latest_msg_id=latest_id,
                        saved_count=len(vault_accounts),
                    ),
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
                    await query.message.reply_text(
                        f"📭 **Inbox is currently empty** for:\n`{address}`\n\n"
                        f"⏳ No emails received yet. Please wait a few moments and tap **🔄 Check Inbox** again.",
                        parse_mode="Markdown",
                        reply_markup=make_tempmail_keyboard(has_messages=False, saved_count=saved_count),
                    )
                return

            latest = messages[0]
            latest_id = latest["id"]
            sender = latest.get("from", {}).get("address", "Unknown sender")
            subject = latest.get("subject", "(No Subject)")
            intro = latest.get("intro", "")

            # Fetch full message to parse OTP
            otp_code = None
            try:
                detail = await fetch_message_detail_safe(active_mail, latest_id)
                full_body = detail.get("text") or intro or ""
                otp_code = extract_otp(full_body)
            except Exception:
                pass

            report = [
                f"📩 **Incoming Email Received!** (Total: {len(messages)})",
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📌 **From:** `{sender}`",
                f"📌 **Subject:** {subject}",
            ]
            if otp_code:
                report.append(f"\n🔑 **Detected Verification / OTP Code:** `{otp_code}`\n")
            if intro:
                report.append(f"📝 **Preview:**\n{intro[:350]}")
            report.append("━━━━━━━━━━━━━━━━━━━━")

            if query.message:
                await query.message.reply_text(
                    "\n".join(report),
                    parse_mode="Markdown",
                    reply_markup=make_tempmail_keyboard(
                        has_messages=True,
                        latest_msg_id=latest_id,
                        saved_count=saved_count,
                    ),
                )
        except Exception as exc:
            log.exception("Tempmail inbox check failed")
            if query.message:
                await query.message.reply_text(f"❌ Failed to check inbox: {str(exc)[:400]}")
        return

    # ── ACTION: Read Full Message ───────────────────────────────────────────
    if action == "read" and extra_id:
        try:
            detail = await fetch_message_detail_safe(active_mail, extra_id)
            sender = detail.get("from", {}).get("address", "Unknown")
            subject = detail.get("subject", "(No Subject)")
            date = detail.get("createdAt", "")[:19].replace("T", " ")
            body = (detail.get("text") or "No plain text content available.").strip()

            otp_code = extract_otp(body)
            header = [
                f"📧 **Full Email Message**",
                f"━━━━━━━━━━━━━━━━━━━━",
                f"👤 **From:** `{sender}`",
                f"📅 **Date:** {date}",
                f"📋 **Subject:** {subject}",
            ]
            if otp_code:
                header.append(f"🔑 **OTP Code:** `{otp_code}`")
            header.append("━━━━━━━━━━━━━━━━━━━━\n")

            full_text = "\n".join(header) + body
            if len(full_text) <= 3900:
                if query.message:
                    await query.message.reply_text(full_text, parse_mode="Markdown")
            else:
                if query.message:
                    await query.message.reply_text(full_text[:3900] + "\n\n… [Truncated due to length]")
        except Exception as exc:
            log.exception("Read message detail failed")
            if query.message:
                await query.message.reply_text(f"❌ Failed to read message: {str(exc)[:400]}")
        return
