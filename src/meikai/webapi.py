"""管理页。只听本机，身份来自 Telegram initData。"""

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from meikai.accounts import (
    added_text,
    collect,
    drop_account,
    format_amount,
    load_balances,
    lang_text,
    nickname,
    remembered,
    run_wallet_scan,
    scanning,
    username_text,
)
from meikai.debug import peek_message
from meikai.lists import find_window, resolve_window
from meikai.login import (
    call_entry,
    complete_import,
    explain,
    import_pack,
    normalize_phone,
    qr_png,
)
from meikai.grab.catalog import listing
from meikai.modes import push_policies
from meikai.node import NODES, node_for_dc, place_text
from meikai.node_rpc import call_node, call_nodes
from meikai.policy import (
    ALL_KINDS,
    COINS,
    AUTO_JOIN,
    account_joins,
    account_brief,
    account_paused,
    account_watches,
    set_join,
    set_phrase_say,
    set_watch,
    append_entry,
    assign_hot,
    shield_picks,
    balance_hidden,
    clear_axis,
    lookup_query,
    parse_target,
    remove_entry,
    set_axis,
    set_floor,
    set_hours,
    set_hot_seat,
    set_hot_sign,
    set_hot_speak,
    set_mode_delay,
    set_pay_keys,
    present_policy,
    set_running,
    set_show_min,
)
from meikai.store import (
    clear_expiry,
    face_used,
    list_claims,
    load_policy,
    notifier_face,
    put_session,
    read_face,
    save_policy,
    take_session,
    today_text,
    used_nodes,
    write_face,
)

log = logging.getLogger(__name__)

ORIGIN = (os.environ.get("WEB_ORIGIN") or "").strip().rstrip("/")
_HITS: dict[str, list[float]] = {}
_LOGIN: dict[int, dict] = {}
_DIALOGS: dict[tuple[int, bool], tuple[float, list]] = {}
_DIST = Path(os.environ.get("WEB_DIST") or Path(__file__).resolve().parents[2] / "web" / "dist")
# 同一窗口的照片按内容存一份。一小时内不重复问 Telegram，过了只对照片 id，变了才重新下载。
FACE_TTL = 3600
_FACE_DIR = Path("/opt/meikai/data/faces")
_FACE: dict[int, tuple[int, str, int]] = {}
_FACE_JOB: dict[int, asyncio.Task] = {}
_FACE_HOLD: dict[int, int] = {}


def web_port() -> int:
    raw = (os.environ.get("WEB_PORT") or "8780").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return 8780


def _shown(message: str) -> str:
    text = str(message or "")
    if text.startswith("主体："):
        return text[3:]
    return text


def fail(status: int, error: str, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": error, "message": _shown(message)}, status=status)


def done(payload: dict, status: int = 200) -> web.Response:
    body = {"ok": True}
    body.update(payload)
    return web.json_response(body, status=status)


def allow_hit(key: str, limit: int, window: int) -> bool:
    now = time.time()
    bucket = [item for item in _HITS.get(key, []) if now - item < window]
    if len(bucket) >= limit:
        _HITS[key] = bucket
        return False
    bucket.append(now)
    _HITS[key] = bucket
    return True


def client_ip(request: web.Request) -> str:
    return str(request.headers.get("CF-Connecting-IP") or request.remote or "")[:64]


def origin_ok(request: web.Request) -> bool:
    origin = request.headers.get("Origin", "")
    return not origin or origin == ORIGIN


def _pairs(init_data: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for part in init_data.split("&"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        found[unquote(key)] = unquote(value.replace("+", " "))
    return found


def _signed(fields: dict[str, str], received: str, bot_token: str) -> bool:
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated, received.lower())


def verify_init(init_data: str, bot_token: str) -> tuple[int | None, str]:
    if not init_data:
        return None, "empty"
    if len(init_data) > 16384:
        return None, "long"
    data = _pairs(init_data)
    received = data.pop("hash", "")
    if not received:
        return None, "hash"
    without_signature = {key: value for key, value in data.items() if key != "signature"}
    if not _signed(without_signature, received, bot_token) and not _signed(data, received, bot_token):
        return None, "hash"
    try:
        auth_date = int(data.get("auth_date") or 0)
    except ValueError:
        return None, "expired"
    now = time.time()
    if auth_date <= 0 or auth_date - now > 300 or now - auth_date > 3600:
        return None, "expired"
    try:
        user = json.loads(data.get("user") or "")
    except json.JSONDecodeError:
        return None, "user"
    if not isinstance(user, dict):
        return None, "user"
    try:
        owner_id = int(user.get("id") or 0)
    except (TypeError, ValueError):
        return None, "user"
    if owner_id <= 0:
        return None, "user"
    return owner_id, ""


def _check_init() -> None:
    from urllib.parse import quote

    token = "42:TEST"
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AA",
        "user": '{"id":42,"first_name":"A+B","username":"ab"}',
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    encoded = "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in fields.items())
    owner, reason = verify_init(f"{encoded}&signature=abc&hash={digest}", token)
    assert reason == "" and owner == 42


_check_init()


def owner_of(request: web.Request) -> int | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token or len(token) > 200:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    return take_session(digest)


async def read_body(request: web.Request) -> dict | None:
    if request.content_length and request.content_length > 100_000:
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body


def usdt_of(entry: dict, policy: dict) -> str:
    from decimal import Decimal

    total = Decimal(0)
    for coins in (entry.get("wallets") or {}).values():
        raw = (coins or {}).get("USDT")
        if raw:
            total += Decimal(str(raw))
    if total == 0 or balance_hidden(policy, "USDT", total):
        return ""
    return format_amount(total)


def org_of(person: dict) -> str:
    raw = str(person.get("org") or "").strip().lower()
    if raw in {"hold", "major"}:
        return raw
    if person.get("hold") is True:
        return "hold"
    return ""


def shape_person(person: dict, policy: dict, balances: dict) -> dict:
    user_id = int(person.get("user_id") or 0)
    node = NODES.get(person.get("node_id") or "")
    raw_dc = person.get("dc")
    dc = int(raw_dc) if str(raw_dc or "").isdigit() else 0
    expected = node_for_dc(dc) if dc else None
    entry = (balances.get("accounts") or {}).get(str(user_id)) or {}
    errors = [item for item in entry.get("errors") or [] if "请求过于频繁" not in item]
    reason = str(person.get("reason") or "")
    return {
        "user_id": user_id,
        "node_id": person.get("node_id") or "",
        "name": nickname(person) or str(user_id),
        "username": username_text(person),
        "online": bool(person.get("online")) and not reason,
        "reason": reason,
        "paused": account_paused(policy, user_id),
        "join": account_joins(policy, user_id),
        "watch": account_watches(policy, user_id),
        "brief": account_brief(policy, user_id),
        "dc": dc,
        "home": place_text(expected, dc) or ("未知" if not dc else f"DC{dc}"),
        "city": place_text(node, dc),
        "added_at": added_text(person.get("added_at") or ""),
        "active_at": str(person.get("active_at") or ""),
        "active": added_text(str(person.get("active_at") or "")) or "无",
        "nft": bool(person.get("nft")),
        "org": org_of(person),
        "premium": bool(person.get("premium")),
        "phone": str(person.get("phone") or ""),
        "about": str(person.get("about") or "").strip(),
        "birthday": str(person.get("birthday") or ""),
        "lang": lang_text(person.get("lang") or ""),
        "verified": bool(person.get("verified")),
        "restricted": bool(person.get("restricted")),
        "scam": bool(person.get("scam")),
        "fake": bool(person.get("fake")),
        "usdt": usdt_of(entry, policy),
        "wallets": entry.get("wallets") or {},
        "errors": errors,
    }


def view_policy(policy: dict, owner_id: int) -> dict:
    from meikai.reports import AUTHOR_ID

    shown = present_policy(policy)
    admin = int(owner_id) == AUTHOR_ID
    shown["pay_admin"] = admin
    if admin:
        shown["pay_ready"] = {"fll": True, "cf": True}
    return shown


async def snapshot(owner_id: int, people=None, offline=None, max_age: float = 2) -> dict:
    if people is None:
        people, offline = await collect(owner_id, max_age=max_age)
    for person in people:
        user_id = int(person.get("user_id") or 0)
        if user_id and person.get("online") and not person.get("reason"):
            clear_expiry(user_id)
    policy = load_policy(owner_id)
    balances = load_balances(owner_id)
    return {
        "accounts": [shape_person(person, policy, balances) for person in people],
        "policy": view_policy(policy, owner_id),
        "packets": listing(),
        "balance_at": added_text(balances.get("at") or ""),
        "pending": balances.get("pending") or ("正在刷新" if scanning(owner_id) else ""),
        "offline": offline,
        "join_open": AUTO_JOIN,
        "notify": notifier_face(owner_id),
    }


_push_job: asyncio.Task | None = None
_push_again = False


def _schedule_push() -> None:
    global _push_job, _push_again
    _push_again = True
    if _push_job is not None and not _push_job.done():
        return
    _push_job = asyncio.get_running_loop().create_task(_run_push())


async def _run_push() -> None:
    global _push_job, _push_again
    try:
        while True:
            _push_again = False
            try:
                failed = await push_policies()
                if failed:
                    log.warning("主体：配置没有推到节点 %s", ",".join(failed))
            except Exception as exc:
                log.warning("主体：配置没有推到节点 %s", type(exc).__name__)
            if not _push_again:
                break
    finally:
        _push_job = None
        if _push_again:
            _schedule_push()


_policy_locks: dict[int, asyncio.Lock] = {}


def policy_lock(owner_id: int) -> asyncio.Lock:
    lock = _policy_locks.get(owner_id)
    if lock is None:
        lock = asyncio.Lock()
        _policy_locks[owner_id] = lock
    return lock


async def persist(owner_id: int, policy: dict) -> dict:
    saved = save_policy(owner_id, policy)
    _schedule_push()
    return {"policy": view_policy(saved, owner_id), "offline": []}


def _dialog_kind(item: dict) -> str:
    kind = str(item.get("kind") or "")
    if kind in {"user", "group", "channel", "bot"}:
        return kind
    return "chat"


def _collapse_dialogs(items: list[dict]) -> list[dict]:
    kept = []
    seen_name = set()
    for item in sorted(items, key=lambda row: (bool(row.get("active_at")), str(row.get("active_at") or "")), reverse=True):
        kind = _dialog_kind(item)
        username = str(item.get("username") or "").strip().lstrip("@").lower()
        number = item.get("user_id") if kind == "bot" else item.get("chat_id")
        if username:
            name = (kind, "u", username)
        else:
            name = (kind, "id", number)
        if name in seen_name:
            continue
        seen_name.add(name)
        kept.append(item)
    return kept


def _name_blocked(item: dict) -> bool:
    from meikai.reports import watched

    return watched(item.get("title")) or watched(item.get("username"))


def _window_name(item: dict) -> str:
    title = " ".join(str(item.get("title") or "").split())
    if title:
        return title[:20]
    username = str(item.get("username") or "").strip().lstrip("@")
    if username and not username.lstrip("-").isdigit():
        return f"@{username}"
    return "这个窗口"


def _system_blocked(item: dict) -> bool:
    from meikai.reports import hits

    if str(item.get("kind") or "") == "bot":
        return bool(hits(
            bots=[item.get("username") or "", item.get("user_id")],
            title=str(item.get("title") or ""),
        ))
    return bool(hits(
        chat_id=int(item.get("chat_id") or 0),
        username=str(item.get("username") or ""),
        title=str(item.get("title") or ""),
    ))


def _skip_note(names: list[str]) -> str:
    shown = "、".join(names[:3])
    if len(names) > 3:
        shown += f"等 {len(names)} 个"
    return f"{shown}已在系统屏蔽，没有加入"


def _dialog_row(item: dict, bots: bool) -> tuple[tuple, dict] | None:
    if not isinstance(item, dict) or _name_blocked(item):
        return None
    kind = _dialog_kind(item)
    if kind in {"bot", "user"} and not bots:
        return None
    try:
        number = int(item.get("user_id") if kind == "bot" else item.get("chat_id"))
    except (TypeError, ValueError):
        return None
    row = {
        "kind": kind,
        "title": str(item.get("title") or number)[:40],
        "username": str(item.get("username") or ""),
        "chat_id": number,
        "active_at": str(item.get("active_at") or ""),
    }
    if kind == "bot":
        row["user_id"] = number
    return (kind, number), row


async def _load_dialogs(owner_id: int, bots: bool) -> tuple[list, list, dict | None]:
    key = (owner_id, bots)
    cached = _DIALOGS.get(key)
    if cached and time.time() - cached[0] < 60:
        return cached[1], [], None
    payload = {"op": "dialogs", "owner_id": owner_id}
    if bots:
        payload["bots"] = True
    results = await call_nodes(payload, timeout=25)
    rows: dict[tuple, dict] = {}
    offline = []
    saw = False
    flood = None
    used = used_nodes(owner_id)
    for node_id, result in results.items():
        if result.get("ok"):
            saw = True
            for item in result.get("dialogs") or []:
                folded = _dialog_row(item, bots)
                if folded is None:
                    continue
                mark, row = folded
                old = rows.get(mark)
                if old and str(old.get("active_at") or "") > str(row.get("active_at") or ""):
                    row["active_at"] = old["active_at"]
                rows[mark] = row
            continue
        if result.get("error") == "flood":
            flood = result
            continue
        if result.get("error") == "offline" and node_id in used:
            offline.append(NODES[node_id].city_zh)
    if not saw:
        if flood:
            return [], [], {"error": "flood", "message": "Telegram：请求过于频繁，请稍后再试"}
        message = "节点：未连接" if offline else "节点：没有读到窗口"
        return [], [], {"error": "offline", "message": message}
    items = _collapse_dialogs(list(rows.values()))
    _DIALOGS[key] = (time.time(), items)
    return items, offline, None


def business(result: dict) -> web.Response:
    return web.json_response(
        {"ok": False, "error": result.get("error") or "internal", "message": _shown(result.get("message") or explain(result))},
        status=200,
    )


@web.middleware
async def guard(request: web.Request, handler):
    if request.path.startswith("/api"):
        if not origin_ok(request):
            return fail(403, "origin", "主体：来源不对")
        if request.path != "/api/session":
            owner_id = owner_of(request)
            if owner_id is None:
                return fail(401, "session", "主体：请重新打开")
            request["owner_id"] = owner_id
    try:
        return await handler(request)
    except Exception as exc:
        log.warning("web %s %s", request.path, type(exc).__name__)
        return fail(500, "internal", "主体：处理失败")


async def open_session(request: web.Request) -> web.Response:
    if not allow_hit(f"session:{client_ip(request)}", 30, 60):
        return fail(429, "too_many", "主体：打开过于频繁")
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        body = await read_body(request)
        init_data = str((body or {}).get("init_data") or "")
    owner_id, reason = verify_init(init_data, request.app["bot_token"])
    if owner_id is None:
        log.warning("主体：身份无效 %s", reason or "hash")
        message = "Telegram：请重新打开" if reason == "expired" else "Telegram：身份无效"
        return fail(401, "init", message)
    token = secrets.token_urlsafe(32)
    expires = int(time.time()) + 12 * 3600
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not put_session(digest, owner_id, expires):
        return fail(500, "internal", "主体：处理失败")
    return done({"token": token, "expires_at": expires})


async def get_home(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    if request.rel_url.query.get("progress") != "1":
        return done(await snapshot(owner_id, max_age=45))
    response = web.StreamResponse(status=200, headers={
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Cache-Control": "no-store, no-transform",
        "X-Content-Type-Options": "nosniff",
    })
    await response.prepare(request)
    total = len(NODES)
    done_n = 0
    lock = asyncio.Lock()

    async def emit(payload: dict) -> None:
        await response.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())

    await response.write(b" " * 2048 + b"\n")
    cached, cached_off = remembered(owner_id)
    if cached:
        await emit({"done": 0, "total": total, "home": await snapshot(owner_id, cached, cached_off)})
    else:
        await emit({"done": 0, "total": total})

    async def on_node(_node_id: str) -> None:
        nonlocal done_n
        async with lock:
            done_n += 1
            await emit({"done": done_n, "total": total})

    try:
        people, offline = await collect(owner_id, on_node, max_age=45)
        await emit({"done": total, "total": total, "home": await snapshot(owner_id, people, offline)})
    except Exception as exc:
        log.warning("home progress %s", type(exc).__name__)
        await emit({"ok": False, "message": "处理失败"})
    await response.write_eof()
    return response


def _pay_state(value) -> str:
    text = str(value or "")
    return text if text in {"ok", "invalid", "empty", "down"} else "down"


def _pay_amount(value) -> str:
    text = str(value or "").strip()
    whole, dot, frac = text.partition(".")
    if not whole.isdigit() or len(whole) > 8 or (dot and (not frac.isdigit() or len(frac) > 5)):
        return ""
    return text


async def _user_pay_balance(owner_id: int) -> dict:
    from meikai.grab.paybalance import ask_balance

    policy = load_policy(owner_id)
    keys = policy.get("pay_keys") if isinstance(policy.get("pay_keys"), dict) else {}
    (fll_state, fll), (cf_state, cf) = await asyncio.gather(
        ask_balance("fll", str(keys.get("fll") or "")),
        ask_balance("cf", str(keys.get("cf") or "")),
    )
    return {"fll": fll, "cf": cf, "fll_state": fll_state, "cf_state": cf_state}


async def _system_pay_balance() -> dict:
    from meikai.node import NODES

    for node_id in NODES:
        result = await call_node(node_id, {"op": "pay_balance"}, timeout=12)
        if not result.get("ok"):
            continue
        return {
            "fll": _pay_amount(result.get("fll")),
            "cf": _pay_amount(result.get("cf")),
            "fll_state": _pay_state(result.get("fll_state")),
            "cf_state": _pay_state(result.get("cf_state")),
        }
    return {"fll": "", "cf": "", "fll_state": "down", "cf_state": "down"}


async def get_pay_balance(request: web.Request) -> web.Response:
    from meikai.reports import AUTHOR_ID

    owner_id = int(request["owner_id"])
    if not allow_hit(f"paybal:{owner_id}", 20, 60):
        return fail(429, "too_many", "查询过于频繁")
    if owner_id == AUTHOR_ID:
        return done(await _system_pay_balance())
    return done(await _user_pay_balance(owner_id))


async def get_balances(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    balances = load_balances(owner_id)
    policy = load_policy(owner_id)
    people, offline = await collect(owner_id)
    return done({
        "accounts": [shape_person(person, policy, balances) for person in people],
        "balance_at": added_text(balances.get("at") or ""),
        "pending": balances.get("pending") or ("正在刷新" if scanning(owner_id) else ""),
        "offline": offline,
    })


async def patch_policy(request: web.Request) -> web.Response:
    body = await read_body(request)
    if body is None:
        return fail(400, "body", "主体：无法识别")
    owner_id = request["owner_id"]
    async with policy_lock(owner_id):
        return await _patch_policy(owner_id, body)


async def _patch_policy(owner_id: int, body: dict) -> web.Response:
    policy = load_policy(owner_id)
    modes = body.get("modes")
    if isinstance(modes, dict):
        for kind, value in modes.items():
            if kind in ALL_KINDS:
                policy["modes"][kind] = bool(value)
    spans = body.get("mode_delays")
    if isinstance(spans, dict):
        for kind, span in spans.items():
            if not isinstance(span, dict):
                return fail(400, "delay", "主体：无法识别")
            try:
                low = float(span.get("low") or 0)
                high = float(span.get("high") or 0)
            except (TypeError, ValueError):
                return fail(400, "delay", "主体：延迟请写在 0 到 60 秒")
            policy, error = set_mode_delay(policy, str(kind), low, high)
            if error:
                return fail(400, "delay", error)
    if isinstance(body.get("phrase_say"), dict):
        try:
            low = float(body["phrase_say"].get("low") or 0)
            high = float(body["phrase_say"].get("high") or 0)
        except (TypeError, ValueError):
            return fail(400, "delay", "主体：延迟请写在 0 到 60 秒")
        policy, error = set_phrase_say(policy, low, high)
        if error:
            return fail(400, "delay", error)
    from meikai.reports import AUTHOR_ID

    admin = int(owner_id) == AUTHOR_ID
    if isinstance(body.get("web_pay"), dict):
        pay = dict(policy.get("web_pay") or {})
        keys = policy.get("pay_keys") if isinstance(policy.get("pay_keys"), dict) else {}
        for key in ("fll", "cf"):
            if key not in body["web_pay"]:
                continue
            want = bool(body["web_pay"][key])
            if want and not admin and not str(keys.get(key) or "").strip():
                return fail(400, "pay", "请先填写 token")
            pay[key] = want
        policy["web_pay"] = pay
    if isinstance(body.get("pay_keys"), dict):
        if not admin:
            policy, error = set_pay_keys(policy, body["pay_keys"])
            if error:
                return fail(400, "pay", error)
            log.info("主体：打码 token 已更新 owner=%s", owner_id)
    if "count_over" in body:
        policy["count_over"] = bool(body.get("count_over"))
    if "auto_receive" in body:
        policy["auto_receive"] = bool(body.get("auto_receive"))
    if "window_block_keywords" in body:
        raw_kw = body.get("window_block_keywords")
        if isinstance(raw_kw, str):
            policy["window_block_keywords"] = [p.strip() for p in raw_kw.replace("，", ",").replace("\n", ",").split(",") if p.strip()]
        elif isinstance(raw_kw, list):
            policy["window_block_keywords"] = [str(p).strip() for p in raw_kw if str(p).strip()]
        else:
            policy["window_block_keywords"] = []
    if "window_block_minutes" in body:
        try:
            policy["window_block_minutes"] = max(0, min(int(body.get("window_block_minutes")), 1440))
        except (TypeError, ValueError):
            policy["window_block_minutes"] = 0
    if "open_at" in body or "close_at" in body:
        policy, error = set_hours(
            policy,
            body.get("open_at", policy.get("open_at")),
            body.get("close_at", policy.get("close_at")),
        )
        if error:
            return fail(400, "hours", error)
    if "window_mode" in body:
        policy = set_axis(policy, "window", str(body.get("window_mode") or ""))
    if "bot_mode" in body:
        policy = set_axis(policy, "bot", str(body.get("bot_mode") or ""))
    if "burn" in body:
        on = bool(body.get("burn"))
        policy["burn"] = on
        policy["burns"] = {"captcha": False, "phrase": on}
    incoming = body.get("burns")
    if isinstance(incoming, dict):
        phrase_on = bool((policy.get("burns") or {}).get("phrase"))
        if "phrase" in incoming:
            phrase_on = bool(incoming["phrase"])
        elif incoming.get("captcha"):
            phrase_on = True
        policy["burns"] = {"captcha": False, "phrase": phrase_on}
        policy["burn"] = phrase_on
    if "unknown_coin" in body:
        policy["unknown_coin"] = bool(body.get("unknown_coin"))
    if "hide_zero" in body:
        policy["hide_zero"] = bool(body.get("hide_zero"))
    coins = body.get("coins")
    if isinstance(coins, dict):
        for code in COINS:
            if code in coins:
                policy["coins"][code] = bool(coins[code])
    for kind, field in (("count", "min_counts"), ("total", "min_totals"), ("each", "min_eaches")):
        if field not in body:
            continue
        mapping = body.get(field)
        if not isinstance(mapping, dict):
            return fail(400, "floor", "主体：无法识别")
        for code in COINS:
            policy = set_floor(policy, kind, code, "")
        for code, value in mapping.items():
            policy = set_floor(policy, kind, str(code), "" if value in (None, "", 0, "0") else str(value))
    if isinstance(body.get("show_min"), dict):
        for code in COINS:
            policy = set_show_min(policy, code, "")
        for code, value in body["show_min"].items():
            policy = set_show_min(policy, str(code), "" if value in (None, "", 0, "0") else str(value))
    hot_fields = {}
    if "hot_on" in body:
        hot_fields["on"] = bool(body.get("hot_on"))
    if "hot_low" in body or "hot_high" in body:
        try:
            hot_fields["low"] = int(body.get("hot_low") if "hot_low" in body else policy["hot_low"])
            hot_fields["high"] = int(body.get("hot_high") if "hot_high" in body else policy["hot_high"])
        except (TypeError, ValueError):
            return fail(400, "hot", "主体：无法识别")
    if "hot_lines" in body:
        if not isinstance(body.get("hot_lines"), list):
            return fail(400, "hot", "主体：无法识别")
        hot_fields["lines"] = body["hot_lines"]
    if "hot_drop" in body or "hot_drop_ms" in body:
        try:
            if "hot_drop" in body:
                hot_fields["drop"] = int(body.get("hot_drop") or 0)
            else:
                raw_drop = int(body.get("hot_drop_ms") or 0)
                hot_fields["drop"] = 0 if raw_drop <= 0 else (raw_drop + 500) // 1000
        except (TypeError, ValueError):
            return fail(400, "hot", "主体：无法识别")
    if "hot_packs" in body:
        if not isinstance(body.get("hot_packs"), list):
            return fail(400, "hot", "主体：无法识别")
        hot_fields["packs"] = body["hot_packs"]
    if "hot_open" in body or "hot_close" in body:
        hot_fields["start"] = body.get("hot_open", policy.get("hot_open"))
        hot_fields["stop"] = body.get("hot_close", policy.get("hot_close"))
    if "hot_cap" in body:
        try:
            hot_fields["cap"] = int(body.get("hot_cap") or 0)
        except (TypeError, ValueError):
            return fail(400, "hot", "主体：每天上限请写在 0 到 500")
    if "hot_off" in body:
        if not isinstance(body.get("hot_off"), list):
            return fail(400, "hot", "主体：无法识别")
        hot_fields["muted"] = body["hot_off"]
    seat = body.get("hot_seat")
    if hot_fields:
        policy, error = assign_hot(policy, **hot_fields)
        if error:
            return fail(400, "hot", error)
    if isinstance(seat, dict):
        policy, error = set_hot_seat(policy, seat.get("chat_id"), seat.get("user_id"), bool(seat.get("on")))
        if error:
            return fail(400, "hot", error)
    sign = body.get("hot_sign")
    if isinstance(sign, dict):
        policy, error = set_hot_sign(policy, sign.get("chat_id"), bool(sign.get("on")))
        if error:
            return fail(400, "hot", error)
    speak = body.get("hot_speak")
    if isinstance(speak, dict):
        policy, error = set_hot_speak(policy, speak.get("chat_id"), bool(speak.get("on")))
        if error:
            return fail(400, "hot", error)
    return done(await persist(owner_id, policy))


async def post_entry(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    axis = str(body.get("axis") or "")
    if axis not in {"window", "bot"}:
        return fail(400, "axis", "主体：无法识别")
    owner_id = request["owner_id"]
    picks = body.get("picks")
    if isinstance(picks, list):
        cached = _DIALOGS.get((owner_id, True))
        chosen, error_text = shield_picks(cached[1] if cached else [], picks)
        if error_text:
            return fail(400, "target", error_text)
        async with policy_lock(owner_id):
            policy = load_policy(owner_id)
            added = False
            skipped = []
            for item in chosen:
                if _system_blocked(item) or _name_blocked(item):
                    skipped.append(_window_name(item))
                    continue
                policy, error_text = append_entry(policy, "window", item)
                if error_text == "主体：已经在名单里":
                    continue
                if error_text:
                    return fail(400, "target", error_text)
                added = True
            if not added:
                if skipped:
                    return fail(400, "target", _skip_note(skipped))
                return fail(400, "target", "主体：已经在名单里")
            saved = await persist(owner_id, policy)
        if skipped:
            saved["message"] = _skip_note(skipped)
        return done(saved)
    parsed = parse_target(str(body.get("text") or ""))
    if parsed is None:
        return fail(400, "target", "主体：写成 @用户名 或链接")
    parsed, error_text = await resolve_window(owner_id, parsed)
    if error_text or parsed is None:
        return fail(400, "target", error_text or "节点：没有这个窗口")
    if _system_blocked(parsed) or _name_blocked(parsed):
        return fail(400, "target", _skip_note([_window_name(parsed)]))
    async with policy_lock(owner_id):
        policy, error_text = append_entry(load_policy(owner_id), "window", parsed)
        if error_text:
            return fail(400, "target", error_text)
        return done(await persist(owner_id, policy))


async def delete_entry(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    axis = str(body.get("axis") or "")
    entry_id = str(body.get("id") or "")
    if axis not in {"window", "bot"} or not entry_id:
        return fail(400, "target", "主体：无法识别")
    owner_id = request["owner_id"]
    async with policy_lock(owner_id):
        policy = remove_entry(load_policy(owner_id), axis, entry_id)
        return done(await persist(owner_id, policy))


async def clear_entries(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    axis = str(body.get("axis") or "")
    if axis not in {"window", "bot"}:
        return fail(400, "axis", "主体：无法识别")
    owner_id = request["owner_id"]
    async with policy_lock(owner_id):
        return done(await persist(owner_id, clear_axis(load_policy(owner_id), axis)))


async def get_dialogs(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    bots = request.query.get("bots") == "1"
    items, offline, error = await _load_dialogs(owner_id, bots)
    if error:
        return business(error)
    return done({"dialogs": items, "offline": offline})


async def get_window_find(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    parsed = lookup_query(str(request.query.get("q") or ""))
    if parsed is None:
        return done({"item": None})
    if not allow_hit(f"find:{owner_id}", 40, 60):
        return done({"item": None, "message": "操作过于频繁"})
    item, error = await find_window(owner_id, parsed)
    if error or item is None:
        return done({"item": None, "message": error or "节点：没有这个窗口"})
    if _name_blocked(item):
        return done({"item": None, "message": "名字里含有监控，不显示"})
    if _system_blocked(item):
        return done({"item": None, "message": _skip_note([_window_name(item)])})
    return done({"item": item})


async def post_hot_windows(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    async with policy_lock(owner_id):
        return await _save_hot_windows(owner_id, body)


async def _save_hot_windows(owner_id: int, body: dict) -> web.Response:
    policy = load_policy(owner_id)
    chosen = []
    skipped = []
    existing = {int(item["chat_id"]): item for item in policy.get("hot_windows") or [] if item.get("chat_id") is not None}
    cached = _DIALOGS.get((owner_id, True))
    known = {}
    if cached:
        for item in cached[1]:
            if item.get("chat_id") is not None:
                known[int(item["chat_id"])] = item
    rows = body.get("windows") if isinstance(body.get("windows"), list) else None
    raw_ids = body.get("chat_ids") if isinstance(body.get("chat_ids"), list) else []
    sources: list = []
    if rows is not None:
        for item in rows:
            if not isinstance(item, dict):
                return fail(400, "target", "主体：无法识别")
            sources.append(item)
    else:
        for raw in raw_ids:
            try:
                chat_id = int(raw)
            except (TypeError, ValueError):
                return fail(400, "target", "主体：无法识别")
            sources.append(known.get(chat_id) or existing.get(chat_id) or {"chat_id": chat_id})
    for item in sources:
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            return fail(400, "target", "主体：无法识别")
        stored = existing.get(chat_id)
        base = dict(known.get(chat_id) or item)
        base["chat_id"] = chat_id
        if stored and stored.get("off"):
            base["off"] = list(stored["off"])
        if stored and stored.get("sign"):
            base["sign"] = True
        if stored is not None and "speak" in stored:
            base["speak"] = bool(stored.get("speak"))
        if _system_blocked(base) or _name_blocked(base):
            skipped.append(_window_name(base))
            continue
        chosen.append(base)
    if not chosen and skipped:
        return fail(400, "target", _skip_note(skipped))
    policy, error = assign_hot(policy, windows=chosen)
    if error:
        return fail(400, "hot", error)
    saved = await persist(owner_id, policy)
    if skipped:
        saved["message"] = _skip_note(skipped)
    return done(saved)


async def post_run(request: web.Request) -> web.Response:
    from meikai.store import notifier_of

    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    try:
        user_id = int(body.get("user_id"))
    except (TypeError, ValueError):
        return fail(400, "account", "主体：无法识别")
    people, offline = await collect(owner_id, max_age=45)
    known = [int(person.get("user_id") or 0) for person in people]
    if user_id not in known:
        people, offline = await collect(owner_id, max_age=0)
        known = [int(person.get("user_id") or 0) for person in people]
    if user_id not in known:
        return business({"error": "not_found", "message": "节点：账号不存在"})
    async with policy_lock(owner_id):
        policy = load_policy(owner_id)
        if "grab" in body or "running" in body:
            running = bool(body["grab"]) if "grab" in body else bool(body.get("running"))
            policy = set_running(policy, [user_id], running)
        if "join" in body:
            policy = set_join(policy, [user_id], bool(body.get("join")), known)
        if "watch" in body:
            watching = bool(body.get("watch"))
            if watching and notifier_of(owner_id) is None:
                return fail(400, "notify", "先添加通知机器人")
            policy = set_watch(policy, [user_id], watching)
        await persist(owner_id, policy)
    return done(await snapshot(owner_id, people, offline))


async def delete_account(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    try:
        user_id = int(request.match_info["user_id"])
    except (TypeError, ValueError):
        return fail(400, "account", "主体：无法识别")
    people, _offline = await collect(owner_id)
    person = next((item for item in people if int(item.get("user_id") or 0) == user_id), None)
    if person is None:
        return business({"error": "not_found", "message": "节点：账号不存在"})
    result = await call_node(
        person["node_id"],
        {"op": "remove", "user_id": user_id, "owner_id": owner_id},
        timeout=30,
    )
    if not result.get("ok"):
        return business(result)
    drop_account(owner_id, user_id)
    async with policy_lock(owner_id):
        policy = set_running(load_policy(owner_id), [user_id], True)
        await persist(owner_id, policy)
    return done(await snapshot(owner_id))


async def post_refresh(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    people, offline = await collect(owner_id)
    if body.get("user_id") is not None:
        try:
            user_id = int(body.get("user_id"))
        except (TypeError, ValueError):
            return fail(400, "account", "主体：无法识别")
        person = next((item for item in people if int(item.get("user_id") or 0) == user_id), None)
        if person is None:
            return business({"error": "not_found", "message": "节点：账号不存在"})
        targets = [(person["node_id"], user_id)]
    else:
        targets = [(person["node_id"], int(person["user_id"])) for person in people]
    if not scanning(owner_id) and targets:
        asyncio_task(_scan(owner_id, targets))
    return done({"pending": "正在刷新", "offline": offline})


async def _scan(owner_id: int, targets: list[tuple[str, int]]) -> None:
    try:
        await run_wallet_scan(owner_id, targets)
    except Exception as exc:
        log.warning("balance %s", type(exc).__name__)


def asyncio_task(coro) -> None:
    import asyncio

    asyncio.get_running_loop().create_task(coro)


async def post_profile(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    try:
        user_id = int(body.get("user_id"))
    except (TypeError, ValueError):
        return fail(400, "account", "主体：无法识别")
    people, _offline = await collect(owner_id)
    person = next((item for item in people if int(item.get("user_id") or 0) == user_id), None)
    if person is None:
        return business({"error": "not_found", "message": "节点：账号不存在"})
    result = await call_node(
        person["node_id"],
        {"op": "refresh", "user_id": user_id, "owner_id": owner_id},
        timeout=30,
    )
    if not result.get("ok"):
        return business(result)
    return done(await snapshot(owner_id))


async def cancel_login(owner_id: int) -> None:
    state = _LOGIN.pop(owner_id, None)
    if not state:
        return
    await call_node(state["node_id"], {"op": "cancel", "login_id": state["login_id"]}, timeout=15)


async def post_phone(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    if not allow_hit(f"phone:{owner_id}", 8, 3600):
        return fail(429, "too_many", "主体：尝试次数过多")
    phone = normalize_phone(str(body.get("phone") or ""))
    if phone is None:
        return fail(400, "invalid_phone", "主体：手机号未带区号")
    await cancel_login(owner_id)
    started = await call_entry({"op": "send_code", "phone": phone, "owner_id": owner_id}, timeout=90)
    if not started.get("ok"):
        return business(started)
    _LOGIN[owner_id] = {
        "node_id": started["node_id"],
        "login_id": started["login_id"],
        "step": "code",
        "phone": phone,
    }
    return done({"step": "code", "phone": phone})


async def finish_node(owner_id: int, result: dict) -> web.Response:
    state = _LOGIN.get(owner_id) or {}
    if result.get("status") == "need_password":
        state["step"] = "password"
        _LOGIN[owner_id] = state
        return done({"step": "password"})
    if result.get("status") in {"ready", "move"} or (result.get("ok") and result.get("user_id")):
        node_id = state.get("node_id") or ""
        _LOGIN.pop(owner_id, None)
        finished = await complete_import(owner_id, node_id, result)
        if not finished.get("ok"):
            return business(finished)
        view = await snapshot(owner_id)
        if finished.get("message"):
            view["message"] = finished["message"]
        return done(view)
    return business(result)


async def post_code(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    state = _LOGIN.get(owner_id)
    if not state or state.get("step") not in {"code", "password"}:
        return business({"error": "not_found"})
    if not allow_hit(f"code:{owner_id}", 10, 600):
        return fail(429, "too_many", "主体：尝试次数过多")
    code = str(body.get("code") or "").strip().replace(" ", "")
    if not code.isdigit():
        return fail(400, "code", "主体：验证码是数字")
    result = await call_node(
        state["node_id"],
        {"op": "sign_code", "login_id": state["login_id"], "code": code},
        timeout=90,
    )
    return await finish_node(owner_id, result)


async def post_password(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    owner_id = request["owner_id"]
    state = _LOGIN.get(owner_id)
    if not state:
        return business({"error": "not_found"})
    if not allow_hit(f"password:{owner_id}", 6, 600):
        return fail(429, "too_many", "主体：尝试次数过多")
    password = str(body.get("password") or "")
    if not password.strip():
        return fail(400, "password", "Telegram：二级密码不正确")
    result = await call_node(
        state["node_id"],
        {"op": "password", "login_id": state["login_id"], "password": password.strip()},
        timeout=90,
    )
    return await finish_node(owner_id, result)


async def post_qr(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    if not allow_hit(f"qr:{owner_id}", 10, 3600):
        return fail(429, "too_many", "主体：尝试次数过多")
    await cancel_login(owner_id)
    started = await call_entry({"op": "qr_start", "owner_id": owner_id}, timeout=90)
    if not started.get("ok"):
        return business(started)
    _LOGIN[owner_id] = {
        "node_id": started["node_id"],
        "login_id": started["login_id"],
        "step": "qr",
    }
    image = base64.b64encode(qr_png(started["url"])).decode()
    return done({"step": "qr", "image": image})


async def get_login(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    state = _LOGIN.get(owner_id)
    if not state:
        return done({"step": ""})
    if state.get("step") != "qr":
        body = {"step": state.get("step") or ""}
        if state.get("phone"):
            body["phone"] = state["phone"]
        return done(body)
    result = await call_node(
        state["node_id"],
        {"op": "qr_status", "login_id": state["login_id"]},
        timeout=25,
    )
    if result.get("status") == "pending":
        return done({"step": "qr"})
    if result.get("status") == "expired":
        refreshed = await call_node(
            state["node_id"],
            {"op": "qr_refresh", "login_id": state["login_id"]},
            timeout=30,
        )
        if not refreshed.get("ok"):
            _LOGIN.pop(owner_id, None)
            return business(refreshed)
        image = base64.b64encode(qr_png(refreshed["url"])).decode() if refreshed.get("url") else ""
        return done({"step": "qr", "image": image})
    return await finish_node(owner_id, result)


async def post_cancel(request: web.Request) -> web.Response:
    await cancel_login(request["owner_id"])
    return done({"step": ""})


_PACK_LIMIT = 8_000_000


async def post_pack(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    if not allow_hit(f"pack:{owner_id}", 12, 3600):
        return fail(429, "too_many", "主体：尝试次数过多")
    if request.content_length and request.content_length > _PACK_LIMIT:
        return fail(400, "bad_pack", "主体：压缩包太大")
    kind = request.content_type or ""
    if "multipart/" not in kind:
        return fail(400, "bad_pack", "主体：仅支持 .zip")
    blob = bytearray()
    try:
        reader = await request.multipart()
        field = await reader.next()
        while field is not None:
            if field.name == "file":
                while True:
                    chunk = await field.read_chunk(65536)
                    if not chunk:
                        break
                    if len(blob) + len(chunk) > _PACK_LIMIT:
                        return fail(400, "bad_pack", "主体：压缩包太大")
                    blob.extend(chunk)
                break
            field = await reader.next()
    except Exception:
        log.warning("pack read failed")
        return fail(400, "bad_pack", "主体：仅支持 .zip")
    if not blob:
        return fail(400, "bad_pack", "主体：仅支持 .zip")
    try:
        outcome = await import_pack(owner_id, bytes(blob))
    except ValueError as exc:
        return fail(400, "bad_pack", str(exc))
    if not outcome.get("ok"):
        return business(outcome)
    view = await snapshot(owner_id)
    view["results"] = outcome["results"]
    view["added"] = outcome["added"]
    return done(view)


async def get_claims(request: web.Request) -> web.Response:
    owner_id = request["owner_id"]
    return done({
        "claims": list_claims(owner_id),
        "today": today_text(owner_id),
    })


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return ""


def _face_due(checked_at: int, now: float) -> bool:
    return checked_at <= 0 or now - checked_at >= FACE_TTL


def _face_path(digest: str) -> Path | None:
    if len(digest) != 32 or any(ch not in "0123456789abcdef" for ch in digest):
        return None
    return _FACE_DIR / digest


def _face_file(digest: str) -> bool:
    path = _face_path(digest)
    return path is not None and path.is_file()


assert _dialog_row({"kind": "channel", "chat_id": -100, "title": "频", "username": "a"}, False)[1]["kind"] == "channel"
assert _dialog_row({"kind": "group", "chat_id": -1, "title": "群"}, False)[1]["kind"] == "group"
assert _dialog_row({"kind": "user", "chat_id": 7, "title": "甲"}, False) is None
assert _dialog_row({"kind": "user", "chat_id": 7, "title": "甲"}, True)[1]["kind"] == "user"
assert _dialog_row({"kind": "bot", "user_id": 9, "title": "机"}, True)[1]["chat_id"] == 9
folded = _collapse_dialogs([
    {"kind": "channel", "chat_id": -100, "title": "甲"},
    {"kind": "group", "chat_id": -200, "title": "甲"},
])
assert {item["kind"] for item in folded} == {"channel", "group"}
ordered = _collapse_dialogs([
    {"kind": "group", "chat_id": -1, "title": "旧", "active_at": "2020-01-01T00:00:00+00:00"},
    {"kind": "group", "chat_id": -2, "title": "新", "active_at": "2026-01-01T00:00:00+00:00"},
    {"kind": "group", "chat_id": -3, "title": "无"},
])
assert [item["title"] for item in ordered] == ["新", "旧", "无"]
same_title = _collapse_dialogs([
    {"kind": "group", "chat_id": -1, "title": "甲"},
    {"kind": "group", "chat_id": -2, "title": "甲"},
])
assert {item["chat_id"] for item in same_title} == {-1, -2}
assert _face_due(0, 100)
assert _face_due(100, 100 + FACE_TTL)
assert not _face_due(100, 100 + FACE_TTL - 1)
assert _face_path("ab" * 16) == _FACE_DIR / ("ab" * 16)
assert _face_path("..") is None
assert _face_path("a" * 31) is None
assert _face_path("A" * 32) is None
assert _image_type(b"\xff\xd8\xffabc") == "image/jpeg"
assert _image_type(b"\x89PNG\r\n\x1a\n") == "image/png"
assert _image_type(b"RIFF????WEBP") == "image/webp"
assert _image_type(b"GIF89a") == ""


def _lookup_face(peer: int) -> tuple[int, str, int] | None:
    hit = _FACE.get(peer)
    if hit is not None:
        return hit
    row = read_face(peer)
    if row:
        _FACE[peer] = row
    return row


def _put_face(peer: int, photo_id: int, digest: str, checked_at: int) -> None:
    if write_face(peer, photo_id, digest, checked_at):
        _FACE[peer] = (int(photo_id), digest, int(checked_at))


def _store_face_bytes(data: bytes) -> str:
    if not _image_type(data) or not (32 < len(data) <= 300_000):
        return ""
    digest = hashlib.md5(data).hexdigest()
    path = _face_path(digest)
    if path is None:
        return ""
    if path.is_file():
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{digest}.{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        log.warning("主体：头像没有写入 %s", type(exc).__name__)
        return ""
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return digest


def _drop_face_file(digest: str, peer: int) -> None:
    if not digest or face_used(digest, peer):
        return
    path = _face_path(digest)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("主体：头像没有清掉 %s", type(exc).__name__)


def _apply_face(peer: int, row: tuple[int, str, int] | None, result: dict) -> None:
    now = int(time.time())
    kept = row[1] if row and row[1] and _face_file(row[1]) else ""
    if result.get("same") and kept:
        _FACE_HOLD.pop(peer, None)
        _put_face(peer, int(result.get("photo_id") or (row[0] if row else 0)), kept, now)
        return
    raw = result.get("image")
    if raw:
        try:
            data = base64.b64decode(raw)
        except (ValueError, TypeError):
            data = b""
        digest = _store_face_bytes(data)
        if digest:
            _FACE_HOLD.pop(peer, None)
            _put_face(peer, int(result.get("photo_id") or 0), digest, now)
            if kept and kept != digest:
                _drop_face_file(kept, peer)
            return
    if result.get("empty"):
        _FACE_HOLD.pop(peer, None)
        _put_face(peer, 0, "", now)
        if kept:
            _drop_face_file(kept, peer)
        return
    if kept and row:
        _put_face(peer, row[0], kept, now)
        return
    if result.get("error") == "flood":
        _FACE_HOLD[peer] = now


async def _pull_face(owner_id: int, peer: int, photo_id: int) -> dict:
    nodes = list(used_nodes(owner_id)) or list(NODES)
    payload = {"op": "face", "owner_id": owner_id, "peer": peer, "photo_id": photo_id}
    flood = False
    for node_id in nodes:
        result = await call_node(node_id, payload, timeout=12)
        if result.get("same") or result.get("image") or result.get("empty"):
            return result
        if result.get("error") == "flood":
            flood = True
            break
    return {"ok": False, "error": "flood" if flood else "offline"}


async def _refresh_face(owner_id: int, peer: int) -> None:
    row = _lookup_face(peer)
    if row and not _face_due(row[2], time.time()) and (not row[1] or _face_file(row[1])):
        return
    known = int(row[0]) if row and row[1] and _face_file(row[1]) else 0
    try:
        result = await _pull_face(owner_id, peer, known)
    except Exception as exc:
        log.warning("主体：头像没有更新 %s", type(exc).__name__)
        return
    _apply_face(peer, row, result)


def _kick_face(owner_id: int, peer: int) -> asyncio.Task:
    job = _FACE_JOB.get(peer)
    if job is not None and not job.done():
        return job
    job = asyncio.create_task(_refresh_face(owner_id, peer))
    _FACE_JOB[peer] = job

    def _clear(task: asyncio.Task, peer: int = peer) -> None:
        if _FACE_JOB.get(peer) is task:
            _FACE_JOB.pop(peer, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.warning("主体：头像没有更新 %s", type(exc).__name__)

    job.add_done_callback(_clear)
    return job


def _serve_face(request: web.Request, digest: str) -> web.Response | None:
    path = _face_path(digest)
    if path is None or not path.is_file():
        return None
    etag = f'"{digest}"'
    headers = {"ETag": etag, "Cache-Control": f"private, max-age={FACE_TTL}"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    data = path.read_bytes()
    return web.Response(body=data, content_type=_image_type(data) or "image/jpeg", headers=headers)


def _no_face() -> web.Response:
    return web.Response(status=204, headers={"Cache-Control": f"private, max-age={FACE_TTL}"})


async def get_face(request: web.Request) -> web.Response:
    owner_id = int(request["owner_id"])
    try:
        peer = int(request.match_info["peer"])
    except (TypeError, ValueError):
        return _no_face()
    if not peer:
        return _no_face()
    now = time.time()
    row = _lookup_face(peer)
    fresh = row is not None and not _face_due(row[2], now)
    if fresh and row and row[1] and _face_file(row[1]):
        served = _serve_face(request, row[1])
        if served is not None:
            return served
    if fresh and row and not row[1]:
        return _no_face()
    held = _FACE_HOLD.get(peer, 0)
    if held and not _face_due(held, now) and not (row and row[1] and _face_file(row[1])):
        return _no_face()
    try:
        await _kick_face(owner_id, peer)
    except Exception as exc:
        log.warning("主体：头像没有更新 %s", type(exc).__name__)
    row = _lookup_face(peer)
    if row and row[1]:
        served = _serve_face(request, row[1])
        if served is not None:
            return served
    return _no_face()


async def post_peek(request: web.Request) -> web.Response:
    body = await read_body(request) or {}
    result = await peek_message(request["owner_id"], str(body.get("link") or ""))
    if not result.get("ok"):
        return business(result)
    return done(result)


def safe_file(rel: str) -> Path | None:
    if not rel or ".." in rel:
        return None
    root = _DIST.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


_assets: dict[str, tuple] = {}


def _asset(path: Path) -> tuple[bytes, bytes, str]:
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    hit = _assets.get(str(path))
    if hit and hit[0] == key:
        return hit[1], hit[2], hit[3]
    raw = path.read_bytes()
    packed = gzip.compress(raw, compresslevel=6)
    if len(packed) >= len(raw):
        packed = raw
    if path.suffix == ".js":
        kind = "text/javascript"
    elif path.suffix == ".css":
        kind = "text/css"
    elif path.suffix == ".html":
        kind = "text/html"
    else:
        kind = "application/octet-stream"
    if len(_assets) > 32:
        _assets.clear()
    _assets[str(path)] = (key, raw, packed, kind)
    return raw, packed, kind


def _page(request: web.Request, path: Path) -> web.Response:
    raw, packed, kind = _asset(path)
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable" if path.parent.name == "assets" else "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    body = raw
    if packed is not raw and "gzip" in request.headers.get("Accept-Encoding", ""):
        body = packed
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    charset = "utf-8" if kind.startswith("text/") else None
    return web.Response(body=body, content_type=kind, headers=headers, charset=charset)


async def post_notify(request: web.Request) -> web.Response:
    from meikai.notify import bind

    if not allow_hit(f"notify:{request['owner_id']}", 8, 60):
        return fail(429, "too_many", "操作过于频繁")
    body = await read_body(request)
    if body is None:
        return fail(400, "body", "无法识别")
    face, error = await bind(int(request["owner_id"]), str(body.get("token") or ""))
    if error or face is None:
        return fail(400, "token", error or "token 无效")
    return done({"notify": face})


async def test_notify(request: web.Request) -> web.Response:
    from meikai.notify import ping

    if not allow_hit(f"notify-test:{request['owner_id']}", 5, 60):
        return fail(429, "too_many", "操作过于频繁")
    message, error = await ping(int(request["owner_id"]))
    if error:
        return fail(400, "token", error)
    return done({"message": message})


async def delete_notify(request: web.Request) -> web.Response:
    from meikai.notify import clear

    owner_id = int(request["owner_id"])
    clear(owner_id)
    async with policy_lock(owner_id):
        policy = load_policy(owner_id)
        if policy.get("watch"):
            policy["watch"] = []
            await persist(owner_id, policy)
    return done(await snapshot(owner_id))


async def spa(request: web.Request) -> web.Response:
    if request.path.startswith("/api"):
        return fail(404, "not_found", "主体：没有这个页面")
    found = safe_file(request.path.lstrip("/"))
    if found is not None:
        return _page(request, found)
    index = _DIST / "index.html"
    if index.is_file():
        return _page(request, index)
    return web.Response(text="页面还没构建", status=503)


def build_web(bot_token: str) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=8_000_000)
    app["bot_token"] = bot_token
    app.router.add_post("/api/session", open_session)
    app.router.add_get("/api/home", get_home)
    app.router.add_get("/api/balances", get_balances)
    app.router.add_get("/api/pay-balance", get_pay_balance)
    app.router.add_patch("/api/policy", patch_policy)
    app.router.add_post("/api/entries", post_entry)
    app.router.add_delete("/api/entries", delete_entry)
    app.router.add_post("/api/entries/clear", clear_entries)
    app.router.add_get("/api/dialogs", get_dialogs)
    app.router.add_get("/api/windows/find", get_window_find)
    app.router.add_post("/api/hot/windows", post_hot_windows)
    app.router.add_post("/api/accounts/run", post_run)
    app.router.add_delete("/api/accounts/{user_id}", delete_account)
    app.router.add_post("/api/accounts/refresh", post_refresh)
    app.router.add_post("/api/accounts/profile", post_profile)
    app.router.add_post("/api/login/phone", post_phone)
    app.router.add_post("/api/login/code", post_code)
    app.router.add_post("/api/login/password", post_password)
    app.router.add_post("/api/login/qr", post_qr)
    app.router.add_get("/api/login", get_login)
    app.router.add_post("/api/login/cancel", post_cancel)
    app.router.add_post("/api/login/pack", post_pack)
    app.router.add_get("/api/claims", get_claims)
    app.router.add_post("/api/notify", post_notify)
    app.router.add_post("/api/notify/test", test_notify)
    app.router.add_delete("/api/notify", delete_notify)
    app.router.add_get("/api/faces/{peer}", get_face)
    app.router.add_post("/api/peek", post_peek)
    app.router.add_get("/{tail:.*}", spa)
    return app


async def start_site(bot_token: str) -> None:
    runner = web.AppRunner(build_web(bot_token), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", web_port()).start()
    log.info("web 127.0.0.1:%s", web_port())
