"""群内领取。"""

import asyncio
import logging
import re
import time

from telethon import TelegramClient, events
from telethon.errors import BotResponseTimeoutError, FloodWaitError, PeerFloodError
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

from meikai.charset import MATH_RE, normalize_operand, solve_expr, solve_expr_with_buttons
from meikai.grab.detect import (
    _BLOCKED,
    _EMPTY,
    _EXHAUSTED,
    _SUCCESS,
    _WRONG,
    choice_data,
    emoji_in_expr,
    exclusive_miss,
    extract_amount,
    find_captcha,
    grabbed_amount,
    judge,
    prompt_answer,
    quiz_buttons,
    raw_text,
)
from meikai.grab.glyphs import apply_corrections, decode_expr, drop_glyphs
from meikai.grab.types import RedPacket, Result
from meikai.node_host import wallet_lock

log = logging.getLogger("meikai.grab")

_ANSWER_TTL = 60


_answers: dict[tuple[int, int], tuple[str, float]] = {}


_phrase_sent: dict[tuple[int, int], None] = {}


def cached_answer(chat_id: int, msg_id: int) -> str | None:
    item = _answers.get((chat_id, msg_id))
    if item is None:
        return None
    answer, stamp = item
    if time.time() - stamp > _ANSWER_TTL:
        _answers.pop((chat_id, msg_id), None)
        return None
    return answer


def store_answer(chat_id: int, msg_id: int, answer: str) -> None:
    _answers[(chat_id, msg_id)] = (answer, time.time())


async def callback(client: TelegramClient, peer, msg_id: int, data: bytes) -> Result:
    try:
        res = await client(GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=data))
    except FloodWaitError as exc:
        seconds = int(exc.seconds)
        log.info("Telegram：领取需等待 %s 秒", seconds)
        await asyncio.sleep(max(seconds, 0))
        return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
    except PeerFloodError:
        return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
    except Exception as exc:
        log.info("Telegram：点击失败 %s", type(exc).__name__)
        return Result(False, retryable=True)
    result = judge(res.message or "")
    result.url = getattr(res, "url", None) or ""
    return result


async def peer_of(client: TelegramClient, event, chat_id: int):
    try:
        return await event.get_input_chat()
    except Exception:
        return await client.get_input_entity(chat_id)


def match_button(buttons, answer: str) -> bytes | None:
    target = answer.strip()
    for label, data in buttons:
        if (label or "").strip() == target:
            return data
    folded = []
    for label, data in buttons:
        raw = (label or "").strip()
        norm = normalize_operand(label)
        if norm == target:
            return data
        if target.isdigit() and norm.lstrip("0") == target.lstrip("0") and target.lstrip("0"):
            return data
        if raw.casefold() == target.casefold():
            folded.append(data)
    if len(folded) == 1:
        return folded[0]
    return None


async def claim_code_choice(client, event, chat_id, msg_id, code: str) -> Result:
    current = None
    try:
        current = await client.get_messages(chat_id, ids=msg_id)
    except Exception:
        current = None
    msg = current if current is not None else getattr(event, "message", None)
    data = choice_data(msg, code) if msg is not None else None
    if not data:
        return Result(False, retryable=True, feedback="节点：答案不在选项里")
    return await callback(client, await peer_of(client, event, chat_id), msg_id, data)


async def brute_force_buttons(client, event, chat_id, msg_id, rp: RedPacket) -> Result:
    """表情算式识别不准、答案不在选项时，逐个点按钮试错，点对即成功。"""
    peer = await peer_of(client, event, chat_id)
    for label, data in rp.buttons:
        result = await callback(client, peer, msg_id, data)
        if result.ok:
            log.info("节点：表情试错命中 %s", label)
            store_answer(chat_id, msg_id, label)
            return result
    return Result(False, retryable=False, feedback="节点：试错没有命中")


async def claim_captcha(client, event, chat_id, msg_id, rp: RedPacket) -> Result:
    if rp.expr and MATH_RE.search(rp.expr) is None:
        return await claim_code_choice(client, event, chat_id, msg_id, rp.expr)
    answer = cached_answer(chat_id, msg_id)
    corrections: dict = {}
    if not answer:
        if not rp.expr:
            return Result(False, retryable=False, feedback="节点：算式无法识别")
        answer, corrections = solve_expr_with_buttons(rp.expr, [label for label, _data in rp.buttons])
        if not answer:
            return Result(False, retryable=False, feedback="节点：算式无法计算")
    data = match_button(rp.buttons, answer)
    if data is None:
        _answers.pop((chat_id, msg_id), None)
        if rp.emoji_map:
            return await brute_force_buttons(client, event, chat_id, msg_id, rp)
        missed = bool(rp.emoji_map)
        return Result(False, retryable=missed, wrong=missed, feedback="节点：答案不在选项里")
    store_answer(chat_id, msg_id, answer)
    result = await callback(client, await peer_of(client, event, chat_id), msg_id, data)
    result.corrections = corrections
    if result.wrong:
        _answers.pop((chat_id, msg_id), None)
    return result


def needs_verify(text: str) -> bool:
    return any(word in (text or "") for word in ("请先", "验证身份", "完成验证", "未完成"))


def verify_passed(text: str) -> bool:
    return any(word in (text or "") for word in ("验证通过", "验证成功", "回答正确", "回答正確"))


async def pending_quiz(client: TelegramClient, bot):
    found = await client.get_messages(bot, limit=6)
    for msg in found or []:
        if getattr(msg, "out", False):
            continue
        if find_captcha(msg):
            return msg
    return None


async def solve_dm_quiz(client: TelegramClient, peer, msg) -> Result:
    mapping: dict = {}
    saw_emoji = emoji_in_expr(msg)
    for force in (False, True):
        buttons = quiz_buttons(msg)
        body = raw_text(msg)
        if buttons and (force or saw_emoji or solve_expr(body) is None):
            decoded, mapping = await decode_expr(client, msg, buttons, force=force)
            if decoded:
                body = decoded
        quiz = find_captcha(msg, body)
        if not quiz:
            return Result(False, retryable=False, feedback="节点：私信算式无法计算")
        expr, buttons = quiz
        answer, corrections = solve_expr_with_buttons(expr, [label for label, _data in buttons])
        if not answer:
            if not force and (saw_emoji or mapping):
                drop_glyphs(mapping)
                continue
            return Result(False, retryable=False, feedback="节点：私信算式无法计算")
        data = match_button(buttons, answer)
        if data is None:
            if not force and mapping:
                drop_glyphs(mapping)
                continue
            return Result(False, retryable=False, feedback="节点：答案不在选项里")
        result = await callback(client, peer, msg.id, data)
        if result.wrong and not force and mapping:
            drop_glyphs(mapping)
            continue
        if result.ok or verify_passed(result.feedback or ""):
            apply_corrections(mapping, corrections)
            if verify_passed(result.feedback or ""):
                result.ok = True
                result.retryable = False
            return result
        if not (result.feedback or ""):
            apply_corrections(mapping, corrections)
            return Result(False, retryable=False, feedback="节点：私信验证没有回执")
        return result
    return Result(False, retryable=False, feedback="节点：私信算式无法计算")


async def claim_kpay(client, user_id, event, chat_id, msg_id, rp: RedPacket) -> Result:
    if not rp.data:
        return Result(False, retryable=False)
    async with wallet_lock(user_id):
        bot = None
        last_error = "找不到"
        for target in (rp.bot or "kpay", 8269599943):
            try:
                bot = await client.get_entity(target)
                break
            except Exception as exc:
                bot = None
                last_error = type(exc).__name__
        if bot is None:
            log.info("Telegram：私信验证找不到机器人 %s", last_error)
            return Result(False, retryable=True)
        try:
            bot_peer = await client.get_input_entity(bot)
        except Exception as exc:
            log.info("Telegram：私信验证找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        current = await client.get_messages(chat_id, ids=msg_id)
        before = left_count(raw_text(current)) if current is not None else None
        group = await peer_of(client, event, chat_id)
        loop = asyncio.get_running_loop()
        quiz_fut: asyncio.Future = loop.create_future()

        async def on_dm(ev):
            if quiz_fut.done():
                return
            if find_captcha(ev.message):
                quiz_fut.set_result(ev.message)

        builder = events.NewMessage(from_users=bot.id)
        edited = events.MessageEdited(from_users=bot.id)
        client.add_event_handler(on_dm, builder)
        client.add_event_handler(on_dm, edited)
        try:
            first = await callback(client, group, msg_id, rp.data)
            if first.ok or first.pause or first.blocked or exclusive_miss(first):
                return first
            if any(word in (first.feedback or "") for word in _EMPTY):
                return first
            dm = None
            try:
                dm = await asyncio.wait_for(quiz_fut, 2)
            except asyncio.TimeoutError:
                dm = await pending_quiz(client, bot)
                if dm is None:
                    try:
                        dm = await asyncio.wait_for(quiz_fut, 6)
                    except asyncio.TimeoutError:
                        dm = await pending_quiz(client, bot)
            if dm is None:
                if needs_verify(first.feedback or ""):
                    first.retryable = True
                return first if first.feedback else Result(
                    False, retryable=True, feedback="节点：私信验证没有回执",
                )
            fresh_dm = await client.get_messages(bot, ids=dm.id)
            if fresh_dm is not None:
                dm = fresh_dm
            answered = await solve_dm_quiz(client, bot_peer, dm)
            if not answered.ok:
                return answered
            second = await callback(client, group, msg_id, rp.data)
            if second.ok or second.pause or second.blocked or exclusive_miss(second):
                return second
            fresh = await client.get_messages(chat_id, ids=msg_id)
            text = raw_text(fresh) if fresh is not None else ""
            now = left_count(text)
            if before is not None and now is not None and now < before:
                return Result(True, grabbed_amount(text), False, text)
            if needs_verify(second.feedback or ""):
                second.retryable = True
            return second
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        finally:
            client.remove_event_handler(on_dm, builder)
            client.remove_event_handler(on_dm, edited)


async def claim_direct(client, user_id, event, chat_id, msg_id, rp: RedPacket) -> Result:
    if not rp.data:
        return Result(False, retryable=False)
    if rp.bot == "kpay":
        return await claim_kpay(client, user_id, event, chat_id, msg_id, rp)
    result = await callback(client, await peer_of(client, event, chat_id), msg_id, rp.data)
    if rp.data.startswith(b"mission_hb_get_"):
        return result
    if result.url and not result.ok and not result.pause and not result.blocked and not exclusive_miss(result):
        if not any(word in (result.feedback or "") for word in _EMPTY):
            from meikai.grab.loop import open_link

            opened = await open_link(client, user_id, event, chat_id, result.url)
            if opened is not None:
                return opened
    return result


_LEFT = re.compile(r"剩余[:：\s]*(\d+)\s*/")


def left_count(text: str) -> int | None:
    matched = _LEFT.search(text or "")
    if not matched:
        return None
    return int(matched.group(1))


def code_outcome(text: str) -> Result | None:
    if "专属" in text or "专享" in text:
        return judge(text)
    if "验证码" in text and any(word in text for word in ("错误", "过期", "失效", "不对")):
        return Result(False, retryable=True, feedback=text[:80])
    if any(word in text for word in _EMPTY + _WRONG):
        return judge(text)
    if any(word in text for word in _SUCCESS) or "验证通过" in text:
        result = judge(text)
        if result.ok and not result.amount:
            result.amount = grabbed_amount(text)
        return result
    if _EXHAUSTED.search(text):
        return Result(True, grabbed_amount(text), False, text)
    return None


async def _destroy(user_id: int, sent, kind: str) -> None:
    if sent is None or not user_id:
        return
    from meikai.grab.loop import policy_of

    burns = policy_of(user_id).get("burns") or {}
    if not burns.get(kind) and not (kind == "phrase" and burns.get("captcha")):
        return
    try:
        await sent.delete()
    except Exception as exc:
        log.info("节点：消息自毁失败 %s", type(exc).__name__)


def take_phrase(chat_id: int, msg_id: int) -> bool:
    key = (int(chat_id), int(msg_id))
    if key in _phrase_sent:
        return False
    _phrase_sent[key] = None
    while len(_phrase_sent) > 4000:
        _phrase_sent.pop(next(iter(_phrase_sent)))
    return True


async def claim_grab_code(client, event, chat_id, msg_id, rp: RedPacket, user_id: int = 0) -> Result:
    if not rp.data:
        return Result(False, retryable=False, feedback="节点：没有抢红包按钮")
    peer = await peer_of(client, event, chat_id) if event is not None else await client.get_input_entity(chat_id)
    try:
        res = await client(GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=rp.data))
    except BotResponseTimeoutError:
        return Result(False, retryable=True, feedback="节点：没有弹出验证码")
    except FloodWaitError as exc:
        seconds = int(exc.seconds)
        await asyncio.sleep(max(seconds, 0))
        return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
    except PeerFloodError:
        return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
    except Exception as exc:
        log.info("Telegram：抢红包点击失败 %s", type(exc).__name__)
        return Result(False, retryable=True, feedback="节点：没有弹出验证码")
    answer = prompt_answer(getattr(res, "message", "") or "")
    if not answer:
        shown = (getattr(res, "message", "") or "").strip()
        if shown:
            return Result(False, retryable=False, feedback=shown[:80])
        return Result(False, retryable=True, feedback="节点：没有弹出验证码")
    before = None
    current = await client.get_messages(chat_id, ids=msg_id)
    if current is not None:
        before = left_count(raw_text(current))
    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    async def on_event(ev):
        if done.done():
            return
        text = ev.message.message or ""
        if ev.message.id == msg_id:
            now = left_count(text)
            if before is not None and now is not None and now < before:
                done.set_result(Result(True, grabbed_amount(text), False, text))
            return
        if getattr(ev.message, "out", False):
            return
        found = code_outcome(text)
        if found is not None:
            done.set_result(found)

    builder = events.NewMessage(chats=chat_id)
    edited = events.MessageEdited(chats=chat_id)
    sent_msg = None
    client.add_event_handler(on_event, builder)
    client.add_event_handler(on_event, edited)
    try:
        sent_msg = await client.send_message(peer, answer)
        result = await asyncio.wait_for(done, 12)
        result.retryable = False
        return result
    except asyncio.TimeoutError:
        return Result(False, retryable=False, feedback="节点：验证码没有回执")
    except FloodWaitError as exc:
        seconds = int(exc.seconds)
        await asyncio.sleep(max(seconds, 0))
        return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
    except PeerFloodError:
        return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
    finally:
        client.remove_event_handler(on_event, builder)
        client.remove_event_handler(on_event, edited)
        await _destroy(user_id, sent_msg, "phrase")


async def claim_phrase(client, event, chat_id, msg_id, rp: RedPacket, user_id: int = 0) -> Result:
    if rp.data and not rp.start:
        return await claim_grab_code(client, event, chat_id, msg_id, rp, user_id)
    if not rp.start:
        return Result(False, retryable=False, feedback="节点：没有口令")
    if not take_phrase(chat_id, msg_id):
        return Result(False, retryable=False)
    sent = False
    try:
        peer = await peer_of(client, event, chat_id) if event is not None else await client.get_input_entity(chat_id)
        before = None
        current = await client.get_messages(chat_id, ids=msg_id)
        if current is not None:
            before = left_count(raw_text(current))
        loop = asyncio.get_running_loop()
        done: asyncio.Future = loop.create_future()

        async def on_event(ev):
            if done.done():
                return
            text = ev.message.message or ""
            if ev.message.id == msg_id:
                now = left_count(text)
                if before is not None and now is not None and now < before:
                    done.set_result(Result(True, grabbed_amount(text), False, text))
                return
            if getattr(ev.message, "out", False):
                return
            found = code_outcome(text)
            if found is not None:
                done.set_result(found)

        builder = events.NewMessage(chats=chat_id)
        edited = events.MessageEdited(chats=chat_id)
        sent_msg = None
        client.add_event_handler(on_event, builder)
        client.add_event_handler(on_event, edited)
        try:
            sent_msg = await client.send_message(peer, rp.start, reply_to=msg_id)
            sent = True
            return await asyncio.wait_for(done, 12)
        except asyncio.TimeoutError:
            return Result(False, retryable=False, feedback="节点：口令没有回执")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        finally:
            client.remove_event_handler(on_event, builder)
            client.remove_event_handler(on_event, edited)
            await _destroy(user_id, sent_msg, "phrase")
    finally:
        if not sent:
            _phrase_sent.pop((int(chat_id), int(msg_id)), None)


async def claim_dm(client: TelegramClient, user_id: int, rp: RedPacket) -> Result:
    if not rp.bot or not rp.start:
        return Result(False, retryable=False, feedback="节点：私信验证缺少入口")
    async with wallet_lock(user_id):
        try:
            bot = await client.get_entity(rp.bot)
            peer = await client.get_input_entity(bot)
        except Exception as exc:
            log.info("Telegram：私信验证找不到机器人 %s", type(exc).__name__)
            return Result(False, retryable=True)
        loop = asyncio.get_running_loop()
        quiz_fut: asyncio.Future = loop.create_future()
        done_fut: asyncio.Future = loop.create_future()

        async def on_dm(ev):
            quiz = find_captcha(ev.message)
            if quiz and not quiz_fut.done():
                quiz_fut.set_result((ev.message, quiz))
                return
            text = ev.message.message or ""
            if done_fut.done():
                return
            if any(word in text for word in _EMPTY):
                done_fut.set_result(("empty", text))
            elif any(word in text for word in _BLOCKED):
                done_fut.set_result(("blocked", text))
            elif any(word in text for word in (*_SUCCESS, "🎉", "🧧")):
                done_fut.set_result(("ok", text))

        builder = events.NewMessage(from_users=bot.id)
        edited = events.MessageEdited(from_users=bot.id)
        client.add_event_handler(on_dm, builder)
        client.add_event_handler(on_dm, edited)
        try:
            await client.send_message(peer, f"/start {rp.start}")
            done, _pending = await asyncio.wait(
                {quiz_fut, done_fut},
                timeout=8,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done_fut in done:
                status, text = done_fut.result()
                if status == "ok":
                    return judge(text)
                return Result(False, extract_amount(text), False, text, blocked=status == "blocked")
            if quiz_fut not in done:
                return Result(False, retryable=True, feedback="节点：私信验证没有回执")
            dm_msg, (expr, buttons) = quiz_fut.result()
            answer, _fixes = solve_expr_with_buttons(expr, [label for label, _data in buttons])
            if not answer:
                return Result(False, retryable=False, feedback="节点：私信算式无法计算")
            data = match_button(buttons, answer)
            if data is None:
                return Result(False, retryable=False, feedback="节点：答案不在选项里")
            result = await callback(client, await client.get_input_entity(bot.id), dm_msg.id, data)
            if result.feedback or result.ok:
                return result
            try:
                status, text = await asyncio.wait_for(done_fut, 10)
            except asyncio.TimeoutError:
                return Result(False, retryable=True, feedback="节点：私信验证没有回执")
            if status == "ok":
                return judge(text)
            return Result(False, extract_amount(text), False, text, blocked=status == "blocked")
        except FloodWaitError as exc:
            seconds = int(exc.seconds)
            await asyncio.sleep(max(seconds, 0))
            return Result(False, retryable=False, feedback=f"Telegram：需要等待 {seconds} 秒")
        except PeerFloodError:
            return Result(False, retryable=False, pause=True, feedback="Telegram：账号被限制对外操作")
        finally:
            client.remove_event_handler(on_dm, builder)
            client.remove_event_handler(on_dm, edited)
