"""Telegram through the user's own account (Telethon): sign-in and downloads.

Public channels also work without this through the engine's t.me crawler.
With a signed-in account NEXDROP can also download:
  - files over the public preview's size limit and documents (zip, pdf, ...)
  - channels and groups the account has joined, including private ones
    (t.me/c/<id>/<post>, t.me/+invite once joined)

Sign-in (run by the window, one JSON object per line on stdin and stdout):
    python telegram_account.py --login
        {"cmd": "send_code", "apiId": 123, "apiHash": "...", "phone": "+60..."}
        {"cmd": "code", "code": "12345"}
        {"cmd": "password", "password": "..."}     (only for two-step verification)
    replies {"step": "code" | "password" | "done" | "error", ...}; "done" carries
    the session string, which the window stores encrypted and never shows.

    python telegram_account.py --check     (stdin: {"apiId", "apiHash", "session"})

Downloads are run by cairn_session through TelegramDownloads.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
THIRDPARTY = ROOT / "engine" / "thirdparty"
if THIRDPARTY.is_dir() and str(THIRDPARTY) not in sys.path:
    sys.path.insert(0, str(THIRDPARTY))

HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
_RESERVED = {"s", "c", "joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "iv", "login",
             "setlanguage", "contact", "boost", "addlist", "m", "i", "k", "z", "a"}
DEVICE = {"device_model": "NEXDROP", "system_version": "Windows", "app_version": "NEXDROP 2.3"}
MAX_PARALLEL = 4  # Telegram slows an account that pulls many files at once


def available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("telethon") is not None
    except (ImportError, ValueError):
        return False


def is_telegram(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.") in HOSTS
    except ValueError:
        return False


@dataclass
class Link:
    kind: str  # channel, post, private_channel, private_post, invite
    peer: str = ""  # username
    channel_id: int = 0  # t.me/c/<id>
    message_id: int = 0
    single: bool = False
    invite: str = ""
    url: str = ""


def parse_link(url: str) -> Link | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").removeprefix("www.") not in HOSTS:
        return None
    single = "single" in parse_qs(parts.query, keep_blank_values=True)
    path = [p for p in parts.path.split("/") if p]
    if path and path[0] == "s":
        path = path[1:]
    if not path:
        return None
    first = path[0]
    if first.startswith("+") and len(first) > 1:
        return Link("invite", invite=first[1:], url=url)
    if first == "joinchat" and len(path) > 1:
        return Link("invite", invite=path[1], url=url)
    if first == "c" and len(path) == 2 and path[1].isdigit():
        # t.me/c/<channel>: the whole private channel or group
        return Link("private_channel", channel_id=int(path[1]), url=url)
    if first == "c" and len(path) >= 3 and path[1].isdigit() and path[-1].isdigit():
        # t.me/c/<channel>/<post> or t.me/c/<channel>/<topic>/<post>
        return Link("private_post", channel_id=int(path[1]), message_id=int(path[-1]), single=single, url=url)
    if first.lower() in _RESERVED or not _NAME_RE.match(first):
        return None
    if len(path) == 1:
        return Link("channel", peer=first, url=url)
    if path[-1].isdigit():
        return Link("post", peer=first, message_id=int(path[-1]), single=single, url=url)
    return None


def message_link(chat, message_id: int) -> str:
    """The link a message has in both modes, so history is shared with the public crawler."""
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{getattr(chat, 'id', 0)}/{message_id}"


def chat_title(chat) -> str:
    return (getattr(chat, "title", None) or " ".join(
        x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x
    ) or getattr(chat, "username", None) or f"chat {getattr(chat, 'id', '')}").strip()


_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".avif"}
_VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".wmv"}
_AUDIO = {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wav", ".aac"}


def kind_of(ext: str) -> str:
    ext = ext.lower()
    return "images" if ext in _IMAGE else "videos" if ext in _VIDEO else "audio" if ext in _AUDIO else "other"


def describe(message) -> dict | None:
    """What NEXDROP would save from a message, or None when it carries no file."""
    if getattr(message, "sticker", None) or getattr(message, "web_preview", None) and not message.file:
        return None
    file = getattr(message, "file", None)
    if file is None or not (getattr(message, "photo", None) or getattr(message, "document", None)):
        return None
    ext = (file.ext or "").lower()
    if ext in {".jpe", ""} and getattr(message, "photo", None):
        ext = ".jpg"
    if ext == ".oga":
        ext = ".ogg"
    plain = bool(getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "voice", None)
                 or getattr(message, "video_note", None) or getattr(message, "gif", None))
    return {"ext": ext or ".bin", "size": file.size or 0, "original": "" if plain else (file.name or ""),
            "grouped_id": getattr(message, "grouped_id", None)}


def file_name(chat, message, info: dict) -> str:
    base = f"{getattr(chat, 'username', None) or 'c' + str(getattr(chat, 'id', 0))}_{message.id}"
    original = info.get("original") or ""
    if original:
        stem = original if original.lower().endswith(info["ext"]) else original + info["ext"]
        return f"{base}_{stem}"
    return base + info["ext"]


def _real_client(account: dict):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(account["session"]), int(account["apiId"]), account["apiHash"],
                          flood_sleep_threshold=120, request_retries=4, **DEVICE)


class StopRun(Exception):
    pass


class TelegramDownloads:
    """Downloads Telegram links with the signed-in account.

    `hooks` connects it to the session: update(url, **fields) for the file list,
    skip(url) -> reason or "" for history and filters, number(url, name) -> (n, name),
    saved(url), failed(url, reason), skipped(url, reason), log(level, text), stop (Event).
    """

    def __init__(self, account: dict, folder: Path, hooks, *, parallel: int = MAX_PARALLEL, type_folders: bool = False,
                 file_types: dict | None = None, min_image_kb: int = 0, client_factory=None) -> None:
        self.account = account
        self.folder = folder
        self.hooks = hooks
        self.parallel = max(1, min(parallel, MAX_PARALLEL))
        self.type_folders = type_folders
        self.file_types = file_types or {}
        self.min_image = max(0, int(min_image_kb or 0)) * 1024
        self.client = None
        self._client_factory = client_factory or _real_client
        self._dialogs_loaded = False
        self._seen: set[str] = set()  # the same message from two links in one run is saved once
        self._names = threading.Lock()

    # ── public ───────────────────────────────────────────────────────────────
    def run(self, links: list[str]) -> None:
        asyncio.run(self._run(links))

    # ── internals ────────────────────────────────────────────────────────────
    async def _run(self, links: list[str]) -> None:
        client = self._client_factory(self.account)
        self.client = client
        await client.connect()
        try:
            if not await client.is_user_authorized():
                for url in links:
                    self.hooks.failed(url, "Telegram sign-in expired. Sign in again in Settings > Sign-ins.")
                self.hooks.log("error", "The Telegram sign-in is no longer valid (signed out from another device?). Sign in again in Settings > Sign-ins.")
                return
            for url in links:
                if self.hooks.stop.is_set():
                    break
                link = parse_link(url)
                if link is None:
                    self.hooks.failed(url, "Not a Telegram channel, post or invite link")
                    continue
                try:
                    await self._one(link)
                except StopRun:
                    break
                except Exception as error:  # noqa: BLE001 - one bad link must not stop the others
                    self.hooks.failed(url, explain(error))
                    self.hooks.log("warning", f"{url}: {explain(error)}")
        finally:
            await client.disconnect()

    async def _entity(self, link: Link):
        from telethon import functions, types

        client = self.client
        if link.kind == "invite":
            result = await client(functions.messages.CheckChatInviteRequest(link.invite))
            chat = getattr(result, "chat", None)
            if isinstance(result, types.ChatInviteAlready) or isinstance(result, types.ChatInvitePeek):
                return chat
            raise PermissionError("This account has not joined that chat. Join it in Telegram first, then paste the link again.")
        if link.kind in {"private_post", "private_channel"}:
            try:
                return await client.get_entity(types.PeerChannel(link.channel_id))
            except (ValueError, TypeError):
                if not self._dialogs_loaded:
                    self._dialogs_loaded = True
                    await client.get_dialogs(limit=None)  # fills the cache with every chat the account is in
                    return await client.get_entity(types.PeerChannel(link.channel_id))
                raise
        return await client.get_entity(link.peer)

    async def _one(self, link: Link) -> None:
        chat = await self._entity(link)
        title = chat_title(chat)
        folder = self.folder / safe_folder(f"{title} (Telegram)")
        if link.kind in {"post", "private_post"}:
            message = await self.client.get_messages(chat, ids=link.message_id)
            if message is None:
                raise LookupError(f"Message {link.message_id} was not found (deleted, or not visible to this account)")
            messages = [message]
            if message.grouped_id and not link.single:
                nearby = await self.client.get_messages(chat, ids=list(range(max(1, message.id - 9), message.id + 10)))
                messages = sorted((m for m in nearby if m is not None and m.grouped_id == message.grouped_id), key=lambda m: m.id)
            await self._download_all(chat, folder, self._iter_list(messages))
            return
        self.hooks.log("info", f"Reading {title} with your Telegram account, oldest post first.")
        await self._download_all(chat, folder, self.client.iter_messages(chat, reverse=True))

    async def _iter_list(self, messages):
        for message in messages:
            yield message

    async def _download_all(self, chat, folder: Path, messages) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.parallel * 4)
        found = 0

        async def worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    await self._save(chat, folder, *item)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.parallel)]
        try:
            async for message in messages:
                if self.hooks.stop.is_set():
                    break
                info = describe(message)
                if info is None:
                    continue
                found += 1
                url = message_link(chat, message.id)
                if url in self._seen:
                    continue
                self._seen.add(url)
                reason = self._skip_reason(url, info)
                if reason:
                    self.hooks.skipped(url, reason)
                    continue
                number, name = self.hooks.number(url, file_name(chat, message, info))
                self.hooks.update(url, name=name, sequence=number or None, state="queued", total=info["size"] or None)
                await queue.put((message, url, name, info))
        finally:
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers, return_exceptions=True)
        if found == 0:
            self.hooks.log("info", f"{chat_title(chat)}: no files in the messages this account can see.")

    def _skip_reason(self, url: str, info: dict) -> str:
        kind = kind_of(info["ext"])
        if self.file_types and self.file_types.get(kind) is False:
            return "file type turned off in Settings"
        if kind == "images" and self.min_image and info["size"] and info["size"] < self.min_image:
            return "smaller than the minimum size"
        return self.hooks.skip(url)

    async def _save(self, chat, folder: Path, message, url: str, name: str, info: dict) -> None:
        if self.hooks.stop.is_set():
            self.hooks.update(url, state="cancelled", reason="stopped before it finished", speed=0)
            return
        target_dir = folder / (TYPE_DIRS[kind_of(info["ext"])] if self.type_folders else "")
        target_dir.mkdir(parents=True, exist_ok=True)
        with self._names:
            target = unique(target_dir, safe_file(name))
            target.touch()
        partial = target.with_name(target.name + ".part")
        started = time.monotonic()
        state = {"last": 0.0, "read": 0, "samples": []}
        self.hooks.update(url, name=target.name, state="active", read=0, total=info["size"] or None, reason="")

        def progress(received: int, total: int) -> None:
            if self.hooks.stop.is_set():
                raise StopRun
            delta = received - state["read"]
            state["read"] = received
            self.hooks.add_bytes(max(0, delta))
            now = time.monotonic()
            samples = state["samples"]
            samples.append((now, received))
            while len(samples) > 2 and now - samples[0][0] > 3:
                samples.pop(0)
            if now - state["last"] >= 0.2:
                state["last"] = now
                span = max(now - samples[0][0], 1e-3)
                speed = int((received - samples[0][1]) / span) if len(samples) > 1 else int(received / max(now - started, 1e-3))
                self.hooks.update(url, read=received, total=total or None, speed=speed)

        try:
            for attempt in range(3):
                try:
                    await self.client.download_media(message, file=str(partial), progress_callback=progress)
                    break
                except StopRun:
                    raise
                except Exception as error:  # noqa: BLE001
                    if attempt == 2 or not transient(error):
                        raise
                    self.hooks.update(url, state="retrying", reason=explain(error))
                    state["read"] = 0
                    await asyncio.sleep(3 * (attempt + 1))
                    message = await self.client.get_messages(chat, ids=message.id) or message  # fresh file reference
            if not partial.exists():
                raise OSError("Telegram sent no data for this file")
            os.replace(partial, target)
        except StopRun:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            self.hooks.update(url, state="cancelled", reason="stopped before it finished", speed=0)
            return
        except Exception as error:  # noqa: BLE001
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            self.hooks.failed(url, explain(error))
            return
        size = target.stat().st_size
        self.hooks.update(url, state="done", read=size, total=size, speed=0, reason="", path=str(target))
        self.hooks.saved(url, target)


TYPE_DIRS = {"images": "Images", "videos": "Videos", "audio": "Audio", "other": "Other"}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_file(name: str) -> str:
    name = _BAD.sub("_", name).strip(" .") or "file"
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.split(".")[0].upper() in _WINDOWS_RESERVED:
        stem = "_" + stem
    if len(stem) > 150:
        stem = stem[:150]
    return f"{stem}.{ext}" if ext else stem


def safe_folder(name: str) -> str:
    name = _BAD.sub("_", name).strip(" .")[:120] or "Telegram"
    return "_" + name if name.split(".")[0].upper() in _WINDOWS_RESERVED else name


def unique(folder: Path, name: str) -> Path:
    candidate = folder / name
    stem, ext = os.path.splitext(name)
    number = 1
    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = folder / f"{stem} ({number}){ext}"
        number += 1
    return candidate


def transient(error: BaseException) -> bool:
    name = type(error).__name__
    return isinstance(error, (ConnectionError, TimeoutError, EOFError, asyncio.TimeoutError)) or name in {
        "FileReferenceExpiredError", "ServerError", "RpcCallFailError", "TimedOutError", "FileMigrateError",
    }


def explain(error: BaseException) -> str:
    name = type(error).__name__
    text = str(error).split(" (caused by")[0].strip()
    known = {
        "ChannelPrivateError": "This channel is private or this account was removed from it",
        "ChannelInvalidError": "This channel does not exist or this account cannot see it",
        "UsernameNotOccupiedError": "No Telegram channel or user has that name",
        "UsernameInvalidError": "That is not a valid Telegram name",
        "InviteHashExpiredError": "That invite link has expired",
        "InviteHashInvalidError": "That invite link is not valid",
        "AuthKeyUnregisteredError": "Telegram sign-in expired. Sign in again in Settings > Sign-ins.",
        "SessionRevokedError": "Telegram sign-in was ended from another device. Sign in again in Settings > Sign-ins.",
        "UserDeactivatedBanError": "Telegram has banned this account",
        "FileReferenceExpiredError": "Telegram's link to this file expired; try again",
        "ChatForwardsRestrictedError": "This chat does not allow saving content",
    }
    if name in known:
        return known[name]
    if name == "FloodWaitError":
        return f"Telegram asked NEXDROP to wait {getattr(error, 'seconds', '?')} s (too many requests); try again later"
    if isinstance(error, PermissionError | LookupError):
        return text
    if "Cannot find any entity" in text or "Could not find the input entity" in text:
        return "This account can't see that chat. Join it in Telegram first."
    return f"{name}: {text}"[:200] if text else name


# ─── sign-in helper ──────────────────────────────────────────────────────────

def _out(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _login_error(error: BaseException) -> dict:
    name = type(error).__name__
    messages = {
        "ApiIdInvalidError": ("api", "Telegram rejected the API ID and API hash. Copy both from my.telegram.org > API development tools."),
        "PhoneNumberInvalidError": ("phone", "Telegram says that phone number is not valid. Include the country code, for example +60123456789."),
        "PhoneNumberBannedError": ("phone", "Telegram has banned this phone number."),
        "PhoneNumberFloodError": ("phone", "Too many sign-in attempts. Wait a while before asking for another code."),
        "PhoneCodeInvalidError": ("code", "That code is not right. Check the latest message from Telegram and type it again."),
        "PhoneCodeExpiredError": ("code", "That code has expired. Ask for a new one."),
        "PhoneCodeEmptyError": ("code", "Type the code Telegram sent you."),
        "PasswordHashInvalidError": ("password", "That two-step verification password is not right."),
        "SendCodeUnavailableError": ("phone", "Telegram cannot send another code right now. Wait a few minutes."),
    }
    if name in messages:
        field, text = messages[name]
        return {"step": "error", "field": field, "message": text}
    if name == "FloodWaitError":
        return {"step": "error", "field": "phone", "message": f"Telegram asks you to wait {getattr(error, 'seconds', '?')} seconds before trying again."}
    if isinstance(error, (ConnectionError, OSError, EOFError, asyncio.TimeoutError)) or name in {"InvalidBufferError", "TypeNotFoundError"}:
        # EOFError covers IncompleteReadError: a proxy or firewall cut the Telegram connection.
        return {"step": "error", "field": "", "message": "Could not reach Telegram (the connection was cut). Check the internet connection, VPN or firewall, then try again."}
    return {"step": "error", "field": "", "message": f"{name}: {error}"[:300]}


def _who(me) -> dict:
    name = " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x) or "your account"
    return {"name": name, "username": getattr(me, "username", None) or ""}


async def login_loop(read_line=None) -> int:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    loop = asyncio.get_running_loop()
    read_line = read_line or (lambda: sys.stdin.readline())
    client = None
    phone = ""
    try:
        while True:
            line = await loop.run_in_executor(None, read_line)
            if not line:
                return 0
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            cmd = command.get("cmd")
            try:
                if cmd == "send_code":
                    api_id = int(str(command.get("apiId") or "0").strip() or 0)
                    api_hash = str(command.get("apiHash") or "").strip()
                    phone = re.sub(r"[^\d+]", "", str(command.get("phone") or ""))
                    if api_id <= 0 or not re.fullmatch(r"[0-9a-f]{32}", api_hash):
                        _out({"step": "error", "field": "api", "message": "The API ID is a number and the API hash is 32 letters and digits, both from my.telegram.org."})
                        continue
                    if len(phone) < 8:
                        _out({"step": "error", "field": "phone", "message": "Type the phone number with its country code, for example +60123456789."})
                        continue
                    if client is not None:
                        await client.disconnect()
                    client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
                    await client.connect()
                    sent = await client.send_code_request(phone)
                    via = type(getattr(sent, "type", None)).__name__.replace("SentCodeType", "").lower()
                    where = "in the Telegram app" if via == "app" else "by SMS" if via == "sms" else "by Telegram"
                    _out({"step": "code", "message": f"Telegram sent a login code {where}. Type it below."})
                elif cmd == "code" and client is not None:
                    code = re.sub(r"\D", "", str(command.get("code") or ""))
                    try:
                        await client.sign_in(phone=phone, code=code)
                    except errors.SessionPasswordNeededError:
                        _out({"step": "password", "message": "This account has two-step verification. Type its password."})
                        continue
                    _out({"step": "done", "session": client.session.save(), **_who(await client.get_me())})
                elif cmd == "password" and client is not None:
                    await client.sign_in(password=str(command.get("password") or ""))
                    _out({"step": "done", "session": client.session.save(), **_who(await client.get_me())})
                elif cmd == "cancel":
                    return 0
                else:
                    _out({"step": "error", "field": "", "message": "Start again with Send code."})
            except Exception as error:  # noqa: BLE001
                _out(_login_error(error))
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def check(account: dict) -> dict:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(account.get("session") or ""), int(account.get("apiId") or 0),
                            str(account.get("apiHash") or ""), **DEVICE)
    try:
        await asyncio.wait_for(client.connect(), 30)
        if not await client.is_user_authorized():
            return {"state": "invalid", "message": "The Telegram sign-in has ended. Sign in again."}
        who = _who(await client.get_me())
        handle = f" (@{who['username']})" if who["username"] else ""
        return {"state": "valid", "message": f"Signed in to Telegram as {who['name']}{handle}."}
    except Exception as error:  # noqa: BLE001
        return {"state": "error", "message": _login_error(error)["message"]}
    finally:
        await client.disconnect()


def main(argv: list[str]) -> int:
    if not available():
        _out({"step": "error", "state": "error", "field": "", "message": "The Telegram library is missing from this NEXDROP folder (engine/thirdparty)."})
        return 1
    if "--login" in argv:
        return asyncio.run(login_loop())
    if "--check" in argv:
        account = json.loads(sys.stdin.readline() or "{}")
        _out(asyncio.run(check(account)))
        return 0
    _out({"step": "error", "message": "Use --login or --check"})
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
