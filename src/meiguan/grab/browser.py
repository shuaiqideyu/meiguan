"""网页验证。"""

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest

from meiguan.grab.detect import follow_names
from meiguan.grab.vision import vision_chat, vision_settings, warn_once

log = logging.getLogger("meiguan.grab")

_INJECT_JS = """
() => {
  document.querySelectorAll('.char-box').forEach(b => {
    const i = parseInt(b.getAttribute('data-index'), 10) + 1;
    if (b.querySelector('.ai-idx')) return;
    const d = document.createElement('div');
    d.className = 'ai-idx';
    d.textContent = i;
    d.style.cssText = 'position:absolute;top:0;left:0;background:#0040ff;color:#fff;font-size:16px;font-weight:900;padding:1px 4px;border-radius:4px;z-index:99999;line-height:1.3;font-family:Arial,sans-serif;';
    b.style.position = 'relative';
    b.appendChild(d);
  });
}
"""


_PAGE_PROMPT = (
    "验证码图片分两区：\n"
    "上区：3个清晰目标汉字，红色标注01/02/03表示点击顺序。\n"
    "下区：12个格子（蓝色角标编号1-12），每格含一个手写汉字+黑色干扰直线。\n"
    "任务：对每个目标汉字，在12格中找同一个字。只比较汉字笔画，忽略黑色直线。\n"
    "找到后读取该格左上角的蓝色编号。\n"
    '只返回JSON：{"order":["01字","02字","03字"],"clicks":[编号,编号,编号]}'
)


_KPAY_PROMPT = (
    "上方三个目标字符按 01、02、03 的顺序排列。\n"
    "下方候选格左上角有蓝色编号。\n"
    "按 01、02、03 的顺序，选出字形相同的候选编号。\n"
    '只返回JSON：{"clicks":[编号,编号,编号]}'
)


_KPAY_PROXY = """
window.TelegramWebviewProxy = {
  postEvent(eventType, eventData) {
    if (eventType === 'web_app_data_send' && window.__meiguanSend) {
      window.__meiguanSend(String(eventData || ''));
    }
  }
};
"""


# 2captcha 不打开页面。脚本域名少了 ca. 会解不出。
_CAPTCHA_SCRIPT = "https://ca.turing.captcha.qcloud.com/TJNCaptcha-global.js"
# 扫雷红包这个 AppId 用旧脚本。TJN 会返回解不出。
_REDPACK_SCRIPT = "https://ca.turing.captcha.qcloud.com/TCaptcha.js"
_CAPTCHA_WAIT = 90


_CLAIM_WATCH = """
(() => {
  if (window.__meiguanClaimWatch) return;
  window.__meiguanClaimWatch = true;
  window.__meiguanClaim = "";
  const keep = (text) => {
    try {
      const data = JSON.parse(text);
      const body = data && typeof data.data === "object" && data.data ? data.data : {};
      const isNumber = typeof data.data === "number";
      window.__meiguanClaim = JSON.stringify({
        code: data.code,
        success: body.success !== undefined ? body.success : (isNumber && data.code === 200 ? true : null),
        amount: body.amount !== undefined ? body.amount : (isNumber ? data.data : null),
        message: String(body.message || data.msg || ""),
      });
    } catch (e) {}
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__meiguanUrl = String(url || "");
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener("load", function() {
      if (String(this.__meiguanUrl || "").includes("redWars")) keep(String(this.responseText || ""));
    });
    return origSend.apply(this, arguments);
  };
  if (window.fetch) {
    const origFetch = window.fetch;
    window.fetch = function(input) {
      const url = String((input && input.url) || input || "");
      return origFetch.apply(this, arguments).then((res) => {
        if (url.includes("redWars")) res.clone().text().then(keep).catch(() => {});
        return res;
      });
    };
  }
})();
"""


_CAPTCHA_HOOK = """
(() => {
  if (window.__meiguanCaptchaHook) return;
  window.__meiguanCaptchaHook = true;
  let installed = null;
  const wrap = function(arg0, arg1, arg2) {
    let appId = arg1;
    let cb = arg2;
    if (typeof arg0 === 'string') {
      appId = arg0;
      cb = arg1;
    }
    window.__meiguanCaptcha = {appId: String(appId || ''), cb};
    return {show() {}, destroy() {}};
  };
  try {
    Object.defineProperty(window, 'TencentCaptcha', {
      configurable: true,
      enumerable: true,
      get() { return installed ? wrap : undefined; },
      set(value) { installed = value; },
    });
  } catch (e) {}
})();
"""


_browser = None


_playwright = None


_browser_lock = asyncio.Lock()


_playwright_lock = asyncio.Lock()


_page_count = 0


class _PageGate:
    async def __aenter__(self):
        global _page_count
        _page_count += 1
        return self

    async def __aexit__(self, *_args):
        global _page_count
        _page_count = max(0, _page_count - 1)
        if _page_count == 0:
            await drop_browser()
            await drop_patchright()
        return False


_pages = _PageGate()


async def drop_browser() -> None:
    global _browser
    browser = _browser
    _browser = None
    if browser is None:
        return
    try:
        await browser.close()
    except Exception:
        pass


async def drop_patchright() -> None:
    global _patchright_browser
    browser = _patchright_browser
    _patchright_browser = None
    if browser is None:
        return
    try:
        await browser.close()
    except Exception:
        pass


async def close_sheet(page=None, context=None, *, patch: bool = False) -> None:
    del patch
    for item in (page, context):
        if item is None:
            continue
        try:
            await item.close()
        except Exception:
            pass


_patchright_browser = None


_patchright_driver = None


_patchright_lock = asyncio.Lock()


def parse_clicks(text: str, limit: int = 12) -> list[int] | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    matched = re.search(r"\{.*\}", text, re.DOTALL)
    if not matched:
        return None
    try:
        data = json.loads(matched.group())
    except json.JSONDecodeError:
        return None
    clicks = data.get("clicks")
    if not isinstance(clicks, list) or len(clicks) != 3:
        return None
    try:
        numbers = [int(item) for item in clicks]
    except (TypeError, ValueError):
        return None
    if any(number < 1 or number > limit for number in numbers):
        return None
    if len(set(numbers)) != 3:
        return None
    return [number - 1 for number in numbers]


def web_data(raw: str) -> str:
    try:
        parsed = json.loads(raw or "")
    except json.JSONDecodeError:
        return raw or ""
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), str):
        return parsed["data"]
    return raw or ""


async def playwright_driver():
    global _playwright
    if _playwright is not None:
        return _playwright
    async with _playwright_lock:
        if _playwright is not None:
            return _playwright
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            warn_once("browser-import", "节点：网页验证未安装浏览器组件")
            return None
        try:
            _playwright = await async_playwright().start()
        except Exception:
            warn_once("browser-start", "节点：网页验证浏览器启动失败")
            return None
        return _playwright


async def open_browser():
    global _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    driver = await playwright_driver()
    if driver is None:
        return None
    async with _browser_lock:
        if _browser is not None and not _browser.is_connected():
            _browser = None
        if _browser is not None:
            return _browser
        try:
            kwargs = {
                "headless": os.environ.get("HEADLESS", "true").lower() != "false",
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            }
            chrome = os.environ.get("CHROME_PATH", "").strip()
            if chrome:
                kwargs["executable_path"] = chrome
            _browser = await driver.chromium.launch(**kwargs)
        except Exception:
            warn_once("browser-start", "节点：网页验证浏览器启动失败")
            return None
        return _browser


async def patchright_driver():
    global _patchright_driver
    if _patchright_driver is not None:
        return _patchright_driver
    async with _patchright_lock:
        if _patchright_driver is not None:
            return _patchright_driver
        try:
            from patchright.async_api import async_playwright
        except ImportError:
            warn_once("patchright-import", "节点：未安装 patchright")
            return None
        try:
            _patchright_driver = await async_playwright().start()
        except Exception:
            warn_once("patchright-start", "节点：patchright 启动失败")
            return None
        return _patchright_driver


async def open_patchright_browser():
    global _patchright_browser
    if _patchright_browser is not None and _patchright_browser.is_connected():
        return _patchright_browser
    driver = await patchright_driver()
    if driver is None:
        return None
    async with _patchright_lock:
        if _patchright_browser is not None and not _patchright_browser.is_connected():
            _patchright_browser = None
        if _patchright_browser is not None:
            return _patchright_browser
        try:
            _patchright_browser = await driver.chromium.launch(
                headless=os.environ.get("HEADLESS", "true").lower() != "false",
            )
        except Exception:
            warn_once("patchright-start", "节点：patchright 浏览器启动失败")
            return None
        return _patchright_browser


async def box_layout(page) -> str:
    return await page.evaluate(
        """() => [...document.querySelectorAll('.char-box')].map(el => {
          const r = el.getBoundingClientRect();
          return el.getAttribute('data-index') + '@' + Math.round(r.x) + ',' + Math.round(r.y);
        }).join(' ')"""
    )


async def wait_settled(page) -> None:
    last = None
    stable = 0
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        now = await box_layout(page)
        if now and now == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
            last = now
        await page.wait_for_timeout(150)


async def click_box(page, index: int) -> bool:
    await wait_settled(page)
    try:
        await page.locator(f'.char-box[data-index="{index}"]').click(timeout=3000)
        return True
    except Exception as exc:
        log.info("节点：网页验证点击失败 %s %s", index, type(exc).__name__)
        return False


async def page_kind(page) -> str:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        kind = await page.evaluate(
            """() => {
              if (document.querySelector('.char-box')) return 'grid';
              if (document.querySelector('.choice-button')) return 'kpay';
              return '';
            }"""
        )
        if kind:
            return kind
        await page.wait_for_timeout(200)
    return ""


def watch_verify(page, note: dict) -> None:
    async def on_response(response):
        if "/envelope/verification/" not in response.url:
            return
        try:
            data = await response.json()
        except Exception:
            return
        message = " ".join(str(data.get("message") or "").split())[:80]
        if "/verification/init" in response.url and not data.get("success"):
            note["init"] = message or "验证码生成失败"
        elif "/verification/validate" in response.url:
            note["validate"] = bool(data.get("success"))
            note["validate_text"] = message

    page.on("response", on_response)


async def solve_kpay(page, model: str, sent: dict) -> bool:
    try:
        await page.wait_for_function(
            """() => {
              const imgs = [...document.querySelectorAll('.target-char-image img, .choice-button img')];
              return imgs.length >= 4 && imgs.every(img => img.complete && img.naturalWidth > 0);
            }""",
            timeout=8000,
        )
    except Exception:
        log.info("节点：网页验证页面未打开")
        return False
    count = await page.evaluate(
        """() => {
          let n = 0;
          document.querySelectorAll('.choice-button').forEach((b) => {
            n += 1;
            if (b.querySelector('.ai-idx')) return;
            const d = document.createElement('div');
            d.className = 'ai-idx';
            d.textContent = String(n);
            d.style.cssText = 'position:absolute;top:0;left:0;background:#0040ff;color:#fff;font-size:16px;font-weight:900;padding:1px 4px;border-radius:4px;z-index:99999;line-height:1.3;font-family:Arial,sans-serif;pointer-events:none;';
            b.style.position = 'relative';
            b.appendChild(d);
          });
          return n;
        }"""
    )
    if count < 3:
        log.info("节点：网页验证页面未打开")
        return False
    shot = await page.locator(".app-container").screenshot(type="png")
    clicks = parse_clicks(await vision_chat(shot, _KPAY_PROMPT, "image/png", model, 80), count)
    if not clicks:
        log.info("节点：网页验证未认出图片")
        return False
    log.info("节点：网页验证点击 %s", ",".join(str(index + 1) for index in clicks))
    for index in clicks:
        try:
            await page.locator(".choice-button").nth(index).click(timeout=3000)
        except Exception as exc:
            log.info("节点：网页验证点击失败 %s %s", index, type(exc).__name__)
            return False
    try:
        await page.locator(".submit-button").click(timeout=3000)
    except Exception as exc:
        log.info("节点：网页验证点击失败 %s", type(exc).__name__)
        return False
    deadline = time.monotonic() + 8
    validated = None
    while time.monotonic() < deadline:
        if sent.get("ok"):
            return True
        if sent.get("fail"):
            return False
        if sent.get("validate") is False:
            log.info("节点：网页验证未通过 %s", sent.get("validate_text") or "失败")
            return False
        if sent.get("validate") is True and validated is None:
            validated = time.monotonic()
        if validated is not None and time.monotonic() - validated > 3:
            if not sent.get("ok"):
                log.info("节点：网页验证回传没有发出")
            return True
        await page.wait_for_timeout(200)
    log.info("节点：网页验证没有结果")
    return False


async def open_sheet(browser, **extra):
    fields = {
        "viewport": {"width": 420, "height": 780},
        "device_scale_factor": 2,
        **_FP_CONTEXT,
        **extra,
    }
    context = await browser.new_context(**fields)
    page = await context.new_page()
    return context, page


async def solve_page(url: str, model: str, on_data=None) -> bool:
    if not vision_settings()[0]:
        warn_once("web-key", "节点：网页验证未配置视觉识别")
        return False
    async with _pages:
        browser = await open_browser()
        if browser is None:
            return False
        context, page = await open_sheet(browser)
        sent = {}
        try:
            if on_data is not None or "kpaye.com" in (url or ""):
                async def incoming(raw: str) -> None:
                    payload = web_data(raw)
                    if not payload or on_data is None:
                        return
                    try:
                        await on_data(payload)
                    except Exception as exc:
                        log.info("Telegram：网页验证回传失败 %s", type(exc).__name__)
                        sent["fail"] = True
                        return
                    sent["ok"] = True

                await page.expose_function("__meiguanSend", incoming)
                await page.add_init_script(_KPAY_PROXY)
            watch_verify(page, sent)
            await page.goto(url, wait_until="domcontentloaded", timeout=10000)
            kind = await page_kind(page)
            if kind == "kpay":
                return await solve_kpay(page, model, sent)
            if kind != "grid":
                if sent.get("init"):
                    log.info("节点：网页验证页面未打开 %s", sent["init"])
                else:
                    log.info("节点：网页验证页面未打开")
                return False
            await page.evaluate(_INJECT_JS)
            shot = await page.locator(".captcha-containers").screenshot(type="png")
            clicks = parse_clicks(await vision_chat(shot, _PAGE_PROMPT, "image/png", model, 200))
            if not clicks:
                log.info("节点：网页验证未认出图片")
                return False
            log.info("节点：网页验证点击 %s", ",".join(str(index + 1) for index in clicks))
            for index in clicks:
                if not await click_box(page, index):
                    return False
            await wait_settled(page)
            try:
                await page.locator("#submitBtn").click(timeout=3000)
            except Exception:
                await page.get_by_role("button", name="验证").click(timeout=3000)
            try:
                await page.wait_for_function(
                    """() => {
                      const t = document.body ? document.body.innerText : '';
                      return t.includes('验证失败') || t.includes('验证成功') || t.includes('正确');
                    }""",
                    timeout=6000,
                )
            except Exception:
                log.info("节点：网页验证没有结果")
                return False
            shown = " ".join((await page.evaluate("() => document.body.innerText") or "").split())
            log.info("节点：网页验证页面 %s", shown[:80])
            return "验证失败" not in shown and ("验证成功" in shown or "正确" in shown)
        except Exception as exc:
            log.info("节点：网页验证失败 %s", type(exc).__name__)
            return False
        finally:
            await close_sheet(page, context)


_SHAPE_PROMPT = (
    "右上角小方格是目标图案。下面格子左上角有蓝色编号，从左到右、从上到下。\n"
    "选出和目标形状相同的一格。颜色、朝向可以不同，只看形状。不要选目标小方格本身。\n"
    "只回答编号数字。"
)


_APP_MARK = """
() => {
  const buttons = [...document.querySelectorAll('button')].filter(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 80 && r.width <= 180 && r.height >= 80 && r.height <= 180;
  });
  buttons.sort((a, b) => {
    const ar = a.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    return ar.y - br.y || ar.x - br.x;
  });
  buttons.forEach((el, i) => {
    el.querySelectorAll('.ai-idx').forEach(node => node.remove());
    if (!el.style.position) el.style.position = 'relative';
    const d = document.createElement('div');
    d.className = 'ai-idx';
    d.textContent = String(i + 1);
    d.style.cssText = 'position:absolute;top:0;left:0;background:#0040ff;color:#fff;font:900 18px Arial,sans-serif;padding:1px 5px;z-index:99999;line-height:1.2;';
    el.appendChild(d);
  });
  return buttons.map(el => {
    const r = el.getBoundingClientRect();
    return {x: r.x + r.width / 2, y: r.y + r.height / 2};
  });
}
"""


def parse_cell(text: str, count: int) -> int | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    matched = re.search(r"\d+", text or "")
    if not matched:
        return None
    number = int(matched.group())
    if 1 <= number <= count:
        return number
    return None


async def solve_shapes(page) -> None:
    if not vision_settings()[0]:
        warn_once("web-key", "节点：小程序验证未配置视觉识别")
        return
    for _ in range(3):
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if "点选相同" not in (body or ""):
            return
        step = re.search(r"第\s*(\d+)", body or "")
        current = step.group(1) if step else ""
        centers = await page.evaluate(_APP_MARK)
        if not centers:
            log.info("节点：小程序验证没有格子")
            return
        cell = parse_cell(
            await vision_chat(await page.screenshot(type="png"), _SHAPE_PROMPT, "image/png", "", 16),
            len(centers),
        )
        if cell is None:
            log.info("节点：小程序验证未认出图案")
            return
        point = centers[cell - 1]
        await page.mouse.click(point["x"], point["y"])
        try:
            await page.wait_for_function(
                """(stepNo) => {
                  const t = document.body ? document.body.innerText : '';
                  if (!t.includes('点选相同')) return true;
                  const matched = t.match(/第\\s*(\\d+)/);
                  return Boolean(matched) && matched[1] !== String(stepNo);
                }""",
                arg=current,
                timeout=8000,
            )
        except Exception:
            return


async def join_channel(client, name: str) -> None:
    from meiguan.reports import entity_watched, hits

    if hits(username=name):
        return
    try:
        entity = await client.get_entity(name)
    except Exception:
        entity = None
    if entity_watched(entity):
        return
    try:
        await client(JoinChannelRequest(entity or name))
        log.info("节点：已关注 %s", name)
    except UserAlreadyParticipantError:
        return
    except Exception as exc:
        log.info("节点：关注失败 %s %s", name, type(exc).__name__)


async def follow_box(page):
    return await page.evaluate(
        """() => {
          const words = ['去关注', '关注频道', '立即关注', '关注'];
          const nodes = [...document.querySelectorAll('a, button, [role="button"], div, span')];
          let best = null;
          let bestLen = 99;
          for (const el of nodes) {
            const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent || '').join('').replace(/\\s+/g, '');
            if (!words.includes(text) || text.length >= bestLen) continue;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const link = el.closest('a');
            best = {x: r.x + r.width / 2, y: r.y + r.height / 2, href: (link && link.href) || ''};
            bestLen = text.length;
          }
          return best;
        }"""
    )


def channel_in(url: str) -> str:
    matched = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,})", url or "")
    if not matched:
        return ""
    name = matched.group(1)
    if name.lower() in {"share", "joinchat", "addstickers"}:
        return ""
    return name


async def follow_then_back(page, client, url: str) -> None:
    body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    if "关注" not in (body or ""):
        return
    names = follow_names(body)
    box = await follow_box(page)
    href = (box or {}).get("href") or ""
    extra = channel_in(href)
    if extra:
        names.append(extra)
    for name in dict.fromkeys(names):
        if client is not None:
            await join_channel(client, name)
    if not box:
        return
    try:
        async with page.expect_popup(timeout=2500) as waiting:
            await page.mouse.click(box["x"], box["y"])
        popup = await waiting.value
        popped = channel_in(popup.url or "")
        await popup.close()
        if popped and client is not None:
            await join_channel(client, popped)
    except Exception:
        try:
            await page.mouse.click(box["x"], box["y"])
        except Exception:
            pass
    if "关注" in (page.url or "") or page.url != url:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
    await page.wait_for_timeout(800)


_APP_BOOT = """
() => {
  const hash = new URLSearchParams((location.hash || '').replace(/^#/, ''));
  const query = new URLSearchParams(location.search || '');
  const initData = hash.get('tgWebAppData') || query.get('tgWebAppData') || '';
  const startParam = query.get('tgWebAppStartParam') || hash.get('tgWebAppStartParam') || hash.get('start_param') || '';
  const unsafe = {start_param: startParam};
  try {
    const user = new URLSearchParams(initData).get('user');
    if (user) unsafe.user = JSON.parse(user);
  } catch (e) {}
  const app = {
    initData,
    initDataUnsafe: unsafe,
    startParam,
    platform: 'android',
    version: '7.10',
    colorScheme: 'light',
    ready() {},
    expand() {},
    close() {},
    enableClosingConfirmation() {},
    disableClosingConfirmation() {},
    setHeaderColor() {},
    setBackgroundColor() {},
    MainButton: { show() {}, hide() {}, setText() {}, onClick() {}, offClick() {} },
    BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
    HapticFeedback: { impactOccurred() {}, notificationOccurred() {}, selectionChanged() {} },
  };
  window.Telegram = window.Telegram || {};
  window.Telegram.WebApp = Object.assign(window.Telegram.WebApp || {}, app);
}
"""


_STATIC = (".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".woff", ".woff2", ".ico")


def api_path(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path or ""
    host = (parsed.netloc or "").lower()
    if not host or "cloudflare.com" in host or "challenge-platform" in path:
        return ""
    if path.lower().endswith(_STATIC):
        return ""
    if path in ("", "/"):
        return parsed.netloc
    return f"{parsed.netloc}{path}"[:140]


_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.200 Mobile Safari/537.36"
)


_FP_CONTEXT = {
    "locale": "zh-CN",
    "timezone_id": "Asia/Shanghai",
    "color_scheme": "light",
}


async def gateway_auth(url: str, host: str, wait: float = 40) -> str:
    if not url:
        return ""
    found = ""
    async with _pages:
        browser = await open_browser()
        if browser is None:
            return ""
        context = await browser.new_context(
            viewport={"width": 420, "height": 780},
            user_agent=_MOBILE_UA,
            is_mobile=True,
            has_touch=True,
            **_FP_CONTEXT,
        )
        page = await context.new_page()
        try:
            await page.add_init_script(_APP_BOOT)

            def remember(request) -> None:
                nonlocal found
                if found:
                    return
                netloc = (urlparse(request.url).netloc or "").lower()
                if host not in netloc:
                    return
                auth = request.headers.get("authorization") or ""
                if len(auth) < 8 or auth.lower() in ("null", "undefined"):
                    return
                found = auth

            page.on("request", remember)
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and not found:
                await page.wait_for_timeout(500)
                if found:
                    break
                try:
                    live = await page.evaluate(
                        """() => {
                          const net = window.app && window.app.net;
                          if (!net) return '';
                          const bags = [net._headers, net._httpService && net._httpService._headers];
                          for (const bag of bags) {
                            const auth = bag && bag.Authorization;
                            if (auth && String(auth).length >= 8) return String(auth);
                          }
                          return '';
                        }"""
                    )
                except Exception:
                    live = ""
                if live and live.lower() not in ("null", "undefined"):
                    found = live
            return found
        except Exception as exc:
            log.info("节点：小程序页面失败 %s", type(exc).__name__)
            return ""
        finally:
            await close_sheet(context=context)


def captcha_key() -> str:
    from meiguan.grab.solver import solver_key

    return solver_key("fll")


def captcha_script(srcs: list[str]) -> str:
    chosen = ""
    for src in srcs or []:
        if "TJNCaptcha" not in src and "TCaptcha" not in src:
            continue
        if "ca.turing.captcha.qcloud.com" in src:
            return src
        if src.startswith("https://turing.captcha.qcloud.com/"):
            chosen = "https://ca." + src[len("https://"):]
    return chosen or _CAPTCHA_SCRIPT


def redpack_step(text: str) -> str:
    flat = "".join((text or "").split())
    if "恭喜您获得" in flat or "恭喜获得" in flat:
        return "got"
    if "暂不可领取" in flat:
        return "stop"
    if "立即解锁" in flat:
        return "unlock"
    if "可领取" in flat:
        return "claim"
    return "stop"


def claim_text(raw: str) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    message = " ".join(str(data.get("message") or "").split())
    if data.get("success") is True:
        amount = data.get("amount")
        got = f"{amount} USDT" if amount not in (None, "") else ""
        return " ".join(part for part in ("恭喜您获得红包", got) if part)
    if any(word in message for word in ("验证", "校验", "滑块")):
        return "节点：滑块打码失败"
    return message


def redpack_page(url: str) -> bool:
    return "tgbet" in (urlparse(url or "").netloc or "").lower()


async def solve_tencent(app_id: str, website: str, script: str = "") -> tuple[str, str]:
    key = captcha_key()
    if not key or not app_id:
        return "", ""
    payload = {
        "clientKey": key,
        "task": {
            "type": "TencentTaskProxyless",
            "appId": app_id,
            "websiteURL": website or "https://tgbet174.net/",
            "captchaScript": script or _CAPTCHA_SCRIPT,
        },
    }
    async with httpx.AsyncClient(timeout=30) as http:
        created = await http.post("https://api.2captcha.com/createTask", json=payload)
        task = created.json()
        if task.get("errorId") or not task.get("taskId"):
            log.info("节点：滑块失败 %s", task.get("errorCode") or created.status_code)
            return "", ""
        deadline = time.monotonic() + _CAPTCHA_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            result = await http.post(
                "https://api.2captcha.com/getTaskResult",
                json={"clientKey": key, "taskId": task["taskId"]},
            )
            data = result.json()
            if data.get("errorId"):
                log.info("节点：滑块失败 %s", data.get("errorCode") or result.status_code)
                return "", ""
            if data.get("status") == "ready":
                solution = data.get("solution") if isinstance(data.get("solution"), dict) else {}
                return str(solution.get("ticket") or ""), str(solution.get("randstr") or "")
    log.info("节点：滑块超时")
    return "", ""


async def page_text(page) -> str:
    return await page.evaluate(
        """() => {
          const t = document.body ? document.body.innerText : '';
          const toast = document.querySelector('.van-toast');
          const extra = toast ? (toast.innerText || '') : '';
          return (t + '\\n' + extra).trim();
        }"""
    )


async def captcha_job(page) -> dict | None:
    return await page.evaluate(
        """() => {
          const cap = window.__meiguanCaptcha;
          if (!cap || typeof cap.cb !== 'function') return null;
          const scripts = [...document.scripts].map(s => s.src).filter(Boolean);
          return {appId: String(cap.appId || ''), scripts};
        }"""
    )


async def finish_redpack(page) -> str:
    deadline = time.monotonic() + 12
    step = "stop"
    while time.monotonic() < deadline:
        shown = await page_text(page)
        step = redpack_step(shown)
        if step != "stop" or await captcha_job(page):
            break
        await page.wait_for_timeout(400)
    shown = await page_text(page)
    step = redpack_step(shown)
    if step != "claim":
        # 兼容新平台（tgbet695 等，按钮文案是「领取红包」而非「可领取」）：
        # 有可点的领取按钮 + 验证码挂上，也视为可领取，不动旧的 redpack_step 判定。
        has_btn = await page.evaluate(
            "() => { const b = document.querySelector('button.receiveHbBtn'); return !!(b && !b.disabled); }"
        )
        if has_btn and await captcha_job(page):
            step = "claim"
    if step != "claim":
        return shown
    # 腾讯滑块新版（TJNCaptcha）打码平台打不了，用前端真实拖动
    loop = asyncio.get_running_loop()
    pre_fut: "asyncio.Future" = loop.create_future()

    async def watch_prehandle(response) -> None:
        try:
            if "cap_union_prehandle" in response.url and not pre_fut.done():
                pre_fut.set_result(await response.text())
        except Exception:
            pass

    page.on("response", watch_prehandle)
    origin = page.url or ""
    parsed = urlparse(origin)
    website = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    try:
        await page.locator("button.receiveHbBtn").first.click(timeout=3000)
        log.info("节点：小程序点击领取")
    except Exception as exc:
        log.info("节点：小程序领取失败 %s", type(exc).__name__)
        return await page_text(page) or "节点：滑块打码失败"
    if not await solve_slider_drag(page, pre_fut, website):
        return "节点：滑块打码失败"
    note = ""
    wait_claim = time.monotonic() + 12
    while time.monotonic() < wait_claim:
        raw = await page.evaluate("() => window.__meiguanClaim || ''")
        note = claim_text(str(raw or ""))
        if note:
            break
        await page.wait_for_timeout(300)
    if note:
        return note
    previous = shown
    try:
        await page.wait_for_function(
            """(previous) => {
              const t = document.body ? document.body.innerText : '';
              if (!t || t === previous) return false;
              if (t.includes('恭喜') || t.includes('失败') || t.includes('流水不足')) return true;
              const toast = document.querySelector('.van-toast');
              return !!(toast && (toast.innerText || '').trim());
            }""",
            arg=previous,
            timeout=12000,
        )
    except Exception:
        log.info("节点：小程序领取没有变化")
    return await page_text(page)


async def solve_slider(page) -> bool:
    """检测腾讯滑块并打码回传。"""
    if not captcha_key():
        warn_once("slider-key", "节点：滑块未配置打码密钥")
        return False
    wait = time.monotonic() + 8
    job = None
    while time.monotonic() < wait:
        job = await captcha_job(page)
        if job:
            break
        await page.wait_for_timeout(400)
    if not job:
        log.info("节点：小程序滑块没有挂上")
        return False
    origin = page.url or ""
    parsed = urlparse(origin)
    website = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    try:
        ticket, randstr = await solve_tencent(
            str(job.get("appId") or ""),
            website,
            _REDPACK_SCRIPT,
        )
    except Exception as exc:
        log.info("节点：滑块失败 %s", type(exc).__name__)
        return False
    if not ticket or not randstr:
        return False
    accepted = await page.evaluate(
        """({ticket, randstr}) => {
          const cap = window.__meiguanCaptcha;
          if (!cap || typeof cap.cb !== 'function') return false;
          cap.cb({ret: 0, ticket, randstr, errorCode: 0});
          return true;
        }""",
        {"ticket": ticket, "randstr": randstr},
    )
    if not accepted:
        return False
    log.info("节点：滑块已打码回传")
    return True


def ncc_gap(bg_bytes: bytes, fg_bytes: bytes, sprite_pos, size) -> int:
    """NCC 匹配腾讯滑块缺口，返回缺口 x（背景图坐标）。纯 PIL，无 numpy/cv2。"""
    from io import BytesIO

    from PIL import Image

    bg = Image.open(BytesIO(bg_bytes)).convert("L")
    fg = Image.open(BytesIO(fg_bytes)).convert("RGBA")
    piece = fg.crop((sprite_pos[0], sprite_pos[1], sprite_pos[0] + size[0], sprite_pos[1] + size[1]))
    pa = piece.split()[3]
    pg = piece.convert("L")
    pw, ph = piece.size
    bw, bh = bg.size
    if pw <= 0 or ph <= 0 or bw <= pw or bh <= ph:
        return 0
    bgp = bg.load()
    pp = pg.load()
    ap = pa.load()
    opaque = [(x, y) for y in range(ph) for x in range(pw) if ap[x, y] > 128]
    if not opaque:
        return 0
    n = len(opaque)
    pm = sum(pp[x, y] for x, y in opaque) / n
    pz = {p: pp[p] - pm for p in opaque}
    denom_t = sum(v * v for v in pz.values()) ** 0.5
    if denom_t == 0:
        return 0

    def ncc_at(dx: int, dy: int) -> float:
        wm = sum(bgp[dx + x, dy + y] for x, y in opaque) / n
        num = 0.0
        den = 0.0
        for (x, y) in opaque:
            v = bgp[dx + x, dy + y] - wm
            num += pz[(x, y)] * v
            den += v * v
        den = den ** 0.5
        return num / (denom_t * den) if den else -1.0

    best = (-1.0, -1, -1)
    for dy in range(0, bh - ph + 1, 4):
        for dx in range(0, bw - pw + 1, 4):
            v = ncc_at(dx, dy)
            if v > best[0]:
                best = (v, dx, dy)
    bx, by = best[1], best[2]
    fine = (-1.0, -1, -1)
    for dy in range(max(0, by - 8), min(bh - ph, by + 8) + 1):
        for dx in range(max(0, bx - 8), min(bw - pw, bx + 8) + 1):
            v = ncc_at(dx, dy)
            if v > fine[0]:
                fine = (v, dx, dy)
    return fine[1]


async def drag_tencent_slider(page, target_x: float) -> bool:
    """真实轨迹拖动腾讯滑块到 target_x（滑块把手中心，页面坐标）。"""
    import random

    sb = await page.evaluate(
        "() => { const e = document.querySelector('.tencent-captcha-dy__slider-block');"
        " if (!e) return null; const r = e.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; }"
    )
    if not sb:
        return False
    sx = sb["x"] + sb["w"] / 2
    sy = sb["y"] + sb["h"] / 2
    delta = target_x - sx

    def gauss(mu=0.0, sigma=0.8):
        return random.gauss(mu, sigma)

    total = random.randint(30, 45)
    await page.mouse.move(sx + gauss(0, 1), sy + gauss(0, 1))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.15, 0.3))
    for i in range(total):
        f = i / (total - 1)
        if f < 0.15:
            v = 0.5 * (f / 0.15) ** 2
        elif f < 0.80:
            v = 0.5 + 0.5 * ((f - 0.15) / 0.65)
        else:
            v = 1 - 0.3 * ((1 - f) / 0.20)
        await page.mouse.move(sx + delta * v + gauss(0, 0.8), sy + gauss(0, 0.7))
        await asyncio.sleep(random.uniform(0.006, 0.014))
    await page.mouse.move(sx + delta + gauss(0, 1.5), sy + gauss(0, 1))
    await page.mouse.move(sx + delta + gauss(0, 1.0), sy + gauss(0, 0.8))
    await page.mouse.move(sx + delta, sy)
    await asyncio.sleep(random.uniform(0.1, 0.25))
    await page.mouse.up()
    return True


def _read_json_text(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def slider_target(prehandle_raw: str, referer: str = "") -> float:
    """解析 prehandle 响应，下载背景图/精灵图，NCC 匹配缺口，返回拖动目标 x（页面坐标）。"""
    raw = (prehandle_raw or "").strip()
    if raw.startswith("_aq_"):
        raw = raw[raw.find("(") + 1: raw.rfind(")")]
    data = _read_json_text(raw)
    dyn = ((data.get("data") or {}).get("dyn_show_info")) or {}
    bg_cfg = dyn.get("bg_elem_cfg") or {}
    fg_list = dyn.get("fg_elem_list") or []
    sprite_url = dyn.get("sprite_url") or ""
    piece = next((e for e in fg_list if str(e.get("id")) == "1"), None)
    if not piece or not bg_cfg or not sprite_url:
        return 0.0
    sprite_pos = piece.get("sprite_pos") or [0, 0]
    size = piece.get("size_2d") or [0, 0]
    base = "https://ca.turing.captcha.qcloud.com"
    bg_url = (bg_cfg.get("img_url") or "").strip()
    fg_url = sprite_url.strip()
    if bg_url.startswith("/"):
        bg_url = base + bg_url
    if fg_url.startswith("/"):
        fg_url = base + fg_url
    headers = {"Referer": referer or "https://hb.tsyl689.com/", "User-Agent": _MOBILE_UA}
    try:
        with httpx.Client(timeout=30, headers=headers) as http:
            bg_bytes = http.get(bg_url).content
            fg_bytes = http.get(fg_url).content
    except Exception:
        return 0.0
    gap_x = ncc_gap(bg_bytes, fg_bytes, sprite_pos, size)
    if gap_x <= 0:
        return 0.0
    # 背景图 672 宽显示在 image-area（页面 x=47 宽 326）；碎片左边缘目标 = 47 + gap_x * 326/672
    piece_page = 47 + gap_x * (326 / 672.0)
    # 碎片左边缘 = 把手中心 - 18（实测）；把手中心目标 = 碎片目标 + 18
    return piece_page + 18.0


async def solve_slider_drag(page, pre_fut: "asyncio.Future", referer: str = "") -> bool:
    """前端真实拖动过腾讯滑块（新版 TJNCaptcha）。pre_fut 须在触发滑块前注册。"""
    try:
        pre_raw = await asyncio.wait_for(pre_fut, timeout=8)
    except asyncio.TimeoutError:
        log.info("节点：滑块 prehandle 未触发")
        return False
    target = slider_target(pre_raw, referer)
    if target <= 0:
        log.info("节点：滑块缺口识别失败")
        return False
    await page.evaluate("() => { const b = document.querySelector('.tencent-captcha-dy__robot-box'); if (b) b.click(); }")
    await asyncio.sleep(1.5)
    if not await drag_tencent_slider(page, target):
        return False
    await asyncio.sleep(2.0)
    return True


async def xvip_follow_then_claim(page, client, redwars: dict) -> bool:
    """xvip 点领取后若提示未关注，join 频道并 reload 再点。返回是否处理了关注。"""
    name = ""
    if redwars.get("body"):
        try:
            import json as _json
            from meiguan.grab.xvip import decrypt as xvip_decrypt
            data = _json.loads(xvip_decrypt(redwars["key"], redwars["body"]))
            if data.get("code") == 4001:
                name = (data.get("chatusername") or "").lstrip("@")
        except Exception as exc:
            log.info("节点：xvip 解密频道失败 %s", type(exc).__name__)
    if not name:
        shown = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if "关注" in (shown or ""):
            names = follow_names(shown)
            name = names[0] if names else ""
    if not name or client is None:
        return False
    await join_channel(client, name)
    for attempt in range(2):
        await page.wait_for_timeout(800)
        await page.reload(wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(1500)
        await page.evaluate(_CAPTCHA_HOOK)
        try:
            await page.wait_for_selector(".getBtn", state="visible", timeout=8000)
        except Exception:
            return True
        await page.locator(".getBtn").first.click(timeout=3000)
        await page.wait_for_timeout(1500)
        shown = await page.evaluate("() => document.body ? document.body.innerText : ''")
        if "暂未关注" not in (shown or ""):
            return True
        log.info("节点：xvip 关注后仍未生效，重试")
    return True


async def read_app_page(url: str, client=None) -> str:
    xvip = "xwallet8.com" in (urlparse(url or "").netloc or "").lower()
    async with _pages:
        if xvip:
            browser = await open_patchright_browser()
        else:
            browser = await open_browser()
        if browser is None:
            return ""
        extra = {}
        if xvip:
            extra = {"user_agent": _MOBILE_UA, "is_mobile": True, "has_touch": True}
        context, page = await open_sheet(browser, **extra)
        try:
            await page.add_init_script(_APP_BOOT)
            hooked = redpack_page(url)
            if hooked:
                await page.add_init_script(_CLAIM_WATCH)
                await page.add_init_script(_CAPTCHA_HOOK)
            seen: list[str] = []

            def remember(response) -> None:
                item = api_path(getattr(response, "url", "") or "")
                if item and item not in seen and len(seen) < 12:
                    seen.append(item)

            page.on("response", remember)
            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
            await page.wait_for_timeout(1500)
            await follow_then_back(page, client, url)
            if not hooked:
                has_host = await page.evaluate("() => !!document.getElementById('redpack-captcha-host')")
                if has_host:
                    await page.add_init_script(_CLAIM_WATCH)
                    await page.add_init_script(_CAPTCHA_HOOK)
                    await page.reload(wait_until="domcontentloaded", timeout=12000)
                    hooked = True
            if hooked:
                try:
                    await page.wait_for_selector("#redpack-captcha-host", timeout=8000)
                except Exception:
                    hooked = False
            if hooked:
                return await finish_redpack(page)
            before = await page.evaluate("() => document.body ? document.body.innerText : ''")
            try:
                await page.wait_for_selector(".getBtn", state="visible", timeout=8000)
            except Exception:
                pass
            claim = page.locator(".getBtn")
            opened = await claim.count() > 0
            if opened:
                try:
                    label = await claim.first.inner_text(timeout=1000)
                except Exception:
                    label = ""
                if "解锁" in (label or ""):
                    log.info("节点：红包未解锁")
                    try:
                        await page.wait_for_function(
                            """() => {
                              const el = document.querySelector('.getBtn');
                              const name = el ? (el.innerText || '').replace(/\\s+/g, '') : '';
                              return Boolean(name) && !name.includes('解锁');
                            }""",
                            timeout=30000,
                        )
                    except Exception:
                        return await page.evaluate("() => document.body ? document.body.innerText : ''")
                if xvip:
                    redwars = {}

                    async def watch_redwars(response) -> None:
                        if "redWars" not in response.url:
                            return
                        try:
                            raw = await response.text()
                        except Exception:
                            return
                        if "<html>" in raw[:100]:
                            return
                        redwars["key"] = response.headers.get("encrypt-key", "")
                        redwars["body"] = raw

                    page.on("response", watch_redwars)
                    loop = asyncio.get_running_loop()
                    pre_fut: asyncio.Future = loop.create_future()

                    async def watch_prehandle(response) -> None:
                        try:
                            if "cap_union_prehandle" in response.url and not pre_fut.done():
                                pre_fut.set_result(await response.text())
                        except Exception:
                            pass

                    page.on("response", watch_prehandle)
                await claim.first.click(timeout=3000)
                log.info("节点：小程序点击领取")
                if xvip:
                    await xvip_follow_then_claim(page, client, redwars)
                    await solve_slider_drag(page, pre_fut, "https://xwallet8.com/")
                try:
                    await page.wait_for_function(
                        """() => {
                          const t = document.body ? document.body.innerText : '';
                          if (t.includes('恭喜获得') || t.includes('手速太快') || t.includes('拖动下方拼图')) return true;
                          const cap = document.querySelector('#tcaptcha_transform_dy');
                          return !!(cap && getComputedStyle(cap).visibility !== 'hidden');
                        }""",
                        timeout=8000,
                    )
                except Exception:
                    log.info("节点：小程序领取没有变化")
            box = None if opened else await page.evaluate(
                """() => {
                  const marks = ['開', '开', '拆', '领取', '領取', '领红包', '領紅包'];
                  let best = null;
                  let bestArea = Infinity;
                  for (const el of document.querySelectorAll('*')) {
                    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent || '').join('').replace(/\\s+/g, '');
                    const flat = (el.innerText || '').replace(/\\s+/g, '');
                    if (flat.includes('列表') || flat.includes('情况') || flat.includes('记录')) continue;
                    if (!marks.includes(own) && !marks.includes(flat)) continue;
                    const r = el.getBoundingClientRect();
                    const area = r.width * r.height;
                    if (area <= 0 || area >= bestArea) continue;
                    best = {x: r.x + r.width / 2, y: r.y + r.height / 2};
                    bestArea = area;
                  }
                  return best;
                }"""
            )
            if box:
                await page.mouse.click(box["x"], box["y"])
            try:
                await page.wait_for_function(
                    """(previous) => {
                      const t = document.body ? document.body.innerText : '';
                      if (!t || t === previous) return false;
                      return !/正在拆|加载中|加載中|请稍候|請稍候/.test(t);
                    }""",
                    arg=before,
                    timeout=12000,
                )
            except Exception:
                pass
            if seen:
                log.info("节点：小程序接口 %s", " ".join(seen))
            log.info("节点：小程序打开 %s", urlparse(page.url or "").netloc or "-")
            shown = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if "点选相同" in (shown or ""):
                await solve_shapes(page)
                try:
                    await page.wait_for_function(
                        """() => {
                          const t = document.body ? document.body.innerText : '';
                          return Boolean(t) && !t.includes('点选相同') && !/正在拆|加载中|加載中|请稍候|請稍候|正在领取|正在領取/.test(t);
                        }""",
                        timeout=8000,
                    )
                except Exception:
                    pass
                shown = await page.evaluate("() => document.body ? document.body.innerText : ''")
            return shown
        except Exception as exc:
            log.info("节点：小程序页面失败 %s", type(exc).__name__)
            return ""
        finally:
            await close_sheet(page, context)


def _self_check() -> None:
    assert redpack_step("可领取") == "claim"
    assert redpack_step("暂不可领取") == "stop"
    assert redpack_step("立即解锁") == "unlock"
    assert redpack_step("恭喜您获得红包") == "got"
    assert redpack_page("https://tgbet174.net/#tgWebAppData=1")
    assert not redpack_page("https://xwallet8.com/")
    script = captcha_script(["https://turing.captcha.qcloud.com/TJNCaptcha-global.js"])
    assert script.startswith("https://ca.turing.captcha.qcloud.com/")
    assert captcha_script(["https://ca.turing.captcha.qcloud.com/TJNCaptcha-global.js"]).startswith("https://ca.")
    assert claim_text('{"success": true, "amount": "2.5"}') == "恭喜您获得红包 2.5 USDT"
    assert claim_text('{"success": false, "message": "校验失败"}') == "节点：滑块打码失败"
    assert claim_text('{"success": false, "message": "流水不足"}') == "流水不足"


_self_check()
