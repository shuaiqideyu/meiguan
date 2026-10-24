"""KKPoker 红包。"""

import asyncio
import base64
import json
import logging
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from meikai.grab.httpclient import http_client
from telethon.errors import FloodWaitError, PeerFloodError

from meikai.grab.browser import gateway_auth, join_channel
from meikai.grab.types import RedPacket, Result
from meikai.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_BOT = "kkpokerprobot"
_ORIGIN = "https://pro-gateway.kkpoker.com"
_DETAIL = "/event/api/v1/red-packets/detail"
_CLAIM = "/event/api/v1/red-packets/claim"
_PAGE = "https://pro.kkpoker.com"
_STATUS = {2: "红包已结束", 3: "红包已关闭", 4: "红包已过期"}
_CODES = {
    1000: "未登录或登录已失效",
    1002: "红包不存在",
    1003: "红包已结束",
    1004: "红包已过期",
    1005: "红包已领完",
    1006: "已达到领取上限",
    1007: "注册时间不符合领取条件",
    1008: "用户标签不符合领取条件",
    1009: "用户不在指定领取名单",
    1010: "牌局数据不符合领取条件",
    1011: "领取条件校验失败",
    1012: "红包已取消",
    1013: "操作过于频繁，请稍后再试",
    1014: "您被禁止领取，请联系客服",
    1099: "系统错误",
}
_RETRY = {1000, 1013, 1099}
_REFRESH = re.compile(r"share:refresh:\d+:(\d+)")
_RECORD = re.compile(r"record_(\d+)")


def packet_id(start: str | None) -> int:
    text = (start or "").strip()
    if not text:
        return 0
    piece = text.split("_", 1)[1] if "_" in text else text
    for sample in (piece, text):
        found = _decode(sample)
        if found:
            return found
    return 0


def is_kkpoker(rp: RedPacket) -> bool:
    bot = (rp.bot or "").lower().lstrip("@")
    return bot == _BOT and packet_id(rp.start) > 0


def start_for(packet: int) -> str:
    raw = json.dumps({"screen": "red_packet", "extra": str(packet)}, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return "64_" + token


def from_buttons(msg) -> RedPacket | None:
    from meikai.grab.detect import button_url, cb_data, raw_text, rows

    packet = 0
    mentioned = "kkpoker" in raw_text(msg).lower()
    for row in rows(msg):
        for btn in getattr(row, "buttons", None) or []:
            url = button_url(btn)
            low = url.lower()
            if _BOT in low:
                mentioned = True
            found = packet_id((parse_qs(urlparse(url).query).get("startapp") or [""])[0])
            if found:
                packet = found
            matched = _RECORD.search(url)
            if matched and _BOT in low:
                packet = int(matched.group(1))
            matched = _REFRESH.search(cb_data(btn))
            if matched:
                packet = int(matched.group(1))
                mentioned = True
    if not packet or not mentioned:
        return None
    return RedPacket("webapp", start=start_for(packet), bot=_BOT, short="poker", place="chat")


def _decode(sample: str) -> int:
    pad = "=" * (-len(sample) % 4)
    try:
        raw = base64.urlsafe_b64decode(sample + pad)
        data = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("screen") != "red_packet":
        return 0
    extra = str(data.get("extra") or "").strip()
    if not extra.isdigit():
        return 0
    return int(extra)


def with_platform(url: str) -> str:
    parsed = urlparse(url or "")
    frag = parsed.fragment or ""
    if "tgWebAppPlatform=" in frag:
        return url
    extra = "tgWebAppPlatform=android"
    frag = f"{frag}&{extra}" if frag else extra
    return urlunparse(parsed._replace(fragment=frag))


def headers(token: str) -> dict[str, str]:
    return {
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": _PAGE,
        "Referer": _PAGE + "/",
    }


def code_of(payload: dict) -> int | None:
    raw = payload.get("code")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def reason_of(payload: dict) -> str:
    code = code_of(payload)
    if code in _CODES:
        return _CODES[code]
    text = str(payload.get("message") or payload.get("msg") or "").strip()
    return text or "领取失败"


def amount_text(data: dict) -> str:
    raw = data.get("amount")
    if raw is None or raw == "":
        return ""
    text = str(raw).strip().lstrip("+")
    if any(ch.isalpha() for ch in text):
        return text
    unit = str(data.get("currency") or data.get("coin") or "USDT").strip().upper()
    if not unit.isalpha():
        unit = "USDT"
    return f"{text} {unit}"


def outcome(payload: dict, claimed: bool = False) -> Result:
    code = code_of(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if code == 0 and claimed:
        shown = amount_text(data)
        return Result(True, shown, False, shown or "已领取")
    text = reason_of(payload)
    return Result(False, retryable=code in _RETRY, feedback=f"节点：{text}"[:80])


async def load(http, token: str, packet: int) -> dict:
    resp = await http.get(
        _ORIGIN + _DETAIL,
        headers=headers(token),
        params={"id": packet},
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


async def take(http, token: str, packet: int) -> dict:
    resp = await http.post(
        _ORIGIN + _CLAIM,
        headers=headers(token),
        json={"id": packet},
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


async def finish(http, token: str, packet: int) -> Result:
    loaded = await load(http, token, packet)
    code = code_of(loaded)
    if code not in (None, 0):
        return outcome(loaded)
    data = loaded.get("data") if isinstance(loaded.get("data"), dict) else {}
    try:
        status = int(data.get("status"))
    except (TypeError, ValueError):
        status = None
    if status in _STATUS:
        return Result(False, retryable=False, feedback=f"节点：{_STATUS[status]}")
    claimed = await take(http, token, packet)
    log.info("节点：KKPoker 领取 code=%s", code_of(claimed))
    return outcome(claimed, claimed=True)


async def claim_kkpoker(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    packet = packet_id(rp.start)
    if not packet:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot or _BOT)
            web = await app_url(client, chat, bot_peer, rp.short or "poker", rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：KKPoker 打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        if not web:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        token = await gateway_auth(with_platform(web), "kkpoker")
        if not token:
            log.info("节点：KKPoker 没有登录")
            return Result(False, retryable=True, feedback="节点：KKPoker 没有登录")
        log.info("节点：KKPoker 已登录")
        try:
            async with http_client(timeout=15) as http:
                return await finish(http, token, packet)
        except Exception as exc:
            log.info("节点：KKPoker 领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：KKPoker 没有回执")

plug("kkpoker", "KKPoker", "webapp", is_kkpoker, claim_kkpoker, 50, 90)
