"""领取循环。"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.functions.channels import GetChannelsRequest
from telethon.tl.functions.messages import GetChatsRequest
from telethon.tl.functions.users import GetFullUserRequest, GetUsersRequest
from telethon.tl.types import InputChannel, InputPeerChannel, InputPeerChat, InputPeerUser, InputUser

from meiguan.grab.chat import _answers
from meiguan.grab.detect import (
    accept_packet,
    button_url,
    claim_slots,
    classify,
    link_bot,
    claim_taken,
    packet_amount,
    packet_from_url,
    packet_slots,
    raw_text,
    resolve_wallet,
    rows,
    wallet_of,
)
from meiguan.grab.catalog import claim_kind, limit_for
from meiguan.grab.glyphs import apply_corrections, decode_expr, drop_glyphs
from meiguan.grab.types import Live, RedPacket, Result
from meiguan.grab.browser import read_app_page
from meiguan.grab.webapp import app_result
from meiguan.policy import (
    account_paused,
    account_watches,
    allow,
    below_amount,
    hours_open,
    mode_delay_for,
    mode_key,
    pay_open,
    pick_delay,
    wallet_key,
    wants_slots,
)
from meiguan.reports import (
    account_names,
    brand_fake,
    hits,
    learn_brand,
    locked_chat,
    note_steward,
    note_watch,
    poseur,
    remember,
    seal_text,
    steward_text,
    watched,
)
from meiguan.store import MODE_KINDS

log = logging.getLogger("meiguan.grab")

_MAX_ATTEMPTS = 4
_GRAB_CAP = 20000
_grabbed: dict[tuple[int, int, int], None] = {}
_grab_ready = False


def _grab_file() -> Path:
    root = Path(os.environ.get("MEIGUAN_SESSIONS", "/opt/meiguan/sessions"))
    return root / "grabbed"


def _load_grabbed() -> None:
    global _grab_ready
    if _grab_ready:
        return
    _grab_ready = True
    try:
        lines = _grab_file().read_text().splitlines()
    except OSError:
        return
    for line in lines[-_GRAB_CAP:]:
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            _grabbed[(int(parts[0]), int(parts[1]), int(parts[2]))] = None
        except ValueError:
            continue


def finished(user_id: int, chat_id: int, msg_id: int) -> bool:
    if not chat_id or not msg_id:
        return False
    _load_grabbed()
    return (int(user_id), int(chat_id), int(msg_id)) in _grabbed


_grab_since_compact = 0


def finish(user_id: int, chat_id: int, msg_id: int) -> None:
    global _grab_since_compact
    if not chat_id or not msg_id:
        return
    _load_grabbed()
    key = (int(user_id), int(chat_id), int(msg_id))
    if key in _grabbed:
        return
    _grabbed[key] = None
    evicted = False
    while len(_grabbed) > _GRAB_CAP:
        _grabbed.pop(next(iter(_grabbed)))
        evicted = True
    path = _grab_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        # 满两千条再整文件重写。每领一次都重写会堵住同节点上其他号。
        if evicted:
            _grab_since_compact += 1
        if evicted and _grab_since_compact >= 2000:
            _grab_since_compact = 0
            path.write_text("".join(f"{a} {b} {c}\n" for a, b, c in _grabbed))
        else:
            with path.open("a") as handle:
                handle.write(f"{key[0]} {key[1]} {key[2]}\n")
        if fresh:
            path.chmod(0o600)
    except OSError:
        return


_skipped: set[tuple] = set()


def _tell_watch(user_id: int, kind: str, shown: str, title: str) -> None:
    from meiguan.node_host import accounts, add_watch

    row = accounts.get(str(user_id)) or {}
    owner = int(row.get("owner_id") or 0)
    if not owner:
        return
    add_watch({
        "owner_id": owner,
        "watch": True,
        "name": str(row.get("name") or "")[:40],
        "amount": " ".join(str(shown or "").split())[:40],
        "title": " ".join(str(title or "").split())[:40],
        "kind": _LABELS.get(kind, ""),
    })


def note_skip(user_id: int, chat_id: int, msg_id: int, reason: str, detail: str = "") -> None:
    key = (int(user_id), int(chat_id or 0), int(msg_id or 0), reason)
    if key in _skipped:
        return
    _skipped.add(key)
    if len(_skipped) > 4000:
        _skipped.clear()
        _skipped.add(key)
    from meiguan.node_host import accounts, add_skip

    owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
    if not owner:
        return
    add_skip({
        "owner_id": owner,
        "reason": reason,
        "detail": " ".join(str(detail or "").split())[:40],
    })


async def occupy(kind: str, coro) -> None:
    try:
        await coro
    except Exception as exc:
        log.info("节点：领取异常 %s", type(exc).__name__)


_LABELS = {
    "direct": "按钮关键词",
    "captcha": "算式计算",
    "webapp": "网页验证码",
    "phrase": "口令红包",
    "cloudflare": "网页验证码",
}


_live: dict[int, Live] = {}


_shared: set[tuple] = set()


_modes = {kind: True for kind in MODE_KINDS}
_policies: dict[int, dict] = {}
_sighted: set[tuple] = set()


def owner_of(user_id: int) -> int:
    from meiguan.node_host import accounts

    return int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)


def policy_of(user_id: int) -> dict:
    from meiguan.policy import default_policy

    owner = owner_of(user_id)
    found = _policies.get(owner)
    if found is not None:
        return found
    base = default_policy()
    for kind in MODE_KINDS:
        base["modes"][kind] = bool(_modes.get(kind, True))
    base["modes"]["shared"] = False
    return base


def mode_on(user_id: int, kind: str) -> bool:
    policy = policy_of(user_id)
    if not hours_open(policy):
        return False
    return bool(policy["modes"].get(mode_key(kind)))


def paused(user_id: int) -> bool:
    live = _live.get(user_id)
    return live is None or time.time() < live.pause_until


def apply_policies(raw: dict) -> dict:
    from meiguan.policy import normalize

    nxt = {}
    for key, value in (raw or {}).items():
        if str(key).lstrip("-").isdigit() and isinstance(value, dict):
            nxt[int(key)] = normalize(value)
    _policies.clear()
    _policies.update(nxt)
    return {str(key): value for key, value in _policies.items()}


def apply_modes(flags: dict) -> dict[str, bool]:
    incoming = dict(flags or {})
    if "webapp" not in incoming and "app" in incoming:
        incoming["webapp"] = bool(incoming["app"])
    if "captcha" not in incoming and "dm" in incoming:
        incoming["captcha"] = bool(incoming["dm"])
    for kind in MODE_KINDS:
        if kind in incoming:
            _modes[kind] = bool(incoming[kind])
    return dict(_modes)


def delay_targets(msg, rp: RedPacket | None) -> list[str]:
    found: list[str] = []

    def add(value) -> None:
        name = wallet_key(str(value or ""))
        if name and name not in found:
            found.append(name)

    add(wallet_of(msg))
    if rp is not None:
        add(rp.bot)
    for row in rows(msg):
        for btn in row.buttons:
            url = button_url(btn).lower()
            if "startapp=" in url or "start=" in url or "/wallet" in url:
                add(link_bot(url))
    add(getattr(getattr(msg, "via_bot", None), "username", None))
    add(getattr(getattr(msg, "sender", None), "username", None))
    return found


def share(
    user_id: int, chat_id: int, msg_id: int, rp: RedPacket,
    wallet: str, packet: str, username: str = "", bots: set | None = None,
    targets: list[str] | None = None,
) -> None:
    if not mode_on(user_id, rp.kind):
        return
    from meiguan.node_host import accounts, clients

    owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
    names = bots or set()
    if hits(names, chat_id, username, title=_titles.get(int(chat_id), "")):
        return
    key = (owner, chat_id, msg_id, rp.kind)
    if key in _shared:
        return
    _shared.add(key)
    if len(_shared) > 4000:
        _shared.clear()
        _shared.add(key)
    copy = RedPacket(
        rp.kind, data=rp.data, start=rp.start, bot=rp.bot, short=rp.short,
        premium_only=rp.premium_only, follows=rp.follows, place=rp.place, page=rp.page,
    )
    for other_id, other in list(clients.items()):
        if other_id == user_id or not other.is_connected():
            continue
        other_owner = int((accounts.get(str(other_id)) or {}).get("owner_id") or 0)
        if other_owner != owner:
            continue
        if not mode_on(other_id, rp.kind):
            continue
        if account_paused(policy_of(other_id), other_id) and not account_watches(policy_of(other_id), other_id):
            continue
        if not allow(policy_of(other_id), chat_id=chat_id, username=username, bots=names):
            continue
        asyncio.create_task(
            safe_shared(
                other, other_id, chat_id, msg_id, copy, wallet, packet, username, names, targets,
            )
        )


async def run_shared(
    client, user_id: int, chat_id: int, msg_id: int, rp: RedPacket,
    wallet: str, packet: str, username: str = "", bots: set | None = None,
    targets: list[str] | None = None,
) -> None:
    from meiguan.grab.solver import bind_solver

    bind_solver(user_id)
    if locked_chat(chat_id):
        return
    live = _live.get(user_id)
    if live is None or not client.is_connected() or time.time() < live.pause_until:
        return
    if not mode_on(user_id, rp.kind):
        return
    if account_paused(policy_of(user_id), user_id) and not account_watches(policy_of(user_id), user_id):
        return
    seen_title = _titles.get(chat_id) or ""
    if not seen_title:
        seen_title = await cached_title(client, chat_id)
    if hits(bots or set(), chat_id, username, title=seen_title):
        return
    if not allow(policy_of(user_id), chat_id=chat_id, username=username, bots=bots or set()):
        return
    kind_key = (chat_id, msg_id, rp.kind)
    if kind_key in live.done or kind_key in live.inflight or finished(user_id, chat_id, msg_id):
        return
    live.inflight.add(kind_key)
    slots_task = asyncio.create_task(claim_slots(client, chat_id, msg_id))
    result = Result(False)
    try:
        policy = policy_of(user_id)
        slots = ""
        if wants_slots(policy):
            try:
                slots = await slots_task
            except Exception:
                slots = ""
        if below_amount(policy, packet, slots or ""):
            live.done.add(kind_key)
            note_skip(user_id, chat_id, msg_id, "floor", "低于领取阈值")
            return
        if not pay_open(policy, rp.bot or "", rp.kind, owner_of(user_id)):
            live.done.add(kind_key)
            note_skip(user_id, chat_id, msg_id, "pay", "这一类打码已关闭")
            return
        if account_watches(policy, user_id):
            live.done.add(kind_key)
            _tell_watch(user_id, rp.kind, packet, seen_title)
            return
        names = list(targets or [])
        if not names:
            names = [item for item in (wallet, rp.bot or "") if item]
        await pause_for(user_id, names, rp.kind)
        if not hours_open(policy):
            live.done.add(kind_key)
            return
        from meiguan.node_roster import touch_active

        touch_active(user_id)
        started = time.monotonic()
        while live.attempts.get(kind_key, 0) < _MAX_ATTEMPTS:
            live.attempts[kind_key] = live.attempts.get(kind_key, 0) + 1
            try:
                result = await asyncio.wait_for(
                    dispatch(client, user_id, rp, None, chat_id, msg_id),
                    timeout=claim_limit(rp),
                )
            except asyncio.TimeoutError:
                result = Result(False, retryable=False, feedback="节点：领取超时")
            if result.pause:
                live.pause_until = time.time() + 3600
            if (rp.kind == "phrase" and rp.start) or result.ok or not result.retryable:
                break
            await asyncio.sleep(0.05)
        if result.hold:
            live.attempts.pop(kind_key, None)
            return
        live.done.add(kind_key)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        title, seen_user = await settled_face(client, chat_id, username, seen_title)
        hits(chat_id=chat_id, username=seen_user, title=title)
        publish(
            user_id, rp.kind, wallet, packet, result, chat_id, title, seen_user, slots,
            msg_id, elapsed_ms,
        )
    finally:
        if not slots_task.done():
            slots_task.cancel()
            try:
                await slots_task
            except asyncio.CancelledError:
                pass
        live.inflight.discard(kind_key)


async def safe_shared(
    client, user_id: int, chat_id: int, msg_id: int, rp: RedPacket,
    wallet: str, packet: str, username: str = "", bots: set | None = None,
    targets: list[str] | None = None,
) -> None:
    await occupy(
        rp.kind,
        run_shared(
            client, user_id, chat_id, msg_id, rp, wallet, packet, username, bots, targets,
        ),
    )


def trim(live: Live) -> None:
    if len(live.done) > 4000:
        live.done.clear()
        live.attempts.clear()


def chat_username(chat) -> str:
    if chat is None:
        return ""
    name = str(getattr(chat, "username", None) or "").strip().lstrip("@")
    if name and "/" not in name and " " not in name:
        return name
    for item in getattr(chat, "usernames", None) or []:
        if not getattr(item, "active", True):
            continue
        name = str(getattr(item, "username", None) or "").strip().lstrip("@")
        if name and "/" not in name and " " not in name:
            return name
    return ""


def publish(
    user_id: int, kind: str, wallet: str, packet: str, result: Result,
    chat_id: int = 0, title: str = "", username: str = "", slots: str = "",
    msg_id: int = 0, elapsed_ms: int = 0,
) -> None:
    name = _LABELS.get(kind, kind)
    state = "领到" if result.ok else "未领到"
    detail = " ".join((result.feedback or result.amount or "").split())[:80]
    finish(user_id, chat_id, msg_id)
    log.info(
        "节点：%s %s user=%s wallet=%s packet=%s %s",
        name, state, user_id, wallet or "-", packet or "-", detail,
    )
    from meiguan.node_host import accounts, add_claim

    row = accounts.get(str(user_id)) or {}
    owner_id = int(row.get("owner_id") or 0)
    if not owner_id:
        return
    add_claim({
        "owner_id": owner_id,
        "user_id": int(user_id),
        "name": str(row.get("name") or "")[:40],
        "kind": kind,
        "ok": bool(result.ok),
        "amount": claim_taken(str(result.amount or ""), str(result.feedback or ""), str(packet or ""))[:40],
        "wallet": str(wallet or "")[:40],
        "chat_id": int(chat_id or 0),
        "title": " ".join(str(title or "").split())[:40],
        "username": str(username or "").strip().lstrip("@")[:40],
        "detail": detail,
        "slots": str(slots or "")[:20],
        "msg_id": int(msg_id or 0),
        "elapsed_ms": max(0, int(elapsed_ms or 0)),
    })


def claim_limit(rp: RedPacket) -> float:
    found = limit_for(rp)
    if found is not None:
        return found
    if (rp.short or "").lower() == "hbpic":
        return 180
    if rp.kind == "cloudflare":
        return 90
    if rp.kind == "webapp" and (rp.bot or "").lower() == "kpay":
        return 90
    if rp.kind == "webapp" or rp.bot == "kpay":
        return 75
    return 40


async def pause_for(user_id: int, names: list[str], kind: str = "") -> None:
    policy = policy_of(user_id)
    chosen = ""
    bounds = None
    if kind:
        bounds = mode_delay_for(policy, kind)
        chosen = kind
    if not bounds:
        return
    low, high = bounds
    wait = pick_delay(low, high)
    if wait <= 0:
        return
    log.info("节点：延迟抢包 user=%s %s %s秒", user_id, chosen or "-", wait)
    await asyncio.sleep(wait)


async def dispatch(client, user_id, rp: RedPacket, event, chat_id: int, msg_id: int) -> Result:
    from meiguan.grab.solver import bind_solver

    bind_solver(user_id)
    plugged = await claim_kind(rp.kind, client, user_id, rp, event, chat_id, msg_id)
    if plugged is not None:
        return plugged
    return Result(False, retryable=False)


async def open_link(client, user_id, event, chat_id: int, url: str) -> Result | None:
    if "tgWebAppData" in (url or ""):
        return app_result(await read_app_page(url, client))
    packet = packet_from_url(url)
    if packet is None:
        return None
    if "yypay" in url.lower():
        packet.kind = "cloudflare"
        packet.place = "chat"
    return await dispatch(client, user_id, packet, event, chat_id, 0)


async def attempt(client, user_id, event, msg, rp: RedPacket) -> Result:
    return await dispatch(client, user_id, rp, event, event.chat_id, msg.id)


def packet_bots(msg, rp: RedPacket | None) -> set[str]:
    keys = set()

    def add(value) -> None:
        name = wallet_key(str(value or ""))
        if name:
            keys.add(name)

    if rp is not None:
        add(rp.bot)
    add(wallet_of(msg))
    add(getattr(getattr(msg, "via_bot", None), "username", None))
    add(getattr(getattr(msg, "sender", None), "username", None))
    for row in rows(msg):
        for btn in row.buttons:
            url = button_url(btn).lower()
            if "t.me/" in url or "telegram.me/" in url:
                add(link_bot(url))
    via = getattr(msg, "via_bot_id", None)
    if via:
        keys.add(str(int(via)))
    sender_id = getattr(msg, "sender_id", None)
    if sender_id:
        keys.add(str(int(sender_id)))
    return keys


def _entity_name(entity) -> str:
    if entity is None:
        return ""
    return " ".join(
        part for part in (
            str(getattr(entity, "first_name", None) or getattr(entity, "title", None) or "").strip(),
            str(getattr(entity, "last_name", None) or "").strip(),
        ) if part
    )


def _message_bots(msg) -> list:
    found = []
    sender = getattr(msg, "sender", None)
    if getattr(sender, "bot", False):
        found.append(sender)
    via = getattr(msg, "via_bot", None)
    if via is not None and getattr(via, "bot", True):
        found.append(via)
    return found


_titles: dict[int, str] = {}


def watch_bots(msg) -> bool:
    blocked = False
    for entity in _message_bots(msg):
        display = _entity_name(entity)
        if not watched(display):
            continue
        blocked = True
        number = int(getattr(entity, "id", 0) or 0)
        names = account_names(entity)
        if names:
            for item in names:
                note_watch(display, username=item, user_id=number)
        elif number:
            note_watch(display, user_id=number)
    return blocked


def poseur_message(msg) -> bool:
    found = False

    def watch(entity, username: str = "", user_id: int = 0) -> None:
        nonlocal found
        names = account_names(entity)
        handle = str(username or "").strip().lstrip("@")
        if handle and handle.lower() not in {item.lower() for item in names}:
            names.append(handle)
        number = user_id or int(getattr(entity, "id", 0) or 0)
        display = _entity_name(entity)
        if not brand_fake(display, names, number) and not poseur(display, names):
            return
        found = True
        if names:
            for item in names:
                remember(item, number)
        elif number:
            remember("", number)

    for entity in _message_bots(msg):
        watch(entity)
    return found


_seal_busy: set[int] = set()
_seal_done: set[int] = set()
_seal_next: dict[int, float] = {}


def begin_seals(client, msg) -> list:
    tasks = []
    for entity in _message_bots(msg):
        number = int(getattr(entity, "id", 0) or 0)
        if not number or number in _seal_done or number in _seal_busy:
            continue
        if time.time() < _seal_next.get(number, 0):
            continue
        _seal_busy.add(number)
        tasks.append(asyncio.create_task(_read_seal(client, entity, number)))
    return tasks


async def _read_seal(client, entity, number: int) -> None:
    try:
        full = await asyncio.wait_for(client(GetFullUserRequest(entity)), timeout=8)
    except Exception:
        _seal_next[number] = time.time() + 600
        return
    finally:
        _seal_busy.discard(number)
    _seal_done.add(number)
    user = entity
    for item in getattr(full, "users", None) or []:
        if int(getattr(item, "id", 0) or 0) == number:
            user = item
            break
    full_user = getattr(full, "full_user", None)
    if steward_text(getattr(full_user, "about", "") or ""):
        names = account_names(user) or account_names(entity)
        note_steward(names, number)
    verification = getattr(full_user, "bot_verification", None)
    if not seal_text(getattr(verification, "description", "") or ""):
        return
    names = account_names(user) or account_names(entity)
    changed = learn_brand(_entity_name(user) or _entity_name(entity), names, number)
    if not changed:
        return
    log.info("节点：记下认证钱包 %s", " ".join(item["nick"] for item in changed))
    try:
        from meiguan.node_host import add_brands
        from meiguan.node_worker import store_brands

        store_brands(changed, persist=True)
        add_brands(changed)
    except Exception:
        log.info("节点：认证钱包没有送出")


async def cached_title(client, chat_id: int) -> str:
    if not chat_id:
        return ""
    if chat_id in _titles:
        return _titles[chat_id]
    try:
        chat = await asyncio.wait_for(client.get_entity(chat_id), timeout=2)
    except Exception:
        _titles[chat_id] = ""
        return ""
    title = chat_title(chat)
    _titles[chat_id] = title
    return title


def keep_title(chat_id: int, title: str) -> None:
    text = " ".join(str(title or "").split())[:40]
    if chat_id and text:
        _titles[int(chat_id)] = text


def chat_title(chat) -> str:
    if chat is None:
        return ""
    title = " ".join(str(getattr(chat, "title", None) or "").split())
    if title:
        return title[:40]
    name = " ".join(
        part for part in (
            str(getattr(chat, "first_name", None) or "").strip(),
            str(getattr(chat, "last_name", None) or "").strip(),
        ) if part
    )
    return name[:40]


async def window_face(event, user_id: int) -> tuple[str, str]:
    preset = str(getattr(event, "_username", "") or "").strip().lstrip("@")
    cached = [getattr(event, "_chat", None)]
    message = getattr(event, "message", None)
    if message is not None:
        cached.append(getattr(message, "_chat", None))
    username = preset
    title = ""
    for chat in cached:
        username = username or chat_username(chat)
        title = title or chat_title(chat)
    if username and title:
        return username, title
    policy = policy_of(user_id)
    chat_id = int(getattr(event, "chat_id", 0) or 0)
    listed = any(int(item.get("chat_id") or 0) == chat_id for item in policy.get("windows") or [])
    if policy.get("window_mode") == "off" or listed or username:
        return username, title
    getter = getattr(event, "get_chat", None)
    if getter is None:
        return username, title
    try:
        chat = await asyncio.wait_for(getter(), timeout=5)
    except Exception:
        return username, title
    username = username or chat_username(chat)
    title = title or chat_title(chat)
    return username, title


async def _fresh_chat(client, chat_id: int):
    peer = await client.get_input_entity(chat_id)
    if isinstance(peer, InputPeerChannel):
        result = await client(GetChannelsRequest([InputChannel(peer.channel_id, peer.access_hash)]))
        return result.chats[0] if result.chats else None
    if isinstance(peer, InputPeerChat):
        result = await client(GetChatsRequest([peer.chat_id]))
        return result.chats[0] if result.chats else None
    if isinstance(peer, InputPeerUser):
        result = await client(GetUsersRequest([InputUser(peer.user_id, peer.access_hash)]))
        return result.users[0] if result.users else None
    return None


async def settled_face(client, chat_id: int, username: str, seen_title: str, event=None) -> tuple[str, str]:
    title = seen_title or _titles.get(chat_id) or ""
    seen_user = username
    if not title:
        fresh_title, fresh_user = await current_face(client, chat_id, event)
        title = fresh_title or title
        seen_user = fresh_user or username
    keep_title(chat_id, title)
    return title, seen_user or username


async def current_face(client, chat_id: int, event=None) -> tuple[str, str]:
    chat = None
    if client is not None and chat_id:
        try:
            chat = await asyncio.wait_for(_fresh_chat(client, int(chat_id)), timeout=5)
        except Exception:
            chat = None
    if chat is None and event is not None:
        getter = getattr(event, "get_chat", None)
        if getter is not None:
            try:
                chat = await asyncio.wait_for(getter(), timeout=5)
            except Exception:
                chat = None
        if chat is None:
            chat = getattr(event, "_chat", None)
    return chat_title(chat), chat_username(chat)


_INVITE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)", re.I)


def _invite(text: str) -> str:
    matched = _INVITE.search(text or "")
    return matched.group(1) if matched else ""


def emit_sighting(user_id: int, chat_id: int, msg_id: int, username: str, bots: set[str], kind: str, invite: str = "") -> None:
    from meiguan.node_host import accounts, add_sighting

    owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
    if not owner:
        return
    key = (int(chat_id), int(msg_id))
    if key in _sighted:
        return
    _sighted.add(key)
    if len(_sighted) > 4000:
        _sighted.clear()
        _sighted.add(key)
    add_sighting({
        "owner_id": owner,
        "chat_id": int(chat_id),
        "msg_id": int(msg_id),
        "username": username or "",
        "bots": sorted(bots),
        "kind": kind,
        "invite": invite,
    })


async def work(client: TelegramClient, user_id: int, event) -> None:
    from meiguan.grab.solver import bind_solver

    bind_solver(user_id)
    live = _live.get(user_id)
    if live is None or not client.is_connected():
        return
    if time.time() < live.pause_until:
        return
    msg = event.message
    chat_id = event.chat_id
    if locked_chat(chat_id):
        return
    if time.time() < live.blocked.get(chat_id, 0):
        return
    trim(live)
    rp = classify(msg)
    if rp is None or rp.kind == "locked" or not mode_on(user_id, rp.kind):
        return
    if account_paused(policy_of(user_id), user_id) and not account_watches(policy_of(user_id), user_id):
        return
    kind_key = (chat_id, msg.id, rp.kind)
    if kind_key in live.done or kind_key in live.inflight or finished(user_id, chat_id, msg.id):
        return
    live.inflight.add(kind_key)
    seals = begin_seals(client, msg)
    try:
        wallet, (username, seen_title) = await asyncio.gather(
            resolve_wallet(client, msg),
            window_face(event, user_id),
        )
        if wallet and not rp.bot:
            rp.bot = wallet
        shown = packet_amount(msg)
        if not seen_title:
            seen_title = _titles.get(chat_id) or ""
        if not seen_title:
            seen_title = await cached_title(client, chat_id)
        keep_title(chat_id, seen_title)
        if hits(chat_id=chat_id, username=username, title=seen_title) or watch_bots(msg) or poseur_message(msg):
            live.done.add(kind_key)
            return
        if rp.needs_emoji:
            decoded, mapping = await decode_expr(client, msg, rp.buttons)
            if not decoded:
                live.done.add(kind_key)
                log.info("节点：窗口验证码表情未认出 user=%s", user_id)
                return
            again = classify(msg, decoded)
            if again is None or not again.expr:
                live.done.add(kind_key)
                log.info("节点：窗口验证码算式未认出 user=%s", user_id)
                return
            rp = again
            rp.emoji_map = mapping
        bots = packet_bots(msg, rp)
        extra = getattr(event, "_bots", None)
        if extra:
            bots |= {str(item) for item in extra if item}
        if seals:
            await asyncio.wait(seals, timeout=2)
        if hits(bots, chat_id, username):
            live.done.add(kind_key)
            return
        if not allow(policy_of(user_id), chat_id=chat_id, username=username, bots=bots):
            live.done.add(kind_key)
            return
        targets = delay_targets(msg, rp)
        if not getattr(event, "foreign", False):
            emit_sighting(user_id, chat_id, msg.id, username, bots, rp.kind, _invite(raw_text(msg)))
            public = bool(username)
            if (
                rp.kind in ("webapp", "cloudflare")
                or (rp.kind == "phrase" and rp.data)
                or (rp.kind == "captcha" and (bool(rp.start) or public))
                or (rp.kind == "direct" and public)
            ):
                share(user_id, chat_id, msg.id, rp, wallet, shown, username, bots, targets)
        slots = packet_slots(raw_text(msg))
        policy = policy_of(user_id)
        if below_amount(policy, shown, slots):
            live.done.add(kind_key)
            note_skip(user_id, chat_id, msg.id, "floor", "低于领取阈值")
            return
        if not pay_open(policy, rp.bot or "", rp.kind, owner_of(user_id)):
            live.done.add(kind_key)
            note_skip(user_id, chat_id, msg.id, "pay", "这一类打码已关闭")
            return
        if account_watches(policy, user_id):
            live.done.add(kind_key)
            _tell_watch(user_id, rp.kind, shown, seen_title)
            return
        await pause_for(user_id, targets, rp.kind)
        if not hours_open(policy_of(user_id)):
            live.done.add(kind_key)
            return
        from meiguan.node_roster import touch_active

        touch_active(user_id)
        result = Result(False)
        started = time.monotonic()
        while live.attempts.get(kind_key, 0) < _MAX_ATTEMPTS:
            live.attempts[kind_key] = live.attempts.get(kind_key, 0) + 1
            try:
                result = await asyncio.wait_for(
                    attempt(client, user_id, event, msg, rp),
                    timeout=claim_limit(rp),
                )
            except asyncio.TimeoutError:
                result = Result(False, retryable=False, feedback="节点：领取超时")
            if result.pause:
                live.pause_until = time.time() + 3600
            if rp.kind == "phrase" and (rp.start or not result.retryable):
                break
            if result.ok:
                apply_corrections(rp.emoji_map, result.corrections)
                break
            if result.wrong and not rp.emoji_map:
                break
            if result.wrong and rp.emoji_map:
                _answers.pop((chat_id, msg.id), None)
                drop_glyphs(rp.emoji_map)
                decoded, mapping = await decode_expr(client, msg, rp.buttons, force=True)
                again = classify(msg, decoded) if decoded else None
                if again and again.expr:
                    rp.expr = again.expr
                    rp.emoji_map = mapping
                    continue
            if not result.retryable:
                break
            await asyncio.sleep(0.05)
        if result.hold:
            live.attempts.pop(kind_key, None)
            return
        live.done.add(kind_key)
        if result.blocked:
            live.blocked[chat_id] = time.time() + 60
        elapsed_ms = int((time.monotonic() - started) * 1000)
        title, seen_user = await settled_face(client, chat_id, username, seen_title, event)
        hits(chat_id=chat_id, username=seen_user, title=title)
        publish(
            user_id, rp.kind, wallet, shown, result, chat_id,
            title, seen_user, slots,
            msg.id, elapsed_ms,
        )
    finally:
        live.inflight.discard(kind_key)


async def safe(client: TelegramClient, user_id: int, event, kind: str = "") -> None:
    if not kind:
        msg = getattr(event, "message", None)
        found = classify(msg) if msg is not None else None
        kind = found.kind if found is not None else "direct"
    await occupy(kind, work(client, user_id, event))


_blocked_windows: dict[int, float] = {}


def block_window(chat_id: int, minutes: int) -> None:
    """屏蔽窗口 minutes 分钟（重复命中只刷新，不叠加）。"""
    if minutes <= 0 or not chat_id:
        return
    _blocked_windows[chat_id] = time.time() + minutes * 60


def window_blocked(chat_id: int) -> bool:
    """窗口是否在临时屏蔽中。"""
    until = _blocked_windows.get(chat_id)
    if until is None:
        return False
    if time.time() >= until:
        _blocked_windows.pop(chat_id, None)
        return False
    return True


def keyword_hit(user_id: int, text: str) -> bool:
    """文本是否命中窗口屏蔽关键词。"""
    keywords = policy_of(user_id).get("window_block_keywords") or []
    if not keywords:
        return False
    body = text or ""
    return any(kw and kw in body for kw in keywords)


def attach(client: TelegramClient, user_id: int) -> None:
    if getattr(client, "_meiguan_grab", False):
        return
    client._meiguan_grab = True
    _live[user_id] = Live()

    async def on_event(event):
        msg = event.message
        if msg is None or getattr(msg, "out", False):
            return
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        if locked_chat(chat_id) or window_blocked(chat_id):
            return
        # 关键词检测：命中后屏蔽窗口 N 分钟（不限于红包消息）
        minutes = int(policy_of(user_id).get("window_block_minutes") or 0)
        if minutes > 0 and keyword_hit(user_id, raw_text(msg)):
            block_window(chat_id, minutes)
            log.info("节点：命中屏蔽关键词，屏蔽窗口 %s 分钟 chat=%s", minutes, chat_id)
            return
        if not getattr(msg, "reply_markup", None):
            return
        known = _titles.get(chat_id)
        if known and hits(chat_id=chat_id, title=known):
            return
        if not accept_packet(msg):
            return
        rp = classify(msg)
        if rp is None or rp.kind == "locked" or not mode_on(user_id, rp.kind):
            return
        if account_paused(policy_of(user_id), user_id) and not account_watches(policy_of(user_id), user_id):
            return
        asyncio.create_task(safe(client, user_id, event, rp.kind))

    client.add_event_handler(on_event, events.NewMessage())
    client.add_event_handler(on_event, events.MessageEdited())
    log.info("节点：开始领取 user=%s", user_id)
