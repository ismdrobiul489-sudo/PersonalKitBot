"""
Temporary Email (Disposable Mailbox / OTP Receiver) Module for Telegram Utility Bot.
Uses the 100% free Mail.tm REST API (no API key required).
"""

import logging
import re
import secrets
from typing import Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

log = logging.getLogger("tempmail")

BASE_URL = "https://api.mail.tm"
TIMEOUT = 12.0


def extract_otp(text: str) -> Optional[str]:
    """Extract a 4 to 8 digit verification/OTP code from email body text."""
    if not text:
        return None
    # 1. Look for explicit keyword patterns like "code: 123456", "OTP: 1234", "is 123456"
    keyword_match = re.search(
        r"(?:code|otp|verification|pin|password|token|is)[:\s]+([0-9]{4,8})\b",
        text,
        re.IGNORECASE,
    )
    if keyword_match:
        return keyword_match.group(1)

    # 2. Fallback: look for standalone 4-8 digit numbers
    candidates = re.findall(r"\b[0-9]{4,8}\b", text)
    if candidates:
        # Avoid common false positives like years (1990-2035)
        filtered = [c for c in candidates if not (len(c) == 4 and 1990 <= int(c) <= 2035)]
        if filtered:
            return filtered[0]
        return candidates[0]
    return None


async def create_mailbox() -> dict:
    """Create a new random temporary email inbox on Mail.tm and acquire a JWT token."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Step 1: Fetch active domains
        dom_resp = await client.get(f"{BASE_URL}/domains")
        dom_resp.raise_for_status()
        dom_data = dom_resp.json()
        members = dom_data.get("hydra:member") or []
        if not members:
            raise RuntimeError("No active domains available from Mail.tm.")
        domain = members[0]["domain"]

        # Step 2: Generate random username & strong password
        username = f"usr_{secrets.token_hex(4)}"
        address = f"{username}@{domain}"
        password = secrets.token_urlsafe(12)

        # Step 3: Register account
        reg_resp = await client.post(
            f"{BASE_URL}/accounts",
            json={"address": address, "password": password},
        )
        reg_resp.raise_for_status()
        account_id = reg_resp.json().get("id")

        # Step 4: Login to obtain JWT Bearer token
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


async def fetch_messages(token: str) -> list[dict]:
    """Retrieve incoming email list for an active mailbox."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json().get("hydra:member") or []


async def fetch_message_detail(token: str, message_id: str) -> dict:
    """Retrieve full email content (text body, sender, html, date)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


def make_tempmail_keyboard(has_messages: bool = False, latest_msg_id: Optional[str] = None) -> InlineKeyboardMarkup:
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
    keyboard.append([
        InlineKeyboardButton("🗑️ Delete Mailbox", callback_data="do:tempmail:delete"),
    ])
    return InlineKeyboardMarkup(keyboard)


async def cmd_tempmail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tempmail command: show existing active email or generate a new one."""
    if not update.effective_message:
        return

    user_data = context.user_data or {}
    active_mail = user_data.get("tempmail")

    if not active_mail or not active_mail.get("token"):
        await update.effective_message.reply_text("⏳ Generating a fresh temporary email address…")
        try:
            active_mail = await create_mailbox()
            if context.user_data is not None:
                context.user_data["tempmail"] = active_mail
        except Exception as exc:
            log.exception("Temp mail creation failed")
            await update.effective_message.reply_text(f"❌ Could not create temporary email: {str(exc)[:400]}")
            return

    address = active_mail["address"]
    msg_text = (
        f"📬 **Your Disposable Temporary Email:**\n\n"
        f"`{address}`\n\n"
        f"💡 _Tap the address above to copy it._\n"
        f"Use this email to sign up on any website or app. "
        f"When they send a verification code or link, tap **Check Inbox** below!"
    )
    await update.effective_message.reply_text(
        msg_text,
        parse_mode="Markdown",
        reply_markup=make_tempmail_keyboard(),
    )


async def handle_tempmail_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str, extra_id: Optional[str] = None) -> None:
    """Handle inline button callbacks for Temp Mail (check, new, read, delete)."""
    user_data = context.user_data or {}
    active_mail = user_data.get("tempmail")

    if action == "new":
        if query.message:
            await query.message.reply_text("⏳ Generating a new temporary email…")
        try:
            new_mail = await create_mailbox()
            if context.user_data is not None:
                context.user_data["tempmail"] = new_mail
            address = new_mail["address"]
            msg_text = (
                f"📬 **New Disposable Temporary Email:**\n\n"
                f"`{address}`\n\n"
                f"💡 _Tap the address above to copy._\n"
                f"When a verification code arrives, tap **Check Inbox** below!"
            )
            if query.message:
                await query.message.reply_text(
                    msg_text,
                    parse_mode="Markdown",
                    reply_markup=make_tempmail_keyboard(),
                )
        except Exception as exc:
            log.exception("New tempmail generation failed")
            if query.message:
                await query.message.reply_text(f"❌ Failed to create new email: {str(exc)[:400]}")
        return

    if not active_mail or not active_mail.get("token"):
        if query.message:
            await query.message.reply_text("No active temporary email found. Use /tempmail to create one.")
        return

    token = active_mail["token"]
    address = active_mail["address"]

    if action == "delete":
        if context.user_data is not None:
            context.user_data.pop("tempmail", None)
        if query.message:
            await query.message.reply_text("🗑️ Temporary mailbox deleted. Send /tempmail anytime you need a new one.")
        return

    if action == "check":
        try:
            messages = await fetch_messages(token)
            if not messages:
                if query.message:
                    await query.message.reply_text(
                        f"📭 **Inbox is currently empty** for:\n`{address}`\n\n"
                        f"⏳ No emails received yet. Please wait a few seconds and tap **Check Inbox** again.",
                        parse_mode="Markdown",
                        reply_markup=make_tempmail_keyboard(has_messages=False),
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
                detail = await fetch_message_detail(token, latest_id)
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
                report.append(f"\n🔑 **Detected Verification/OTP Code:** `{otp_code}`\n")
            if intro:
                report.append(f"📝 **Preview:**\n{intro[:350]}")
            report.append("━━━━━━━━━━━━━━━━━━━━")

            if query.message:
                await query.message.reply_text(
                    "\n".join(report),
                    parse_mode="Markdown",
                    reply_markup=make_tempmail_keyboard(has_messages=True, latest_msg_id=latest_id),
                )
        except Exception as exc:
            log.exception("Tempmail inbox check failed")
            if query.message:
                await query.message.reply_text(f"❌ Failed to check inbox: {str(exc)[:400]}")
        return

    if action == "read" and extra_id:
        try:
            detail = await fetch_message_detail(token, extra_id)
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

