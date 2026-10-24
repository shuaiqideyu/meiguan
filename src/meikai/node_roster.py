"""节点账号。"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import SQLiteSession, StringSession

from meikai.grab import attach
from meikai.node_host import (
    HANDOFF,
    SESSIONS,
    accounts,
    clean_profile,
    clear_session,
    clients,
    error_code,
    make_client,
    node_id,
    owns,
    save_client,
    session_notes,
    unlink_session,
)

log = logging.getLogger("meikai.node")

_ORGS = {
    7487372978: "major",
    8365813836: "hold",
}


def org_name(bot_id) -> str:
    try:
        number = int(bot_id or 0)
    except (TypeError, ValueError):
        return ""
    return _ORGS.get(number, "")


def stored_org(record: dict) -> str:
    raw = str(record.get("org") or "").strip().lower()
    if raw in {"hold", "major"}:
        return raw
    if record.get("hold") is True:
        return "hold"
    return ""

_save_handle: asyncio.TimerHandle | None = None


def touch_active(user_id: int) -> None:
    record = accounts.get(str(user_id))
    if record is None:
        return
    record["active_at"] = datetime.now(timezone.utc).isoformat()
    global _save_handle
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        save_accounts()
        return
    if _save_handle is not None:
        return
    _save_handle = loop.call_later(1.5, _flush_accounts)


def _flush_accounts() -> None:
    global _save_handle
    _save_handle = None
    try:
        save_accounts()
    except OSError:
        log.warning("节点：账号没有写入")


def save_accounts() -> None:
    global _save_handle
    if _save_handle is not None:
        _save_handle.cancel()
        _save_handle = None
    path = SESSIONS / "accounts.json"
    path.write_text(json.dumps(accounts, ensure_ascii=False, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def session_added_at(user_id: int) -> str:
    path = SESSIONS / f"{user_id}.session"
    if not path.exists():
        return datetime.now(timezone.utc).isoformat()
    stat = path.stat()
    stamp = min(stat.st_mtime, getattr(stat, "st_ctime", stat.st_mtime))
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def public_usernames(me) -> list[str]:
    found = []
    seen = set()

    def add(value) -> None:
        name = str(value or "").strip().lstrip("@")
        if not name or name.lower() in seen:
            return
        seen.add(name.lower())
        found.append(name)

    add(getattr(me, "username", None))
    for item in getattr(me, "usernames", None) or []:
        if not getattr(item, "active", False):
            continue
        add(getattr(item, "username", None))
    return found


def public_username(me) -> str:
    names = public_usernames(me)
    return names[0] if names else ""


def phone_text(value) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        return ""
    return "+" + digits


def birthday_text(value) -> str:
    if value is None:
        return ""
    try:
        day = int(getattr(value, "day", 0) or 0)
        month = int(getattr(value, "month", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    year = getattr(value, "year", None)
    try:
        year_n = int(year) if year else 0
    except (TypeError, ValueError):
        year_n = 0
    if year_n:
        return f"{year_n}年{month}月{day}日"
    return f"{month}月{day}日"


def about_text(value) -> str:
    return str(value or "").strip()[:140]


def profile_marks(user, full_user) -> dict:
    nft = any(
        bool(getattr(item, "active", False)) and not bool(getattr(item, "editable", False))
        for item in (user.usernames or [])
    )
    verification = getattr(full_user, "bot_verification", None)
    marks = {
        "premium": bool(user.premium),
        "nft": nft,
        "org": org_name(getattr(verification, "bot_id", 0) if verification else 0),
        "about": about_text(getattr(full_user, "about", None)),
        "verified": bool(getattr(user, "verified", False)),
        "birthday": birthday_text(getattr(full_user, "birthday", None)),
        "restricted": bool(getattr(user, "restricted", False)),
        "scam": bool(getattr(user, "scam", False)),
        "fake": bool(getattr(user, "fake", False)),
    }
    phone = phone_text(getattr(user, "phone", None))
    if phone:
        marks["phone"] = phone
    lang = str(getattr(user, "lang_code", None) or "").strip()[:16]
    if lang:
        marks["lang"] = lang
    return marks


async def read_marks(client: TelegramClient) -> dict:
    from telethon.tl.functions.users import GetFullUserRequest

    me = await client.get_me()
    full = await client(GetFullUserRequest(me))
    marks = profile_marks(me, full.full_user)
    names = public_usernames(me)
    marks["username"] = names[0] if names else ""
    marks["usernames"] = names
    return marks


async def apply_marks(client: TelegramClient, user_id: int) -> None:
    try:
        marks = await asyncio.wait_for(read_marks(client), timeout=8)
    except Exception:
        log.warning("profile marks failed user=%s", user_id)
        return
    row = accounts.get(str(user_id))
    if row is None:
        return
    row.update(marks)
    row.pop("hold", None)
    save_accounts()


def remember(me, dc: int, owner_id: int | None = None) -> dict:
    previous = accounts.get(str(me.id), {})
    owner = previous.get("owner_id") if owner_id is None else owner_id
    added_at = previous.get("added_at") or session_added_at(me.id)
    names = public_usernames(me)
    record = {
        "user_id": me.id,
        "username": names[0] if names else "",
        "usernames": names,
        "name": " ".join(part for part in (me.first_name, me.last_name) if part),
        "dc": dc,
        "node_id": node_id(),
        "owner_id": owner,
        "added_at": added_at,
        "premium": bool(getattr(me, "premium", False)),
        "nft": bool(previous.get("nft")),
        "org": stored_org(previous),
        "phone": phone_text(getattr(me, "phone", None)) or str(previous.get("phone") or ""),
        "about": str(previous.get("about") or ""),
        "verified": bool(getattr(me, "verified", False)),
        "birthday": str(previous.get("birthday") or ""),
        "lang": str(getattr(me, "lang_code", None) or previous.get("lang") or "").strip()[:16],
        "restricted": bool(getattr(me, "restricted", False)),
        "scam": bool(getattr(me, "scam", False)),
        "fake": bool(getattr(me, "fake", False)),
        "active_at": str(previous.get("active_at") or ""),
    }
    accounts[str(me.id)] = record
    save_accounts()
    return record


async def disconnect_user(user_id: int) -> None:
    client = clients.pop(user_id, None)
    if client is not None:
        await client.disconnect()
    unlink_session(SESSIONS / str(user_id))


async def place_session(session_string: str, owner_id: int | None = None, app: dict | None = None) -> dict:
    app = clean_profile(app) if app else None
    probe = make_client(StringSession(session_string), app)
    try:
        await asyncio.wait_for(probe.connect(), timeout=20)
        me = await asyncio.wait_for(probe.get_me(), timeout=15)
    except Exception:
        await probe.disconnect()
        raise
    if me is None:
        await probe.disconnect()
        return {"status": "empty"}
    previous = accounts.get(str(me.id))
    if previous and owner_id is not None and int(previous.get("owner_id") or 0) not in (0, int(owner_id)):
        await probe.disconnect()
        return {"status": "taken"}
    dc = int(probe.session.dc_id)
    server = probe.session.server_address
    port = probe.session.port
    auth_key = probe.session.auth_key
    await probe.disconnect()
    card = {
        "user_id": me.id,
        "username": me.username or "",
        "name": " ".join(part for part in (me.first_name, me.last_name) if part),
        "dc": dc,
    }
    SESSIONS.mkdir(parents=True, exist_ok=True)
    SESSIONS.chmod(0o700)
    if not owns(dc):
        HANDOFF.mkdir(parents=True, exist_ok=True)
        path = HANDOFF / f"{me.id}.json"
        body = {"session": session_string, "dc": dc}
        if app:
            body["app"] = app
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        moved = {"status": "move", "session": session_string, **card}
        if app:
            moved["app"] = app
        return moved
    await disconnect_user(me.id)
    sqlite = SQLiteSession(str(SESSIONS / str(me.id)))
    sqlite.set_dc(dc, server, port)
    sqlite.auth_key = auth_key
    sqlite.save()
    Path(sqlite.filename).chmod(0o600)
    if app:
        save_client(me.id, app)
    live = make_client(sqlite, app)
    try:
        await asyncio.wait_for(live.connect(), timeout=20)
    except Exception:
        await live.disconnect()
        raise
    clear_session(me.id)
    clients[me.id] = live
    remember(me, dc, owner_id)
    attach(live, me.id)
    return {"status": "ready", **card}


async def op_adopt(req: dict) -> dict:
    session = str(req.get("session") or "")
    if not session:
        return {"ok": False, "error": "not_found"}
    app = req.get("app") if isinstance(req.get("app"), dict) else None
    try:
        placed = await place_session(session, int(req.get("owner_id") or 0), app)
    except AuthKeyDuplicatedError:
        log.error("adopt duplicated")
        return {"ok": False, "error": "revoked"}
    except TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("adopt failed %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    if placed.get("status") == "taken":
        return {"ok": False, "error": "taken"}
    if placed.get("status") != "ready":
        return {"ok": False, "error": "wrong_dc", "dc": placed.get("dc")}
    return {"ok": True, **placed}


async def op_hold(req: dict) -> dict:
    raw_ids = req.get("user_ids")
    if not isinstance(raw_ids, list):
        raw_ids = [req.get("user_id")]
    found = {}
    for raw in raw_ids:
        try:
            user_id = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if user_id <= 0:
            continue
        record = accounts.get(str(user_id))
        if record:
            found[str(user_id)] = int(record.get("owner_id") or 0)
    return {"ok": True, "found": found}


async def op_import(req: dict) -> dict:
    session = str(req.get("session") or "")
    app = clean_profile(req.get("app") if isinstance(req.get("app"), dict) else None)
    try:
        owner_id = int(req.get("owner_id") or 0)
    except (TypeError, ValueError):
        owner_id = 0
    if not session or app is None or owner_id <= 0:
        return {"ok": False, "error": "bad_pack"}
    try:
        placed = await place_session(session, owner_id, app)
    except AuthKeyDuplicatedError:
        log.error("import duplicated")
        return {"ok": False, "error": "revoked"}
    except TimeoutError:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("import failed %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    if placed.get("status") == "taken":
        return {"ok": False, "error": "taken"}
    if placed.get("status") not in {"ready", "move"}:
        return {"ok": False, "error": "revoked"}
    return {"ok": True, **placed}


async def op_drop(req: dict) -> dict:
    path = HANDOFF / f"{req.get('user_id')}.json"
    if path.exists():
        path.unlink()
    return {"ok": True}


async def op_list(req: dict) -> dict:
    rows = list(accounts.values())
    if req.get("owner_id") is not None:
        owner_id = int(req["owner_id"])
        rows = [row for row in rows if int(row.get("owner_id") or 0) == owner_id]
    listed = []
    for row in rows:
        item = dict(row)
        user_id = int(item.get("user_id") or 0)
        client = clients.get(user_id)
        note = session_notes.get(user_id, "")
        item["reason"] = note
        item["online"] = client is not None and client.is_connected() and not note
        item["org"] = stored_org(item)
        listed.append(item)
    return {"ok": True, "accounts": listed}


async def op_refresh(req: dict) -> dict:
    user_id = int(req.get("user_id") or 0)
    owner_id = int(req.get("owner_id") or 0)
    record = accounts.get(str(user_id))
    if record is None or int(record.get("owner_id") or 0) != owner_id:
        return {"ok": False, "error": "not_found"}
    if user_id in session_notes:
        return {"ok": False, "error": "revoked", "message": session_notes[user_id]}
    client = clients.get(user_id)
    if client is None:
        return {"ok": False, "error": "offline"}
    try:
        me = await client.get_me()
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("refresh %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    remember(me, int(client.session.dc_id), owner_id)
    await apply_marks(client, me.id)
    return {"ok": True, "account": accounts[str(me.id)]}


async def op_remove(req: dict) -> dict:
    user_id = int(req.get("user_id") or 0)
    owner_id = int(req.get("owner_id") or 0)
    record = accounts.get(str(user_id))
    if record is None or int(record.get("owner_id") or 0) != owner_id:
        return {"ok": False, "error": "not_found"}
    await disconnect_user(user_id)
    accounts.pop(str(user_id), None)
    save_accounts()
    handoff = HANDOFF / f"{user_id}.json"
    if handoff.exists():
        handoff.unlink()
    return {"ok": True}


assert org_name(7487372978) == "major"
assert org_name(8365813836) == "hold"
assert org_name(1) == ""
assert stored_org({"hold": True}) == "hold"
assert stored_org({"org": "major", "hold": True}) == "major"
assert stored_org({}) == ""
assert phone_text("8613800138000") == "+8613800138000"
assert phone_text("+86 138 0013 8000") == "+8613800138000"
assert phone_text("123") == ""
assert birthday_text(None) == ""


class _Mark:
    def __init__(self, **kw):
        self.__dict__.update(kw)


assert birthday_text(_Mark(day=5, month=3, year=1990)) == "1990年3月5日"
assert birthday_text(_Mark(day=5, month=3, year=None)) == "3月5日"
assert about_text("  你好  ") == "你好"
_marks = profile_marks(
    _Mark(premium=True, usernames=[], phone="88812345678", verified=False, lang_code="zh-hans", restricted=False, scam=False, fake=False),
    _Mark(about="简介", birthday=_Mark(day=1, month=2, year=None), bot_verification=None),
)
assert _marks["phone"] == "+88812345678"
assert _marks["about"] == "简介"
assert _marks["birthday"] == "2月1日"
assert _marks["lang"] == "zh-hans"
assert _marks["verified"] is False
