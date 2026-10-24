"""法拉利领取。"""

import asyncio
import logging
import re
import uuid

from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import RequestWebViewRequest

from meikai.grab.browser import join_channel
from meikai.grab.types import RedPacket, Result
from meikai.grab.vision import warn_once
from meikai.grab.webapp import account_premium, app_url, input_user, message_peer, webview_blocked
from meikai.grab.packets.yypay import yy_body, yy_init
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_FLL_SITEKEY = "146a7320-f922-436a-948c-dfe677fef6c4"


_FLL_PAGE = "https://img1.shjyzn.com/tgbot/"


_FLL_API = "https://aliyun.xg805.com"


_FLL_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def two_key() -> str:
    from meikai.grab.solver import solver_key

    return solver_key("fll")


def fll_ids(start: str | None) -> tuple[str, str]:
    text = start or ""
    matched = re.search(r"_hash=([0-9a-fA-F]+)", text)
    if matched:
        return matched.group(1), ""
    matched = re.search(r"dataId=(\d+)", text)
    if matched:
        return "", matched.group(1)
    return "", ""


def fll_hash(start: str | None) -> str:
    packet_hash, packet_id = fll_ids(start)
    return packet_hash or packet_id


async def fll_init(client, start: str, chat_peer) -> str:
    if chat_peer is None:
        return ""
    bot = await client.get_entity("fllqb")
    bot_peer = await client.get_input_entity(bot)
    init_data = yy_init(await app_url(client, chat_peer, bot_peer, "wallet", start))
    if init_data:
        return init_data
    try:
        opened = await client(RequestWebViewRequest(
            peer=chat_peer,
            bot=input_user(bot_peer),
            platform="android",
            url=f"https://t.me/fllqb/wallet?startapp={start}",
        ))
    except Exception as exc:
        if webview_blocked(exc):
            log.info("Telegram：窗口不能打开网页")
            return ""
        raise
    return yy_init(getattr(opened, "url", "") or "")


async def fll_solve(http) -> str:
    created = await http.post(
        "https://api.2captcha.com/createTask",
        json={
            "clientKey": two_key(),
            "task": {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": _FLL_PAGE,
                "websiteKey": _FLL_SITEKEY,
            },
        },
    )
    task = yy_body(created)
    if task.get("errorId") or not task.get("taskId"):
        log.info("节点：法拉利打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(30):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.2captcha.com/getTaskResult",
            json={"clientKey": two_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：法拉利打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            solution = data.get("solution") if isinstance(data.get("solution"), dict) else {}
            return str(solution.get("gRecaptchaResponse") or solution.get("token") or "")
    log.info("节点：法拉利打码超时")
    return ""


def fll_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": _FLL_UA,
        "Referer": "https://img1.shjyzn.com/",
        "Accept": "application/json",
    }


async def claim_fllqb(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    packet_hash, packet_id = fll_ids(rp.start)
    if not packet_hash and not packet_id:
        return Result(False, retryable=False, feedback="节点：网页验证缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not two_key():
        warn_once("fll-key", "节点：法拉利验证未配置打码密钥")
        from meikai.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("法拉利验证"))
    from meikai.grab.httpclient import http_client

    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        try:
            chat = await message_peer(client, event, chat_id)
            if chat is None:
                return Result(False, retryable=True, feedback="节点：窗口打不开")
            init_data = await fll_init(client, rp.start or "", chat)
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：法拉利打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        if not init_data:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        try:
            async with http_client(timeout=30) as http:
                token = await fll_solve(http)
                if not token:
                    return Result(False, retryable=True, feedback="节点：法拉利打码失败")
                payload = {
                    "tg_id": str(user_id),
                    "visitor_id": "",
                    "request_id": str(uuid.uuid4()),
                    "rp_id": packet_id,
                    "hash": packet_hash,
                    "init_data": init_data,
                }
                claimed = yy_body(await http.post(
                    f"{_FLL_API}/api/gate/trade/web/redpacket/get",
                    headers=fll_headers(),
                    json={**payload, "captcha_token": token},
                ))
                message = str(claimed.get("msg") or "")
                if claimed.get("code") != 20000:
                    retry = "稍" in message or "频繁" in message
                    if "已领取" in message or "领完" in message or "抢完" in message:
                        retry = False
                    return Result(False, retryable=retry, feedback=f"节点：{message or '法拉利领取失败'}"[:80])
                status = yy_body(await http.post(
                    f"{_FLL_API}/api/gate/trade/web/redpacket/get_status",
                    headers=fll_headers(),
                    json=payload,
                ))
                shown = str(status.get("msg") or message or "已领取")
                return Result(True, retryable=False, feedback=f"节点：{shown}"[:80])
        except Exception as exc:
            log.info("节点：法拉利领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：法拉利领取失败")

def _fll(rp) -> bool:
    return bool(fll_hash(rp.start))


plug("fll", "法拉利钱包", "webapp", _fll, claim_fllqb, 20, 150)
