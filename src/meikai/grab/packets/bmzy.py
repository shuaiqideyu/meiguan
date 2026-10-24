"""白马庄园彩金红包。"""

import asyncio
import logging
import re
from decimal import ROUND_DOWN, Decimal
from urllib.parse import parse_qs, urlparse

from meikai.grab.httpclient import http_client
from telethon.errors import FloodWaitError, PeerFloodError

from meikai.grab.browser import channel_in, join_channel
from meikai.grab.types import RedPacket, Result
from meikai.grab.vision import vision_settings, warn_once
from meikai.grab.webapp import account_premium, bot_peers, main_url, message_peer
from meikai.grab.packets.yypay import yy_init
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_ORIGIN = "https://app.bmzygame.com"
_PREFIX = "cashrp_"
_CN = "零一二三四五六七八九"
_HEAR = "qwen3-omni-flash"


def cash_ids(start: str | None) -> tuple[str, str]:
    text = (start or "").strip()
    if not text.lower().startswith(_PREFIX):
        return "", ""
    packet_id, _, token = text[len(_PREFIX):].partition("_")
    packet_id = packet_id.strip().upper()
    token = token.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,40}", packet_id):
        return "", ""
    if not re.fullmatch(r"[0-9A-F]{8}", token):
        token = ""
    return packet_id, token


def is_bmzy(rp: RedPacket) -> bool:
    return bool(cash_ids(rp.start)[0])


def yuan(cents) -> str:
    try:
        value = int(cents)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    text = format(
        (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN),
        "f",
    )
    text = text.rstrip("0").rstrip(".")
    return f"{text} CNY"


def digits_of(text: str) -> str:
    found = []
    for ch in text or "":
        if ch.isdigit():
            found.append(ch)
        elif ch in _CN:
            found.append(str(_CN.index(ch)))
    return "".join(found)


def bot_start(url: str) -> tuple[str, str]:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ("t.me", "telegram.me"):
        return "", ""
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if not parts:
        return "", ""
    query = parse_qs(parsed.query)
    start = (query.get("start") or query.get("startapp") or [""])[0]
    return parts[0], start


class ApiError(Exception):
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        super().__init__(str(self.payload.get("error") or status))


def api_message(exc: ApiError) -> str:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    text = str(payload.get("message") or payload.get("error") or "")
    return text.strip()


def headers(init_data: str) -> dict[str, str]:
    return {
        "X-Telegram-Init-Data": init_data,
        "Accept": "application/json",
        "Origin": _ORIGIN,
        "Referer": _ORIGIN + "/",
        "X-Device-Screen": "1080x2400",
        "X-Device-Viewport": "420x780",
        "X-Device-OS": "Android",
        "X-Device-TZ": "Asia/Shanghai",
    }


async def call(http, method: str, path: str, init_data: str, body=None) -> dict:
    resp = await http.request(
        method,
        _ORIGIN + path,
        headers=headers(init_data),
        json=body,
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, data)
    return data


async def hear(http, audio: str) -> str:
    key, base, _model = vision_settings()
    if not key:
        warn_once("bmzy-key", "节点：白马验证码未配置视觉识别")
        return ""
    raw = audio or ""
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    if not raw:
        return ""
    try:
        resp = await http.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            timeout=45,
            json={
                "model": _HEAR,
                "max_tokens": 20,
                "temperature": 0,
                "modalities": ["text"],
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": "data:;base64," + raw, "format": "wav"},
                        },
                        {"type": "text", "text": "这是6位数字验证码。只返回这6个阿拉伯数字。"},
                    ],
                }],
            },
        )
    except Exception as exc:
        log.info("节点：白马验证码识别失败 %s", type(exc).__name__)
        return ""
    if resp.status_code != 200:
        log.info("节点：白马验证码识别失败 HTTP %s", resp.status_code)
        return ""
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        return ""
    code = digits_of(str(content or ""))
    return code if len(code) == 6 else ""


async def open_bot(client, url: str) -> None:
    bot, start = bot_start(url)
    if not bot:
        return
    text = f"/start {start}".strip()
    await client.send_message(bot, text)
    log.info("节点：白马激活 %s", bot)
    await asyncio.sleep(1)


async def follow_chats(client, packet: dict) -> None:
    chats = packet.get("requiredChats") if isinstance(packet.get("requiredChats"), list) else []
    for item in chats:
        if not isinstance(item, dict):
            continue
        name = channel_in(str(item.get("url") or ""))
        if name:
            await join_channel(client, name)


def claimed(packet: dict) -> Result | None:
    mine = packet.get("myClaim") if isinstance(packet.get("myClaim"), dict) else None
    if not mine:
        return None
    amount = yuan(mine.get("cents"))
    return Result(False, amount, False, "已领取过")


async def claim_bmzy(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    packet_id, token = cash_ids(rp.start)
    if not packet_id:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot or "pcyx")
            web = await main_url(client, chat, bot_peer, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：白马打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        init_data = yy_init(web)
        if not init_data:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        try:
            async with http_client(timeout=20) as http:
                try:
                    return await finish(http, client, packet_id, token, init_data)
                except ApiError as exc:
                    message = api_message(exc)
                    if "不在这个群" not in message and "发一句话" not in message:
                        raise
                    try:
                        await client.send_message(chat, "1")
                    except Exception as send_exc:
                        log.info("Telegram：白马发言失败 %s", type(send_exc).__name__)
                        return Result(False, retryable=False, feedback=f"节点：{message}"[:80])
                    log.info("节点：白马先在群里发一句话")
                    await asyncio.sleep(4)
                    return await finish(http, client, packet_id, token, init_data)
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except ApiError as exc:
            message = api_message(exc) or "领取失败"
            retry = exc.status >= 500 or "稍后" in message
            return Result(False, retryable=retry, feedback=f"节点：{message}"[:80])


async def finish(http, client, packet_id: str, token: str, init_data: str) -> Result:
    loaded = await call(http, "GET", f"/api/player/promo/redpacket/{packet_id}", init_data)
    packet = loaded.get("packet") if isinstance(loaded.get("packet"), dict) else None
    if not packet:
        return Result(False, retryable=False, feedback="节点：红包不存在或已结束")
    done = claimed(packet)
    if done:
        return done
    if packet.get("premiumEligible") is False:
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    status = str(packet.get("status") or "")
    if status == "closed":
        return Result(False, retryable=False, feedback="节点：红包已结束")
    if status == "expired":
        return Result(False, retryable=False, feedback="节点：红包已过期")
    if packet.get("needActivateBot"):
        try:
            await open_bot(client, str(packet.get("activateBotUrl") or ""))
        except FloodWaitError:
            raise
        except Exception as exc:
            log.info("Telegram：白马激活失败 %s", type(exc).__name__)
        await call(http, "POST", "/api/player/promo/bonus/activate", init_data, {"id": packet_id})
        loaded = await call(http, "GET", f"/api/player/promo/redpacket/{packet_id}", init_data)
        packet = loaded.get("packet") if isinstance(loaded.get("packet"), dict) else packet
        if packet.get("needActivateBot"):
            return Result(False, retryable=True, feedback="节点：请先激活机器人")
    await follow_chats(client, packet)
    for _ in range(2):
        issued = await call(http, "POST", "/api/player/captcha/audio", init_data, {})
        code = await hear(http, str(issued.get("audio") or ""))
        captcha_id = str(issued.get("id") or "")
        if not code or not captcha_id:
            continue
        try:
            claimed_body = await call(
                http,
                "POST",
                f"/api/player/promo/redpacket/{packet_id}/claim",
                init_data,
                {"captchaId": captcha_id, "captchaCode": code, "groupToken": token},
            )
        except ApiError as exc:
            code = str(exc.payload.get("error") or "")
            message = api_message(exc)
            if "不在这个群" in message or "发一句话" in message:
                raise
            if code == "captcha_required" or "验证码" in message:
                continue
            if code == "follow_required":
                chats = exc.payload.get("chats")
                if isinstance(chats, list):
                    await follow_chats(client, {"requiredChats": chats})
                continue
            retry = "稍后" in message or "频繁" in message
            return Result(False, retryable=retry, feedback=f"节点：{message or '领取失败'}"[:80])
        got = claimed_body.get("packet") if isinstance(claimed_body.get("packet"), dict) else {}
        mine = got.get("myClaim") if isinstance(got.get("myClaim"), dict) else {}
        amount = yuan(mine.get("cents"))
        if claimed_body.get("already"):
            return Result(False, amount, False, "已领取过")
        return Result(True, amount, False, amount or "已领取")
    return Result(False, retryable=True, feedback="节点：验证码没听清")

plug("bmzy", "白马庄园", "webapp", is_bmzy, claim_bmzy, 30, 90)
