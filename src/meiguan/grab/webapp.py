"""网页和小程序领取。"""

import asyncio
import logging
import os

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import (
    RequestAppWebViewRequest,
    RequestMainWebViewRequest,
    RequestSimpleWebViewRequest,
    RequestWebViewRequest,
    SendWebViewDataRequest,
)
from telethon.tl.types import InputBotAppShortName, InputPeerUser, InputUser

from meiguan.grab.browser import join_channel, read_app_page, solve_page
from meiguan.grab.detect import (
    _BLOCKED,
    _EMPTY,
    _SUCCESS,
    _WRONG,
    button_url,
    extract_amount,
    grabbed_amount,
    is_webview,
    judge,
)
from meiguan.grab.types import RedPacket, Result
from meiguan.grab.vision import vision_models, vision_settings, warn_once
from meiguan.node_host import wallet_lock

log = logging.getLogger("meiguan.grab")

# 「领取红包」是网页首页按钮文案（金博等），不是成功回执；网页成功判定去掉它
_APP_SUCCESS = tuple(word for word in _SUCCESS if "领取" not in word)

def button_label(btn) -> str:
    return (getattr(btn, "text", None) or "").strip()


async def latest_web_url(client: TelegramClient, bot) -> tuple[str, str]:
    found = await client.get_messages(bot, limit=6)
    for msg in found or []:
        if getattr(msg, "out", False):
            continue
        if "验证" not in (msg.message or ""):
            continue
        markup = getattr(msg, "reply_markup", None)
        if not markup:
            continue
        for row in markup.rows:
            for btn in row.buttons:
                if is_webview(btn):
                    url = button_url(btn)
                    if url:
                        return url, button_label(btn)
    return "", ""


async def claim_web(client: TelegramClient, user_id: int, rp: RedPacket) -> Result:
    if not rp.bot or not rp.start:
        return Result(False, retryable=False, feedback="节点：网页验证缺少入口")
    if not vision_settings()[0]:
        warn_once("web-key", "节点：网页验证未配置视觉识别")
        return Result(False, retryable=False, feedback="节点：网页验证未配置视觉识别")
    async with wallet_lock(user_id):
        try:
            bot = await client.get_entity(rp.bot)
            peer = await client.get_input_entity(bot)
        except Exception as exc:
            log.info("Telegram：网页验证找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        loop = asyncio.get_running_loop()
        page_fut: asyncio.Future = loop.create_future()
        done_fut: asyncio.Future = loop.create_future()

        def classify_dm(text: str):
            if "🎉" in text and any(word in text for word in ("领取了", "領取了", "抢到了", "搶到了", "成功")):
                return "ok", text
            if any(word in text for word in _EMPTY) or "频繁" in text or "頻繁" in text:
                return "fail", text
            return None

        async def on_dm(ev):
            markup = ev.message.reply_markup
            if markup and not page_fut.done():
                for row in markup.rows:
                    for btn in row.buttons:
                        if is_webview(btn) and not page_fut.done():
                            page_fut.set_result((button_url(btn), button_label(btn)))
            found = classify_dm(ev.message.message or "")
            if found and not done_fut.done():
                done_fut.set_result(found)

        builder = events.NewMessage(from_users=bot.id)
        edited = events.MessageEdited(from_users=bot.id)
        client.add_event_handler(on_dm, builder)
        client.add_event_handler(on_dm, edited)
        try:
            await client.send_message(peer, f"/start {rp.start}")
            try:
                web_url, button_text = await asyncio.wait_for(page_fut, 6)
            except asyncio.TimeoutError:
                web_url, button_text = await latest_web_url(client, bot)
            if not web_url:
                recent = await client.get_messages(bot, limit=4)
                for item in recent or []:
                    if getattr(item, "out", False):
                        continue
                    body = item.message or ""
                    if "已领取" in body or "已領取" in body:
                        return Result(True, grabbed_amount(body) or extract_amount(body), False, body[:80])
                return Result(False, retryable=True, feedback="节点：网页验证没有打开")
            models = vision_models()
            if "kpaye.com" in web_url:
                models = [next((item for item in models if "plus" in item[0] or item[1] == "plus"), models[0])]
            tasks = [
                one_page(client, peer, web_url, model, button_text) for model, _tag in models
            ]
            opened = await asyncio.gather(*tasks)
            if not any(opened):
                return Result(False, retryable=True, feedback="节点：网页验证未提交")
            try:
                status, text = await asyncio.wait_for(done_fut, 15)
            except asyncio.TimeoutError:
                return Result(False, retryable=False, feedback="节点：网页验证没有回执")
            if status == "ok":
                return judge(text)
            return Result(False, extract_amount(text), False, text)
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        finally:
            client.remove_event_handler(on_dm, builder)
            client.remove_event_handler(on_dm, edited)


def input_user(peer):
    if isinstance(peer, InputPeerUser):
        return InputUser(peer.user_id, peer.access_hash or 0)
    return peer


def bot_ref(bot: str):
    text = str(bot or "").strip().lstrip("@")
    if text.isdigit():
        return int(text)
    return text


def webview_blocked(exc: Exception) -> bool:
    return "WEBVIEW_DISABLED" in f"{type(exc).__name__} {exc}".upper()


async def message_peer(client, event, chat_id: int):
    if event is not None:
        try:
            return await event.get_input_chat()
        except Exception as exc:
            log.info("Telegram：窗口解析失败 %s", type(exc).__name__)
    if chat_id:
        try:
            return await client.get_input_entity(chat_id)
        except Exception as exc:
            log.info("Telegram：窗口解析失败 %s", type(exc).__name__)
    return None


async def main_url(client, chat_peer, bot_peer, start: str) -> str:
    opened = await client(RequestMainWebViewRequest(
        peer=chat_peer,
        bot=input_user(bot_peer),
        platform="android",
        start_param=start or None,
    ))
    return getattr(opened, "url", "") or ""


async def app_url(client, chat_peer, bot_peer, short: str, start: str) -> str:
    opened = await client(RequestAppWebViewRequest(
        peer=chat_peer,
        app=InputBotAppShortName(bot_id=input_user(bot_peer), short_name=short),
        platform="android",
        start_param=start or None,
    ))
    return getattr(opened, "url", "") or ""


async def bot_peers(client, bot: str):
    found = await client.get_entity(bot_ref(bot))
    peer = await client.get_input_entity(found)
    return found, peer


async def one_page(client, peer, web_url: str, model: str, button_text: str = "") -> bool:
    try:
        opened = await client(RequestWebViewRequest(
            peer=peer, bot=input_user(peer), platform="android", url=web_url,
        ))
    except Exception as exc:
        log.info("Telegram：网页验证打开失败 %s", type(exc).__name__)
        return False

    async def on_data(data: str) -> None:
        await client(SendWebViewDataRequest(
            bot=input_user(peer),
            button_text=button_text or "立即验证",
            data=data,
            random_id=int.from_bytes(os.urandom(8), "little", signed=True),
        ))

    if "kpaye.com" in (web_url or ""):
        return await solve_page(opened.url, model, on_data)
    return await solve_page(opened.url, model)


def account_premium(user_id: int) -> bool:
    from meiguan.node_host import accounts as rows

    row = rows.get(str(user_id)) or {}
    if "premium" not in row:
        return True
    return bool(row.get("premium"))


def app_result(text: str) -> Result:
    shown = " ".join((text or "").split())
    if not shown:
        return Result(False, retryable=True, feedback="节点：小程序没有回执")
    if any(word in shown for word in _EMPTY):
        return Result(False, extract_amount(shown), False, shown[:80])
    if "请稍候" in shown or "請稍候" in shown or "正在领取" in shown or "正在領取" in shown:
        return Result(False, retryable=True, feedback=shown[:80])
    if "安全验证" in shown or "人机验证" in shown or "请完成人机验证" in shown or "我是人类" in shown:
        return Result(False, retryable=True, feedback=shown[:80])
    if any(word in shown for word in _APP_SUCCESS) or "🎉" in shown:
        return Result(True, grabbed_amount(shown) or extract_amount(shown), False, shown[:80])
    if "已到账" in shown or "已存入" in shown or "手气最佳" in shown:
        return Result(True, grabbed_amount(shown) or extract_amount(shown), False, shown[:80])
    if any(word in shown for word in _WRONG):
        return Result(False, retryable=False, feedback=shown[:80], wrong=True)
    if "Premium" in shown or any(word in shown for word in _BLOCKED):
        return Result(False, retryable=False, feedback=shown[:80])
    if "正在拆" in shown or "加载" in shown or "加載" in shown:
        return Result(False, retryable=True, feedback=shown[:80])
    if "滑块未配置" in shown or "请先填写打码 token" in shown:
        return Result(False, retryable=False, feedback=shown[:80])
    if "滑块打码失败" in shown or "滑块超时" in shown:
        return Result(False, retryable=True, feedback=shown[:80])
    if "恭喜您获得" in shown or "恭喜获得" in shown:
        return Result(True, grabbed_amount(shown) or extract_amount(shown), False, shown[:80])
    if "点选相同" in shown or "拖动下方拼图" in shown:
        return Result(False, retryable=True, feedback="节点：小程序验证未通过")
    if "手速太快" in shown:
        return Result(False, retryable=True, feedback="节点：操作过快")
    if "剩余" in shown and ("開" in shown or "开" in shown):
        return Result(False, retryable=True, feedback=shown[:80])
    flat = "".join(shown.split())
    if "解锁" in flat and "成功" not in flat and "恭喜" not in flat and not any(word in flat for word in _EMPTY):
        return Result(False, retryable=False, hold=True)
    return Result(False, retryable=False, feedback=shown[:80])


async def claim_app(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not (rp.short or rp.start):
        return Result(False, retryable=False, feedback="节点：小程序缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot)
        except Exception as exc:
            log.info("Telegram：小程序找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        try:
            if rp.short:
                web = await app_url(client, chat, bot_peer, rp.short, rp.start or "")
            else:
                web = await main_url(client, chat, bot_peer, rp.start or "")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：小程序打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True)
        if not web:
            return Result(False, retryable=True, feedback="节点：网页验证没有打开")
        for name in (rp.follows or "").split():
            await join_channel(client, name)
        if rp.start and rp.start not in web:
            web += ("&" if "#" in web else "#") + "tgWebAppStartParam=" + rp.start
        text = await read_app_page(web, client)
        if not rp.short:
            log.info("节点：小程序正文 %s", " ".join((text or "").split())[:240])
        return app_result(text)


async def claim_chat_page(client, user_id: int, rp: RedPacket, event=None, chat_id: int = 0) -> Result:
    if not rp.bot or not rp.page:
        return Result(False, retryable=False, feedback="节点：网页验证缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        chat = await message_peer(client, event, chat_id)
        if chat is None:
            return Result(False, retryable=True, feedback="节点：窗口打不开")
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot)
        except Exception as exc:
            log.info("Telegram：网页验证找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        try:
            opened = await client(RequestWebViewRequest(
                peer=chat,
                bot=input_user(bot_peer),
                platform="android",
                url=rp.page,
            ))
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            if webview_blocked(exc):
                log.info("Telegram：窗口不能打开网页")
                return Result(False, retryable=False, feedback="Telegram：这个按钮不能从窗口打开")
            log.info("Telegram：网页验证打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True)
        web = getattr(opened, "url", "") or ""
        return app_result(await read_app_page(web, client))


async def claim_simple(client, user_id: int, rp: RedPacket) -> Result:
    if not rp.bot or not rp.page:
        return Result(False, retryable=False, feedback="节点：网页验证缺少入口")
    if rp.premium_only and not account_premium(user_id):
        return Result(False, retryable=False, feedback="Telegram：仅限 Premium 会员")
    async with wallet_lock(user_id):
        try:
            _bot, bot_peer = await bot_peers(client, rp.bot)
        except Exception as exc:
            log.info("Telegram：网页验证找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        try:
            opened = await client(RequestSimpleWebViewRequest(
                bot=input_user(bot_peer),
                platform="android",
                url=rp.page,
                start_param=rp.start or None,
            ))
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        except Exception as exc:
            log.info("Telegram：网页验证打开失败 %s", type(exc).__name__)
            return Result(False, retryable=True)
        web = getattr(opened, "url", "") or ""
        return app_result(await read_app_page(web, client))
