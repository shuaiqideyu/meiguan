"""操作者自己的通知机器人。token 只留在主体库里，不进日志。"""

import asyncio
import logging
import re
import time

from aiohttp import ClientSession, ClientTimeout

from meiguan.store import clear_notifier, notifier_of, save_notifier

log = logging.getLogger("meiguan.notify")
_TOKEN = re.compile(r"^\d{6,16}:[A-Za-z0-9_-]{30,90}$")
_TIMEOUT = ClientTimeout(total=8)
_q: asyncio.Queue | None = None
_last: dict[int, float] = {}


def _clean(token: str) -> str:
    return "".join(str(token or "").split())


_QUIET = "quiet"


def _explain(code: int, desc: str) -> str:
    low = str(desc or "").lower()
    if code == 401 or "unauthorized" in low:
        return "token 无效"
    if "blocked by the user" in low:
        return "主号已停用这个机器人"
    if "can't initiate" in low or "chat not found" in low:
        return _QUIET
    if code == 429:
        return "Telegram：发送过于频繁，请稍后再试"
    return "Telegram 没有响应"


async def _call(token: str, method: str, payload: dict | None = None) -> tuple[dict | None, str]:
    if not _TOKEN.fullmatch(token):
        return None, "token 无效"
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        async with ClientSession(timeout=_TIMEOUT) as session:
            if payload is None:
                resp = await session.get(url)
            else:
                resp = await session.post(url, json=payload)
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
    except TimeoutError:
        return None, "Telegram 没有响应"
    except Exception as exc:
        log.warning("通知机器人 %s", type(exc).__name__)
        return None, "Telegram 没有响应"
    if isinstance(data, dict) and data.get("ok"):
        result = data.get("result")
        return result if isinstance(result, dict) else {}, ""
    code = 0
    desc = ""
    if isinstance(data, dict):
        try:
            code = int(data.get("error_code") or 0)
        except (TypeError, ValueError):
            code = 0
        desc = str(data.get("description") or "")
    log.info("通知机器人未送达 code=%s", code or "-")
    return None, _explain(code, desc)


async def bind(owner_id: int, token: str) -> tuple[dict | None, str]:
    token = _clean(token)
    me, error = await _call(token, "getMe")
    if error or not me or not me.get("is_bot"):
        return None, error or "token 无效"
    try:
        bot_id = int(me.get("id") or 0)
    except (TypeError, ValueError):
        bot_id = 0
    username = str(me.get("username") or "").strip().lstrip("@")
    if not bot_id or not username:
        return None, "token 无效"
    save_notifier(owner_id, token, username, bot_id)
    return {"bound": True, "username": username}, ""


async def ping(owner_id: int) -> tuple[str, str]:
    row = notifier_of(owner_id)
    if row is None:
        return "", "还没有通知机器人"
    _, error = await _call(row["token"], "sendMessage", {
        "chat_id": int(owner_id),
        "text": "测试通知。领到红包后会发到这里。",
    })
    if error == _QUIET:
        return "机器人可用", ""
    if error:
        return "", error
    return "已发到私聊", ""


def clear(owner_id: int) -> dict:
    clear_notifier(owner_id)
    return {"bound": False, "username": ""}


def _watch_text(item: dict) -> str:
    lines = ["有红包"]
    name = " ".join(str(item.get("name") or "").split())
    amount = " ".join(str(item.get("amount") or "").split())
    place = " ".join(str(item.get("title") or "").split())
    kind = " ".join(str(item.get("kind") or "").split())
    if name:
        lines.append(name)
    if amount:
        lines.append(amount)
    if place:
        lines.append(place)
    if kind:
        lines.append(kind)
    return "\n".join(lines)[:400]


def _grab_text(item: dict) -> str:
    lines = ["领到"]
    name = " ".join(str(item.get("name") or "").split())
    amount = " ".join(str(item.get("amount") or "").split())
    place = " ".join(str(item.get("title") or item.get("place") or "").split())
    if name:
        lines.append(name)
    if amount:
        lines.append(amount)
    if place:
        lines.append(place)
    return "\n".join(lines)[:400]


async def _send_grab(item: dict) -> None:
    try:
        owner_id = int(item.get("owner_id") or 0)
    except (TypeError, ValueError):
        return
    if not owner_id or not (item.get("ok") or item.get("watch")):
        return
    row = notifier_of(owner_id)
    if row is None:
        return
    text = _watch_text(item) if item.get("watch") else _grab_text(item)
    _, error = await _call(row["token"], "sendMessage", {
        "chat_id": owner_id,
        "text": text,
    })
    if error in ("", _QUIET, "Telegram：发送过于频繁，请稍后再试"):
        return
    log.info("通知没发出 owner=%s", owner_id)


async def _loop() -> None:
    assert _q is not None
    while True:
        item = await _q.get()
        try:
            owner_id = int(item.get("owner_id") or 0)
        except (TypeError, ValueError):
            continue
        wait = 1.0 - (time.monotonic() - _last.get(owner_id, 0))
        if wait > 0:
            await asyncio.sleep(min(wait, 2))
        try:
            await _send_grab(item)
        except Exception as exc:
            log.warning("通知失败 %s", type(exc).__name__)
        if owner_id:
            _last[owner_id] = time.monotonic()


def enqueue(item: dict) -> None:
    global _q
    if not item.get("ok") and not item.get("watch"):
        return
    if _q is None:
        _q = asyncio.Queue(maxsize=200)
        asyncio.get_running_loop().create_task(_loop())
    try:
        _q.put_nowait(item)
    except asyncio.QueueFull:
        log.info("通知队列已满")


def _check() -> None:
    assert _clean(" 12 3 ") == "123"
    assert _TOKEN.fullmatch("123456:abc") is None
    assert _explain(401, "Unauthorized") == "token 无效"
    assert _explain(403, "Forbidden: bot can't initiate conversation with a user") == _QUIET
    assert _explain(400, "Bad Request: chat not found") == _QUIET
    assert _explain(403, "Forbidden: bot was blocked by the user") == "主号已停用这个机器人"
    assert _grab_text({"name": "甲", "amount": "1.00 USDT", "title": "群"}) == "领到\n甲\n1.00 USDT\n群"
    assert _watch_text({"name": "甲", "amount": "5 USDT", "title": "群", "kind": "口令红包"}) == "有红包\n甲\n5 USDT\n群\n口令红包"


_check()
