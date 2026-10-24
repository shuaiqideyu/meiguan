"""YYpay 领取。"""

import asyncio
import logging
import uuid
from urllib.parse import parse_qs, urlparse

from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import RequestWebViewRequest

from meikai.grab.browser import join_channel
from meikai.grab.types import RedPacket, Result
from meikai.grab.vision import warn_once
from meikai.grab.webapp import (
    account_premium,
    app_url,
    input_user,
    message_peer,
    webview_blocked,
)
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_YY_ORIGIN = "https://minapp.yypay.info"


_YY_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.200 Mobile Safari/537.36"
)


def yy_key() -> str:
    from meikai.grab.solver import solver_key

    return solver_key("cf")


def yy_init(url: str) -> str:
    parsed = urlparse(url or "")
    parts = [parsed.query, parsed.fragment]
    if "?" in (parsed.fragment or ""):
        parts.append(parsed.fragment.split("?", 1)[1])
    for part in parts:
        found = parse_qs(part).get("tgWebAppData", [""])[0]
        if found:
            return found
    return ""


def yy_headers(token: str, init_data: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Telegram-Init-Data": init_data,
        "X-Request-Id": str(uuid.uuid4()),
        "X-Client-Platform": "android",
        "X-Miniapp-Version": "development_0.3.6",
        "X-Tg-WebApp-Version": "9.1",
        "User-Agent": _YY_UA,
        "Accept": "application/json",
    }


def yy_message(payload: dict) -> str:
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or "")
    if isinstance(detail, str):
        return detail
    return str(payload.get("message") or "")


def yy_miss(text: str) -> Result:
    shown = " ".join((text or "YYpay 没有回执").split())[:80]
    retry = "重复" in shown or "稍" in shown or "INVALID_INIT_DATA" in shown
    if any(word in shown for word in ("已领取", "抢光", "领完", "已结束", "已过期")):
        retry = False
    return Result(
        False,
        retryable=retry,
        feedback=f"节点：{shown}",
        blocked="会员" in shown or "Premium" in shown,
    )


def yy_body(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def yy_site_key(payload: dict) -> str:
    captcha = payload.get("captcha") if isinstance(payload.get("captcha"), dict) else {}
    providers = captcha.get("providers") if isinstance(captcha.get("providers"), list) else []
    want = str(payload.get("resolved_captcha_provider") or "cf")
    for item in providers:
        if isinstance(item, dict) and str(item.get("value") or "") == want and item.get("site_key"):
            return str(item["site_key"])
    for item in providers:
        if isinstance(item, dict) and item.get("site_key"):
            return str(item["site_key"])
    return ""


async def yy_open(client, rp: RedPacket, chat_peer) -> str:
    if chat_peer is None:
        return ""
    bot = await client.get_entity(rp.bot)
    bot_peer = await client.get_input_entity(bot)
    init_data = yy_init(await app_url(client, chat_peer, bot_peer, rp.short or "wallet", rp.start or ""))
    if init_data:
        return init_data
    try:
        opened = await client(RequestWebViewRequest(
            peer=chat_peer,
            bot=input_user(bot_peer),
            platform="android",
            url=f"https://t.me/{rp.bot}/{rp.short}?startapp={rp.start}",
        ))
    except Exception as exc:
        if webview_blocked(exc):
            log.info("Telegram：窗口不能打开网页")
            return ""
        raise
    return yy_init(getattr(opened, "url", "") or "")


async def yy_solve(http, site_key: str) -> str:
    created = await http.post(
        "https://api.capmonster.cloud/createTask",
        json={
            "clientKey": yy_key(),
            "task": {
                "type": "TurnstileTaskProxyless",
                "websiteURL": f"{_YY_ORIGIN}/verifyPage",
                "websiteKey": site_key,
                "pageAction": "red_packet_claim",
            },
        },
    )
    task = yy_body(created)
    if task.get("errorId") or not task.get("taskId"):
        log.info("节点：YYpay 打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(18):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.capmonster.cloud/getTaskResult",
            json={"clientKey": yy_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：YYpay 打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            return str((data.get("solution") or {}).get("token") or "")
    log.info("节点：YYpay 打码超时")
    return ""


def yy_amount(payload: dict) -> str | None:
    amount = payload.get("amount")
    if amount in (None, ""):
        return None
    currency = str(payload.get("currency") or "").strip()
    return f"{amount} {currency}".strip()


async def claim_yypay(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：YYpay 缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not yy_key():
        warn_once("yy-key", "节点：YYpay 未配置打码密钥")
        from meikai.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("YYpay"))
    from meikai.grab.httpclient import http_client

    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        try:
            chat = await message_peer(client, event, chat_id)
            if chat is None:
                return Result(False, retryable=True, feedback="节点：窗口打不开")
            init_data = await yy_open(client, rp, chat)
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：YYpay 打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：YYpay 没有打开")
        if not init_data:
            return Result(False, retryable=True, feedback="节点：YYpay 没有打开")
        try:
            async with http_client(timeout=30) as http:
                session_resp = await http.post(
                    f"{_YY_ORIGIN}/miniapp/auth/session",
                    headers={"User-Agent": _YY_UA, "Accept": "application/json"},
                    json={"init_data": init_data, "start_param": rp.start},
                )
                session = yy_body(session_resp)
                access = str(session.get("access_token") or "")
                if session_resp.status_code != 200 or not access:
                    return yy_miss(yy_message(session) or f"登录失败 HTTP {session_resp.status_code}")
                ctx = session.get("start_context") if isinstance(session.get("start_context"), dict) else {}
                params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
                try:
                    red_id = int(params.get("red_packet_id"))
                except (TypeError, ValueError):
                    return Result(False, retryable=False, feedback="节点：YYpay 没有红包编号")
                headers = yy_headers(access, init_data)
                validator_resp = await http.get(
                    f"{_YY_ORIGIN}/miniapp/red-packet/claim-validator",
                    params={"red_packet_id": red_id},
                    headers=headers,
                )
                validator = yy_body(validator_resp)
                if validator_resp.status_code != 200:
                    return yy_miss(yy_message(validator) or f"验证配置失败 HTTP {validator_resp.status_code}")
                pre_resp = await http.post(
                    f"{_YY_ORIGIN}/miniapp/red-packet/claim-validator/precheck",
                    headers=headers,
                    json={"red_packet_id": red_id, "chat_id": None, "request_id": str(uuid.uuid4())},
                )
                pre = yy_body(pre_resp)
                if pre_resp.status_code != 200 or not pre.get("ok") or not pre.get("precheck_ticket"):
                    return yy_miss(yy_message(pre) or f"领取校验失败 HTTP {pre_resp.status_code}")
                token = None
                provider = None
                if not pre.get("skips_captcha"):
                    site_key = yy_site_key(validator)
                    if not site_key:
                        return Result(False, retryable=False, feedback="节点：YYpay 没有验证码")
                    token = await yy_solve(http, site_key)
                    if not token:
                        return Result(False, retryable=True, feedback="节点：YYpay 打码失败")
                    provider = "cf"
                verify_resp = await http.post(
                    f"{_YY_ORIGIN}/miniapp/red-packet/claim-validator/verify",
                    headers=headers,
                    json={
                        "red_packet_id": red_id,
                        "precheck_ticket": pre.get("precheck_ticket"),
                        "token": token,
                        "provider": provider,
                        "chat_id": None,
                        "request_id": str(uuid.uuid4()),
                    },
                )
                verified = yy_body(verify_resp)
                if verify_resp.status_code != 200 or not verified.get("success"):
                    return yy_miss(yy_message(verified) or f"验证失败 HTTP {verify_resp.status_code}")
                for _ in range(8):
                    await asyncio.sleep(1)
                    result_resp = await http.get(
                        f"{_YY_ORIGIN}/miniapp/red-packet/claim-validator/result",
                        params={"red_packet_id": red_id},
                        headers=headers,
                    )
                    result = yy_body(result_resp)
                    if result.get("status") == "success":
                        return Result(True, yy_amount(result), False)
                    if result.get("status") == "failed":
                        return yy_miss(yy_message(result) or "领取失败")
        except Exception as exc:
            log.info("节点：YYpay 领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：YYpay 领取失败")
    return Result(False, retryable=False, feedback="节点：YYpay 没有回执")

def _yy(rp) -> bool:
    return True


plug("yypay", "YYpay", "cloudflare", _yy, claim_yypay, 10, 90)
