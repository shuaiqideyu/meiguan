"""奶龙能量红包（nlhbbot）。TRX 能量，start 链接 + WebView + Cloudflare Turnstile。"""

import asyncio
import logging
import re

from meikai.grab.httpclient import http_client
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import RequestWebViewRequest
from telethon.tl.types import InputUser

from meikai.grab.types import RedPacket, Result
from meikai.grab.vision import warn_once
from meikai.grab.packets.yypay import yy_body, yy_init, yy_key
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_BOT = "nlhbbot"
_API = "https://app.trxx.pro/api/v1"
_SITEKEY = "0x4AAAAAADWB2H8AhQctQKFH"

_START_RE = re.compile(r"grab_([A-Za-z0-9]+)_v([0-9_]+)$")


def is_nlhb(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT


def nlhb_parts(rp: RedPacket) -> tuple[str | None, str | None]:
    matched = _START_RE.match(rp.start or "")
    if not matched:
        return None, None
    return matched.group(1), matched.group(2)


async def nlhb_turnstile(http, sitekey: str) -> str:
    created = await http.post(
        "https://api.capmonster.cloud/createTask",
        json={
            "clientKey": yy_key(),
            "task": {
                "type": "TurnstileTaskProxyless",
                "websiteURL": "https://app.trxx.pro",
                "websiteKey": sitekey,
            },
        },
    )
    task = yy_body(created)
    if task.get("errorId") or not task.get("taskId"):
        log.info("节点：奶龙打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(18):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.capmonster.cloud/getTaskResult",
            json={"clientKey": yy_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：奶龙打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            return str((data.get("solution") or {}).get("token") or "")
    log.info("节点：奶龙打码超时")
    return ""


def nlhb_money(payload: dict) -> Result:
    packet = payload.get("packet") or {}
    msg = str(payload.get("message") or payload.get("error") or payload.get("detail") or "")
    if not packet:
        retry = any(word in msg for word in ("频繁", "稍后", "稍後", "系统", "维护", "繁忙"))
        return Result(False, retryable=retry, feedback=f"节点：{msg or '领取失败'}"[:80])
    share = str(packet.get("share_text") or "")
    matched = re.search(r"每个\s*([\d,]+)\s*能量", share)
    energy = matched.group(1).replace(",", "") if matched else ""
    shown = f"{energy} 能量" if energy else "能量红包"
    return Result(True, shown, False, "红包已抢到"[:80])


async def claim_nlhb(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not yy_key():
        warn_once("nlhb-key", "节点：奶龙验证未配置打码密钥")
        from meikai.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("奶龙验证"))
    pid, version = nlhb_parts(rp)
    if not pid:
        return Result(False, retryable=False, feedback="节点：奶龙红包号解析失败")
    async with wallet_lock(user_id):
        try:
            bot = await client.get_entity(_BOT)
            peer = await client.get_input_entity(_BOT)
            web_url = f"https://app.trxx.pro/grab/{pid}"
            if version:
                web_url += f"?v={version}"
            opened = await client(RequestWebViewRequest(
                peer=peer, bot=InputUser(bot.id, bot.access_hash), platform="android", url=web_url))
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：奶龙窗口失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：奶龙窗口打开失败")
        init_data = yy_init(opened.url)
        if not init_data:
            return Result(False, retryable=True, feedback="节点：奶龙 initData 缺失")
        try:
            async with http_client(timeout=30) as http:
                turnstile = await nlhb_turnstile(http, _SITEKEY)
                if not turnstile:
                    return Result(False, retryable=True, feedback="节点：奶龙打码失败")
                resp = await http.post(
                    f"{_API}/red-packets/{pid}/claim",
                    json={"turnstile_token": turnstile},
                    headers={"Content-Type": "application/json", "X-Telegram-Init-Data": init_data},
                )
        except Exception as exc:
            log.info("节点：奶龙领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：奶龙领取失败")
        payload = yy_body(resp)
        if resp.status_code >= 400 and not payload:
            return Result(False, retryable=True, feedback=f"节点：奶龙领取失败 HTTP {resp.status_code}")
        return nlhb_money(payload)


plug("nlhb", "奶龙", "captcha", is_nlhb, claim_nlhb, 35, 120)
