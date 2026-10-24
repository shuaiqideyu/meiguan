"""永盛娱乐红包（gzyl.vip，前端页面驱动）。"""

import asyncio
import json
import logging

from telethon.errors import FloodWaitError, PeerFloodError

from meiguan.grab.browser import _FP_CONTEXT, _MOBILE_UA, _pages, close_sheet, join_channel, open_patchright_browser
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.webapp import account_premium, bot_peers, main_url, message_peer
from meiguan.grab.catalog import plug
from meiguan.node_host import wallet_lock

log = logging.getLogger("meiguan.grab")

_BOT = "ysylhdbot"
_PAGE_WAIT = 20


def is_ysyl(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT


def _read_json(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _result(payload: dict) -> Result:
    s = payload.get("s")
    m = " ".join(str(payload.get("m") or "").split())
    if s in (0, "0"):
        return Result(True, m or "红包", False, m[:80])
    if m:
        return Result(False, retryable=False, feedback=m[:80])
    return Result(False, retryable=False, feedback=f"节点：状态 {s}")


async def claim_ysyl(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.start:
        return Result(False, retryable=False, feedback="节点：红包缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        try:
            chat = await message_peer(client, event, chat_id)
            if chat is None:
                return Result(False, retryable=True, feedback="节点：窗口打不开")
            _bot, bot_peer = await bot_peers(client, rp.bot)
            web = await main_url(client, chat, bot_peer, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：永盛打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        async with _pages:
            browser = await open_patchright_browser()
            if browser is None:
                return Result(False, retryable=False, feedback="节点：网页验证浏览器启动失败")
            page = await browser.new_page(
                viewport={"width": 420, "height": 780},
                device_scale_factor=2,
                user_agent=_MOBILE_UA,
                is_mobile=True,
                has_touch=True,
                **_FP_CONTEXT,
            )
            loop = asyncio.get_running_loop()
            claim_fut: asyncio.Future = loop.create_future()

            async def on_res(res):
                try:
                    if "redpacket/claim" in res.url and not claim_fut.done():
                        claim_fut.set_result(await res.text())
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(on_res(r)))
            try:
                await page.goto(web, wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000)
                for _ in range(8):
                    await asyncio.sleep(1)
                    body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    if "领取红包" in (body or ""):
                        break
                await page.evaluate("() => { const el = document.querySelector('.adm-checkbox'); if (el) el.click(); }")
                await asyncio.sleep(0.5)
                clicked = await page.evaluate(
                    "() => { const btns = [...document.querySelectorAll('button')];"
                    " const b = btns.find(e => (e.innerText || '').trim() === '领取红包');"
                    " if (b) { b.click(); return true; } return false; }"
                )
                if not clicked:
                    return Result(False, retryable=True, feedback="节点：领取按钮没有出现")
                try:
                    raw = await asyncio.wait_for(claim_fut, timeout=_PAGE_WAIT)
                except asyncio.TimeoutError:
                    return Result(False, retryable=True, feedback="节点：永盛没有回执")
                return _result(_read_json(raw))
            finally:
                await close_sheet(page=page)


plug("ysyl", "永盛娱乐", "webapp", is_ysyl, claim_ysyl, 60, 90)
