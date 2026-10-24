"""节点进程。"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from telethon.errors import AuthKeyDuplicatedError, AuthKeyError, UnauthorizedError

import meikai.node_host as host
from meikai.grab import apply_modes, apply_policies, attach
from meikai.node_host import (
    SESSIONS,
    accounts,
    clients,
    credentials,
    load_client,
    make_client,
    node_id,
)
from meikai.node_login import (
    op_cancel,
    op_password,
    op_qr_refresh,
    op_qr_start,
    op_qr_status,
    op_send_code,
    op_sign_code,
)
from meikai.node_roster import (
    op_adopt,
    op_drop,
    op_hold,
    op_import,
    op_list,
    op_refresh,
    op_remove,
    apply_marks,
    remember,
    touch_active,
)
from meikai.node_wallets import op_okpay, op_wallets

log = logging.getLogger("meikai.node")

SOCKET = "/run/meikai/node.sock"


def modes_file() -> Path:
    return SESSIONS / "modes.json"


def restore_modes() -> None:
    path = modes_file()
    if not path.exists():
        return
    try:
        apply_modes(json.loads(path.read_text()))
    except json.JSONDecodeError:
        pass


def store_modes(flags: dict) -> dict:
    current = apply_modes(flags)
    path = modes_file()
    path.write_text(json.dumps(current, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return current


def policy_file() -> Path:
    return SESSIONS / "policy.json"


def blocks_file() -> Path:
    return SESSIONS / "blocks.json"


def brands_file() -> Path:
    return SESSIONS / "brands.json"


def restore_brands() -> None:
    from meikai.reports import apply_brands

    path = brands_file()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    if isinstance(raw, list):
        apply_brands(raw)


def store_brands(raw, persist: bool = False) -> dict:
    from meikai.reports import apply_brands, dump_brands

    changed = apply_brands(raw if isinstance(raw, list) else [])
    if not changed and not persist:
        return {"ok": True, "count": 0}
    items = dump_brands()
    path = brands_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return {"ok": True, "count": len(items)}


def restore_blocks() -> None:
    from meikai.reports import apply_alias, apply_blocks

    path = blocks_file()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    if isinstance(raw, list):
        apply_blocks(raw)
        apply_alias({})
        return
    if isinstance(raw, dict):
        apply_blocks(raw.get("ids") or raw.get("names") or [])
        apply_alias(raw.get("alias") or {})


def store_blocks(raw, alias=None) -> dict:
    from meikai.reports import _by_name, apply_alias, apply_blocks

    names = apply_blocks(raw if isinstance(raw, list) else [])
    if isinstance(alias, dict):
        apply_alias(alias)
    path = blocks_file()
    path.write_text(json.dumps({"ids": names, "alias": dict(_by_name)}, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return {"ok": True, "count": len(names)}


def restore_policy() -> None:
    path = policy_file()
    if not path.exists():
        return
    try:
        apply_policies(json.loads(path.read_text()))
    except json.JSONDecodeError:
        pass


def store_policies(raw: dict) -> dict:
    current = apply_policies(raw)
    path = policy_file()
    path.write_text(json.dumps(current, ensure_ascii=False) + "\n")
    path.chmod(0o600)
    return current


_flush_task: asyncio.Task | None = None


def _on_glyph_change() -> None:
    global _flush_task
    if _flush_task is not None and not _flush_task.done():
        return
    _flush_task = asyncio.get_running_loop().create_task(_flush_glyphs())


async def _flush_glyphs() -> None:
    global _flush_task
    from meikai.grab import glyph_dirty, take_glyph_snapshot

    try:
        await asyncio.sleep(0)
        glyphs, docs, learned, learned_docs = take_glyph_snapshot()
        if glyphs is not None or docs is not None:
            await asyncio.to_thread(_write_glyph_files, glyphs, docs)
        if learned or learned_docs:
            await host._emit({"event": "learned", "glyphs": learned, "docs": learned_docs})
    except Exception as exc:
        log.info("节点：表情缓存写入失败 %s", type(exc).__name__)
    finally:
        _flush_task = None
        if glyph_dirty():
            _on_glyph_change()


def _write_glyph_files(glyphs: dict | None, docs: dict | None) -> None:
    from meikai.grab import doc_file, glyph_file

    if glyphs is not None:
        path = glyph_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(glyphs, ensure_ascii=False, separators=(",", ":")) + "\n")
        path.chmod(0o600)
    if docs is not None:
        path = doc_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(docs, ensure_ascii=False, separators=(",", ":")) + "\n")
        path.chmod(0o600)


async def watch_session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    from meikai.grab import apply_remote_docs, apply_remote_glyphs, pending_docs, pending_glyphs

    previous = host._watch
    host._watch = writer
    host._watch_lock = asyncio.Lock()
    if previous is not None and previous is not writer:
        previous.close()
    try:
        await host._emit({"event": "ready"})
        for item in host.expiry_events():
            await host._emit({"event": "expired", "item": item})
        from meikai.reports import dump_brands

        brands = dump_brands()
        if brands:
            await host._emit({"event": "brand", "items": brands})
        learned = pending_glyphs()
        learned_docs = pending_docs()
        if learned or learned_docs:
            await host._emit({"event": "learned", "glyphs": learned, "docs": learned_docs})
        limit = asyncio.Semaphore(8)

        async def _parallel(msg: dict) -> None:
            try:
                async with limit:
                    resp = await _answer(msg)
                    resp["id"] = msg["id"]
                    await host._emit(resp)
            except Exception:
                log.warning("request failed")

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if msg.get("id"):
                if msg.get("op") in _PARALLEL_OPS:
                    asyncio.create_task(_parallel(msg))
                else:
                    resp = await _answer(msg)
                    resp["id"] = msg["id"]
                    await host._emit(resp)
                continue
            op = msg.get("op")
            if op == "glyph_delta":
                apply_remote_glyphs(msg.get("glyphs") or {})
                apply_remote_docs(msg.get("docs") or {})
    finally:
        if host._watch is writer:
            host._watch = None
            host._watch_lock = None
        writer.close()


async def op_glyphs(req: dict) -> dict:
    from meikai.grab import apply_remote_docs, apply_remote_glyphs, pending_docs, pending_glyphs

    raw = req.get("glyphs")
    if isinstance(raw, dict):
        apply_remote_glyphs(raw)
    docs = req.get("docs")
    if isinstance(docs, dict):
        apply_remote_docs(docs)
    return {"ok": True, "learned": pending_glyphs(), "learned_docs": pending_docs()}


async def op_peek(req: dict) -> dict:
    from meikai.grab import (
        button_url,
        cb_data,
        classify,
        detect_state,
        emoji_info,
        is_red_packet,
        raw_text,
        rows,
    )

    owner_id = int(req.get("owner_id") or 0)
    chat_id = int(req.get("chat_id") or 0)
    username = str(req.get("username") or "").lstrip("@")
    msg_id = int(req.get("msg_id") or 0)
    target = chat_id or username
    if not owner_id or not target or not msg_id:
        return {"ok": False, "error": "not_found"}
    errors = []
    for user_id, client in list(clients.items()):
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner != owner_id:
            continue
        if not client.is_connected():
            errors.append(f"{user_id}:offline")
            continue
        try:
            msg = await client.get_messages(target, ids=msg_id)
        except Exception as exc:
            errors.append(f"{user_id}:{type(exc).__name__}")
            continue
        if msg is None:
            errors.append(f"{user_id}:empty")
            continue
        text = raw_text(msg)
        buttons = []
        for row in rows(msg):
            for btn in getattr(row, "buttons", None) or []:
                buttons.append({
                    "text": getattr(btn, "text", "") or "",
                    "url": button_url(btn)[:200],
                    "data": cb_data(btn)[:120],
                })
        found = classify(msg)
        date = getattr(msg, "date", None)
        from meikai.grab.detect import resolve_wallet, _user_from_message

        sender = getattr(msg, "sender", None)
        via = getattr(msg, "via_bot", None)
        via_user, via_err = (None, "")
        if getattr(msg, "via_bot_id", None):
            via_user, via_err = await _user_from_message(client, msg, int(msg.via_bot_id))
        return {
            "ok": True,
            "user_id": user_id,
            "date": date.isoformat() if date else "",
            "text": text[:1500],
            "buttons": buttons,
            "emojis": await emoji_info(client, msg),
            "is_red": is_red_packet(text),
            "state": detect_state(msg),
            "kind": None if found is None else found.kind,
            "wallet": await resolve_wallet(client, msg),
            "via": getattr(via, "username", None) or getattr(via_user, "username", None) or "",
            "via_id": int(getattr(msg, "via_bot_id", None) or 0),
            "via_user_id": int(getattr(via_user, "id", 0) or 0),
            "via_bot_flag": bool(getattr(via_user, "bot", False)),
            "via_names": [
                {
                    "username": getattr(item, "username", None) or "",
                    "active": bool(getattr(item, "active", False)),
                }
                for item in (getattr(via_user, "usernames", None) or [])
            ],
            "via_title": str(getattr(via_user, "first_name", None) or "")[:40],
            "via_err": via_err,
            "via_type": type(via_user).__name__ if via_user is not None else "",
            "sender": getattr(sender, "username", None) or "",
            "sender_id": int(getattr(msg, "sender_id", None) or 0),
            "sender_bot": bool(getattr(sender, "bot", False)),
        }
    return {"ok": False, "error": "not_found", "errors": errors}


async def op_replay(req: dict) -> dict:
    from meikai.grab import safe

    chat_id = int(req.get("chat_id") or 0)
    msg_id = int(req.get("msg_id") or 0)
    if not chat_id or not msg_id:
        return {"ok": False, "error": "not_found"}
    for user_id, client in list(clients.items()):
        if not client.is_connected():
            continue
        try:
            msg = await client.get_messages(chat_id, ids=msg_id)
        except Exception as exc:
            log.info("replay %s", type(exc).__name__)
            continue
        if msg is None:
            continue

        class _Event:
            def __init__(self, message):
                self.message = message
                self.chat_id = chat_id

            async def get_input_chat(self):
                return await client.get_input_entity(chat_id)

        await safe(client, user_id, _Event(msg))
        return {"ok": True, "user_id": user_id}
    return {"ok": False, "error": "not_found"}


async def op_pay_balance(_req: dict) -> dict:
    import os

    from meikai.grab.paybalance import ask_balance

    (fll_state, fll), (cf_state, cf) = await asyncio.gather(
        ask_balance("fll", os.environ.get("TWOCAPTCHA_KEY", "")),
        ask_balance("cf", os.environ.get("CAPMONSTER_KEY", "")),
    )
    return {"ok": True, "fll": fll, "cf": cf, "fll_state": fll_state, "cf_state": cf_state}


async def op_modes(req: dict) -> dict:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    policies = req.get("policies")
    if isinstance(policies, dict):
        store_policies(policies)
        return {"ok": True}
    raw = req.get("modes")
    if isinstance(raw, dict):
        return {"ok": True, "modes": store_modes(raw)}
    return {"ok": True, "modes": apply_modes({})}


async def op_shared(req: dict) -> dict:
    from meikai.grab.shared import schedule

    owners = []
    for item in req.get("owners") or []:
        if str(item).lstrip("-").isdigit():
            owners.append(int(item))
    try:
        chat_id = int(req.get("chat_id") or 0)
        msg_id = int(req.get("msg_id") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "not_found"}
    if not owners or not chat_id or not msg_id:
        return {"ok": False, "error": "not_found"}
    username = str(req.get("username") or "")[:64]
    bots = [str(item)[:64] for item in (req.get("bots") or [])[:12] if item]
    kind = str(req.get("kind") or "")
    invite = str(req.get("invite") or "")[:80]
    schedule(owners, chat_id, msg_id, username, bots, kind, invite)
    return {"ok": True}


async def op_resolve(req: dict) -> dict:
    from telethon.tl.types import Channel, Chat, User
    from telethon.utils import get_peer_id

    from meikai.node_host import error_code

    owner_id = int(req.get("owner_id") or 0)
    username = str(req.get("username") or "").strip().lstrip("@")
    if username:
        target = username
    else:
        try:
            target = int(req.get("chat_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "not_found"}
    ordered = []
    for user_id, client in list(clients.items()):
        if not client.is_connected():
            continue
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner == owner_id:
            ordered.append(client)
    if not ordered:
        return {"ok": False, "error": "offline"}
    last = {"ok": False, "error": "not_found"}
    for client in ordered:
        try:
            entity = await asyncio.wait_for(client.get_entity(target), timeout=12)
        except Exception as exc:
            mapped = error_code(exc)
            if mapped and mapped.get("error") == "flood":
                return mapped
            last = mapped or {"ok": False, "error": "not_found"}
            continue
        if isinstance(entity, User):
            if not getattr(entity, "bot", False):
                return {"ok": False, "error": "not_chat"}
            name = str(getattr(entity, "username", "") or "").strip()
            title = str(getattr(entity, "first_name", "") or "").strip()[:40]
            return {
                "ok": True,
                "kind": "bot",
                "user_id": int(entity.id),
                "title": title or (f"@{name}" if name else str(entity.id)),
                "username": name,
            }
        if not isinstance(entity, (Channel, Chat)):
            return {"ok": False, "error": "not_chat"}
        chat_id = int(get_peer_id(entity))
        title = str(getattr(entity, "title", "") or "").strip()[:40]
        name = str(getattr(entity, "username", "") or "").strip()
        return {
            "ok": True,
            "chat_id": chat_id,
            "title": title or (f"@{name}" if name else str(chat_id)),
            "username": name,
        }
    return last


def _entity_row(entity) -> dict | None:
    from telethon.tl.types import Channel, Chat, User
    from telethon.utils import get_peer_id

    from meikai.reports import account_names

    if type(entity).__name__ in {"ChannelForbidden", "ChatForbidden", "UserEmpty"}:
        return None
    names = account_names(entity)
    name = names[0] if names else ""
    if isinstance(entity, User):
        kind = peer_kind(
            user=True,
            bot=bool(getattr(entity, "bot", False)),
            deleted=bool(getattr(entity, "deleted", False)),
            self_chat=bool(getattr(entity, "is_self", False)),
        )
        if kind is None:
            return None
        number = int(entity.id)
        title = str(getattr(entity, "first_name", "") or "").strip()[:40]
        row = {
            "kind": kind,
            "chat_id": number,
            "title": title or (f"@{name}" if name else str(number)),
            "username": name,
        }
        if kind in {"bot", "user"}:
            row["user_id"] = number
        return row
    if isinstance(entity, Channel):
        kind = peer_kind(
            channel=True,
            megagroup=bool(getattr(entity, "megagroup", False)),
            gigagroup=bool(getattr(entity, "gigagroup", False)),
        )
    elif isinstance(entity, Chat):
        kind = peer_kind(chat=True, deactivated=bool(getattr(entity, "deactivated", False)))
    else:
        kind = None
    if kind is None:
        return None
    number = int(get_peer_id(entity))
    title = str(getattr(entity, "title", "") or "").strip()[:40]
    return {
        "kind": kind,
        "chat_id": number,
        "title": title or (f"@{name}" if name else str(number)),
        "username": name,
    }


_INVITE_HASH = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


async def op_find(req: dict) -> dict:
    from telethon.tl.functions.messages import CheckChatInviteRequest
    from telethon.tl.types import ChatInvite

    from meikai.node_host import error_code

    owner_id = int(req.get("owner_id") or 0)
    invite = str(req.get("invite") or "").strip()
    username = str(req.get("username") or "").strip().lstrip("@")
    if invite:
        if not _INVITE_HASH.fullmatch(invite):
            return {"ok": False, "error": "not_found"}
        target = None
    elif username:
        target = username
    else:
        try:
            target = int(req.get("chat_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "not_found"}
    ordered = []
    for user_id, client in list(clients.items()):
        if not client.is_connected():
            continue
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner == owner_id:
            ordered.append(client)
    if not ordered:
        return {"ok": False, "error": "offline"}
    last = {"ok": False, "error": "not_found"}
    for client in ordered:
        try:
            if invite:
                preview = await asyncio.wait_for(client(CheckChatInviteRequest(invite)), timeout=12)
                if isinstance(preview, ChatInvite):
                    last = {"ok": False, "error": "not_joined"}
                    continue
                entity = getattr(preview, "chat", None)
                if entity is None:
                    last = {"ok": False, "error": "not_found"}
                    continue
            else:
                entity = await asyncio.wait_for(client.get_entity(target), timeout=12)
        except Exception as exc:
            mapped = error_code(exc)
            if mapped and mapped.get("error") == "flood":
                return mapped
            if mapped and mapped.get("error") == "no_bot":
                last = {"ok": False, "error": "not_found"}
            else:
                last = mapped or {"ok": False, "error": "not_found"}
            continue
        row = _entity_row(entity)
        if row is None:
            last = {"ok": False, "error": "not_found"}
            continue
        return {"ok": True, **row}
    return last


async def op_lookup(req: dict) -> dict:
    from telethon.tl.types import Channel, Chat, User
    from telethon.utils import get_peer_id

    from meikai.node_host import error_code

    username = str(req.get("username") or "").strip().lstrip("@")
    if username:
        target = username
    else:
        try:
            target = int(req.get("peer"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "not_found"}
    tried = 0
    last = {"ok": False, "error": "offline"}
    for client in list(clients.values()):
        if not client.is_connected():
            continue
        tried += 1
        try:
            entity = await asyncio.wait_for(client.get_entity(target), timeout=12)
        except Exception as exc:
            mapped = error_code(exc)
            if mapped and mapped.get("error") == "flood":
                return mapped
            last = mapped or {"ok": False, "error": "not_found"}
            if tried >= 2:
                break
            continue
        name = str(getattr(entity, "username", "") or "").strip()
        if not name:
            for item in getattr(entity, "usernames", None) or []:
                name = str(getattr(item, "username", "") or "").strip()
                if name:
                    break
        if isinstance(entity, User):
            title = str(getattr(entity, "first_name", "") or "").strip()[:40]
            return {
                "ok": True,
                "kind": "bot" if getattr(entity, "bot", False) else "user",
                "user_id": int(entity.id),
                "username": name,
                "title": title,
            }
        if isinstance(entity, Channel):
            title = str(getattr(entity, "title", "") or "").strip()[:40]
            return {
                "ok": True,
                "kind": "channel" if getattr(entity, "broadcast", False) else "group",
                "user_id": int(get_peer_id(entity)),
                "username": name,
                "title": title,
            }
        if isinstance(entity, Chat):
            title = str(getattr(entity, "title", "") or "").strip()[:40]
            return {
                "ok": True,
                "kind": "group",
                "user_id": int(get_peer_id(entity)),
                "username": name,
                "title": title,
            }
        return {"ok": False, "error": "not_found"}
    if tried == 0:
        return {"ok": False, "error": "offline"}
    return last


async def op_blocks(req: dict) -> dict:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    return store_blocks(req.get("names"), req.get("alias"))


async def op_brands(req: dict) -> dict:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    return store_brands(req.get("items"))


def peer_kind(
    *,
    user: bool = False,
    bot: bool = False,
    deleted: bool = False,
    self_chat: bool = False,
    chat: bool = False,
    deactivated: bool = False,
    channel: bool = False,
    megagroup: bool = False,
    gigagroup: bool = False,
) -> str | None:
    if user:
        if deleted or self_chat:
            return None
        return "bot" if bot else "user"
    if chat:
        if deactivated:
            return None
        return "group"
    if channel:
        if megagroup or gigagroup:
            return "group"
        return "channel"
    return None


assert peer_kind(user=True) == "user"
assert peer_kind(user=True, bot=True) == "bot"
assert peer_kind(user=True, deleted=True) is None
assert peer_kind(user=True, self_chat=True) is None
assert peer_kind(chat=True) == "group"
assert peer_kind(chat=True, deactivated=True) is None
assert peer_kind(channel=True, megagroup=True) == "group"
assert peer_kind(channel=True, gigagroup=True) == "group"
assert peer_kind(channel=True) == "channel"
assert peer_kind() is None


_KIND_CAP = 400


def _dialog_full(counts: dict, want_people: bool) -> bool:
    if counts["group"] < _KIND_CAP or counts["channel"] < _KIND_CAP:
        return False
    if not want_people:
        return True
    return counts["user"] >= _KIND_CAP and counts["bot"] >= _KIND_CAP


assert _dialog_full({"user": 0, "group": 400, "channel": 400, "bot": 0}, False)
assert not _dialog_full({"user": 0, "group": 399, "channel": 400, "bot": 0}, False)
assert not _dialog_full({"user": 0, "group": 400, "channel": 400, "bot": 0}, True)
assert _dialog_full({"user": 400, "group": 400, "channel": 400, "bot": 400}, True)


_dialog_cache: dict[tuple, tuple[float, dict]] = {}
_DIALOG_TTL = 45.0


def _fold_dialog(dialog, found: dict, counts: dict, want_people: bool) -> None:
    from telethon.tl.types import Channel, Chat, User
    from telethon.utils import get_peer_id

    entity = dialog.entity
    if isinstance(entity, User):
        kind = peer_kind(
            user=True,
            bot=bool(getattr(entity, "bot", False)),
            deleted=bool(getattr(entity, "deleted", False)),
            self_chat=bool(getattr(entity, "is_self", False)),
        )
    elif isinstance(entity, Chat):
        kind = peer_kind(chat=True, deactivated=bool(getattr(entity, "deactivated", False)))
    elif isinstance(entity, Channel):
        kind = peer_kind(
            channel=True,
            megagroup=bool(getattr(entity, "megagroup", False)),
            gigagroup=bool(getattr(entity, "gigagroup", False)),
        )
    else:
        kind = None
    if kind is None or (kind in {"user", "bot"} and not want_people):
        return
    if isinstance(entity, User):
        number = int(entity.id)
        title = str(getattr(entity, "first_name", "") or getattr(dialog, "name", "") or "").strip()[:40]
    else:
        number = int(get_peer_id(entity))
        title = str(getattr(entity, "title", "") or getattr(dialog, "name", "") or "").strip()[:40]
    if kind == "user":
        key = ("user", number)
    elif kind == "bot":
        key = ("bot", number)
    else:
        key = ("chat", number)
    if key not in found:
        if counts[kind] >= _KIND_CAP:
            return
        counts[kind] += 1
    name = str(getattr(entity, "username", "") or "").strip()
    when = getattr(dialog, "date", None)
    stamp = when.isoformat() if when is not None else ""
    previous = found.get(key)
    if previous and str(previous.get("active_at") or "") > stamp:
        stamp = str(previous.get("active_at") or "")
    found[key] = {
        "kind": kind,
        "chat_id": number,
        "title": title or (f"@{name}" if name else str(number)),
        "username": name,
        "active_at": stamp,
    }
    if kind == "bot":
        found[key]["user_id"] = number


def _dialog_rows(found: dict) -> list:
    rows = []
    seen_name = set()
    for item in sorted(found.values(), key=lambda row: (bool(row.get("active_at")), str(row.get("active_at") or "")), reverse=True):
        kind = str(item.get("kind") or "chat")
        username = str(item.get("username") or "").strip().lstrip("@").lower()
        number = item.get("user_id") if kind == "bot" else item.get("chat_id")
        if username:
            name = (kind, "u", username)
        else:
            name = (kind, "id", number)
        if name in seen_name:
            continue
        seen_name.add(name)
        rows.append(item)
    return rows


assert len(_dialog_rows({
    ("chat", -1): {"kind": "group", "chat_id": -1, "title": "甲", "username": "", "active_at": ""},
    ("chat", -2): {"kind": "group", "chat_id": -2, "title": "甲", "username": "", "active_at": ""},
})) == 2


async def _consume_dialogs(client, archived, limit, found, counts, want_people, state, lock, deadline) -> None:
    from meikai.node_host import error_code

    async def walk() -> None:
        kwargs = {"limit": limit}
        if archived is not None:
            kwargs["archived"] = archived
        async for dialog in client.iter_dialogs(**kwargs):
            if time.monotonic() >= deadline:
                return
            async with lock:
                if state["flood"] is not None or _dialog_full(counts, want_people):
                    return
                _fold_dialog(dialog, found, counts, want_people)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    try:
        await asyncio.wait_for(walk(), timeout=remaining)
    except TimeoutError:
        return
    except Exception as exc:
        mapped = error_code(exc)
        if mapped and mapped.get("error") == "flood":
            async with lock:
                state["flood"] = mapped
            return
        log.warning("dialogs %s", type(exc).__name__)


async def op_dialogs(req: dict) -> dict:
    owner_id = int(req.get("owner_id") or 0)
    if not owner_id:
        return {"ok": False, "error": "not_found"}
    want_people = bool(req.get("bots"))
    cache_key = (owner_id, want_people)
    cached = _dialog_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _DIALOG_TTL:
        return cached[1]
    targets = []
    for user_id, client in list(clients.items()):
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner != owner_id or not client.is_connected():
            continue
        targets.append(client)
    if not targets:
        return {"ok": False, "error": "offline"}
    found: dict[tuple, dict] = {}
    counts = {"user": 0, "group": 0, "channel": 0, "bot": 0}
    state: dict = {"flood": None}
    lock = asyncio.Lock()
    gate = asyncio.Semaphore(3)

    async def pull(client) -> None:
        async with gate:
            async with lock:
                if state["flood"] is not None or _dialog_full(counts, want_people):
                    return
            # 主列表不含归档。先读最近的，留几秒给归档。超时留下已读到的。
            overall = time.monotonic() + 14
            await _consume_dialogs(client, None, 1200, found, counts, want_people, state, lock, min(time.monotonic() + 10, overall))
            async with lock:
                if state["flood"] is not None or _dialog_full(counts, want_people):
                    return
            await _consume_dialogs(client, True, 400, found, counts, want_people, state, lock, overall)

    await asyncio.gather(*(pull(client) for client in targets))
    if state["flood"] is not None and not found:
        return state["flood"]
    result = {"ok": True, "dialogs": _dialog_rows(found)}
    if state["flood"] is None:
        if len(_dialog_cache) > 40:
            oldest = min(_dialog_cache, key=lambda key: _dialog_cache[key][0])
            _dialog_cache.pop(oldest, None)
        _dialog_cache[cache_key] = (time.monotonic(), result)
    return result


def _face_step(known: int, photo_id: int | None) -> str:
    if photo_id is None:
        return "skip"
    if photo_id == 0:
        return "empty"
    if known and photo_id == known:
        return "same"
    return "fetch"


assert _face_step(0, None) == "skip"
assert _face_step(5, 0) == "empty"
assert _face_step(5, 5) == "same"
assert _face_step(0, 5) == "fetch"
assert _face_step(5, 6) == "fetch"


async def _face_photo_id(client, peer: int, mine: bool) -> tuple[int | None, dict | None]:
    from telethon.tl.functions.channels import GetChannelsRequest
    from telethon.tl.functions.messages import GetChatsRequest
    from telethon.tl.functions.users import GetUsersRequest
    from telethon.tl.types import ChatPhoto, InputPeerChannel, InputPeerChat, UserProfilePhoto

    from meikai.node_host import error_code

    try:
        if mine:
            found = await asyncio.wait_for(client(GetUsersRequest(["me"])), timeout=8)
            entity = found[0] if found else None
        else:
            inp = await asyncio.wait_for(client.get_input_entity(peer), timeout=8)
            if isinstance(inp, InputPeerChannel):
                found = await asyncio.wait_for(client(GetChannelsRequest([inp])), timeout=8)
                entity = found.chats[0] if getattr(found, "chats", None) else None
            elif isinstance(inp, InputPeerChat):
                found = await asyncio.wait_for(client(GetChatsRequest([int(inp.chat_id)])), timeout=8)
                entity = found.chats[0] if getattr(found, "chats", None) else None
            else:
                found = await asyncio.wait_for(client(GetUsersRequest([inp])), timeout=8)
                entity = found[0] if found else None
    except Exception as exc:
        mapped = error_code(exc)
        if mapped and mapped.get("error") == "flood":
            return None, mapped
        return None, None
    if entity is None or type(entity).__name__ in {"ChannelForbidden", "ChatForbidden", "UserEmpty"}:
        return None, None
    photo = getattr(entity, "photo", None)
    if isinstance(photo, (UserProfilePhoto, ChatPhoto)):
        return int(photo.photo_id), None
    return 0, None


async def op_face(req: dict) -> dict:
    import base64

    from meikai.node_host import error_code

    owner_id = int(req.get("owner_id") or 0)
    try:
        peer = int(req.get("peer"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "not_found"}
    try:
        known = int(req.get("photo_id") or 0)
    except (TypeError, ValueError):
        known = 0
    if not owner_id or not peer:
        return {"ok": False, "error": "not_found"}
    tried = 0
    saw = False
    ordered = sorted(clients.items(), key=lambda item: 0 if int(item[0]) == peer else 1)
    for user_id, client in ordered:
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner != owner_id or not client.is_connected():
            continue
        mine = int(user_id) == peer
        if not mine:
            tried += 1
            if tried > 2:
                break
        saw = True
        photo_id, mapped = await _face_photo_id(client, peer, mine)
        if mapped:
            return mapped
        step = _face_step(known, photo_id)
        if step == "skip":
            continue
        if step == "empty":
            return {"ok": True, "empty": True, "photo_id": 0}
        if step == "same":
            return {"ok": True, "same": True, "photo_id": photo_id}
        try:
            data = await asyncio.wait_for(
                client.download_profile_photo("me" if mine else peer, file=bytes, download_big=False),
                timeout=8,
            )
        except Exception as exc:
            mapped = error_code(exc)
            if mapped and mapped.get("error") == "flood":
                return mapped
            continue
        if isinstance(data, (bytes, bytearray)) and 32 < len(data) <= 300_000:
            return {
                "ok": True,
                "image": base64.b64encode(bytes(data)).decode(),
                "photo_id": photo_id,
            }
    if not saw:
        return {"ok": False, "error": "offline"}
    return {"ok": False, "error": "offline"}


async def _join_chat(client, chat_id: int, username: str) -> bool:
    from telethon.tl.functions.channels import JoinChannelRequest

    from meikai.reports import entity_watched, hits

    if hits(chat_id=chat_id, username=username):
        return False
    target = username or chat_id
    try:
        entity = await asyncio.wait_for(client.get_entity(target), timeout=12)
        if entity_watched(entity):
            return False
        await asyncio.wait_for(client(JoinChannelRequest(entity)), timeout=15)
        return True
    except Exception as exc:
        if "ALREADY" in type(exc).__name__.upper():
            return True
        log.info("节点：炒群加入失败 %s", type(exc).__name__)
        return False


async def op_speak(req: dict) -> dict:
    from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError, UserNotParticipantError

    from meikai.node_host import error_code

    owner_id = int(req.get("owner_id") or 0)
    user_id = int(req.get("user_id") or 0)
    chat_id = int(req.get("chat_id") or 0)
    text = " ".join(str(req.get("text") or "").split())
    try:
        delete_ms = max(0, min(int(req.get("delete_ms") or 0), 600000))
    except (TypeError, ValueError):
        delete_ms = 0
    if not owner_id or not user_id or not chat_id or not text or len(text) > 80:
        return {"ok": False, "error": "not_found"}
    if re.search(r"(https?://|t\.me/|telegram\.me/)", text, re.I):
        return {"ok": False, "error": "not_found"}
    record = accounts.get(str(user_id)) or {}
    if int(record.get("owner_id") or 0) != owner_id:
        return {"ok": False, "error": "not_found"}
    client = clients.get(user_id)
    if client is None or not client.is_connected():
        return {"ok": False, "error": "offline"}
    username = str(req.get("username") or "").strip().lstrip("@")
    from meikai.reports import hits

    if hits(chat_id=chat_id, username=username):
        return {"ok": False, "error": "blocked"}
    touch_active(user_id)
    try:
        sent = await asyncio.wait_for(client.send_message(chat_id, text), timeout=20)
    except (UserNotParticipantError, ChannelPrivateError, ChatWriteForbiddenError):
        if not await _join_chat(client, chat_id, username):
            return {"ok": False, "error": "not_in_chat"}
        touch_active(user_id)
        try:
            sent = await asyncio.wait_for(client.send_message(chat_id, text), timeout=20)
        except Exception as exc:
            mapped = error_code(exc)
            if mapped:
                return mapped
            return {"ok": False, "error": "not_in_chat"}
    except Exception as exc:
        mapped = error_code(exc)
        if mapped:
            return mapped
        name = type(exc).__name__
        if "Forbidden" in name or "Participant" in name or "Private" in name:
            return {"ok": False, "error": "not_in_chat"}
        log.warning("speak %s", name)
        return {"ok": False, "error": "internal"}
    if delete_ms > 0 and getattr(sent, "id", None):
        asyncio.create_task(_drop_line(client, chat_id, int(sent.id), delete_ms / 1000))
    return {"ok": True}


async def _drop_line(client, chat_id: int, msg_id: int, seconds: float) -> None:
    await asyncio.sleep(seconds)
    try:
        await asyncio.wait_for(client.delete_messages(chat_id, msg_id), timeout=20)
    except Exception as exc:
        log.info("节点：炒群删除失败 %s", type(exc).__name__)


_PARALLEL_OPS = frozenset({
    "list",
    "dialogs",
    "face",
    "peek",
    "wallets",
    "okpay",
    "speak",
    "resolve",
    "lookup",
    "replay",
    "glyphs",
    "blocks",
    "brands",
    "modes",
    "shared",
})


_MARKS = asyncio.Semaphore(4)


async def _boot_marks(client, user_id: int) -> None:
    async with _MARKS:
        await apply_marks(client, user_id)


OPS = {
    "send_code": op_send_code,
    "sign_code": op_sign_code,
    "password": op_password,
    "qr_start": op_qr_start,
    "qr_status": op_qr_status,
    "qr_refresh": op_qr_refresh,
    "cancel": op_cancel,
    "adopt": op_adopt,
    "hold": op_hold,
    "import_session": op_import,
    "drop": op_drop,
    "list": op_list,
    "okpay": op_okpay,
    "wallets": op_wallets,
    "refresh": op_refresh,
    "remove": op_remove,
    "modes": op_modes,
    "pay_balance": op_pay_balance,
    "shared": op_shared,
    "resolve": op_resolve,
    "find": op_find,
    "lookup": op_lookup,
    "blocks": op_blocks,
    "brands": op_brands,
    "glyphs": op_glyphs,
    "peek": op_peek,
    "replay": op_replay,
    "dialogs": op_dialogs,
    "face": op_face,
    "speak": op_speak,
}


async def _answer(req: dict) -> dict:
    try:
        op = OPS.get(req.get("op"))
        if op is None:
            return {"ok": False, "error": "unknown"}
        return await op(req)
    except Exception:
        log.warning("request failed")
        return {"ok": False, "error": "internal"}


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await reader.readline()
        if not line:
            writer.close()
            return
        req = json.loads(line.decode())
    except Exception:
        log.warning("request failed")
        writer.close()
        return
    if req.get("op") == "watch":
        await watch_session(reader, writer)
        return
    try:
        resp = await _answer(req)
    except Exception:
        log.warning("request failed")
        resp = {"ok": False, "error": "internal"}
    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    writer.close()


async def restore() -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    restore_modes()
    restore_policy()
    restore_blocks()
    restore_brands()
    from meikai.grab import load_docs, load_glyphs
    load_glyphs()
    load_docs()
    for path in SESSIONS.glob("pending-*.session"):
        path.unlink()
    saved = SESSIONS / "accounts.json"
    if saved.exists():
        try:
            accounts.update(json.loads(saved.read_text()))
        except json.JSONDecodeError:
            pass
    paths = [path for path in sorted(SESSIONS.glob("*.session")) if path.stem.isdigit()]
    gate = asyncio.Semaphore(8)

    async def boot(path: Path) -> None:
        async with gate:
            client = make_client(str(path.with_suffix("")), load_client(int(path.stem)))
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    note = host.mark_session(int(path.stem))
                    log.info("节点：%s user=%s", note, path.stem)
                    await client.disconnect()
                    return
                me = await client.get_me()
                clients[me.id] = client
                remember(me, int(client.session.dc_id))
                attach(client, me.id)
                asyncio.create_task(_boot_marks(client, me.id))
                log.info("restored user=%s dc=%s", me.id, client.session.dc_id)
            except AuthKeyDuplicatedError:
                note = host.mark_session(int(path.stem))
                log.info("节点：%s user=%s", note, path.stem)
                await client.disconnect()
            except Exception:
                log.warning("restore failed file=%s", path.name)

    if paths:
        await asyncio.gather(*(boot(path) for path in paths))


def session_dead(client) -> bool:
    err = getattr(client, "_updates_error", None)
    return isinstance(err, (UnauthorizedError, AuthKeyError))


async def retire(user_id: int, client) -> None:
    note = host.mark_session(user_id)
    clients.pop(user_id, None)
    log.info("节点：%s user=%s", note, user_id)
    try:
        await client.disconnect()
    except Exception:
        pass


async def keep_clients() -> None:
    while True:
        await asyncio.sleep(8)
        for user_id, client in list(clients.items()):
            if session_dead(client):
                await retire(user_id, client)
                continue
            if client.is_connected():
                continue
            try:
                await client.connect()
                if session_dead(client):
                    await retire(user_id, client)
                    continue
                log.info("节点：已重连 user=%s", user_id)
            except (UnauthorizedError, AuthKeyError):
                await retire(user_id, client)
            except Exception:
                log.info("节点：重连失败 user=%s", user_id)


async def serve() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    from meikai.grab import bind_glyph_change
    bind_glyph_change(_on_glyph_change)
    if credentials() is None:
        log.error("missing API_ID")
    if SOCKET and os.path.exists(SOCKET):
        os.unlink(SOCKET)
    await restore()
    asyncio.create_task(keep_clients())
    server = await asyncio.start_unix_server(handle, SOCKET, limit=4 * 1024 * 1024)
    os.chmod(SOCKET, 0o600)
    log.info("node=%s", node_id())
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
