"""富贵红包。"""

import asyncio
import json
import logging
from urllib.parse import parse_qs

from meikai.grab.httpclient import http_client
from telethon.errors import FloodWaitError, PeerFloodError

from meikai.grab.browser import join_channel
from meikai.grab.types import RedPacket, Result
from meikai.grab.vision import warn_once
from meikai.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meikai.grab.packets.yypay import yy_body, yy_init, yy_key
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_ORIGIN = "https://fugui.apifjslfsaf.com/api"
_PAGE = "https://receive.fugui.game/redPacket"
_PAGE_ORIGIN = "https://receive.fugui.game"
_SITEKEY = "0x4AAAAAACMPVYn47M2hlgu5"
_BOT = "fuguihbbot"
_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def is_fugui(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT


def user_of(init_data: str) -> tuple[dict, str]:
    parsed = parse_qs(init_data or "", keep_blank_values=True)
    raw = (parsed.get("user") or ["{}"])[0]
    try:
        user = json.loads(raw)
    except json.JSONDecodeError:
        user = {}
    if not isinstance(user, dict):
        user = {}
    chat = (parsed.get("chat_instance") or [""])[0]
    return user, chat


def money(payload: dict) -> Result:
    msg = str(payload.get("msg") or "")
    if payload.get("code") == 1:
        retry = "稍后" in msg or "频繁" in msg
        if any(word in msg for word in ("已领取", "抢完", "领完", "结束", "过期", "请先注册")):
            retry = False
        return Result(False, retryable=retry, feedback=f"节点：{msg or '领取失败'}"[:80])
    raw = payload.get("data")
    if isinstance(raw, str) and "," in raw:
        amount, currency = raw.split(",", 1)
        unit = "USDT" if currency.strip().upper() == "USDT" else "CNY"
        shown = f"{amount.strip()} {unit}"
        return Result(True, shown, False, shown)
    if payload.get("code") in (0, "0", None) and not msg:
        return Result(False, retryable=True, feedback="节点：富贵没有回执")
    return Result(False, retryable=False, feedback=f"节点：{msg or '领取失败'}"[:80])


async def solve(http) -> str:
    created = await http.post(
        "https://api.capmonster.cloud/createTask",
        json={
            "clientKey": yy_key(),
            "task": {
                "type": "TurnstileTaskProxyless",
                "websiteURL": _PAGE,
                "websiteKey": _SITEKEY,
            },
        },
    )
    task = yy_body(created)
    if task.get("errorId") or not task.get("taskId"):
        log.info("节点：富贵打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(18):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.capmonster.cloud/getTaskResult",
            json={"clientKey": yy_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：富贵打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            return str((data.get("solution") or {}).get("token") or "")
    log.info("节点：富贵打码超时")
    return ""


async def claim_fugui(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not yy_key():
        warn_once("fugui-key", "节点：富贵验证未配置打码密钥")
        from meikai.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("富贵验证"))
    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot)
            init_data = yy_init(await app_url(client, chat, bot_peer, rp.short, rp.start or ""))
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：富贵打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        if not init_data:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        user, chat_instance = user_of(init_data)
        if not user.get("id"):
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        first = str(user.get("first_name") or "")
        last = str(user.get("last_name") or "")
        try:
            async with http_client(timeout=30) as http:
                token = await solve(http)
                if not token:
                    return Result(False, retryable=True, feedback="节点：富贵打码失败")
                resp = await http.post(
                    f"{_ORIGIN}/bonus/bonus-red-packet/receive",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Accept-Language": "zh-CN",
                        "CLIENT-TOC": "Y",
                        "User-Agent": _UA,
                        "Origin": _PAGE_ORIGIN,
                        "Referer": _PAGE_ORIGIN + "/",
                    },
                    json={
                        "id": rp.start,
                        "telegramId": user.get("id"),
                        "username": user.get("username") or "",
                        "fullName": f"{first}{last}",
                        "userPremium": bool(user.get("is_premium")),
                        "chatInstance": chat_instance,
                        "token": token,
                        "initData": init_data,
                    },
                )
        except Exception as exc:
            log.info("节点：富贵领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：富贵领取失败")
        payload = yy_body(resp)
        if resp.status_code >= 400 and not payload:
            return Result(False, retryable=True, feedback=f"节点：富贵领取失败 HTTP {resp.status_code}")
        return money(payload)

plug("fugui", "富贵", "webapp", is_fugui, claim_fugui, 40, 90)
