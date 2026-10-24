"""节点进程状态。"""

import asyncio
import json
import logging
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    PasswordHashInvalidError,
    PeerFloodError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeHashEmptyError,
    PhoneCodeInvalidError,
    PhoneHashExpiredError,
    PhoneNotOccupiedError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    PhonePasswordFloodError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from meiguan.node import node_for_dc

log = logging.getLogger("meiguan.node")
SESSIONS = Path("/opt/meiguan/sessions")


HANDOFF = SESSIONS / "handoff"


DEVICE = {
    "device_model": "meiguan",
    "system_version": "Linux",
    "app_version": "1.0",
    "lang_code": "zh",
    "system_lang_code": "zh-CN",
    "flood_sleep_threshold": 0,
    "sequential_updates": True,
    "catch_up": False,
    "receive_updates": True,
    "connection_retries": 5,
    "request_retries": 5,
}


clients: dict[int, TelegramClient] = {}


accounts: dict[str, dict] = {}


session_notes: dict[int, str] = {}


def session_note(user_id: int) -> str:
    name = str((accounts.get(str(user_id)) or {}).get("name") or "")
    if name == "已注销账号":
        return "Telegram：账号已注销"
    return "Telegram：登录已失效，请重新登录"


def mark_session(user_id: int) -> str:
    note = session_note(user_id)
    session_notes[user_id] = note
    _emit_expiry(user_id, note)
    return note


def clear_session(user_id: int) -> None:
    session_notes.pop(user_id, None)


def credentials() -> tuple[int, str] | None:
    raw_id = os.environ.get("API_ID", "").strip()
    raw_hash = os.environ.get("API_HASH", "").strip()
    if not raw_id or not raw_hash:
        return None
    return int(raw_id), raw_hash


def clean_profile(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        api_id = int(raw.get("api_id") or 0)
    except (TypeError, ValueError):
        return None
    api_hash = str(raw.get("api_hash") or "").strip()
    if api_id <= 0 or api_id > 2_000_000_000 or not api_hash or len(api_hash) > 64:
        return None
    profile = {"api_id": api_id, "api_hash": api_hash}
    for key in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code", "lang_pack"):
        value = str(raw.get(key) or "").strip()
        if value:
            profile[key] = value[:64]
    return profile


def client_file(user_id: int) -> Path:
    return SESSIONS / f"{int(user_id)}.client.json"


def load_client(user_id: int) -> dict | None:
    path = client_file(user_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return clean_profile(raw)


def save_client(user_id: int, profile: dict) -> None:
    cleaned = clean_profile(profile)
    if cleaned is None:
        return
    path = client_file(user_id)
    path.write_text(json.dumps(cleaned, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def make_client(session, profile: dict | None = None) -> TelegramClient:
    picked = clean_profile(profile) if profile else None
    if picked:
        api_id, api_hash = int(picked["api_id"]), str(picked["api_hash"])
    else:
        api_id, api_hash = credentials() or (0, "")
    kwargs = dict(DEVICE)
    if picked:
        for key in ("device_model", "system_version", "app_version", "lang_code", "system_lang_code"):
            if picked.get(key):
                kwargs[key] = picked[key]
    client = TelegramClient(session, api_id, api_hash, **kwargs)
    lang = str((picked or {}).get("lang_pack") or "")
    if lang:
        client._init_request.lang_pack = lang
    # 路过的用户不写入会话。同步写库会堵住更新循环，消息堆在内存里。
    if hasattr(client.session, "save_entities"):
        client.session.save_entities = False
    return client


def node_id() -> str:
    return os.environ.get("NODE_ID", "").strip()


def owns(dc: int) -> bool:
    node = node_for_dc(dc)
    return node is not None and node.node_id == node_id()


def unlink_session(base: Path) -> None:
    for suffix in (".session", ".session-journal", ".client.json"):
        path = Path(str(base) + suffix)
        if path.exists():
            path.unlink()


def error_code(exc: Exception) -> dict | None:
    if isinstance(exc, (FloodWaitError, FloodTestPhoneWaitError, FloodPremiumWaitError)):
        return {"ok": False, "error": "flood", "seconds": int(exc.seconds)}
    if isinstance(exc, PhoneNumberFloodError):
        return {"ok": False, "error": "phone_flood"}
    if isinstance(exc, PhonePasswordFloodError):
        return {"ok": False, "error": "password_flood"}
    if isinstance(exc, PhoneNumberInvalidError):
        return {"ok": False, "error": "invalid_phone"}
    if isinstance(exc, PhoneNumberBannedError):
        return {"ok": False, "error": "banned"}
    if isinstance(exc, (PhoneNumberUnoccupiedError, PhoneNotOccupiedError)):
        return {"ok": False, "error": "unoccupied"}
    if isinstance(exc, PhoneCodeInvalidError):
        return {"ok": False, "error": "invalid_code"}
    if isinstance(exc, PhoneCodeEmptyError):
        return {"ok": False, "error": "code_empty"}
    if isinstance(exc, (PhoneCodeExpiredError, PhoneHashExpiredError, PhoneCodeHashEmptyError)):
        return {"ok": False, "error": "code_expired"}
    if isinstance(exc, PasswordHashInvalidError):
        return {"ok": False, "error": "invalid_password"}
    if isinstance(exc, (AuthKeyUnregisteredError, AuthKeyInvalidError)):
        return {"ok": False, "error": "revoked"}
    if isinstance(exc, PeerFloodError):
        return {"ok": False, "error": "peer_flood"}
    if isinstance(exc, (UsernameInvalidError, UsernameNotOccupiedError)):
        return {"ok": False, "error": "no_bot"}
    return None


_wallet_locks: dict[int, asyncio.Lock] = {}


def wallet_lock(user_id: int) -> asyncio.Lock:
    lock = _wallet_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _wallet_locks[user_id] = lock
    return lock


_watch: asyncio.StreamWriter | None = None


_watch_lock: asyncio.Lock | None = None


async def _emit(obj: dict) -> None:
    writer = _watch
    lock = _watch_lock
    if writer is None or lock is None:
        return
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    try:
        async with lock:
            if _watch is not writer:
                return
            writer.write(data)
            await writer.drain()
    except Exception:
        return


def add_brands(items) -> None:
    rows = [item for item in items or [] if item.get("nick")]
    if not rows:
        return
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "brand", "items": rows}))
    except RuntimeError:
        return


def add_sighting(item: dict) -> None:
    if not item.get("owner_id"):
        return
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "sighting", "item": item}))
    except RuntimeError:
        return


def add_watch(item: dict) -> None:
    if not item.get("owner_id"):
        return
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "watch", "item": item}))
    except RuntimeError:
        return


def add_claim(item: dict) -> None:
    if not item.get("owner_id"):
        return
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "claim", "item": item}))
    except RuntimeError:
        return


def add_skip(item: dict) -> None:
    if not item.get("owner_id"):
        return
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "skip", "item": item}))
    except RuntimeError:
        return


def expiry_events() -> list[dict]:
    rows = []
    for user_id, note in list(session_notes.items()):
        row = accounts.get(str(user_id)) or {}
        owner = int(row.get("owner_id") or 0)
        if not owner:
            continue
        rows.append({
            "owner_id": owner,
            "user_id": int(user_id),
            "name": str(row.get("name") or ""),
            "note": note,
        })
    return rows


def _emit_expiry(user_id: int, note: str) -> None:
    row = accounts.get(str(user_id)) or {}
    owner = int(row.get("owner_id") or 0)
    if not owner:
        return
    item = {
        "owner_id": owner,
        "user_id": int(user_id),
        "name": str(row.get("name") or ""),
        "note": note,
    }
    try:
        asyncio.get_running_loop().create_task(_emit({"event": "expired", "item": item}))
    except RuntimeError:
        return
