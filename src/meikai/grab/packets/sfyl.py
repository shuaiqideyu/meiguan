"""盛丰娱乐红包（sfylc888bot）。Cloudflare Turnstile + 混淆 JS，纯 HTTP 领取。"""

import asyncio
import logging
import uuid

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

_BOT = "sfylc888bot"
_ORIGIN = "https://p12red.mzbxdt.com"
_PAGE = "https://redp12.jbkolo.com"
_SITEKEY = "0x4AAAAAACMEcwEulUQeH0-K"


def is_sfyl(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT


def rid_of(rp: RedPacket) -> str:
    start = rp.start or ""
    if "=" in start:
        return start.split("=", 1)[1]
    return start


async def sfyl_turnstile(http) -> str:
    """用 capmonster 破解 Cloudflare Turnstile，返回 cf-turnstile-response token。"""
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
        log.info("节点：盛丰打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(18):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.capmonster.cloud/getTaskResult",
            json={"clientKey": yy_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：盛丰打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            return str((data.get("solution") or {}).get("token") or "")
    log.info("节点：盛丰打码超时")
    return ""


def sfyl_money(payload: dict) -> Result:
    msg = str(payload.get("message") or "")
    if payload.get("code") != 0:
        retry = any(word in msg for word in ("频繁", "稍后", "稍後", "系统", "维护", "繁忙"))
        if any(word in msg for word in ("抢完", "领完", "已领取", "已領取", "结束", "結束", "过期", "過期", "不存在")):
            retry = False
        return Result(False, retryable=retry, feedback=f"节点：{msg or '领取失败'}"[:80])
    data = payload.get("data") or {}
    amount = data.get("grabAmount")
    text = str(data.get("message") or "红包已到账")
    shown = ""
    if amount not in (None, 0, "0", ""):
        shown = f"{amount} USDT"
    return Result(True, shown, False, text[:80])


async def claim_sfyl(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not yy_key():
        warn_once("sfyl-key", "节点：盛丰验证未配置打码密钥")
        from meikai.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("盛丰验证"))
    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot)
            web = await app_url(client, chat, bot_peer, rp.short, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：盛丰打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        init_data = yy_init(web)
        if not init_data:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        rid = rid_of(rp)
        serial = f"iphone-{uuid.uuid4().hex[:12]}"
        try:
            async with http_client(timeout=30) as http:
                login = await http.post(
                    f"{_ORIGIN}/login-tg",
                    json={"initData": init_data},
                    headers={"lang": "zh", "Content-Type": "application/json"},
                )
                login_body = yy_body(login)
                token = str((login_body.get("data") or {}).get("token") or "")
                if not token:
                    log.info("节点：盛丰登录失败 %s", login_body.get("message") or login.status_code)
                    return Result(False, retryable=True, feedback="节点：盛丰登录失败")
                turnstile = await sfyl_turnstile(http)
                if not turnstile:
                    return Result(False, retryable=True, feedback="节点：盛丰打码失败")
                resp = await http.post(
                    f"{_ORIGIN}/grab/red/packet",
                    json={
                        "packetId": rid,
                        "initData": init_data,
                        "turnstileToken": turnstile,
                        "serialNo": serial,
                        "machineName": "iphone",
                    },
                    headers={
                        "lang": "zh",
                        "Content-Type": "application/json",
                        "Authorization": token,
                    },
                )
        except Exception as exc:
            log.info("节点：盛丰领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：盛丰领取失败")
        payload = yy_body(resp)
        if resp.status_code >= 400 and not payload:
            return Result(False, retryable=True, feedback=f"节点：盛丰领取失败 HTTP {resp.status_code}")
        return sfyl_money(payload)


plug("sfyl", "盛丰", "webapp", is_sfyl, claim_sfyl, 40, 120)
