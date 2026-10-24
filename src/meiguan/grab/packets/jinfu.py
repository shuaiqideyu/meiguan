"""金富游戏空投红包（JinFuPGbot）。语音验证码（腾讯 TCaptcha 音频）。"""

import asyncio
import base64
import logging

from telethon.errors import FloodWaitError, PeerFloodError

from meiguan.grab.browser import _FP_CONTEXT, open_browser
from meiguan.grab.httpclient import http_client
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.vision import vision_settings, warn_once
from meiguan.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meiguan.grab.packets.bmzy import hear
from meiguan.grab.catalog import plug
from meiguan.node_host import wallet_lock

log = logging.getLogger("meiguan.grab")

_BOT = "JinFuPGbot"

_CLAIM_BTN = "领取空投"
_INPUT = ".tencent-captcha-dy__audio-v2-input"
_CONFIRM = ".tencent-captcha-dy__verify-confirm-btn"


def is_jinfu(rp: RedPacket) -> bool:
    return (rp.bot or "").lower().lstrip("@") == _BOT.lower()


def jinfu_result(text: str) -> Result:
    shown = " ".join((text or "").split())
    if "领取成功" in shown or "已领取" in shown or "已放入" in shown:
        return Result(True, "", False, "领取成功")
    if "领取失败" in shown or "未达标" in shown or "不满足" in shown or "风控" in shown:
        return Result(False, retryable=False, feedback=shown[:80])
    if "处理中" in shown or "请稍候" in shown:
        return Result(False, retryable=True, feedback=shown[:80])
    return Result(False, retryable=True, feedback=shown[:80])


async def claim_jinfu(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    if not vision_settings()[0]:
        warn_once("jinfu-key", "节点：金富验证未配置视觉识别")
        return Result(False, retryable=False, feedback="节点：金富验证未配置视觉识别")
    async with wallet_lock(user_id):
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
            log.info("Telegram：金富打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        browser = await open_browser()
        if browser is None:
            return Result(False, retryable=True, feedback="节点：浏览器打不开")
        page = await browser.new_page(viewport={"width": 420, "height": 780}, device_scale_factor=2, **_FP_CONTEXT)
        try:
            await page.goto(web, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(4000)
            clicked = await page.evaluate(
                """() => {
                  const b = [...document.querySelectorAll('button,[class*=btn],[class*=Btn]')]
                    .find(e => (e.innerText || '').includes('领取空投'));
                  if (b) { b.click(); return true; }
                  return false;
                }"""
            )
            if not clicked:
                return Result(False, retryable=True, feedback="节点：领取按钮没有出现")
            try:
                await page.wait_for_selector(_INPUT, timeout=8000)
            except Exception:
                return Result(False, retryable=True, feedback="节点：验证码没有出现")
            audio_url = await page.evaluate(
                "() => { const a = document.querySelector('audio source') || document.querySelector('audio'); return a ? a.src : ''; }"
            )
            if not audio_url:
                return Result(False, retryable=True, feedback="节点：验证码语音没有出现")
            async with http_client(timeout=30) as http:
                blob = (await http.get(audio_url, follow_redirects=True)).content
                code = await hear(http, base64.b64encode(blob).decode())
            if not code:
                return Result(False, retryable=True, feedback="节点：验证码识别失败")
            inputs = page.locator(_INPUT)
            await inputs.nth(0).click()
            await page.keyboard.type(code)
            await page.wait_for_timeout(600)
            try:
                await page.locator(_CONFIRM).click(force=True, timeout=3000)
            except Exception:
                return Result(False, retryable=True, feedback="节点：验证码提交失败")
            await page.wait_for_timeout(6000)
            body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            return jinfu_result(body)
        except Exception as exc:
            log.info("节点：金富领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：金富领取失败")
        finally:
            try:
                await page.close()
            except Exception:
                pass


plug("jinfu", "金富", "webapp", is_jinfu, claim_jinfu, 40, 120)
