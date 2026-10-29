"""红包分类。"""

import asyncio
import re
import unicodedata
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

from telethon.tl.types import MessageEntityCustomEmoji

from meiguan.charset import MATH_RE, solve_expr
from meiguan.grab.types import RedPacket, Result

try:
    from telethon.tl.types import (
        InlineButtonTypeCallback,
        InlineButtonTypeUrl,
        InlineButtonTypeWebView,
    )
except ImportError:  # Telethon 1.44 及更早仍是 KeyboardButton*
    InlineButtonTypeCallback = None
    InlineButtonTypeUrl = None
    InlineButtonTypeWebView = None
    from telethon.tl.types import (
        KeyboardButtonCallback,
        KeyboardButtonUrl,
        KeyboardButtonWebView,
    )
else:
    KeyboardButtonCallback = None
    KeyboardButtonUrl = None
    KeyboardButtonWebView = None

try:
    from telethon.tl.types import ButtonTypeSimpleWebView
except ImportError:
    ButtonTypeSimpleWebView = None

try:
    from telethon.tl.types import KeyboardButtonSimpleWebView
except ImportError:
    KeyboardButtonSimpleWebView = None

_KEYWORDS = ("发送了一个红包", "手气红包", "拼手气红包", "口令红包", "💵总金额", "抽奖红包", "能量红包")


_DIRECT = ("领取", "会员红包")


_LOCK_MARKS = ("unlock_lucky", "unlock_redpacket", "locked_redpacket", "mhblock")


_WEBAPP_BOTS = {"okpay", "kkpay"}


_GENERIC_START = {"home", "start", "help", "menu", "settings", "about", "wallet"}


_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff\u2060"


_EXHAUSTED = re.compile(r"剩余[:：]\s*0\s*/")


_AMOUNT = re.compile(r"[🎉🧧].*?([\d]+\.?\d*)\s*([A-Za-z\u4e00-\u9fff]+)")


_TOTAL = re.compile(r"总金额[:：]?\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)?")


_YUAN = re.compile(r"总额\s*[¥￥]\s*([\d,]+(?:\.\d+)?)")


_SUCCESS = ("领取了", "領取了", "成功", "领取红包", "領取紅包", "抢到", "搶到", "已参与")


_EMPTY = (
    "已被领完", "已被領完", "手慢", "已领完", "已過期", "已过期",
    "被抢完", "已抢完", "已领取过", "已領取過",
)


_BLOCKED = (
    "次数不足", "發言次數不足", "发言不足", "条件不满足", "條件不滿足",
    "不满足", "不滿足", "无权限", "無權限", "被禁言", "被限制", "已禁言",
)


_WRONG = (
    "验证码错误", "驗證碼錯誤", "答案错误", "答案錯誤", "回答错误",
    "回答錯誤", "答错", "選擇錯誤", "选择错误",
)


def clean(text: str) -> str:
    out = []
    for ch in text or "":
        if ch in _ZERO_WIDTH:
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("C") or cat == "So" or ch == " ":
            continue
        out.append(ch)
    return "".join(out)


def rows(msg):
    return getattr(getattr(msg, "reply_markup", None), "rows", None) or []


def raw_text(msg) -> str:
    return getattr(msg, "raw_text", None) or getattr(msg, "message", None) or getattr(msg, "text", None) or ""


def is_red_packet(text: str) -> bool:
    body = text or ""
    if _EXHAUSTED.search(body) and "开奖" not in body and "抽奖" not in body:
        return False
    if any(word in body for word in _KEYWORDS):
        return True
    return MATH_RE.search(body) is not None


def callback_bytes(btn) -> bytes | None:
    kind = getattr(btn, "type", None)
    if InlineButtonTypeCallback is not None and isinstance(kind, InlineButtonTypeCallback):
        data = kind.data
    elif KeyboardButtonCallback is not None and isinstance(btn, KeyboardButtonCallback):
        data = btn.data
    else:
        return None
    if isinstance(data, bytearray):
        return bytes(data)
    return data if isinstance(data, bytes) and data else None


def button_url(btn) -> str:
    kind = getattr(btn, "type", None)
    if InlineButtonTypeUrl is not None and isinstance(kind, (InlineButtonTypeUrl, InlineButtonTypeWebView)):
        return kind.url or ""
    if ButtonTypeSimpleWebView is not None and isinstance(kind, ButtonTypeSimpleWebView):
        return kind.url or ""
    if KeyboardButtonUrl is not None and isinstance(btn, (KeyboardButtonUrl, KeyboardButtonWebView)):
        return btn.url or ""
    if KeyboardButtonSimpleWebView is not None and isinstance(btn, KeyboardButtonSimpleWebView):
        return btn.url or ""
    return ""


def is_simple_webview(btn) -> bool:
    if KeyboardButtonSimpleWebView is not None and isinstance(btn, KeyboardButtonSimpleWebView):
        return True
    kind = getattr(btn, "type", None)
    return ButtonTypeSimpleWebView is not None and isinstance(kind, ButtonTypeSimpleWebView)


def is_webview(btn) -> bool:
    if is_simple_webview(btn):
        return False
    kind = getattr(btn, "type", None)
    if InlineButtonTypeWebView is not None and isinstance(kind, InlineButtonTypeWebView):
        return True
    return KeyboardButtonWebView is not None and isinstance(btn, KeyboardButtonWebView)


def cb_data(btn) -> str:
    data = callback_bytes(btn)
    if not data:
        return ""
    try:
        return data.decode("utf-8", "ignore").lower()
    except Exception:
        return ""


def is_countdown(msg) -> bool:
    text = raw_text(msg)
    if "后可领取" in text or "后可领" in text:
        return True
    for row in rows(msg):
        for btn in row.buttons:
            if "countdown" in cb_data(btn) or "后可领" in clean(btn.text):
                return True
    return False


def detect_state(msg) -> str:
    if _EXHAUSTED.search(raw_text(msg)):
        return "exhausted"
    if is_countdown(msg):
        return "locked"
    for row in rows(msg):
        for btn in row.buttons:
            label = clean(btn.text)
            if "解锁" in label or "已锁定" in label:
                return "locked"
            if callback_bytes(btn) is None:
                continue
            data = cb_data(btn)
            if data and any(mark in data for mark in _LOCK_MARKS):
                return "locked"
    return "claimable"


def _account_username(user) -> str:
    if user is None:
        return ""
    from meiguan.policy import wallet_key
    from meiguan.reports import account_names

    for name in account_names(user):
        key = wallet_key(name)
        if key:
            return key
    return ""


_LINK_SKIP = {"c", "s", "share", "joinchat", "addstickers", "addtheme", "iv", "proxy"}


def bot_username(msg) -> str:
    name = _account_username(getattr(msg, "via_bot", None))
    if name:
        return name
    sender = getattr(msg, "sender", None)
    if getattr(sender, "bot", False):
        name = _account_username(sender)
        if name:
            return name
    return ""


def _link_bot_name(msg) -> str:
    for row in rows(msg):
        for btn in row.buttons:
            url = button_url(btn)
            host = urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            if host not in ("t.me", "telegram.me"):
                continue
            bot = link_bot(url)
            if bot in _LINK_SKIP:
                continue
            from meiguan.policy import wallet_key

            name = wallet_key(bot)
            if name:
                return name
    return ""


def wallet_of(msg) -> str:
    found = bot_username(msg)
    if found:
        return found
    return _link_bot_name(msg)


async def resolve_wallet(client, msg) -> str:
    found = bot_username(msg)
    if found:
        return found
    via_id = getattr(msg, "via_bot_id", None)
    if via_id:
        name = await _lookup_bot(client, int(via_id), msg)
        if name:
            return name
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    if sender_id and getattr(sender, "bot", False):
        name = await _lookup_bot(client, int(sender_id), msg)
        if name:
            return name
    return wallet_of(msg)


async def _lookup_bot(client, user_id: int, msg=None) -> str:
    entity = None
    try:
        entity = await asyncio.wait_for(client.get_entity(user_id), timeout=3)
    except Exception:
        entity = None
    if entity is None and msg is not None:
        entity, _err = await _user_from_message(client, msg, user_id)
    if not getattr(entity, "bot", False):
        return ""
    return _account_username(entity)


async def _user_from_message(client, msg, user_id: int):
    from telethon.tl.functions.users import GetUsersRequest
    from telethon.tl.types import InputUserFromMessage

    try:
        peer = await msg.get_input_chat()
    except Exception as exc:
        return None, type(exc).__name__
    try:
        users = await asyncio.wait_for(
            client(GetUsersRequest([InputUserFromMessage(peer, int(msg.id), int(user_id))])),
            timeout=3,
        )
    except Exception as exc:
        return None, type(exc).__name__
    if not users:
        return None, "empty"
    return users[0], type(users[0]).__name__


def find_join(msg) -> bytes | None:
    for row in rows(msg):
        for btn in row.buttons:
            if "参与" not in clean(btn.text):
                continue
            data = callback_bytes(btn)
            if data:
                return data
    return None


def find_direct(msg) -> bytes | None:
    for row in rows(msg):
        for btn in row.buttons:
            if callback_bytes(btn) is None:
                continue
            label = clean(btn.text)
            if "解锁" in label or "已锁定" in label or "口令" in label:
                continue
            data = cb_data(btn)
            if data and (data.startswith(("mhblock", "unlock", "locked_")) or any(mark in data for mark in _LOCK_MARKS)):
                continue
            if data.startswith("mission_hb_get_") or any(word in label for word in _DIRECT):
                return callback_bytes(btn)
    return None


def find_transfer(msg) -> bytes | None:
    for row in rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            if "收款" not in clean(btn.text):
                continue
            data = callback_bytes(btn)
            if data and data.lower().startswith(b"transferil--"):
                return data
    return None


def is_coupon(msg) -> bool:
    """X钱包券包（「抢券」按钮或 startapp 带 routertoshareCoupon）。"""
    for row in rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            if "抢券" in clean(btn.text) or "routertoshareCoupon" in button_url(btn):
                return True
    return False


_PHRASE = re.compile(r"口令[:：]\s*([^\n]+)")


_CODE_PROMPT = re.compile(r"验证码[:：]\s*(.+)")


_CODE_CHOICE = re.compile(r"验证码\s*([A-Za-z0-9]+)")


_FOLLOW = re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,})")


def phrase_of(text: str) -> str | None:
    matched = _PHRASE.search(text or "")
    if not matched:
        return None
    return matched.group(1).strip("，。！!,. ")


def find_grab(msg) -> bytes | None:
    for row in rows(msg):
        for btn in row.buttons:
            data = callback_bytes(btn)
            if data and "抢红包" in clean(btn.text):
                return data
    return None


def prompt_answer(text: str) -> str | None:
    matched = _CODE_PROMPT.search(text or "")
    if not matched:
        return None
    token = matched.group(1).strip().splitlines()[0].strip().strip("。！!，,")
    if not token:
        return None
    if MATH_RE.search(token):
        return solve_expr(token)
    return token


def follow_names(text: str) -> list[str]:
    found = []
    for line in (text or "").splitlines():
        if "关注" not in line:
            continue
        found.extend(_FOLLOW.findall(line))
    return list(dict.fromkeys(found))


def shown_label(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch not in _ZERO_WIDTH).strip()


def choice_data(msg, code: str) -> bytes | None:
    for row in rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            if shown_label(getattr(btn, "text", "")) != code:
                continue
            data = callback_bytes(btn)
            if data:
                return data
    return None


def code_choice(msg, text: str) -> tuple[str, list] | None:
    matched = _CODE_CHOICE.search(text or "")
    if not matched:
        return None
    code = matched.group(1)
    if choice_data(msg, code) is None:
        return None
    buttons = []
    for row in rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            data = callback_bytes(btn)
            if not data:
                continue
            label = shown_label(getattr(btn, "text", ""))
            if label:
                buttons.append((label, data))
    return code, buttons


def quiz_buttons(msg) -> list[tuple[str, bytes]]:
    found = []
    for row in rows(msg):
        for btn in row.buttons:
            data = callback_bytes(btn)
            if data is None:
                continue
            label = clean(btn.text).strip()
            if label and "领取" not in label:
                found.append((label, data))
    return found


def find_captcha(msg, text: str | None = None):
    body = text if text is not None else raw_text(msg)
    matched = MATH_RE.search(body)
    if not matched:
        return None
    buttons = quiz_buttons(msg)
    if not buttons:
        return None
    return matched.group(0), buttons


def link_bot(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path.strip("/")
    if not path:
        return ""
    return path.split("/")[0].lower()


def expr_line(text: str) -> str | None:
    for line in reversed((text or "").split("\n")):
        if MATH_RE.search(line):
            return line.strip()
    return None


def emoji_in_expr(msg) -> bool:
    body = raw_text(msg)
    line = expr_line(body)
    if not line:
        return False
    start = body.rfind(line)
    if start < 0:
        return False
    table = []
    pos = 0
    for ch in body:
        table.append(pos)
        pos += 2 if ord(ch) > 0xFFFF else 1
    end = start + len(line)
    start_u = table[start] if start < len(table) else pos
    end_u = table[end] if end < len(table) else pos
    for entity in getattr(msg, "entities", None) or []:
        if isinstance(entity, MessageEntityCustomEmoji) and start_u <= entity.offset < end_u:
            return True
    return False


def member_only(text: str) -> bool:
    return "Premium" in (text or "") or "限TG会员" in (text or "") or ("仅限" in (text or "") and "会员" in (text or ""))


def button_bot(msg) -> str:
    name = wallet_of(msg)
    if name:
        return name
    via = getattr(msg, "via_bot_id", None)
    if via:
        return str(int(via))
    sender = getattr(getattr(msg, "from_id", None), "user_id", None)
    if sender:
        return str(int(sender))
    return ""


def is_web_start(bot: str, param: str) -> bool:
    name = (bot or "").lower()
    token = (param or "").lower()
    if token.startswith("vweb--") or name in _WEBAPP_BOTS:
        return True
    return name == "kpay" and token.startswith("web_")


def packet_from_url(url: str, text: str = "") -> RedPacket | None:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("t.me", "telegram.me"):
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    query = parse_qs(parsed.query)
    startapp = (query.get("startapp") or [None])[0]
    start = (query.get("start") or [None])[0]
    follows = " ".join(follow_names(text))
    if startapp and parts:
        return RedPacket(
            "webapp",
            start=startapp,
            bot=parts[0],
            short=parts[1] if len(parts) >= 2 else None,
            premium_only=member_only(text),
            follows=follows,
            place="chat",
        )
    if not start or start.lower() in _GENERIC_START or not parts:
        return None
    bot = parts[-1]
    if is_web_start(bot, start):
        return RedPacket("webapp", start=start, bot=bot, place="dm")
    return RedPacket("captcha", start=start, bot=bot, place="dm")


def skipped_label(btn) -> bool:
    label = clean(btn.text)
    return "列表" in label or "记录" in label


def register_of(msg) -> tuple[str, str] | None:
    """识别「一键注册」类按钮，返回 (bot, start)。"""
    for row in rows(msg):
        for btn in row.buttons:
            label = clean(btn.text)
            if "注册" not in label:
                continue
            url = button_url(btn)
            parsed = urlparse(url or "")
            host = (parsed.netloc or "").lower()
            if host not in ("t.me", "telegram.me"):
                continue
            parts = [p for p in parsed.path.strip("/").split("/") if p]
            if not parts:
                continue
            bot = parts[-1].lstrip("@")
            start = (parse_qs(parsed.query).get("start") or [None])[0]
            return bot, start or ""
    return None


def extract_app(msg) -> RedPacket | None:
    text = raw_text(msg)
    for row in rows(msg):
        for btn in row.buttons:
            if is_simple_webview(btn) or skipped_label(btn):
                continue
            packet = packet_from_url(button_url(btn), text)
            if packet and packet.place == "chat" and (packet.short or packet.start):
                reg = register_of(msg)
                if reg:
                    packet.register_bot, packet.register_start = reg
                return packet
    return None


def chat_page(msg) -> tuple[str, str] | None:
    for row in rows(msg):
        for btn in row.buttons:
            if not is_webview(btn) or skipped_label(btn):
                continue
            url = button_url(btn)
            if not url or "startapp=" in url.lower():
                continue
            return button_bot(msg), url
    return None


def simple_page(msg) -> str:
    for row in rows(msg):
        for btn in row.buttons:
            if not is_simple_webview(btn) or skipped_label(btn):
                continue
            url = button_url(btn)
            if url:
                return url
    return ""


def extract_start(msg) -> RedPacket | None:
    text = raw_text(msg)
    for row in rows(msg):
        for btn in row.buttons:
            if is_webview(btn) or is_simple_webview(btn):
                continue
            packet = packet_from_url(button_url(btn), text)
            if packet and not packet.short:
                return packet
    return None


def is_yypay(msg) -> bool:
    if "yypay" in raw_text(msg).lower():
        return True
    for row in rows(msg):
        for btn in row.buttons:
            if "yypay" in button_url(btn).lower():
                return True
    return False


def is_kpay_quiz(msg) -> bool:
    for row in rows(msg):
        for btn in row.buttons:
            if cb_data(btn).startswith("envelope_global_human_verify"):
                return True
    return False


def yypay_packet(msg, text: str) -> RedPacket | None:
    app = extract_app(msg)
    if not app:
        return None
    app.kind = "cloudflare"
    app.premium_only = member_only(text)
    app.follows = " ".join(follow_names(text))
    app.place = "chat"
    return app


def startapp_link(msg) -> bool:
    for row in rows(msg):
        for btn in row.buttons:
            url = button_url(btn).lower()
            if "startapp=" in url and ("t.me/" in url or "telegram.me/" in url):
                return True
    return False


def accept_packet(msg, text: str | None = None) -> bool:
    body = raw_text(msg) if text is None else text
    if is_red_packet(body):
        return True
    if is_coupon(msg):
        return True
    if "红包" not in (body or ""):
        return find_transfer(msg) is not None or find_direct(msg) is not None
    if startapp_link(msg):
        return True
    from meiguan.grab.packets.kkpoker import from_buttons

    return from_buttons(msg) is not None


def _classify(msg, decoded: str | None = None) -> RedPacket | None:
    text = decoded if decoded is not None else raw_text(msg)
    if not getattr(msg, "reply_markup", None):
        return None
    if not accept_packet(msg, text):
        return None
    from meiguan.grab.packets.kkpoker import from_buttons

    found = from_buttons(msg)
    if found:
        return found
    if is_kpay_quiz(msg):
        return None
    if "抽奖" in text or "开奖" in text:
        if any(word in text for word in ("已结束", "已开奖", "已取消")):
            return None
        data = find_join(msg)
        if data:
            return RedPacket("direct", data=data)
    state = detect_state(msg)
    if state == "exhausted":
        return None
    if state == "locked":
        return RedPacket("locked")
    if is_yypay(msg):
        return yypay_packet(msg, text)
    phrase = phrase_of(text)
    if phrase:
        return RedPacket("phrase", start=phrase)
    grab = find_grab(msg)
    if grab:
        return RedPacket("phrase", data=grab)
    data = find_transfer(msg)
    if data:
        return RedPacket("direct", data=data)
    data = find_direct(msg)
    if data:
        packet = RedPacket("direct", data=data)
        if wallet_of(msg) == "kpay":
            packet.bot = "kpay"
        return packet
    chosen = code_choice(msg, text)
    if chosen:
        code, buttons = chosen
        return RedPacket("captcha", expr=code, buttons=buttons)
    quiz = find_captcha(msg, text)
    if quiz:
        expr, buttons = quiz
        if solve_expr(expr) is not None:
            return RedPacket("captcha", expr=expr, buttons=buttons)
        if decoded is None and emoji_in_expr(msg):
            return RedPacket("captcha", buttons=buttons, needs_emoji=True)
        return RedPacket("captcha", expr=expr, buttons=buttons)
    app = extract_app(msg)
    if app:
        return app
    page = chat_page(msg)
    if page:
        bot, url = page
        return RedPacket(
            "webapp",
            place="chat",
            page=url,
            bot=bot,
            premium_only=member_only(text),
            follows=" ".join(follow_names(text)),
        )
    simple = simple_page(msg)
    if simple:
        linked = packet_from_url(simple, text)
        return RedPacket(
            "webapp",
            place="dm",
            page=simple,
            bot=button_bot(msg) or (linked.bot if linked else ""),
            start=linked.start if linked else None,
            short=linked.short if linked else None,
            premium_only=member_only(text),
            follows=" ".join(follow_names(text)),
        )
    return extract_start(msg)


def classify(msg, decoded: str | None = None) -> RedPacket | None:
    packet = _classify(msg, decoded)
    if packet is None or packet.kind == "locked":
        return packet
    from meiguan.grab.catalog import apply_kind

    return apply_kind(packet)


_PLAIN_TOTAL = re.compile(r"(\d+(?:\.\d+)?)\s*(CNY|USDT|TRX)\b")


_GAVE = re.compile(r"发了个\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)")

_TRANSFER = re.compile(r"转账给你\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)")


_PACK_TOTAL = re.compile(r"总额[:：]\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)?")


_GAVE_MARK = re.compile(r"(?:发送了一个红包|发了个红包|送出了一个红包|发了个|发出一个红包)")


def _total_after_gave(text: str) -> str:
    """找「发送了一个红包」标记之后的「总金额」，避免拿到上一条残留金额。"""
    marks = list(_GAVE_MARK.finditer(text or ""))
    if not marks:
        return ""
    tail = text[marks[-1].start():]
    matched = _TOTAL.search(tail)
    if not matched:
        return ""
    number = matched.group(1).replace(",", "")
    unit = (matched.group(2) or "").upper()
    if unit == "U":
        unit = "USDT"
    return f"{number} {unit}".strip()


def packet_amount(msg) -> str:
    text = raw_text(msg)
    matched = _TRANSFER.search(text)
    if matched:
        number = matched.group(1).replace(",", "")
        unit = matched.group(2).upper()
        if unit == "U":
            unit = "USDT"
        return f"{number} {unit}"
    gave_total = _total_after_gave(text)
    if gave_total:
        return gave_total
    matched = _TOTAL.search(text)
    if matched:
        number, unit = matched.group(1), matched.group(2) or ""
        return f"{number} {unit}".strip()
    matched = _YUAN.search(text)
    if matched:
        return f"{matched.group(1).replace(',', '')} CNY"
    matched = _PACK_TOTAL.search(text)
    if matched:
        number = matched.group(1).replace(",", "")
        unit = (matched.group(2) or "").upper()
        if unit == "U":
            unit = "USDT"
        return f"{number} {unit}".strip()
    matched = _GAVE.search(text)
    if matched:
        number = matched.group(1).replace(",", "")
        unit = matched.group(2).upper()
        if unit == "U":
            unit = "USDT"
        return f"{number} {unit}"
    matched = _PLAIN_TOTAL.search(text)
    if not matched:
        return ""
    return f"{matched.group(1)} {matched.group(2)}"


_SLOTS = re.compile(
    r"(?:剩余(?:红包)?(?:个数|数量|數量)?|剩餘|个数|数量|數量)[:：\s]*(\d+)\s*/\s*(\d+)"
)


def packet_slots(text: str) -> str:
    matched = _SLOTS.search(text or "")
    if not matched:
        matched = re.search(r"(\d+)\s*/\s*(\d+)\s*张", text or "")
    if not matched:
        return ""
    return f"{matched.group(1)}/{matched.group(2)}"


def view_link(chat_id: int, msg_id: int, username: str) -> str:
    name = str(username or "").strip().lstrip("@")
    if name and "/" not in name and " " not in name:
        return f"https://t.me/{name}/{int(msg_id)}"
    number = str(int(chat_id or 0))
    if number.startswith("-100"):
        return f"https://t.me/c/{number[4:]}/{int(msg_id)}"
    return ""


def claim_link(msg) -> str:
    for row in rows(msg):
        for btn in row.buttons:
            url = button_url(btn).strip()
            low = url.lower()
            if "startapp=" not in low and "start=" not in low:
                continue
            if "t.me/" not in low and "telegram.me/" not in low:
                continue
            if low.startswith("http://"):
                url = "https://" + url[7:]
            if url.lower().startswith("https://"):
                return url[:500]
    return ""


_LOCK_WAIT = re.compile(r"(?:(\d+)\s*分(?:钟)?)?\s*(\d+)\s*秒")


def lock_wait(text: str) -> int:
    body = text or ""
    matched = _LOCK_WAIT.search(body)
    if matched is None or "后可领" not in body and "解锁" not in body:
        return 0
    minutes = int(matched.group(1) or 0)
    seconds = int(matched.group(2) or 0)
    return minutes * 60 + seconds


def _clip_line(text: str, limit: int = 28) -> str:
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return line
    return line[: limit - 1] + "..."


def limit_lines(text: str) -> list[str]:
    found = []
    if member_only(text):
        found.append("Telegram 会员")
    for name in follow_names(text):
        line = _clip_line(f"须加入 {name}")
        if line not in found:
            found.append(line)
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("http"):
            continue
        if not any(word in line for word in ("仅限", "须加入", "今日发言", "所需今日发言", "发言:", "发言：")):
            continue
        line = _clip_line(line)
        if line not in found:
            found.append(line)
        if len(found) >= 4:
            break
    return found[:4]


def wallet_face(msg) -> tuple[str, str]:
    entities = []
    sender = getattr(msg, "sender", None)
    if getattr(sender, "bot", False):
        entities.append(sender)
    via = getattr(msg, "via_bot", None)
    if via is not None:
        entities.append(via)
    for entity in entities:
        user = str(getattr(entity, "username", None) or "").strip().lstrip("@")
        title = " ".join(
            part for part in (
                str(getattr(entity, "first_name", None) or "").strip(),
                str(getattr(entity, "last_name", None) or "").strip(),
            ) if part
        )
        if user or title:
            return title[:40], user[:32]
    return "", wallet_of(msg)[:32]


def _coupon_amount(text: str) -> str:
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if "券" in line and "红包" not in line:
            return line[:60]
    return "能量券"


def _web_packet(msg, rp) -> bool:
    if rp is not None and getattr(rp, "kind", "") in ("webapp", "cloudflare"):
        return True
    for row in rows(msg):
        for btn in row.buttons:
            if is_webview(btn) or "startapp=" in button_url(btn).lower():
                return True
    return False


def listen_facts(msg, rp, *, chat_id: int, msg_id: int, title: str, username: str) -> dict:
    body = raw_text(msg)
    wallet_title, wallet_user = wallet_face(msg)
    coupon = is_coupon(msg)
    amount = packet_amount(msg)
    if coupon and not amount:
        amount = _coupon_amount(body)
    slots = packet_slots(body)
    locked = rp is not None and getattr(rp, "kind", "") == "locked"
    expr = ""
    answer = ""
    phrase = ""
    if rp is not None and getattr(rp, "kind", "") == "captcha":
        expr = " ".join(str(getattr(rp, "expr", "") or "").split())[:80]
        if expr:
            answer = str(solve_expr(expr) or "")[:40]
    if rp is not None and getattr(rp, "kind", "") == "phrase":
        phrase = " ".join(str(getattr(rp, "start", "") or "").split())[:40]
    return {
        "title": " ".join(str(title or "").split())[:80],
        "wallet_title": wallet_title,
        "wallet_user": wallet_user,
        "amount": " ".join(str(amount or "").split())[:60],
        "slots": slots[:20],
        "coupon": coupon,
        "web": _web_packet(msg, rp),
        "expr": expr,
        "answer": answer,
        "phrase": phrase,
        "locked": locked,
        "limits": [] if locked else limit_lines(body),
        "view_url": view_link(chat_id, msg_id, username),
        "claim_url": claim_link(msg),
    }


async def claim_slots(client, chat_id: int, msg_id: int) -> str:
    try:
        current = await asyncio.wait_for(client.get_messages(chat_id, ids=msg_id), timeout=2)
    except Exception:
        return ""
    if current is None:
        return ""
    return packet_slots(raw_text(current))


_COINS = "USDT|USD|CNY|TRX|TON|GRAM|KKCOIN|KK币|WLCOIN|WL币|FTCOIN|JIBA|AIDU|RMB|U"
_TAKEN = re.compile(
    rf"(?:你已领取了|你已经领取了|您已领取了|你领取了|已领取了|领取了|領取了|"
    rf"你已领取(?!过)|您已领取(?!过)|已领取(?!过)|"
    rf"抢到了|搶到了|抢到|搶到|"
    rf"恭喜您获得了|恭喜您获得红包|恭喜您获得|恭喜获得|获得了|獲得了|"
    rf"已到账|已存入|手气最佳|(?<![值总總])金额\s*[:：])"
    rf"\s*([\d,]+(?:\.\d+)?)(?:\s*({_COINS}))?",
    re.I,
)
_BARE = re.compile(rf"^\s*([\d,]+(?:\.\d+)?)(?:\s*({_COINS}))?\s*$", re.I)
_COIN_FIND = re.compile(rf"\b({_COINS})\b", re.I)


def _coin(text: str) -> str:
    matched = _COIN_FIND.search(text or "")
    if not matched:
        return ""
    unit = matched.group(1).upper()
    if unit == "U":
        return "USDT"
    if unit == "RMB":
        return "CNY"
    if unit == "KK币":
        return "KKCOIN"
    if unit == "WL币":
        return "WLCOIN"
    if unit == "GRAM":
        return "TON"
    if unit == "USD":
        return "USDT"
    return unit


def _fmt_amount(number: str, unit: str) -> str:
    try:
        value = Decimal(str(number).replace(",", "").strip())
        if not value.is_finite():
            return ""
        shown = format(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")
    except (InvalidOperation, ValueError):
        return ""
    unit = (unit or "").upper()
    if unit == "U":
        unit = "USDT"
    if unit == "RMB":
        unit = "CNY"
    if unit == "KK币":
        unit = "KKCOIN"
    if unit == "WL币":
        unit = "WLCOIN"
    if unit == "GRAM":
        unit = "TON"
    if unit == "USD":
        unit = "USDT"
    return f"{shown} {unit}".strip() if unit else shown


def taken_amount(text: str, hint: str = "") -> str:
    raw = text or ""
    for matched in _TAKEN.finditer(raw):
        if re.match(r"\s*/", raw[matched.end():]):
            continue
        return _fmt_amount(matched.group(1), matched.group(2) or _coin(hint))
    return ""


def claim_taken(got: str, feedback: str, shown: str) -> str:
    found = taken_amount(feedback, got or shown) or taken_amount(got, shown)
    if found:
        return found
    bare = _BARE.match(got or "")
    if not bare:
        return ""
    try:
        number = float(bare.group(1).replace(",", ""))
    except ValueError:
        return ""
    total = re.search(r"([\d,]+(?:\.\d+)?)", shown or "")
    if total is not None:
        try:
            if abs(number - float(total.group(1).replace(",", ""))) < 1e-6:
                return ""
        except ValueError:
            pass
    return _fmt_amount(bare.group(1), bare.group(2) or _coin(shown))


def extract_amount(text: str) -> str | None:
    matched = _AMOUNT.search(text or "")
    if not matched:
        return None
    return f"{matched.group(1)} {matched.group(2)}"


def grabbed_amount(text: str) -> str | None:
    found = taken_amount(text)
    return found or None


def judge(text: str) -> Result:
    amount = grabbed_amount(text)
    if any(word in text for word in _EMPTY):
        return Result(False, amount, False, text)
    if any(word in text for word in _WRONG):
        return Result(False, amount, True, text, wrong=True)
    if any(word in text for word in _SUCCESS):
        return Result(True, amount, False, text)
    if "专属" in text or "专享" in text:
        return Result(False, None, False, text)
    if any(word in text for word in _BLOCKED) or "Premium" in text or "会员" in text:
        return Result(False, amount, False, text, blocked=True)
    if amount:
        return Result(True, amount, False, text)
    if text:
        return Result(False, None, False, text)
    return Result(False, retryable=True)


assert claim_taken("", "领取红包成功，金额: 8.00", "800.00 USDT") == "8.00 USDT"
assert claim_taken("", "你领取了 24.141288 USDT", "88.80 USDT") == "24.14 USDT"
assert claim_taken("", "恭喜您获得了 0.00", "3888.00 USDT") == "0.00 USDT"
assert claim_taken("", "🧧 恭喜抢到 0.06 USDT", "800 USDT") == "0.06 USDT"
assert claim_taken("", "🎉 恭喜！抢到了 21.28 CNY", "") == "21.28 CNY"
assert claim_taken("", "🎉 你已领取了0.02 USDT", "") == "0.02 USDT"
assert claim_taken("", "🧧 你已领取 996.35 USDT", "") == "996.35 USDT"
assert claim_taken("", "总金额 800.00 USDT", "800 USDT") == ""
assert claim_taken("", "已领取 0.00 / 8888.00 USDT", "8888 USDT") == ""
assert claim_taken("", "安全验证 请完成人机验证后领取红包", "2888.00 USDT") == ""
assert claim_taken("8.00", "", "800 USDT") == "8.00 USDT"
assert claim_taken("800 USDT", "", "800.00 USDT") == ""
assert claim_taken("1.2 RMB", "已领取", "") == "1.20 CNY"
assert claim_taken("", "恭喜您获得红包 2.5 USDT", "800 USDT") == "2.50 USDT"
assert claim_taken("", "已到账 12.34 USDT", "800 USDT") == "12.34 USDT"
assert claim_taken("", "手气最佳 1.20 CNY", "") == "1.20 CNY"
assert claim_taken("", "🎉 恭喜！您获得了 1270.18 KK币", "") == "1270.18 KKCOIN"
assert claim_taken("", "限制今日充值金额：500.00，实际充值金额：0：未达标，不能领取红包", "") == ""


class _Name:
    def __init__(self, username, bot=True):
        self.username = username
        self.bot = bot


class _Bare:
    def __init__(self, via=None, sender=None):
        self.via_bot = via
        self.sender = sender
        self.via_bot_id = None
        self.from_id = None
        self.reply_markup = None


assert view_link(-1001975935870, 2431568, "") == "https://t.me/c/1975935870/2431568"
assert view_link(-1001, 9, "room") == "https://t.me/room/9"
assert lock_wait("15秒后可领取") == 15
assert lock_wait("1分20秒后可领") == 80
assert lock_wait("普通 15秒") == 0
assert limit_lines("仅限群组成员领取 【成交老板交流群】\n所需今日发言: 5")[0].startswith("仅限")
assert "Telegram 会员" in limit_lines("仅限 Premium 会员")


class _ListenMsg:
    def __init__(self):
        self.message = "发送了一个红包\n总金额: 300 USDT\n剩余: 0/35\n15秒后可领取"
        self.sender = _Name("FLLQB")
        self.sender.first_name = "法拉利钱包"
        self.via_bot = None
        self.reply_markup = None


_listen = listen_facts(_ListenMsg(), RedPacket("locked"), chat_id=-1004451398387, msg_id=32719, title="Cx大家庭", username="")
assert _listen["wallet_title"] == "法拉利钱包" and _listen["wallet_user"] == "FLLQB"
assert _listen["amount"] == "300 USDT" and _listen["slots"] == "0/35" and _listen["locked"]
assert _listen["view_url"] == "https://t.me/c/4451398387/32719" and _listen["claim_url"] == ""
assert wallet_of(_Bare(via=_Name("HX20"))) == "hx20"
assert wallet_of(_Bare(sender=_Name("fllqb"))) == "fllqb"
assert wallet_of(_Bare(sender=_Name("alice", bot=False))) == ""


def exclusive_miss(result: Result) -> bool:
    if result.ok:
        return False
    text = result.feedback or ""
    return "专属" in text or "专享" in text
