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
        "text": "测试通知。领到或听到红包后会发到这里。",
    })
    if error == _QUIET:
        return "机器人可用", ""
    if error:
        return "", error
    return "已发到私聊", ""


def clear(owner_id: int) -> dict:
    clear_notifier(owner_id)
    return {"bound": False, "username": ""}


def _one(value) -> str:
    return " ".join(str(value or "").split())


def _wallet_line(item: dict) -> str:
    title = _one(item.get("wallet_title"))
    user = _one(item.get("wallet_user")).lstrip("@")
    if title and user:
        return f"{title} @{user}"
    if user:
        return f"@{user}"
    return title


def _clock(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60} 分 {seconds % 60} 秒"


def listen_text(item: dict) -> str:
    wallet = _wallet_line(item)
    place = _one(item.get("title"))
    amount = _one(item.get("amount"))
    slots = _one(item.get("slots"))
    coupon = bool(item.get("coupon"))
    phase = str(item.get("phase") or "open")
    if phase == "unlock":
        head = "🔓 红包监听 · 已解锁"
        if wallet:
            head = f"{head} | {wallet}"
        lines = [head]
        if place:
            lines.append(f"📍 来源: {place}")
        count = f"{slots} 张" if coupon else f"{slots} 个"
        if amount and slots:
            lines.append(f"💵 金额: {amount} · 📦 {count}")
        elif amount:
            lines.append(f"💵 金额: {amount}")
        elif slots:
            lines.append(f"📦 数量: {count}")
        if item.get("web"):
            lines.append("🌐 群发包，需在群")
        try:
            waited = int(item.get("lock_seconds") or 0)
        except (TypeError, ValueError):
            waited = 0
        if waited > 0:
            lines.append(f"⏱ 锁定 {_clock(waited)}后解锁")
        return "\n".join(lines)[:900]
    lines = ["🧧 红包监听", ""]
    if place:
        lines.append(f"📍 来源: {place}")
    if wallet:
        lines.append(f"💰 钱包: {wallet}")
    if amount:
        lines.append(f"💵 金额: {amount}")
    if slots:
        lines.append(f"📦 数量: {slots}" + (" 张" if coupon else ""))
    if item.get("web"):
        lines.append("🌐 网页验证红包")
    expr = _one(item.get("expr"))
    answer = _one(item.get("answer"))
    if expr and answer:
        lines.append(f"🧮 {expr} → 答案: {answer}")
    elif expr:
        lines.append(f"🧮 {expr}")
    phrase = _one(item.get("phrase"))
    if phrase:
        lines.append(f"🔑 口令: {phrase}")
    if phase == "locked":
        lines.append("🔒 状态: 未解锁，解锁后会回复本条消息并附上领取链接")
    else:
        limits = [_one(line) for line in item.get("limits") or [] if _one(line)]
        if limits:
            lines.append("🔒 限制")
            last = len(limits) - 1
            for index, line in enumerate(limits[:4]):
                mark = "└" if index == last else "├"
                lines.append(f"   {mark} {line}")
    return "\n".join(lines)[:900]


def listen_buttons(item: dict) -> list[list[dict]]:
    rows = []
    view = str(item.get("view_url") or "")
    claim = str(item.get("claim_url") or "")
    if view.startswith("https://t.me/") or view.startswith("https://telegram.me/"):
        rows.append([{"text": "🔗 查看消息", "url": view}])
    if str(item.get("phase") or "") != "locked" and (
        claim.startswith("https://t.me/") or claim.startswith("https://telegram.me/")
    ):
        rows.append([{"text": "🧧 点击领取", "url": claim}])
    return rows


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


def _spent(item: dict) -> str:
    try:
        ms = int(item.get("elapsed_ms") or 0)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    if ms < 1000:
        return f"{ms} 毫秒"
    whole, frac = divmod(ms // 100, 10)
    if frac:
        return f"{whole}.{frac} 秒"
    return f"{whole} 秒"


def _grab_text(item: dict) -> str:
    name = _one(item.get("name"))
    place = _one(item.get("title") or item.get("place"))
    amount = _one(item.get("amount"))
    wallet = _one(item.get("wallet")).lstrip("@")
    head = f"🧧 领到 · {name}" if name else "🧧 领到"
    lines = [head, ""]
    if place:
        lines.append(f"📍 来源: {place}")
    if wallet:
        lines.append(f"💰 钱包: @{wallet}")
    if amount:
        lines.append(f"💵 金额: {amount}")
    spent = _spent(item)
    if spent:
        lines.append(f"⏱ {spent}")
    return "\n".join(lines)[:900]


def grab_buttons(item: dict) -> list[list[dict]]:
    from meiguan.grab.detect import view_link

    try:
        chat_id = int(item.get("chat_id") or 0)
        msg_id = int(item.get("msg_id") or 0)
    except (TypeError, ValueError):
        return []
    if msg_id <= 0:
        return []
    url = view_link(chat_id, msg_id, str(item.get("username") or ""))
    if url.startswith("https://t.me/") or url.startswith("https://telegram.me/"):
        return [[{"text": "🔗 查看消息", "url": url}]]
    return []


_listen_sent: set[tuple] = set()
_listen_msg: dict[tuple, int] = {}


def _listen_key(owner_id: int, item: dict) -> tuple:
    try:
        chat_id = int(item.get("chat_id") or 0)
        msg_id = int(item.get("msg_id") or 0)
    except (TypeError, ValueError):
        chat_id = msg_id = 0
    return owner_id, chat_id, msg_id, str(item.get("phase") or "open")


async def _send_listen(owner_id: int, item: dict, token: str) -> None:
    key = _listen_key(owner_id, item)
    if key in _listen_sent:
        return
    _listen_sent.add(key)
    if len(_listen_sent) > 4000:
        _listen_sent.clear()
        _listen_sent.add(key)
    payload = {
        "chat_id": owner_id,
        "text": listen_text(item),
        "disable_web_page_preview": True,
    }
    buttons = listen_buttons(item)
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    thread = (owner_id, key[1], key[2])
    if item.get("phase") == "unlock":
        reply = _listen_msg.pop(thread, 0)
        if reply:
            payload["reply_to_message_id"] = reply
    result, error = await _call(token, "sendMessage", payload)
    if item.get("phase") == "locked" and isinstance(result, dict):
        try:
            sent = int(result.get("message_id") or 0)
        except (TypeError, ValueError):
            sent = 0
        if sent:
            _listen_msg[thread] = sent
            if len(_listen_msg) > 2000:
                _listen_msg.clear()
                _listen_msg[thread] = sent
    if error in ("", _QUIET, "Telegram：发送过于频繁，请稍后再试"):
        return
    log.info("监听没发出 owner=%s", owner_id)


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
    if item.get("listen"):
        await _send_listen(owner_id, item, row["token"])
        return
    text = _watch_text(item) if item.get("watch") else _grab_text(item)
    payload = {
        "chat_id": owner_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if not item.get("watch"):
        buttons = grab_buttons(item)
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
    _, error = await _call(row["token"], "sendMessage", payload)
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
    got = _grab_text({"name": "甲", "amount": "1.00 USDT", "title": "群", "wallet": "fllqb", "elapsed_ms": 1234})
    assert got.startswith("🧧 领到 · 甲\n\n📍 来源: 群")
    assert "💰 钱包: @fllqb" in got and "💵 金额: 1.00 USDT" in got and "⏱ 1.2 秒" in got
    assert _grab_text({"elapsed_ms": 800}).endswith("⏱ 800 毫秒")
    assert "⏱ 2 秒" in _grab_text({"elapsed_ms": 2000})
    assert grab_buttons({"chat_id": -1001975935870, "msg_id": 9}) == [[
        {"text": "🔗 查看消息", "url": "https://t.me/c/1975935870/9"},
    ]]
    assert _watch_text({"name": "甲", "amount": "5 USDT", "title": "群", "kind": "口令红包"}) == "有红包\n甲\n5 USDT\n群\n口令红包"
    heard = {
        "phase": "locked",
        "title": "Cx大家庭",
        "wallet_title": "法拉利钱包",
        "wallet_user": "FLLQB",
        "amount": "300 USDT",
        "slots": "0/35",
        "web": True,
        "locked": True,
        "view_url": "https://t.me/c/4451398387/32719",
        "claim_url": "https://t.me/fllqb/wallet?startapp=abc",
    }
    text = listen_text(heard)
    assert text.startswith("🧧 红包监听\n\n📍 来源: Cx大家庭")
    assert "💰 钱包: 法拉利钱包 @FLLQB" in text
    assert "💵 金额: 300 USDT" in text and "📦 数量: 0/35" in text
    assert "🌐 网页验证红包" in text and "未解锁" in text
    assert listen_buttons(heard) == [[{"text": "🔗 查看消息", "url": heard["view_url"]}]]
    opened = dict(heard, phase="open", locked=False, web=False, expr="J + Q = ?", answer="JQ", limits=["Telegram 会员", "须加入 远航"])
    opened_text = listen_text(opened)
    assert "🧮 J + Q = ? → 答案: JQ" in opened_text
    assert "├ Telegram 会员" in opened_text and "└ 须加入 远航" in opened_text
    assert len(listen_buttons(opened)) == 2
    unlocked = listen_text({"phase": "unlock", "title": "用户名交易", "wallet_title": "法拉利钱包", "wallet_user": "FLLQB", "amount": "666 USDT", "slots": "111/111", "web": True, "lock_seconds": 15})
    assert unlocked.startswith("🔓 红包监听 · 已解锁 | 法拉利钱包 @FLLQB")
    assert "666 USDT · 📦 111/111 个" in unlocked and "群发包，需在群" in unlocked and "0 分 15 秒" in unlocked


_check()
