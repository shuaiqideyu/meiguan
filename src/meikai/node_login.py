"""节点登录。"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from meikai.node_host import SESSIONS, credentials, error_code, make_client, unlink_session
from meikai.node_roster import place_session

log = logging.getLogger("meikai.node")

logins: dict[str, "Pending"] = {}


@dataclass
class Pending:
    login_id: str
    client: TelegramClient | None
    phone: str = ""
    qr: object = None
    task: asyncio.Task | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    stage: str = "pending"
    error: str = ""
    seconds: int = 0
    result: dict | None = None
    code_attempts: int = 0
    password_attempts: int = 0
    owner_id: int = 0


def pending_base(login_id: str) -> Path:
    return SESSIONS / f"pending-{login_id}"


async def settle_client(login: Pending) -> dict:
    client = login.client
    if client is None:
        return {"ok": False, "error": "not_found"}
    session_string = StringSession.save(client.session)
    await client.disconnect()
    login.client = None
    unlink_session(pending_base(login.login_id))
    try:
        placed = await place_session(session_string, login.owner_id)
    except Exception:
        log.warning("place session failed")
        return {"ok": False, "error": "internal"}
    return {"ok": True, **placed}


async def cancel_login(login: Pending) -> None:
    if login.task and not login.task.done():
        login.task.cancel()
    if login.client is not None:
        await login.client.disconnect()
        login.client = None
    unlink_session(pending_base(login.login_id))
    logins.pop(login.login_id, None)


async def cancel_owner(owner_id: int) -> None:
    for login in list(logins.values()):
        if login.owner_id == owner_id:
            await cancel_login(login)


async def open_pending(login_id: str) -> TelegramClient | dict:
    creds = credentials()
    if creds is None:
        return {"ok": False, "error": "api_missing"}
    SESSIONS.mkdir(parents=True, exist_ok=True)
    client = make_client(str(pending_base(login_id)))
    await client.connect()
    return client


async def op_send_code(req: dict) -> dict:
    owner_id = int(req.get("owner_id") or 0)
    await cancel_owner(owner_id)
    login_id = uuid.uuid4().hex[:16]
    opened = await open_pending(login_id)
    if isinstance(opened, dict):
        return opened
    phone = str(req.get("phone") or "")
    try:
        await opened.send_code_request(phone)
    except Exception as exc:
        await opened.disconnect()
        unlink_session(pending_base(login_id))
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("send_code %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    login = Pending(login_id=login_id, client=opened, phone=phone, stage="code", owner_id=owner_id)
    logins[login_id] = login
    return {"ok": True, "login_id": login_id}


async def op_sign_code(req: dict) -> dict:
    login = logins.get(str(req.get("login_id") or ""))
    if login is None or login.client is None:
        return {"ok": False, "error": "not_found"}
    login.code_attempts += 1
    if login.code_attempts > 5:
        await cancel_login(login)
        return {"ok": False, "error": "too_many"}
    try:
        await login.client.sign_in(login.phone, str(req.get("code") or "").strip())
    except SessionPasswordNeededError:
        login.stage = "need_password"
        return {"ok": True, "status": "need_password"}
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("sign_code %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    result = await settle_client(login)
    logins.pop(login.login_id, None)
    return result


async def op_password(req: dict) -> dict:
    login = logins.get(str(req.get("login_id") or ""))
    if login is None or login.client is None:
        return {"ok": False, "error": "not_found"}
    login.password_attempts += 1
    if login.password_attempts > 3:
        await cancel_login(login)
        return {"ok": False, "error": "too_many"}
    try:
        await login.client.sign_in(password=str(req.get("password") or ""))
    except PasswordHashInvalidError:
        return {"ok": False, "error": "invalid_password"}
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("password %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    result = await settle_client(login)
    logins.pop(login.login_id, None)
    return result


async def watch_qr(login: Pending) -> None:
    try:
        await login.qr.wait()
        login.result = await settle_client(login)
        login.stage = "done"
    except SessionPasswordNeededError:
        login.stage = "need_password"
    except asyncio.TimeoutError:
        login.stage = "expired"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        mapped = error_code(exc)
        login.stage = "error"
        login.error = mapped["error"] if mapped else "internal"
        login.seconds = int(mapped.get("seconds") or 0) if mapped else 0
        if mapped is None:
            log.warning("qr wait %s", type(exc).__name__)
    finally:
        login.event.set()


async def op_qr_start(req: dict) -> dict:
    owner_id = int(req.get("owner_id") or 0)
    await cancel_owner(owner_id)
    login_id = uuid.uuid4().hex[:16]
    opened = await open_pending(login_id)
    if isinstance(opened, dict):
        return opened
    try:
        qr = await opened.qr_login()
    except Exception as exc:
        await opened.disconnect()
        unlink_session(pending_base(login_id))
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("qr_start %s", type(exc).__name__)
        return {"ok": False, "error": "internal"}
    login = Pending(login_id=login_id, client=opened, qr=qr, stage="pending", owner_id=owner_id)
    login.task = asyncio.create_task(watch_qr(login))
    logins[login_id] = login
    return {"ok": True, "login_id": login_id, "url": qr.url}


def status_of(login: Pending) -> dict:
    if login.stage == "need_password":
        return {"ok": True, "status": "need_password"}
    if login.stage == "expired":
        return {"ok": True, "status": "expired"}
    if login.stage == "error":
        body = {"ok": False, "error": login.error or "internal"}
        if login.seconds:
            body["seconds"] = login.seconds
        return body
    if login.stage == "done" and login.result is not None:
        logins.pop(login.login_id, None)
        return login.result
    return {"ok": True, "status": "pending"}


async def op_qr_status(req: dict) -> dict:
    login = logins.get(str(req.get("login_id") or ""))
    if login is None:
        return {"ok": False, "error": "not_found"}
    if not login.event.is_set():
        try:
            await asyncio.wait_for(login.event.wait(), timeout=20)
        except asyncio.TimeoutError:
            return {"ok": True, "status": "pending"}
    return status_of(login)


async def op_qr_refresh(req: dict) -> dict:
    login = logins.get(str(req.get("login_id") or ""))
    if login is None or login.qr is None or login.client is None:
        return {"ok": False, "error": "not_found"}
    if login.stage != "expired":
        return {"ok": True, "status": login.stage, "url": login.qr.url}
    await login.qr.recreate()
    login.stage = "pending"
    login.event = asyncio.Event()
    login.task = asyncio.create_task(watch_qr(login))
    return {"ok": True, "status": "pending", "url": login.qr.url}


async def op_cancel(req: dict) -> dict:
    login = logins.get(str(req.get("login_id") or ""))
    if login is not None:
        await cancel_login(login)
    return {"ok": True}
