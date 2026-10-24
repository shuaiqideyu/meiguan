"""盛大娱乐红包（sdrp_bot）。注册走浏览器 WASM 签名，领取纯 HTTP + Turnstile。"""

import asyncio
import logging
import uuid

from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName

from meiguan.grab.browser import _FP_CONTEXT, _MOBILE_UA, join_channel, patchright_driver
from meiguan.grab.catalog import plug
from meiguan.grab.httpclient import http_client
from meiguan.node_host import wallet_lock
from meiguan.grab.packets.yypay import yy_body, yy_init, yy_key
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.vision import warn_once
from meiguan.grab.webapp import account_premium, app_url, bot_peers, message_peer

log = logging.getLogger("meiguan.grab")

_BOT = "sdrp_bot"
_ORIGIN = "https://rp.sdyl888.com"
_REG_BOT = "sdyl"
_REG_ORIGIN = "https://h5.sdyl888.com"
_HEADERS = {"content-type": "application/json"}
# 盛大 Turnstile sitekey（React 版从 RSC Flight Data 注入，固定值）
_SITE_KEY = "0x4AAAAAAFLosS6gdzUc86oT"


def sdyl_chat(init_data: str) -> dict:
    """从 initData 提取 claim 需要的 chatKey/chatType。"""
    from urllib.parse import parse_qs

    out: dict = {}
    if not init_data:
        return out
    q = parse_qs(init_data)
    ci = (q.get("chat_instance") or [None])[0]
    ct = (q.get("chat_type") or [None])[0]
    if ci:
        out["chatKey"] = ci
    if ct:
        out["chatType"] = ct
    return out


def is_sdyl(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT


def sdyl_code(rp: RedPacket) -> str:
    """start 形如 rp_dfBlfoIu，取 dfBlfoIu。"""
    return (rp.start or "").replace("rp_", "", 1).strip()


async def sdyl_turnstile(http, sitekey: str) -> str:
    """capmonster Turnstile，sitekey 由 detail 动态返回，不硬编码。"""
    created = await http.post(
        "https://api.capmonster.cloud/createTask",
        json={
            "clientKey": yy_key(),
            "task": {
                "type": "TurnstileTaskProxyless",
                "websiteURL": _ORIGIN,
                "websiteKey": sitekey,
            },
        },
    )
    task = yy_body(created)
    if task.get("errorId") or not task.get("taskId"):
        log.info("节点：盛大打码失败 %s", task.get("errorCode") or created.status_code)
        return ""
    for _ in range(18):
        await asyncio.sleep(3)
        result = await http.post(
            "https://api.capmonster.cloud/getTaskResult",
            json={"clientKey": yy_key(), "taskId": task["taskId"]},
        )
        data = yy_body(result)
        if data.get("errorId"):
            log.info("节点：盛大打码失败 %s", data.get("errorCode") or result.status_code)
            return ""
        if data.get("status") == "ready":
            return str((data.get("solution") or {}).get("token") or "")
    log.info("节点：盛大打码超时")
    return ""


async def sdyl_register(client, start: str) -> bool:
    """盛大会员注册：先点「去游玩」启动 @sdyl（/start），再打开 h5 让前端自动 login（带 WASM x-sign）。"""
    try:
        bot = await client.get_entity(_REG_BOT)
        # 先点「去游玩」启动对方机器人，再进游戏 login
        await client.send_message(bot, f"/start {start}")
        peer = await client.get_input_entity(bot)
        opened = await client(
            RequestAppWebViewRequest(
                peer=peer,
                app=InputBotAppShortName(bot_id=peer, short_name="app"),
                platform="android",
                start_param=start,
            )
        )
    except Exception as exc:
        log.info("节点：盛大注册打开失败 %s", type(exc).__name__)
        return False
    url = getattr(opened, "url", "") or ""
    if not url:
        return False
    driver = await patchright_driver()
    browser = await driver.chromium.launch(headless=False)
    page = await browser.new_page(
        viewport={"width": 420, "height": 780},
        device_scale_factor=2,
        user_agent=_MOBILE_UA,
        is_mobile=True,
        has_touch=True,
        **_FP_CONTEXT,
    )
    ok = False
    try:
        async def on_res(res):
            nonlocal ok
            if "tgapp/login" in res.url and not ok:
                try:
                    if '"code":0' in (await res.text()):
                        ok = True
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.ensure_future(on_res(r)))
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(8000)
    except Exception as exc:
        log.info("节点：盛大注册失败 %s", type(exc).__name__)
    finally:
        await browser.close()
    return ok


def sdyl_money(payload: dict) -> Result:
    """claim 结果判定。code=0 成功，金额从 data 或 myClaim 提取。"""
    msg = str(payload.get("message") or "")
    if payload.get("code") != 0:
        retry = any(word in msg for word in ("频繁", "稍后", "稍後", "系统", "维护", "繁忙", "验证", "人机"))
        if any(word in msg for word in ("抢完", "领完", "已领取", "已領取", "结束", "結束", "过期", "過期", "不存在", "注册")):
            retry = False
        return Result(False, retryable=retry, feedback=f"节点：{msg or '盛大领取失败'}"[:80])
    data = payload.get("data") or {}
    amount = data.get("amount") or data.get("myClaimAmount")
    my_claim = data.get("myClaim") or {}
    if not amount:
        amount = my_claim.get("amount")
    shown = ""
    if amount not in (None, 0, "0", ""):
        shown = f"{amount}"
    text = str(data.get("message") or "红包已到账")
    return Result(True, shown, False, text[:80])


async def claim_sdyl(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not yy_key():
        warn_once("sdyl-key", "节点：盛大验证未配置打码密钥")
        from meiguan.grab.solver import pay_missing

        return Result(False, retryable=False, feedback=pay_missing("盛大验证"))
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
            log.info("Telegram：盛大打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        init_data = yy_init(web)
        if not init_data:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        code = sdyl_code(rp)
        headers = {**{"x-tg-init-data": init_data}, **_HEADERS}
        try:
            async with http_client(timeout=30) as http:
                # 1. 详情
                detail = await http.get(f"{_ORIGIN}/__rp/detail/{code}", headers=headers)
                detail_body = yy_body(detail)
                data = detail_body.get("data") or {}
                if detail_body.get("code") != 0:
                    return sdyl_money(detail_body)
                status = data.get("status")
                if status in (2, "2", "finished", "expired"):
                    return Result(False, retryable=False, feedback="节点：红包已抢完")
                my_claim = data.get("myClaim") or {}
                if my_claim.get("amount"):
                    return Result(False, retryable=False, feedback=f"节点：已经领过 {my_claim.get('amount')}")
                # 2. 配额（未注册返回 code 15210）
                quota = await http.get(f"{_ORIGIN}/api/redpacket/quota", headers=headers)
                quota_body = yy_body(quota)
                if quota_body.get("code") == 15210:
                    # 先点「去游玩」启动 @sdyl 再注册盛大 member
                    if not await sdyl_register(client, rp.start or ""):
                        return Result(False, retryable=True, feedback="节点：盛大注册失败")
                    quota = await http.get(f"{_ORIGIN}/api/redpacket/quota", headers=headers)
                    quota_body = yy_body(quota)
                if quota_body.get("code") != 0:
                    return Result(False, retryable=quota_body.get("code") == 15210, feedback=f"节点：{quota_body.get('message') or '配额失败'}"[:80])
                # 3. Turnstile（siteKey 从 RSC Flight Data 注入，固定值）
                turnstile = await sdyl_turnstile(http, _SITE_KEY)
                if not turnstile:
                    return Result(False, retryable=True, feedback="节点：盛大打码失败")
                # 4. claim-ticket（React 版要多带 env 反自动化标志位，正常 0）
                ticket = await http.post(f"{_ORIGIN}/__rp/claim-ticket", json={"rpCode": code, "turnstileToken": turnstile, "env": 0}, headers=headers)
                ticket_body = yy_body(ticket)
                if ticket_body.get("result") != "issued" and ticket_body.get("code") != 0:
                    return Result(False, retryable=True, feedback=f"节点：{ticket_body.get('message') or '取票失败'}"[:80])
                # 5. claim（React 版 body 要多带 chatKey/chatType）
                claim_body = {"rpCode": code, **sdyl_chat(init_data)}
                resp = await http.post(f"{_ORIGIN}/api/redpacket/claim", json=claim_body, headers=headers)
        except Exception as exc:
            log.info("节点：盛大领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：盛大领取失败")
        payload = yy_body(resp)
        if resp.status_code >= 400 and not payload:
            return Result(False, retryable=True, feedback=f"节点：盛大领取失败 HTTP {resp.status_code}")
        return sdyl_money(payload)


plug("sdyl", "盛大", "webapp", is_sdyl, claim_sdyl, 40, 120)
