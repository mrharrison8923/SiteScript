"""
Telegram Group Greeter — Telethon userbot.

Monitors a specific group and greets new members with a simple message.
Uses user (not bot) authorization via Telegram API.
QR-code login for convenience.
Supports multiple account profiles.
"""

import asyncio
import json
import logging
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
SESSION_FILE = str(BASE_DIR / "greet_bot.session")
ENV_FILE = BASE_DIR / ".env"
PROFILES_FILE = BASE_DIR / "profiles.json"
ACTIVE_PROFILE_FILE = BASE_DIR / "active_profile.txt"

GREETING_TEMPLATE = "Хелов, {name}."
MIN_DELAY = 0  # seconds
MAX_DELAY = 0  # seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("greet_bot")

# ---------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------


def _load_profiles() -> dict:
    """Load all profiles from profiles.json."""
    if not PROFILES_FILE.exists():
        return {}
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_profiles(profiles: dict) -> None:
    """Save profiles to profiles.json."""
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


def _get_active_profile() -> str | None:
    """Get the name of the currently active profile."""
    if not ACTIVE_PROFILE_FILE.exists():
        return None
    return ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip() or None


def _set_active_profile(name: str) -> None:
    """Set the active profile."""
    ACTIVE_PROFILE_FILE.write_text(name, encoding="utf-8")


def _select_profile() -> str | None:
    """Interactive profile selection. Returns profile name or None to exit."""
    profiles = _load_profiles()

    # If .env exists, offer it as "default" profile
    if ENV_FILE.exists():
        profiles.setdefault("default", {})

    if not profiles:
        log.info("No profiles found. Using .env configuration.")
        return "default"

    print("\n" + "=" * 50)
    print("  Доступні профілі акаунтів")
    print("=" * 50)

    profile_names = list(profiles.keys())
    for i, name in enumerate(profile_names, 1):
        marker = " (активний)" if name == _get_active_profile() else ""
        print(f"  {i}. {name}{marker}")

    print(f"  {len(profile_names) + 1}. Додати новий профіль")
    print(f"  0. Вийти")
    print("=" * 50)

    while True:
        try:
            choice = input("\nВиберіть профіль (номер): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if choice == "0":
            return None

        if choice == str(len(profile_names) + 1):
            return _create_profile()

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(profile_names):
                selected = profile_names[idx]
                _set_active_profile(selected)
                log.info("Активний профіль: %s", selected)
                return selected
        except ValueError:
            pass

        print("Невірний вибір. Спробуйте ще раз.")


def _create_profile() -> str | None:
    """Create a new profile interactively. Returns profile name or None."""
    print("\n" + "=" * 50)
    print("  Створення нового профілю")
    print("=" * 50)

    try:
        name = input("Назва профілю (наприклад: 'робочий'): ").strip()
        if not name:
            print("Назва не може бути порожньою.")
            return None

        api_id = input("API_ID: ").strip()
        api_hash = input("API_HASH: ").strip()
        phone = input("Номер телефону (+380...): ").strip()
        group_id = input("ID групи (-100...): ").strip()

        if not all([api_id, api_hash, phone, group_id]):
            print("Усі поля обов'язкові.")
            return None

        profiles = _load_profiles()
        profiles[name] = {
            "api_id": api_id,
            "api_hash": api_hash,
            "phone": phone,
            "group_id": group_id,
        }
        _save_profiles(profiles)
        _set_active_profile(name)

        log.info("Профіль '%s' створено та активовано.", name)
        return name

    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _get_profile_config(profile_name: str | None) -> dict:
    """Get configuration for the active profile."""
    profiles = _load_profiles()

    if profile_name and profile_name in profiles:
        p = profiles[profile_name]
        return {
            "API_ID": p.get("api_id", ""),
            "API_HASH": p.get("api_hash", ""),
            "PHONE_NUMBER": p.get("phone", ""),
            "TARGET_GROUP_ID": p.get("group_id", ""),
        }

    # Fallback to .env
    load_dotenv(ENV_FILE)
    return {
        "API_ID": os.getenv("API_ID", ""),
        "API_HASH": os.getenv("API_HASH", ""),
        "PHONE_NUMBER": os.getenv("PHONE_NUMBER", ""),
        "TARGET_GROUP_ID": os.getenv("TARGET_GROUP_ID", ""),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_config(config: dict) -> list[str]:
    """Return a list of configuration errors (empty if all OK)."""
    errors = []
    if not config.get("API_ID", "").strip().isdigit():
        errors.append("API_ID is missing or invalid")
    if not config.get("API_HASH", "").strip():
        errors.append("API_HASH is missing")
    if not config.get("TARGET_GROUP_ID", "").strip().lstrip("-").isdigit():
        errors.append("TARGET_GROUP_ID is missing or invalid")
    return errors


def _safe_phone(phone: str) -> str:
    """Return a masked phone number for logging."""
    if not phone or len(phone) < 4:
        return "***"
    return f"***{phone[-4:]}"


async def _get_user_input(prompt: str) -> str:
    """Async wrapper for input() so the event loop is not blocked."""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


# ---------------------------------------------------------------------------
# QR Code display
# ---------------------------------------------------------------------------


def _display_qr(url: str) -> None:
    """Display QR code in terminal and save as image."""
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)

        qr.print_ascii(invert=True)

        img_path = BASE_DIR / "tg_qr.png"
        qr.make_image().save(str(img_path))
        log.info("QR code also saved to: %s", img_path)

    except ImportError:
        log.info("Install 'qrcode' package for visual QR: pip install qrcode[pil]")
        log.info("Open this URL in your browser to scan with Telegram:")
        log.info("%s", url)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def authorize(client: TelegramClient, phone: str) -> bool:
    """
    Perform user authorization flow via QR code.
    Returns True on success, False on unrecoverable error.
    """
    log.info("Connecting to Telegram ...")

    try:
        await client.connect()
    except Exception as exc:
        log.error("Connection failed: %s", type(exc).__name__)
        return False

    if await client.is_user_authorized():
        log.info("Session is already authorized — skipping login.")
        return True

    log.info("Generating QR code for login ...")
    log.info("Open Telegram on your phone:")
    log.info("  Settings > Devices > Link Desktop Device")
    log.info("Then scan the QR code below (or open the URL).\n")

    try:
        qr_login = await client.qr_login()
    except Exception as exc:
        log.error("Failed to generate QR code: %s", type(exc).__name__)
        return False

    _display_qr(qr_login.url)

    log.info("Waiting for you to scan the QR code (timeout: 120 s) ...")

    try:
        await asyncio.wait_for(qr_login.wait(), timeout=120)
        log.info("QR code scanned successfully!")
    except asyncio.TimeoutError:
        log.error("QR code expired (timeout). Please restart the script.")
        return False
    except SessionPasswordNeededError:
        log.info("QR accepted, but 2FA password is required.")
    except Exception as exc:
        log.error("QR login failed: %s", type(exc).__name__)
        return False

    if not await client.is_user_authorized():
        log.info("QR accepted, but 2FA password is required.")
        for pw_attempt in range(1, 4):
            try:
                password = (await _get_user_input(
                    f"Enter 2FA password (attempt {pw_attempt}/3): "
                )).strip()
            except (EOFError, KeyboardInterrupt):
                log.info("Aborted by user.")
                return False

            try:
                await client.sign_in(password=password)
                log.info("2FA password accepted.")
                break
            except Exception:
                log.warning("Wrong 2FA password. Try again.")
        else:
            log.error("Too many wrong 2FA attempts.")
            return False

    me = await client.get_me()
    if me:
        log.info("Authorized as: %s (id=%d)", me.first_name or "?", me.id)
    else:
        log.info("Authorized successfully.")
    return True


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

_processed_events: set[int] = set()


async def _debug_log_event(event: events.NewMessage.Event) -> None:
    """Log ALL messages in the target group for diagnostics."""
    try:
        if event.chat_id == int(TARGET_GROUP_ID):
            log.debug("MESSAGE in target group: %s", event.text[:50] if event.text else "(no text)")
    except Exception:
        pass


async def _debug_log_chataction(event: events.ChatAction.Event) -> None:
    """Log ALL ChatAction events in the target group for diagnostics."""
    try:
        if event.chat_id == int(TARGET_GROUP_ID):
            log.debug("CHATACTION: joined=%s added=%s kicked=%s user=%s",
                     event.user_joined, event.user_added, event.user_kicked, event.user)
    except Exception:
        pass


async def _handle_new_member(event: events.ChatAction.Event) -> None:
    """Greet a newly added user in the target group."""
    try:
        if event.chat_id != int(TARGET_GROUP_ID):
            return

        if event.id in _processed_events:
            return
        _processed_events.add(event.id)

        log.info("ChatAction event received: joined=%s, added=%s, user=%s",
                 event.user_joined, event.user_added, event.user)

        user: User | None = event.user
        if user is None:
            try:
                user = await event.get_input_user()
            except Exception:
                pass

        if user is None:
            log.info("New member detected but could not fetch user info (event %d).", event.id)
            return

        if getattr(user, "bot", False):
            log.info("New member is a bot (id=%d) — skipping greeting.", user.id)
            return

        name = f"@{user.username}" if user.username else None
        if not name:
            name = user.first_name or f"[друг](tg://user?id={user.id})"

        log.info("New member detected: %s (id=%d)", name, user.id)

        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        log.info("Waiting %.1f s before greeting ...", delay)
        await asyncio.sleep(delay)

        await event.client.send_message(event.chat_id, GREETING_TEMPLATE.format(name=name))
        log.info("Greeting sent to %s.", name)

    except FloodWaitError as e:
        log.warning("FloodWait: need to wait %d s. Skipping this greeting.", e.seconds)
    except Exception as exc:
        log.error("Error handling new member: %s", type(exc).__name__)


async def _handle_service_message(event: events.NewMessage.Event) -> None:
    """Fallback: catch join events sent as service messages."""
    try:
        if event.chat_id != int(TARGET_GROUP_ID):
            return

        if not event.message or not event.message.action:
            return

        from telethon.tl.types import MessageActionChatAddUser, MessageActionChatJoinedByLink

        if isinstance(event.message.action, MessageActionChatAddUser):
            user_ids = event.message.action.users
            for user_id in user_ids:
                if user_id in _processed_events:
                    continue
                _processed_events.add(user_id)

                try:
                    user = await event.client.get_entity(user_id)
                except Exception:
                    log.info("Added user (id=%d) but could not fetch info.", user_id)
                    continue

                if getattr(user, "bot", False):
                    log.info("Added user is a bot (id=%d) — skipping.", user.id)
                    continue

                name = f"@{user.username}" if user.username else None
                if not name:
                    name = user.first_name or f"[друг](tg://user?id={user.id})"
                log.info("New member (via add): %s (id=%d)", name, user.id)

                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                log.info("Waiting %.1f s before greeting ...", delay)
                await asyncio.sleep(delay)

                await event.client.send_message(event.chat_id, GREETING_TEMPLATE.format(name=name))
                log.info("Greeting sent to %s.", name)

        elif isinstance(event.message.action, MessageActionChatJoinedByLink):
            user_id = event.message.sender_id
            if user_id in _processed_events:
                return
            _processed_events.add(user_id)

            try:
                user = await event.client.get_entity(user_id)
            except Exception:
                log.info("Joined user (id=%d) but could not fetch info.", user_id)
                return

            if getattr(user, "bot", False):
                log.info("Joined user is a bot (id=%d) — skipping.", user.id)
                return

            name = f"@{user.username}" if user.username else None
            if not name:
                name = user.first_name or f"[друг](tg://user?id={user.id})"
            log.info("New member (via link): %s (id=%d)", name, user.id)

            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            log.info("Waiting %.1f s before greeting ...", delay)
            await asyncio.sleep(delay)

            await event.client.send_message(event.chat_id, GREETING_TEMPLATE.format(name=name))
            log.info("Greeting sent to %s.", name)

    except FloodWaitError as e:
        log.warning("FloodWait: need to wait %d s. Skipping this greeting.", e.seconds)
    except Exception as exc:
        log.error("Error handling service message: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Polling fallback
# ---------------------------------------------------------------------------

_known_members: set[int] = set()
_polling_task: asyncio.Task | None = None


async def _poll_members(client: TelegramClient) -> None:
    """Periodically check for new members (fallback for closed groups)."""
    global _known_members

    log.info("Starting member polling (every 5 s) ...")

    try:
        async for user in client.iter_participants(int(TARGET_GROUP_ID)):
            _known_members.add(user.id)
        log.info("Initial member count: %d", len(_known_members))
    except Exception as exc:
        log.error("Failed to fetch initial members: %s", type(exc).__name__)
        return

    while True:
        await asyncio.sleep(5)
        try:
            current_members: set[int] = set()
            async for user in client.iter_participants(int(TARGET_GROUP_ID)):
                current_members.add(user.id)

            new_members = current_members - _known_members
            for user_id in new_members:
                if user_id in _processed_events:
                    continue
                _processed_events.add(user_id)

                try:
                    user = await client.get_entity(user_id)
                except Exception:
                    log.info("New member (id=%d) but could not fetch info.", user_id)
                    continue

                if getattr(user, "bot", False):
                    log.info("New member is a bot (id=%d) — skipping.", user.id)
                    continue

                name = f"@{user.username}" if user.username else None
                if not name:
                    name = user.first_name or f"[друг](tg://user?id={user.id})"
                log.info("New member detected (via polling): %s (id=%d)", name, user.id)

                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                log.info("Waiting %.1f s before greeting ...", delay)
                await asyncio.sleep(delay)

                await client.send_message(int(TARGET_GROUP_ID), GREETING_TEMPLATE.format(name=name))
                log.info("Greeting sent to %s.", name)

            _known_members = current_members

        except FloodWaitError as e:
            log.warning("FloodWait during polling: wait %d s.", e.seconds)
            await asyncio.sleep(e.seconds)
        except Exception as exc:
            log.error("Polling error: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Global config (set in main)
API_ID = ""
API_HASH = ""
PHONE_NUMBER = ""
TARGET_GROUP_ID = ""


async def main() -> None:
    """Entry point."""
    global API_ID, API_HASH, PHONE_NUMBER, TARGET_GROUP_ID

    log.info("=== Telegram Group Greeter ===")

    # Profile selection
    profile_name = _select_profile()
    if profile_name is None:
        log.info("Exiting.")
        sys.exit(0)

    # Load config
    config = _get_profile_config(profile_name)
    API_ID = config["API_ID"]
    API_HASH = config["API_HASH"]
    PHONE_NUMBER = config["PHONE_NUMBER"]
    TARGET_GROUP_ID = config["TARGET_GROUP_ID"]

    # Validate config
    config_errors = _validate_config(config)
    if config_errors:
        for err in config_errors:
            log.error("Config error: %s", err)
        log.error("Please check your configuration.")
        sys.exit(1)

    # Create client
    client = TelegramClient(SESSION_FILE, int(API_ID), API_HASH)

    # Authorize
    try:
        ok = await authorize(client, PHONE_NUMBER)
    except AuthKeyUnregisteredError:
        log.error("Session key is unregistered. Delete the .session file and try again.")
        sys.exit(1)
    except Exception as exc:
        log.error("Authorization failed: %s", type(exc).__name__)
        sys.exit(1)

    if not ok:
        log.error("Authorization was not successful. Exiting.")
        sys.exit(1)

    # Register debug handler
    client.add_event_handler(_debug_log_event, events.NewMessage(chats=int(TARGET_GROUP_ID)))
    client.add_event_handler(_debug_log_chataction, events.ChatAction(chats=int(TARGET_GROUP_ID)))

    # Register main handlers
    client.add_event_handler(
        _handle_new_member,
        events.ChatAction(chats=int(TARGET_GROUP_ID)),
    )
    client.add_event_handler(
        _handle_service_message,
        events.NewMessage(chats=int(TARGET_GROUP_ID)),
    )

    log.info("Monitoring group %s for new members ...", TARGET_GROUP_ID)
    log.info("Press Ctrl+C to stop.")

    # Start polling fallback
    global _polling_task
    _polling_task = asyncio.create_task(_poll_members(client))

    try:
        await client.run_until_disconnected()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        if _polling_task:
            _polling_task.cancel()
        await client.disconnect()
        log.info("Disconnected. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
