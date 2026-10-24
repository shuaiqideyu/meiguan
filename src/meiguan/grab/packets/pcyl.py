"""U投红包。"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from urllib.parse import parse_qs

from meiguan.grab.httpclient import http_client
from curl_cffi.requests.exceptions import Timeout
from telethon.errors import FloodWaitError, PeerFloodError

from meiguan.grab.browser import join_channel, playwright_driver
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.vision import warn_once
from meiguan.grab.webapp import account_premium, app_url, bot_peers, message_peer
from meiguan.grab.packets.yypay import yy_init
from meiguan.grab.catalog import plug
from meiguan.node_host import wallet_lock

log = logging.getLogger("meiguan.grab")

_BOT = "pcyl"
_ORIGIN = "https://rp.utou.win"
_API = _ORIGIN + "/api/Telegram/Bot"
_PAGE = _ORIGIN + "/red-packet"
_KEY = "qrjfk7NdGWVJfP1klQ7Y"
_LOADER = f"https://fpnpmcdn.net/v3/{_KEY}/loader_v3.12.13.js"
_ID = re.compile(r"^red_([A-Z0-9]{6,20})$")
_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
_HTTP_WAIT = 15
_PAGE_WAIT = 20
_FP_WAIT = 15

_STEALTH = """
(() => {
  const define = (obj, key, get) => {
    try { Object.defineProperty(obj, key, { get }); } catch (e) {}
  };
  define(navigator, "webdriver", () => undefined);
  define(navigator, "plugins", () => [1, 2, 3, 4, 5]);
  define(navigator, "languages", () => ["zh-CN", "zh", "en"]);
  window.chrome = window.chrome || { runtime: {} };
})();
"""

_FP_JS = """
async () => {
  const key = %r;
  const src = %r;
  const name = "__fpjs_p_l_b";
  if (!window.__meiguanFp) {
    if (typeof window[name]?.load !== "function") {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error("loader"));
        document.documentElement.appendChild(script);
      });
    }
    const loader = window[name];
    if (!loader || typeof loader.load !== "function") return null;
    window.__meiguanFp = await loader.load({ apiKey: key });
  }
  const got = await window.__meiguanFp.get();
  if (!got || !got.visitorId || !got.requestId) return null;
  return { visitorId: String(got.visitorId), requestId: String(got.requestId) };
}
""" % (_KEY, _LOADER)

_fp_lock = asyncio.Lock()
_fp_browser = None
_fp_context = None
_fp_page = None
_xvfb = None


def packet_id(start: str | None) -> str:
    matched = _ID.fullmatch((start or "").strip())
    if not matched:
        return ""
    return matched.group(1)


def is_pcyl(rp: RedPacket) -> bool:
    bot = (rp.bot or "").lower().lstrip("@")
    return bot == _BOT and bool(packet_id(rp.start))


def in_chat(init_data: str) -> bool:
    kind = (parse_qs(init_data or "").get("chat_type") or [""])[0]
    return kind in {"group", "supergroup", "channel"}


def shown_amount(amount, currency) -> str:
    if isinstance(amount, bool) or amount is None:
        return ""
    if isinstance(amount, float):
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            return ""
        if not value.is_finite():
            return ""
        text = format(value.quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")
        text = text.rstrip("0").rstrip(".")
    elif isinstance(amount, int):
        text = str(amount)
    else:
        text = str(amount).strip()
    if not text:
        return ""
    unit = str(currency or "").strip().upper()
    if unit == "U":
        unit = "USDT"
    return f"{text} {unit}".strip()


def body_of(resp) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def message_of(payload: dict) -> str:
    return str(payload.get("message") or payload.get("Message") or "").strip()


def reidentify(payload: dict) -> bool:
    errors = payload.get("errors")
    return isinstance(errors, dict) and bool(errors.get("Reidentify"))


def headers(init_data: str, visitor: str = "", request_id: str = "") -> dict[str, str]:
    found = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "X-Telegram-Init-Data": init_data,
    }
    if visitor:
        found["X-Fingerprint"] = visitor
    if request_id:
        found["X-Fp-Request-Id"] = request_id
    return found


def detail_result(payload: dict, status: int) -> tuple[dict | None, Result | None]:
    if status == 401 or payload.get("register_url"):
        return None, Result(False, retryable=False, feedback="节点：请先注册")
    if status == 503 or payload.get("code") == 503:
        text = message_of(payload) or "系统正在维护"
        return None, Result(False, retryable=True, feedback=f"节点：{text}"[:80])
    if payload.get("success") is False or status >= 400:
        text = message_of(payload) or "红包不存在"
        return None, Result(False, retryable=status >= 500, feedback=f"节点：{text}"[:80])
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    mine = data.get("MyClaim")
    if isinstance(mine, dict):
        amount = shown_amount(mine.get("Amount"), data.get("CurrencyId") or mine.get("CurrencyId"))
        return None, Result(False, amount, False, "已领取过")
    status_name = str(data.get("Status") or "").lower()
    if data.get("IsExpired") or status_name in {"expired", "completed"}:
        return None, Result(False, retryable=False, feedback="节点：红包已结束")
    try:
        remain = int(data.get("RemainCount"))
    except (TypeError, ValueError):
        remain = None
    if remain is not None and remain <= 0:
        return None, Result(False, retryable=False, feedback="节点：红包已抢完")
    return data, None


def claim_result(payload: dict, status: int) -> Result:
    if reidentify(payload):
        return Result(False, retryable=True, feedback="节点：指纹未通过")
    if status == 401 or payload.get("register_url"):
        return Result(False, retryable=False, feedback="节点：请先注册")
    if status == 503 or payload.get("code") == 503:
        text = message_of(payload) or "系统正在维护"
        return Result(False, retryable=True, feedback=f"节点：{text}"[:80])
    if payload.get("success") is True:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        amount = shown_amount(data.get("Amount"), data.get("CurrencyId"))
        return Result(True, amount, False, amount or "已领取")
    text = message_of(payload) or "领取失败"
    retry = status >= 500 or any(word in text for word in ("稍后", "频繁", "维护"))
    if any(word in text for word in ("不存在", "已领", "抢完", "领完", "结束", "过期", "注册")):
        retry = False
    return Result(False, retryable=retry, feedback=f"节点：{text}"[:80])


async def ensure_display() -> bool:
    if os.environ.get("DISPLAY"):
        return True
    if not sys.platform.startswith("linux"):
        return True
    if os.path.exists("/tmp/.X99-lock"):
        os.environ["DISPLAY"] = ":99"
        return True
    if shutil.which("Xvfb") is None:
        return False
    global _xvfb
    if _xvfb is None or _xvfb.poll() is not None:
        _xvfb = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    await asyncio.sleep(0.4)
    if _xvfb.poll() is not None and not os.path.exists("/tmp/.X99-lock"):
        return False
    os.environ["DISPLAY"] = ":99"
    return True


async def skip_bundle(route) -> None:
    if "/build/assets/main-" in route.request.url:
        await route.abort()
        return
    await route.continue_()


async def drop_fp() -> None:
    global _fp_browser, _fp_context, _fp_page
    page, context, browser = _fp_page, _fp_context, _fp_browser
    _fp_page = None
    _fp_context = None
    _fp_browser = None
    for item in (page, context, browser):
        if item is None:
            continue
        try:
            await item.close()
        except Exception:
            pass


_fp_uses = 0


async def fp_page():
    global _fp_browser, _fp_context, _fp_page, _fp_uses
    _fp_uses += 1
    if _fp_uses >= 20:
        _fp_uses = 0
        await drop_fp()
    if _fp_page is not None and not _fp_page.is_closed():
        return _fp_page
    await drop_fp()
    if not await ensure_display():
        warn_once("pcyl-display", "节点：U投指纹浏览器没有显示器")
        return None
    driver = await playwright_driver()
    if driver is None:
        return None
    try:
        _fp_browser = await driver.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        _fp_context = await _fp_browser.new_context(
            user_agent=_UA,
            viewport={"width": 412, "height": 915},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
        )
        await _fp_context.route("**/*", skip_bundle)
        _fp_page = await _fp_context.new_page()
        _fp_page.set_default_timeout(_FP_WAIT * 1000)
        await _fp_page.goto(_PAGE, wait_until="domcontentloaded", timeout=_PAGE_WAIT * 1000)
    except Exception as exc:
        log.info("节点：U投指纹浏览器启动失败 %s", type(exc).__name__)
        await drop_fp()
        return None
    return _fp_page


async def fingerprint() -> tuple[str, str]:
    async with _fp_lock:
        page = await fp_page()
        if page is None:
            return "", ""
        try:
            got = await page.evaluate(_FP_JS)
        except Exception as exc:
            log.info("节点：U投指纹失败 %s", type(exc).__name__)
            await drop_fp()
            return "", ""
        if not isinstance(got, dict):
            return "", ""
        return str(got.get("visitorId") or ""), str(got.get("requestId") or "")


async def claim_pcyl(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    found = packet_id(rp.start)
    if not found or not rp.short:
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot or _BOT)
            web = await app_url(client, chat, bot_peer, rp.short, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：U投打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        init_data = yy_init(web)
        if not init_data or not in_chat(init_data):
            return Result(False, retryable=False, feedback="节点：要在窗口里打开")
        try:
            async with http_client(timeout=_HTTP_WAIT) as http:
                loaded = await http.get(
                    _API + "/RedPacket/Detail",
                    params={"Id": found},
                    headers=headers(init_data),
                )
                _data, early = detail_result(body_of(loaded), loaded.status_code)
                if early is not None:
                    return early
                result = Result(False, retryable=False, feedback="节点：指纹没有生成")
                for _ in range(2):
                    visitor, request_id = await fingerprint()
                    if not visitor or not request_id:
                        result = Result(False, retryable=False, feedback="节点：指纹没有生成")
                        continue
                    claimed = await http.post(
                        _API + "/RedPacket/Claim",
                        json={"RedPacketId": found},
                        headers=headers(init_data, visitor, request_id),
                    )
                    result = claim_result(body_of(claimed), claimed.status_code)
                    if result.feedback != "节点：指纹未通过":
                        return result
                result.retryable = False
                return result
        except Timeout:
            return Result(False, retryable=True, feedback="节点：领取超时")
        except Exception as exc:
            log.info("节点：U投领取失败 %s", type(exc).__name__)
            return Result(False, retryable=True, feedback="节点：领取失败")

plug("pcyl", "U投", "webapp", is_pcyl, claim_pcyl, 60, 90)
