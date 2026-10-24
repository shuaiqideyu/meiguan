"""金博娱乐网页红包。"""

import asyncio
import json
import logging
import uuid
from urllib.parse import parse_qs, urlparse

from telethon.errors import FloodWaitError, PeerFloodError

from meiguan.grab.browser import _FP_CONTEXT, _MOBILE_UA, _pages, close_sheet, drag_tencent_slider, join_channel, open_patchright_browser, slider_target, solve_tencent
from meiguan.grab.packets.fll import two_key
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.vision import warn_once
from meiguan.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meiguan.grab.catalog import plug
from meiguan.node_host import wallet_lock

log = logging.getLogger("meiguan.grab")

_BOTS = {"jbhongbao666_bot", "jbviphb1_bot", "tshongbao_bot"}
_SHORTS = {"jbhbpick", "hbpickup"}
_API = {
    "hb.jbyl882.com": "https://int.jbyl602.com",
}
_DEFAULT_API = "https://int.jbyl702.com"
_PAGE_WAIT = 20

_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

_SUBMIT = """
({url, fields}) => {
  const form = document.createElement('form');
  form.method = 'POST';
  form.enctype = 'multipart/form-data';
  form.action = url;
  for (const [key, value] of Object.entries(fields)) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = key;
    input.value = value == null ? '' : String(value);
    form.appendChild(input);
  }
  (document.body || document.documentElement).appendChild(form);
  form.submit();
}
"""

_CLICK_GET = """
() => {
  const els = [...document.querySelectorAll('*')];
  const b = els.find(e => (e.innerText || '').trim() === '领红包' && e.children.length === 0);
  if (b) { b.click(); return true; }
  return false;
}
"""

_INJECT_CAPTCHA = """
({ticket, randstr}) => {
  if (typeof window.onSubmit === 'function') {
    window.onSubmit({ret: 0, ticket, randstr});
    return true;
  }
  return false;
}
"""

_READY = """
(path) => {
  if (!location.pathname.includes(path)) return false;
  const text = document.body ? document.body.innerText : '';
  if (!text.includes('{')) return false;
  if (text.includes('aliyun_waf') || text.includes('alicfw')) return false;
  return text.includes('status') || text.includes('code');
}
"""




def is_jbyl(rp: RedPacket) -> bool:
    bot = (rp.bot or "").lower().lstrip("@")
    return bot in _BOTS


def web_init(url: str) -> str:
    parsed = urlparse(url or "")
    for part in (parsed.fragment, parsed.query):
        found = parse_qs(part.split("?", 1)[-1]).get("tgWebAppData", [""])[0]
        if found:
            return found
    return ""


def api_origin(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return _API.get(host, _DEFAULT_API)


def page_origin(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://hb.jb690.com"


def captcha_app(payload: dict) -> str:
    if str(payload.get("status")) != "1":
        return ""
    if str(payload.get("needyzm") or "0") != "1":
        return ""
    return str(payload.get("CaptchaAppId") or "")


def read_json(text: str) -> dict:
    start = (text or "").find("{")
    end = (text or "").rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def packet_ids(payload: dict) -> tuple[str, str]:
    redpack = payload.get("redpack") if isinstance(payload.get("redpack"), dict) else {}
    return str(payload.get("userid") or ""), str(redpack.get("id") or "")


def threshold(payload: dict) -> float:
    redpack = payload.get("redpack") if isinstance(payload.get("redpack"), dict) else {}
    try:
        return float(redpack.get("needmoney") or 0)
    except (TypeError, ValueError):
        return 0.0


def grabbed(payload: dict) -> str:
    for key in ("moneyDesc1", "money", "data"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def api_result(payload: dict, *, claim: bool) -> Result | None:
    status = str(payload.get("status") if payload.get("status") is not None else "")
    msg = " ".join(str(payload.get("msg") or "").split())
    if not claim:
        if status == "1":
            return None
        retry = status not in {"1001", "1004"} and not any(
            word in msg for word in ("校验通不过", "无效", "过期", "领完", "已领")
        )
        return Result(False, retryable=retry, feedback=f"节点：{msg or '金博没有打开'}"[:80])
    if status == "1":
        return Result(True, grabbed(payload) or None, False)
    # 1007 = 领取失败 = 没有次数了（一直抢红包不充值，次数没刷新）
    if status == "1007":
        return Result(False, retryable=True, feedback="节点：没有次数了，需充值刷新")
    retry = any(word in msg for word in ("验证", "校验", "稍", "频繁"))
    if any(word in msg for word in ("充值", "资格", "无效", "过期", "领完", "已领")):
        retry = False
    return Result(False, retryable=retry, feedback=f"节点：{msg or '金博领取失败'}"[:80])


def visitor_of(user_id: int) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"jbyl:{user_id}").hex


async def warm(page, origin: str) -> None:
    await page.goto(f"{origin}/getBotinfo", wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000)
    await page.wait_for_function(_READY, arg="getBotinfo", timeout=_PAGE_WAIT * 1000)


async def post_form(page, url: str, fields: dict, path: str) -> dict:
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000):
        await page.evaluate(_SUBMIT, {"url": url, "fields": fields})
    await page.wait_for_function(_READY, arg=path, timeout=_PAGE_WAIT * 1000)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return read_json(text)


async def claim_jbyl(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.short or not rp.start:
        return Result(False, retryable=False, feedback="节点：网页验证缺少入口")
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
            web = await app_url(client, chat, bot_peer, rp.short, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：金博打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        async with _pages:
            browser = await open_patchright_browser()
            if browser is None:
                return Result(False, retryable=False, feedback="节点：网页验证浏览器启动失败")
            page = await browser.new_page(viewport={"width": 420, "height": 780}, device_scale_factor=2,
                                          user_agent=_MOBILE_UA, is_mobile=True, has_touch=True, **_FP_CONTEXT)
            try:
                loop = asyncio.get_running_loop()
                check_fut: asyncio.Future = loop.create_future()
                hb_fut: asyncio.Future = loop.create_future()
                pre_fut: asyncio.Future = loop.create_future()

                async def on_res(res):
                    try:
                        if "checkInitDataForQb" in res.url and not check_fut.done():
                            check_fut.set_result(await res.text())
                        if "getHb" in res.url and not hb_fut.done():
                            hb_fut.set_result(await res.text())
                        if "cap_union_prehandle" in res.url and not pre_fut.done():
                            pre_fut.set_result(await res.text())
                    except Exception:
                        pass

                page.on("response", lambda r: asyncio.ensure_future(on_res(r)))
                await page.goto(web, wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000)
                try:
                    check_raw = await asyncio.wait_for(check_fut, timeout=_PAGE_WAIT)
                except asyncio.TimeoutError:
                    return Result(False, retryable=True, feedback="节点：金博初始化失败")
                opened = read_json(check_raw)
                early = api_result(opened, claim=False)
                if early is not None:
                    return early
                if str(opened.get("isget") or "0") == "1":
                    return Result(False, retryable=False, feedback="节点：已经领过")
                needmoney = threshold(opened)
                if needmoney > 0:
                    return Result(False, retryable=False, feedback=f"节点：需当日充值 {needmoney:g}")
                # 动态验证码：needyzm==1 且 CaptchaAppId 存在时，前端弹腾讯滑块（新版 TJNCaptcha）
                # 打码平台解不了，走前端真实拖动：NCC 匹配缺口 + 真实轨迹拖动
                app_id = captcha_app(opened)
                if app_id:
                    try:
                        pre_raw = await asyncio.wait_for(pre_fut, timeout=_PAGE_WAIT)
                    except asyncio.TimeoutError:
                        return Result(False, retryable=True, feedback="节点：滑块初始化失败")
                    target = slider_target(pre_raw, "https://hb.tsyl689.com/")
                    if target <= 0:
                        return Result(False, retryable=True, feedback="节点：滑块缺口识别失败")
                    await page.evaluate(
                        "() => { const b = document.querySelector('.tencent-captcha-dy__robot-box');"
                        " if (b) b.click(); }"
                    )
                    await asyncio.sleep(1.5)
                    if not await drag_tencent_slider(page, target):
                        return Result(False, retryable=True, feedback="节点：滑块拖动失败")
                    await asyncio.sleep(2.0)
                # 等按钮从灰色变可点（.get-btn），再立即点，避免点太早无效、点太晚超时
                try:
                    await page.wait_for_selector(".get-btn", timeout=5000)
                except Exception:
                    pass
                clicked = await page.evaluate(_CLICK_GET)
                if not clicked:
                    return Result(False, retryable=True, feedback="节点：领取按钮没有出现")
                try:
                    hb_raw = await asyncio.wait_for(hb_fut, timeout=_PAGE_WAIT)
                except asyncio.TimeoutError:
                    return Result(False, retryable=True, feedback="节点：金博没有回执")
                done = api_result(read_json(hb_raw), claim=True)
                if done is None:
                    return Result(False, retryable=True, feedback="节点：金博没有回执")
                return done
            finally:
                await close_sheet(page=page)


def _self_check() -> None:
    assert captcha_app({"status": "1", "needyzm": "1", "CaptchaAppId": "9"}) == "9"
    assert captcha_app({"status": "1", "needyzm": 0, "CaptchaAppId": "9"}) == ""
    assert web_init("https://hb.jb690.com/#/robPacket?tgWebAppData=user%3D1%26hash%3Dab") == "user=1&hash=ab"
    missed = api_result({"status": 1001, "msg": "数据校验通不过！"}, claim=False)
    assert missed is not None and not missed.ok and not missed.retryable
    assert api_result({"status": "1", "needyzm": 0}, claim=False) is None
    got = api_result({"status": "1", "moneyDesc1": "1.2 RMB"}, claim=True)
    assert got is not None and got.ok and got.amount == "1.2 RMB"


_self_check()

plug("jbyl", "金博娱乐", "webapp", is_jbyl, claim_jbyl, 10, 150)
