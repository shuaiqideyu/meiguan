"""X钱包能量券（nl88 分享券包，前端页面驱动）。"""

import asyncio
import logging
import re

from telethon.errors import FloodWaitError, PeerFloodError, UserNotParticipantError

from meikai.grab.browser import _FP_CONTEXT, _MOBILE_UA, _pages, close_sheet, join_channel, open_patchright_browser
from meikai.grab.types import RedPacket, Result
from meikai.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meikai.grab.catalog import plug
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_BOT = "nl88"
_PAGE_WAIT = 20

_COUPON_RE = re.compile(r"优惠券\s*X?(\d+)")


def is_nl88(rp: RedPacket) -> bool:
    bot = (rp.bot or "").lower().lstrip("@")
    return bot == _BOT and "routertoshareCoupon" in (rp.start or "")


async def claim_nl88(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：券包缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        # 账号不在发布券包的群/频道则领不了，直接跳过（省一次前端驱动）
        if chat_id:
            try:
                await client.get_participant(chat_id, user_id)
            except UserNotParticipantError:
                return Result(False, retryable=False, feedback="节点：不在群")
            except Exception:
                pass
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        try:
            chat = await message_peer(client, event, chat_id)
            if chat is None:
                return Result(False, retryable=True, feedback="节点：窗口打不开")
            _bot, bot_peer = await bot_peers(client, rp.bot)
            web = await app_url(client, chat, bot_peer, rp.short, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：X钱包券打开失败 %s", type(exc).__name__)
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
            try:
                await page.goto(web, wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000)
                # 等「立即抢券」按钮出现（事件驱动，比固定 sleep 快）
                try:
                    await page.wait_for_function(
                        "() => { const t = document.body ? document.body.innerText : '';"
                        " return t.includes('立即抢券') || t.includes('已经领取') || t.includes('抢完') || t.includes('已放入'); }",
                        timeout=8000,
                    )
                except Exception:
                    pass
                clicked = await page.evaluate(
                    "() => { const els = [...document.querySelectorAll('*')];"
                    " const b = els.find(e => (e.innerText || '').trim() === '立即抢券' && e.children.length === 0);"
                    " if (b) { b.click(); return true; } return false; }"
                )
                # 等领取结果（事件驱动）
                try:
                    await page.wait_for_function(
                        "() => { const t = document.body ? document.body.innerText : '';"
                        " return t.includes('已放入') || t.includes('已经领取') || t.includes('抢完') || t.includes('领取成功'); }",
                        timeout=10000,
                    )
                except Exception:
                    pass
                body = await page.evaluate("() => document.body ? document.body.innerText : ''")
                flat = "".join((body or "").split())
                if "等待解锁" in flat or "请先完成解锁" in flat:
                    return Result(False, retryable=True, feedback="节点：券包等待解锁")
                if "已经领取过" in flat or "您已经领取" in flat:
                    return Result(False, retryable=False, feedback="节点：已经领过")
                if "已放入你的卡包" in flat or "已放入卡包" in flat or "放入你的卡包" in flat:
                    matched = _COUPON_RE.search(flat)
                    count = matched.group(1) if matched else ""
                    shown = f"{count} 张券" if count else "券"
                    return Result(True, shown, False, f"领到 {shown}")
                # 「券包抢完或过期后」是领取规则文案，不算抢完；只有剩余 0 才抢完
                if re.search(r"剩余[:：]\s*0\s*/\s*\d+", flat) or "已抢完" in flat or "抢光了" in flat:
                    return Result(False, retryable=False, feedback="节点：券包抢完")
                if not clicked:
                    return Result(False, retryable=True, feedback="节点：抢券按钮没有出现")
                return Result(False, retryable=True, feedback="节点：券包没有回执")
            finally:
                await close_sheet(page=page)


plug("nl88", "X钱包券", "webapp", is_nl88, claim_nl88, 50, 90)
