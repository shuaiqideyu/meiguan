"""主体入口。"""

import asyncio
import html
import json
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestUsers,
    LinkPreviewOptions,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from meikai.config import Config
from meikai.fresh import is_stale_message
from meikai.login import operator_id
from meikai.modes import push_policies
from meikai.node import NODES
from meikai.node_rpc import attach_link, call_nodes, detach_link, deliver, set_node_up, start_bridge
from meikai.policy import allow, hours_open, mode_key
from meikai.reports import (
    AUTHOR_ID,
    cancel,
    clean_note,
    decide,
    drop_name,
    face,
    hits,
    attach_counts,
    ASK,
    OPEN,
    filed,
    find_target,
    lines,
    parse_report,
    preview,
    ready,
    ensure_brands,
    fake_notice,
    impostor,
    release_known_fakes,
    taken,
    typed_name,
    verdict,
    push_blocks,
    push_brand_delta,
    push_brands,
    push_glyphs,
    refresh,
    save_brands,
    remove,
    submit,
)
from meikai.speak import run as speak_run
from meikai.store import begin_expiry, load_policies, merge_docs, merge_glyphs, save_claim, save_skip
from meikai.webapi import ORIGIN, start_site

log = logging.getLogger(__name__)
_seen: dict[tuple, float] = {}
_bot_app: Application | None = None


async def _send_line(proc: asyncio.subprocess.Process, lock: asyncio.Lock, payload: dict) -> None:
    if proc.stdin is None:
        return
    data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    async with lock:
        proc.stdin.write(data)
        await proc.stdin.drain()


async def _safe_send(node_id: str, proc: asyncio.subprocess.Process, lock: asyncio.Lock, payload: dict) -> None:
    try:
        await _send_line(proc, lock, payload)
    except Exception as exc:
        log.warning("主体：表情同步失败 %s %s", node_id, type(exc).__name__)


async def _apply_learned(msg: dict, links: dict) -> None:
    learned = {str(key): str(value or "") for key, value in (msg.get("glyphs") or {}).items()}
    learned_docs = {str(key): str(value or "") for key, value in (msg.get("docs") or {}).items()}
    if not learned and not learned_docs:
        return
    if learned:
        merge_glyphs(learned)
    if learned_docs:
        merge_docs(learned_docs)
    payload = {"op": "glyph_delta", "glyphs": learned, "docs": learned_docs}
    for node_id, (proc, lock) in list(links.items()):
        asyncio.create_task(_safe_send(node_id, proc, lock, payload))


_store_q: asyncio.Queue | None = None


def _queue_store(kind: str, item: dict) -> None:
    global _store_q
    if _store_q is None:
        _store_q = asyncio.Queue(maxsize=4000)
        asyncio.get_running_loop().create_task(_store_loop())
    try:
        _store_q.put_nowait((kind, item))
    except asyncio.QueueFull:
        (save_claim if kind == "claim" else save_skip)(item)


async def _store_loop() -> None:
    assert _store_q is not None
    while True:
        kind, item = await _store_q.get()
        fn = save_claim if kind == "claim" else save_skip
        try:
            await asyncio.to_thread(fn, item)
        except Exception as exc:
            log.warning("主体：记录没有写入 %s", type(exc).__name__)
            continue
        if kind == "claim" and item.get("ok"):
            from meikai.notify import enqueue

            enqueue(item)


async def _read_node(
    node_id: str, proc: asyncio.subprocess.Process, links: dict, ready: asyncio.Event,
) -> None:
    if proc.stdout is None:
        return
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line.decode())
        except json.JSONDecodeError:
            continue
        if msg.get("id") and deliver(node_id, msg):
            continue
        event = msg.get("event")
        if event == "ready":
            ready.set()
        elif event == "brand":
            asyncio.create_task(_apply_brands(msg.get("items") or []))
        elif event == "sighting":
            asyncio.create_task(fanout_sighting(msg.get("item") or {}))
        elif event == "learned":
            await _apply_learned(msg, links)
        elif event == "watch":
            from meikai.notify import enqueue

            enqueue(msg.get("item") or {})
        elif event == "claim":
            _queue_store("claim", msg.get("item") or {})
        elif event == "skip":
            _queue_store("skip", msg.get("item") or {})
        elif event == "expired":
            asyncio.create_task(notify_expired(msg.get("item") or {}))


async def notify_expired(item: dict) -> None:
    try:
        owner_id = int(item.get("owner_id") or 0)
        user_id = int(item.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    note = str(item.get("note") or "").strip() or "Telegram：登录已失效，请重新登录"
    name = str(item.get("name") or "").strip()
    if not owner_id or not user_id or _bot_app is None:
        return
    if not begin_expiry(owner_id, user_id, note):
        return
    text = f"{name}\n{note}" if name else note
    try:
        await _bot_app.bot.send_message(owner_id, text)
    except Exception as exc:
        log.warning("主体：登录失效通知失败 %s", type(exc).__name__)


async def _watch_once(node_id: str, links: dict) -> None:
    proc = await start_bridge(node_id)
    lock = asyncio.Lock()
    links[node_id] = (proc, lock)
    ready = asyncio.Event()
    read_task = asyncio.create_task(_read_node(node_id, proc, links, ready))
    try:
        await _send_line(proc, lock, {"op": "watch"})
        try:
            await asyncio.wait_for(ready.wait(), 25)
        except TimeoutError:
            log.warning("主体：节点连接没有接通 %s", node_id)
            return
        set_node_up(node_id, True)
        attach_link(node_id, proc, lock)
        asyncio.create_task(push_blocks(node_id))
        asyncio.create_task(push_brands(node_id))
        asyncio.create_task(push_glyphs(node_id))
        await read_task
    finally:
        if not read_task.done():
            read_task.cancel()
        current = links.get(node_id)
        if current is not None and current[0] is proc:
            links.pop(node_id, None)
        detach_link(node_id, proc)
        set_node_up(node_id, False)
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if not read_task.done():
            try:
                await read_task
            except asyncio.CancelledError:
                pass


async def _watch_node(node_id: str, links: dict) -> None:
    delay = 1
    while True:
        started = time.monotonic()
        try:
            await _watch_once(node_id, links)
        except Exception as exc:
            log.warning("主体：表情同步断开 %s %s", node_id, type(exc).__name__)
        if time.monotonic() - started > 10:
            delay = 1
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def fanout_sighting(item: dict) -> None:
    try:
        chat_id = int(item.get("chat_id") or 0)
        msg_id = int(item.get("msg_id") or 0)
        source = int(item.get("owner_id") or 0)
    except (TypeError, ValueError):
        return
    if not chat_id or not msg_id or not source:
        return
    key = (chat_id, msg_id)
    now = time.monotonic()
    if now - _seen.get(key, 0) < 120:
        return
    _seen[key] = now
    if len(_seen) > 4000:
        _seen.clear()
        _seen[key] = now
    username = str(item.get("username") or "")
    kind = str(item.get("kind") or "")
    bots = {str(value) for value in (item.get("bots") or []) if value}
    if hits(bots, chat_id, username):
        return
    saved = load_policies() or {}
    owners = []
    for raw, policy in saved.items():
        if not str(raw).lstrip("-").isdigit():
            continue
        owner = int(raw)
        if owner != source:
            continue
        modes = policy.get("modes") or {}
        if not modes.get(mode_key(kind)):
            continue
        if not hours_open(policy):
            continue
        if not allow(policy, chat_id=chat_id, username=username, bots=bots):
            continue
        owners.append(owner)
    if not owners:
        return
    try:
        await call_nodes({
            "op": "shared",
            "owners": owners,
            "chat_id": chat_id,
            "msg_id": msg_id,
            "username": username,
            "bots": sorted(bots),
            "kind": kind,
            "invite": str(item.get("invite") or "")[:80],
        }, timeout=20)
    except Exception as exc:
        log.warning("主体：共享抢包失败 %s", type(exc).__name__)


_OLD = {"账号", "模块", "名单", "偏好", "账号管理", "模块管理"}


async def _apply_brands(items) -> None:
    rows = [item for item in items or [] if isinstance(item, dict) and item.get("nick")]
    if not rows:
        return
    try:
        saved = save_brands(rows)
        await push_brand_delta(saved)
        if release_known_fakes():
            await push_blocks()
    except Exception as exc:
        log.warning("主体：认证钱包没有记下 %s", type(exc).__name__)


async def _warm(app: Application) -> None:
    try:
        await app.bot.delete_my_commands()
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="管理", web_app=WebAppInfo(url=ORIGIN)),
        )
    except Exception as exc:
        log.warning("主体：菜单 %s", type(exc).__name__)
    await call_nodes({"op": "list", "owner_id": 0}, timeout=15)
    await push_policies()
    await push_brands()
    release_known_fakes()
    await push_blocks()
    await push_glyphs()


async def clear_commands(app: Application) -> None:
    refresh()
    await start_site(app.bot.token)
    loop = asyncio.get_running_loop()
    links: dict = {}
    loop.create_task(speak_run())
    loop.create_task(_warm(app))
    for node_id in NODES:
        loop.create_task(_watch_node(node_id, links))


def build_application(cfg: Config) -> Application:
    global _bot_app
    app = (
        Application.builder()
        .token(cfg.bot_token)
        .post_init(clear_commands)
        .build()
    )
    _bot_app = app
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", on_open, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(
        (filters.StatusUpdate.USERS_SHARED | filters.StatusUpdate.CHAT_SHARED) & private,
        on_shared,
    ))
    app.add_handler(MessageHandler(filters.TEXT & private, on_text))
    app.add_error_handler(on_error)
    return app


_WAIT = 30 * 60
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_HTML = {"parse_mode": ParseMode.HTML, "link_preview_options": _NO_PREVIEW}
_REQUEST_KIND = {1: "user", 2: "bot", 3: "group", 4: "channel"}
_drafts: dict[int, dict] = {}


def _units(text: str) -> int:
    return sum(2 if ord(ch) > 127 else 1 for ch in str(text or ""))


def _pack_buttons(items: list[tuple[str, InlineKeyboardButton]]) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    used = 0
    for label, button in items:
        width = _units(label)
        if width > 14 or (row and (len(row) >= 3 or used + width > 28)):
            if row:
                rows.append(row)
                row = []
                used = 0
            if width > 14:
                rows.append([button])
                continue
        row.append(button)
        used += width
    if row:
        rows.append(row)
    return rows


def open_markup(owner: int = 0) -> InlineKeyboardMarkup:
    items = [
        ("🌐 打开", InlineKeyboardButton("🌐 打开", web_app=WebAppInfo(url=ORIGIN))),
        ("投稿", InlineKeyboardButton("投稿", callback_data="report")),
    ]
    if int(owner or 0) == AUTHOR_ID:
        items.append(("系统屏蔽", InlineKeyboardButton("系统屏蔽", callback_data="blocks")))
    return InlineKeyboardMarkup(_pack_buttons(items))


def _review_markup(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("通过", callback_data=f"r:{report_id}:y"),
        InlineKeyboardButton("拒绝", callback_data=f"r:{report_id}:n"),
    ]])


def _drop_markup(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("移出", callback_data=f"r:{report_id}:x"),
    ]])


def _pick(request_id: int, *, bot: bool | None = None, channel: bool | None = None) -> KeyboardButton:
    if bot is not None:
        return KeyboardButton(
            "👤 用户" if not bot else "🤖 机器人",
            request_users=KeyboardButtonRequestUsers(
                request_id=request_id,
                user_is_bot=bot,
                max_quantity=1,
                request_name=True,
                request_username=True,
            ),
        )
    return KeyboardButton(
        "👥 群组" if not channel else "📣 频道",
        request_chat=KeyboardButtonRequestChat(
            request_id=request_id,
            chat_is_channel=bool(channel),
            request_title=True,
            request_username=True,
        ),
    )


_SUBMIT = "提交"
_BACK = "返回"
_SUBMIT_WORDS = {_SUBMIT, "✅ 提交"}
_BACK_WORDS = {_BACK, "↩️ 返回"}
_FLOW_OK = "report:ok"
_FLOW_BACK = "report:back"


def _type_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [_pick(1, bot=False), _pick(2, bot=True)],
            [_pick(3, channel=False), _pick(4, channel=True)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _flow_markup(*, submit: bool) -> InlineKeyboardMarkup:
    row = []
    if submit:
        row.append(InlineKeyboardButton(_SUBMIT, callback_data=_FLOW_OK))
    row.append(InlineKeyboardButton(_BACK, callback_data=_FLOW_BACK))
    return InlineKeyboardMarkup([row])


def _shown(text: str) -> str:
    if str(text or "").startswith("主体："):
        return str(text)[3:]
    return text


async def _reply_flow(message, text: str, *, submit: bool) -> None:
    text = _shown(text)
    markup = _flow_markup(submit=submit)
    sent = await message.reply_text(text, reply_markup=_type_keyboard(), **_HTML)
    try:
        await sent.edit_text(text, reply_markup=markup, **_HTML)
        return
    except Exception as exc:
        log.warning("主体：投稿按钮 %s", str(exc).replace("\n", " ")[:120])
    try:
        await sent.delete()
    except Exception as exc:
        log.warning("主体：投稿按钮 %s", type(exc).__name__)
    await message.reply_text(text, reply_markup=markup, **_HTML)


def _fresh(owner_id: int) -> dict:
    draft = {"step": "kind", "at": time.monotonic()}
    _drafts[owner_id] = draft
    return draft


def _disarm(owner_id: int) -> None:
    _drafts.pop(owner_id, None)


def _touch(owner_id: int) -> dict | None:
    draft = _drafts.get(owner_id)
    if draft is None:
        return None
    if time.monotonic() - float(draft["at"]) > _WAIT:
        _drafts.pop(owner_id, None)
        return None
    draft["at"] = time.monotonic()
    return draft


def _who(user) -> str:
    if user is None:
        return ""
    full = " ".join(part for part in (
        str(user.first_name or "").strip(),
        str(user.last_name or "").strip(),
    ) if part)
    bits = []
    if full:
        bits.append(" ".join(full.split()))
    handle = str(user.username or "").strip().lstrip("@")
    if handle:
        bits.append(f"@{handle}")
    return " ".join(bits)[:64]


async def show_open(message, owner: int = 0, *, hide_keyboard: bool = False) -> None:
    if not hide_keyboard:
        await message.reply_text(OPEN, reply_markup=open_markup(owner), **_HTML)
        return
    sent = await message.reply_text(OPEN, reply_markup=ReplyKeyboardRemove(), **_HTML)
    try:
        await sent.edit_reply_markup(reply_markup=open_markup(owner))
    except Exception as exc:
        log.warning("主体：打开 %s", type(exc).__name__)


async def _ask_kind(message, owner: int, *, fresh: bool = True) -> None:
    if fresh or owner not in _drafts:
        _fresh(owner)
    else:
        draft = _drafts[owner]
        draft["step"] = "kind"
        draft["at"] = time.monotonic()
    await _reply_flow(message, ASK, submit=False)


async def _show_note(message, draft: dict) -> None:
    draft["step"] = "note"
    await _reply_flow(
        message,
        preview(str(draft.get("kind") or ""), str(draft.get("title") or ""), str(draft.get("username") or ""), int(draft.get("user_id") or 0)),
        submit=False,
    )


async def _show_ready(message, draft: dict) -> None:
    draft["step"] = "confirm"
    await _reply_flow(
        message,
        ready(
            str(draft.get("kind") or ""),
            str(draft.get("title") or ""),
            str(draft.get("username") or ""),
            int(draft.get("user_id") or 0),
            str(draft.get("note") or ""),
        ),
        submit=True,
    )


async def _step_back(message, owner: int, draft: dict) -> None:
    if draft.get("note"):
        draft.pop("note", None)
        if draft.get("user_id"):
            await _show_note(message, draft)
            return
    if draft.get("user_id"):
        for key in ("kind", "username", "user_id", "title"):
            draft.pop(key, None)
        await _ask_kind(message, owner, fresh=False)
        return
    _disarm(owner)
    await show_open(message, owner, hide_keyboard=True)


async def on_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_stale_message(update) or update.message is None:
        return
    owner = operator_id(update)
    if owner is None:
        return
    draft = _drafts.get(owner)
    hide_keyboard = draft is not None
    _disarm(owner)
    await show_open(update.message, owner, hide_keyboard=hide_keyboard)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_stale_message(update) or update.message is None:
        return
    owner = operator_id(update)
    if owner is None:
        return
    text = " ".join((update.message.text or "").split())
    if text in _OLD:
        _disarm(owner)
        await show_open(update.message, owner)
        return
    stale = _drafts.get(owner)
    if stale is not None and time.monotonic() - float(stale["at"]) > _WAIT:
        _disarm(owner)
        await update.message.reply_text("投稿已超时，请重新发起。")
        return
    draft = _touch(owner)
    if text in _BACK_WORDS and draft is not None:
        await _step_back(update.message, owner, draft)
        return
    if text in _SUBMIT_WORDS and draft is not None:
        await _submit_draft(update.message, owner, draft)
        return
    if owner == AUTHOR_ID and text == "系统屏蔽" and (draft is None or draft.get("step") == "kind"):
        await _show_blocks(update.message, 0)
        return
    if owner == AUTHOR_ID and text.startswith("移出") and (draft is None or draft.get("step") == "kind"):
        await _drop_text(update.message, text.removeprefix("移出").strip())
        return
    if text == "投稿":
        await _ask_kind(update.message, owner)
        return
    if draft is None:
        return
    picked = typed_name(text, bare=not draft.get("user_id"))
    if picked is not None:
        await _take_name(update.message, owner, picked)
        return
    if not draft.get("user_id"):
        await _reply_flow(update.message, "请发送 @用户名，或从下方列表选择。", submit=False)
        return
    note, error = clean_note(text)
    if error:
        await _reply_flow(update.message, error, submit=False)
        return
    draft["note"] = note
    draft["who"] = _who(update.message.from_user)
    await _show_ready(update.message, draft)


async def _known_fake(message, owner: int, kind: str, user_id: int, username: str, title: str, note: str = "") -> bool:
    if kind != "bot":
        return False
    ensure_brands()
    hit = impostor(title, username, user_id)
    if not hit:
        return False
    if release_known_fakes():
        await push_blocks()
    row = {
        "kind": kind,
        "title": title,
        "username": username,
        "user_id": user_id,
        "note": note,
    }
    await message.reply_text(
        fake_notice(row, hit),
        reply_markup=ReplyKeyboardRemove(),
        **_HTML,
    )
    _disarm(owner)
    return True


async def _take_name(message, owner: int, parsed: dict) -> None:
    pending = await message.reply_text("正在查询。")
    try:
        found, error = await find_target("", parsed)
    except Exception as exc:
        log.warning("主体：查找用户名 %s", type(exc).__name__)
        found, error = None, "节点：查找失败"
    try:
        await pending.delete()
    except Exception as exc:
        log.warning("主体：查找提示 %s", type(exc).__name__)
    if error or found is None:
        await _reply_flow(message, error or "主体：无法识别", submit=False)
        return
    username = str(found.get("username") or "")
    user_id = int(found.get("user_id") or 0)
    if await _known_fake(
        message, owner, str(found.get("kind") or ""), user_id, username, str(found.get("title") or ""),
    ):
        return
    blocked = taken(username, user_id)
    if blocked:
        await _reply_flow(message, blocked, submit=False)
        return
    _hold(message, owner, str(found.get("kind") or ""), user_id, username, str(found.get("title") or ""))
    draft = _drafts.get(owner)
    if draft is None:
        return
    if draft.get("note"):
        await _show_ready(message, draft)
        return
    await _show_note(message, draft)


def _blocks_view(page: int) -> tuple[str, InlineKeyboardMarkup]:
    from meikai.reports import list_blocks

    items = list_blocks()
    if not items:
        return "还没有屏蔽。", InlineKeyboardMarkup([])
    size = 8
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(int(page), pages - 1))
    chunk = items[page * size:(page + 1) * size]
    lines = ["<b>系统屏蔽</b>", "点移出即可去掉。"]
    packed = []
    for item in chunk:
        title = " ".join(str(item.get("title") or "").split()) or "未命名"
        handle = str(item.get("username") or "").strip().lstrip("@")
        if handle.lstrip("-").isdigit():
            handle = ""
        shown = f"{title} @{handle}" if handle else title
        lines.append(html.escape(shown)[:80])
        label = f"移出 {title}"[:30]
        packed.append((label, InlineKeyboardButton(label, callback_data=f"bx:{int(item['user_id'])}")))
    buttons = _pack_buttons(packed)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"blocks:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"blocks:{page + 1}"))
    if nav:
        buttons.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def _show_blocks(message, page: int, *, edit: bool = False) -> None:
    text, markup = _blocks_view(page)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup, **_HTML)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
    await message.reply_text(text, reply_markup=markup, **_HTML)


async def _drop_block(query, owner: int, user_id: int) -> None:
    from meikai.reports import drop_name, list_blocks, remove

    if owner != AUTHOR_ID:
        await query.answer("不能管理屏蔽")
        return
    found = next((item for item in list_blocks() if int(item.get("user_id") or 0) == user_id), None)
    if found is None:
        await query.answer("不在屏蔽名单")
        return
    row = None
    error = ""
    report_id = int(found.get("report_id") or 0)
    if report_id:
        row, error = remove(report_id)
    if error or not report_id:
        error = drop_name(str(user_id))
        row = None
    if error:
        await query.answer(_shown(error))
        return
    await query.answer("已移出")
    await push_blocks()
    if row and _bot_app is not None:
        try:
            await _bot_app.bot.send_message(int(row["owner_id"]), verdict(row, "out"), **_HTML)
        except Exception:
            log.warning("主体：移出通知失败")
    if query.message is not None:
        await _show_blocks(query.message, 0, edit=True)


async def _drop_text(message, text: str) -> None:
    username = parse_report(text)
    if not username:
        await message.reply_text("无法识别")
        return
    error = drop_name(username)
    if error:
        await message.reply_text(_shown(error))
        return
    await message.reply_text(f"已移出 {face({'username': username})}")
    await push_blocks()


def _hold(message, owner: int, kind: str, user_id: int, username: str, title: str) -> None:
    name = str(username or "").strip().lstrip("@")
    draft = _drafts.setdefault(owner, {"step": "note", "at": time.monotonic()})
    draft["at"] = time.monotonic()
    draft["kind"] = kind
    draft["username"] = name or str(user_id)
    draft["user_id"] = user_id
    draft["title"] = " ".join(str(title or "").split())[:40]
    draft["who"] = _who(message.from_user)
    draft["step"] = "confirm" if draft.get("note") else "note"


async def on_shared(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner = operator_id(update)
    if is_stale_message(update) or update.message is None or owner is None:
        return
    message = update.message
    users = message.users_shared
    chat = message.chat_shared
    if users is not None and users.users:
        kind = _REQUEST_KIND.get(int(users.request_id))
        person = users.users[0]
        if kind is None:
            return
        user_id, username, title = int(person.user_id), str(person.username or ""), " ".join(
            part for part in (str(person.first_name or ""), str(person.last_name or "")) if part
        )
    elif chat is not None:
        kind = _REQUEST_KIND.get(int(chat.request_id))
        if kind is None:
            return
        user_id, username, title = int(chat.chat_id), str(chat.username or ""), str(chat.title or "")
    else:
        return
    if await _known_fake(message, owner, kind, user_id, username, title):
        return
    error = taken(username, user_id)
    if error:
        await _reply_flow(message, error, submit=False)
        return
    _hold(message, owner, kind, user_id, username, title)
    draft = _drafts.get(owner)
    if draft is None:
        return
    if draft.get("note"):
        await _show_ready(message, draft)
        return
    await _show_note(message, draft)


async def _submit_draft(message, owner: int, draft: dict) -> None:
    if not draft.get("user_id"):
        await _reply_flow(message, "请发送 @用户名，或从下方列表选择。", submit=False)
        return
    if not draft.get("note"):
        await _reply_flow(message, "请发送提交原因。", submit=False)
        return
    if await _known_fake(
        message,
        owner,
        str(draft.get("kind") or ""),
        int(draft.get("user_id") or 0),
        str(draft.get("username") or ""),
        str(draft.get("title") or ""),
        str(draft.get("note") or ""),
    ):
        return
    row, error = submit(
        owner,
        str(draft.get("username") or ""),
        int(draft.get("user_id") or 0),
        str(draft.get("title") or ""),
        str(draft.get("who") or _who(message.from_user)),
        str(draft.get("kind") or ""),
        str(draft.get("note") or ""),
    )
    if error or row is None:
        await message.reply_text(_shown(error or "无法识别"))
        if error in ("主体：已在屏蔽名单", "主体：已提交，等待审核"):
            _disarm(owner)
        return
    row = attach_counts(row)
    try:
        await message.get_bot().send_message(
            AUTHOR_ID,
            lines(row, "投稿"),
            reply_markup=_review_markup(int(row["id"])),
            **_HTML,
        )
    except Exception as exc:
        cancel(int(row["id"]))
        log.warning("主体：投稿通知失败 %s", type(exc).__name__)
        await message.reply_text("通知失败")
        return
    _disarm(owner)
    await message.reply_text(filed(row), reply_markup=ReplyKeyboardRemove(), **_HTML)


async def _review(query, context, owner: int, data: str) -> None:
    if owner != AUTHOR_ID:
        await query.answer("不能审核")
        return
    parts = data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"y", "n", "x"}:
        await query.answer("已经处理")
        return
    report_id = int(parts[1])
    action = parts[2]
    if action == "x":
        row, error = remove(report_id)
        head, status = "已移出", "out"
    else:
        row, error = decide(report_id, action == "y")
        head = "已确认" if action == "y" else "已拒绝"
        status = "yes" if action == "y" else "no"
    if error or row is None:
        await query.answer(_shown(error or "已经处理"))
        return
    row = attach_counts(row)
    known = row.get("known_fake") if isinstance(row.get("known_fake"), dict) else None
    await query.answer("已识别" if known else head)
    markup = InlineKeyboardMarkup([]) if known or status != "yes" else _drop_markup(report_id)
    shown = fake_notice(row, known) if known else lines(row, head)
    try:
        await query.edit_message_text(shown, reply_markup=markup, **_HTML)
    except Exception as exc:
        if "not modified" not in str(exc).lower():
            log.warning("主体：投稿更新失败 %s", type(exc).__name__)
    try:
        await context.bot.send_message(
            int(row["owner_id"]),
            fake_notice(row, known) if known else verdict(row, status),
            **_HTML,
        )
    except Exception:
        log.warning("主体：投稿回执失败 id=%s", report_id)
    await push_blocks()


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    owner = operator_id(update)
    if owner is None:
        await query.answer()
        return
    data = query.data or ""
    if data == "report":
        await query.answer()
        if query.message is not None:
            await _ask_kind(query.message, owner)
        return
    if data == "blocks" or data.startswith("blocks:"):
        if owner != AUTHOR_ID:
            await query.answer("不能管理屏蔽")
            return
        page = 0
        if data.startswith("blocks:"):
            try:
                page = int(data.removeprefix("blocks:"))
            except ValueError:
                page = 0
        await query.answer()
        if query.message is not None:
            await _show_blocks(query.message, page, edit=data.startswith("blocks:"))
        return
    if data.startswith("bx:"):
        try:
            user_id = int(data.removeprefix("bx:"))
        except ValueError:
            await query.answer("无法识别")
            return
        await _drop_block(query, owner, user_id)
        return
    if data in {_FLOW_OK, _FLOW_BACK}:
        if query.message is None:
            await query.answer()
            return
        draft = _touch(owner)
        if draft is None:
            await query.answer("这条已经处理过了")
            return
        await query.answer()
        if data == _FLOW_BACK:
            await _step_back(query.message, owner, draft)
        else:
            await _submit_draft(query.message, owner, draft)
        return
    if data.startswith("r:"):
        await _review(query, context, owner, data)
        return
    await query.answer("已改到菜单里打开")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if err is not None and "not modified" in str(err).lower():
        return
    log.error("处理更新失败", exc_info=err)
