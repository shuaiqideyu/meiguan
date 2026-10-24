"""导入账号。"""

import io
import json
import logging
import tempfile
import zipfile
from pathlib import Path

import qrcode
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from meiguan.node import ENTRY_ORDER, node_for_dc
from meiguan.node_host import clean_profile
from meiguan.node_rpc import call_node, call_nodes

log = logging.getLogger(__name__)

_PUBLIC = (
    "user_id",
    "username",
    "usernames",
    "name",
    "dc",
    "premium",
    "nft",
    "org",
    "phone",
    "about",
    "verified",
    "birthday",
    "lang",
    "restricted",
    "scam",
    "fake",
    "added_at",
    "online",
)


def normalize_phone(text: str) -> str | None:
    raw = "".join(text.split())
    raw = raw.replace("-", "").replace("(", "").replace(")", "")
    if not raw.startswith("+"):
        return None
    digits = raw[1:]
    if digits.isdigit() and 7 <= len(digits) <= 15:
        return "+" + digits
    return None


def _wait(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return f"{secs} 秒"


def explain(result: dict) -> str:
    code = result.get("error")
    if code == "offline":
        return "节点：未连接"
    if code == "api_missing":
        return "节点：未配置 Telegram API"
    if code == "invalid_phone":
        return "Telegram：手机号无效"
    if code == "unoccupied":
        return "Telegram：手机号未注册"
    if code == "invalid_code":
        return "Telegram：验证码不正确"
    if code == "code_empty":
        return "Telegram：未收到验证码"
    if code == "code_expired":
        return "Telegram：验证码已过期"
    if code == "invalid_password":
        return "Telegram：二级密码不正确"
    if code == "too_many":
        return "主体：尝试次数过多"
    if code == "flood":
        return f"Telegram：发送验证码过于频繁，请 {_wait(int(result.get('seconds') or 0))}后再试"
    if code == "phone_flood":
        return "Telegram：发送验证码过于频繁"
    if code == "password_flood":
        return "Telegram：登录尝试过多"
    if code == "banned":
        return "Telegram：该手机号已封禁"
    if code == "revoked":
        return "Telegram：登录已失效，请重新登录"
    if code == "not_found":
        return "主体：添加已结束"
    if code == "wrong_dc":
        return "主体：没有对应节点"
    if code == "taken":
        return "这个账号已在系统里"
    if code == "bad_pack":
        return "主体：账号缺少登录数据"
    if code == "timeout":
        return "节点：登录超时"
    if code == "not_in_chat":
        return "节点：账号不在这个窗口"
    return "主体：添加失败"


def operator_id(update) -> int | None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type != "private" or user is None or not user.id:
        return None
    return user.id


def qr_png(url: str) -> bytes:
    image = qrcode.make(url)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def profile_note(result: dict) -> str:
    code = result.get("error")
    if code == "offline":
        return "节点：未连接"
    if code == "revoked":
        return str(result.get("message") or "") or "Telegram：登录已失效，请重新登录"
    if code == "not_found":
        return "节点：账号不存在"
    return "节点：刷新失败"


def public_account(record: dict, node_id: str) -> dict:
    item = {}
    for key in _PUBLIC:
        if key in record:
            item[key] = record.get(key)
    item["user_id"] = int(record.get("user_id") or 0)
    item["node_id"] = node_id
    item["name"] = str(record.get("name") or "")
    item["username"] = str(record.get("username") or "")
    return item


async def call_entry(payload: dict, timeout: float) -> dict:
    last = {"ok": False, "error": "offline"}
    for node_id in ENTRY_ORDER:
        result = await call_node(node_id, payload, timeout=timeout)
        if result.get("error") == "offline":
            last = result
            continue
        result["node_id"] = node_id
        return result
    return last


async def complete_import(owner_id: int, node_id: str, result: dict) -> dict:
    source = node_id
    record = result
    if result.get("status") == "move":
        target = node_for_dc(int(result.get("dc") or 0))
        if target is None:
            return {"ok": False, "error": "wrong_dc", "message": explain({"error": "wrong_dc"})}
        adopted = await call_node(
            target.node_id,
            {
                "op": "adopt",
                "session": result.get("session") or "",
                "owner_id": owner_id,
                "app": result.get("app") if isinstance(result.get("app"), dict) else {},
            },
            timeout=90,
        )
        if source:
            await call_node(source, {"op": "drop", "user_id": result.get("user_id")})
        if not adopted.get("ok"):
            return {"ok": False, "error": adopted.get("error") or "internal", "message": explain(adopted)}
        record = adopted
        node_id = target.node_id
    elif result.get("status") != "ready" and not result.get("user_id"):
        return {"ok": False, "error": result.get("error") or "internal", "message": explain(result)}
    user_id = int(record.get("user_id") or 0)
    refreshed = await call_node(
        node_id,
        {"op": "refresh", "user_id": user_id, "owner_id": owner_id},
        timeout=30,
    )
    account = dict(refreshed.get("account") or record)
    account["online"] = bool(refreshed.get("ok"))
    body = {"ok": True, "account": public_account(account, node_id)}
    if not refreshed.get("ok"):
        body["message"] = profile_note(refreshed)
    return body


_PACK_ACCOUNTS = 20
_PACK_FILES = 80


def _safe_zip_name(name: str) -> str | None:
    text = name.replace("\\", "/").lstrip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _session_text(raw: str) -> str:
    text = "".join(str(raw or "").split())
    if len(text) < 50 or len(text) > 2000 or not text.startswith("1"):
        return ""
    try:
        StringSession(text)
    except (ValueError, TypeError):
        return ""
    return text


def _session_from_sqlite(blob: bytes) -> str:
    if not blob.startswith(b"SQLite format 3") or len(blob) > 2_000_000:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "pack"
        Path(str(base) + ".session").write_bytes(blob)
        sess = SQLiteSession(str(base))
        try:
            return _session_text(StringSession.save(sess))
        finally:
            sess.close()


def _pack_label(data: dict) -> str:
    name = " ".join(
        part
        for part in (str(data.get("first_name") or "").strip(), str(data.get("last_name") or "").strip())
        if part
    )
    if name:
        return name[:40]
    digits = "".join(ch for ch in str(data.get("phone") or "") if ch.isdigit())
    if len(digits) >= 6:
        return f"+{digits[:3]}…{digits[-2:]}"
    return "账号"


def _pack_app(data: dict) -> dict | None:
    return clean_profile({
        "api_id": data.get("app_id", data.get("api_id")),
        "api_hash": data.get("app_hash", data.get("api_hash")),
        "device_model": data.get("device") or data.get("device_model") or "",
        "system_version": data.get("sdk") or data.get("system_version") or "",
        "app_version": data.get("app_version") or "",
        "lang_code": data.get("lang_code") or "",
        "system_lang_code": data.get("system_lang_code") or "",
        "lang_pack": data.get("lang_pack") or "",
    })


def _pack_user(data: dict) -> int:
    try:
        user_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0
    if user_id <= 0:
        return 0
    return user_id


def read_pack(blob: bytes) -> tuple[list[dict], list[dict]]:
    if len(blob) > 8_000_000 or not blob.startswith(b"PK"):
        raise ValueError("主体：仅支持 .zip")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError("主体：仅支持 .zip") from exc
    groups: dict[str, dict] = {}
    order: list[str] = []
    total = 0
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > _PACK_FILES:
            raise ValueError("主体：压缩包文件过多")
        for info in infos:
            total += info.file_size
            if info.file_size > 2_000_000 or total > 32_000_000:
                raise ValueError("主体：压缩包太大")
            name = _safe_zip_name(info.filename)
            if name is None:
                continue
            lower = name.lower()
            if lower.startswith("__macosx/") or "/__macosx/" in f"/{lower}":
                continue
            if lower.endswith(".json"):
                kind = "json"
            elif lower.endswith(".session"):
                kind = "session"
            else:
                continue
            base = name[: -len(Path(name).suffix)]
            slot = groups.get(base)
            if slot is None:
                slot = {}
                groups[base] = slot
                order.append(base)
            slot[kind] = archive.read(info)
    if not any(slot.get("json") for slot in groups.values()):
        raise ValueError("主体：压缩包里没有账号")
    items: list[dict] = []
    failed: list[dict] = []
    for base in order:
        slot = groups[base]
        raw = slot.get("json")
        if not raw:
            continue
        label = Path(base).name or "账号"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failed.append({"ok": False, "label": label, "message": "主体：账号无法识别"})
            continue
        if not isinstance(data, dict):
            failed.append({"ok": False, "label": label, "message": "主体：账号无法识别"})
            continue
        label = _pack_label(data)
        if len(items) >= _PACK_ACCOUNTS:
            failed.append({"ok": False, "label": label, "message": "主体：一次最多 20 个"})
            continue
        session = _session_text(str(data.get("session_str") or data.get("session_string") or ""))
        if not session and slot.get("session"):
            session = _session_from_sqlite(slot["session"])
        app = _pack_app(data)
        if not session or app is None:
            failed.append({"ok": False, "label": label, "message": "主体：账号缺少登录数据"})
            continue
        try:
            dc = int(data.get("dc") or 0)
        except (TypeError, ValueError):
            dc = 0
        items.append({
            "label": label,
            "user_id": _pack_user(data),
            "dc": dc,
            "session": session,
            "app": app,
        })
    return items, failed


async def lookup_holders(user_ids: list[int]) -> tuple[dict[int, int], bool]:
    results = await call_nodes({"op": "hold", "user_ids": user_ids}, timeout=12)
    found: dict[int, int] = {}
    offline = False
    for result in results.values():
        if result.get("error") == "offline" or not result.get("ok"):
            offline = True
            continue
        raw = result.get("found")
        if not isinstance(raw, dict):
            continue
        for key, owner in raw.items():
            try:
                found[int(key)] = int(owner)
            except (TypeError, ValueError):
                continue
    return found, offline


async def import_one(owner_id: int, item: dict) -> dict:
    label = str(item.get("label") or "账号")
    dc = int(item.get("dc") or 0)
    payload = {
        "op": "import_session",
        "session": item.get("session") or "",
        "owner_id": owner_id,
        "app": item.get("app") or {},
    }
    target = node_for_dc(dc)
    if target is None:
        result = await call_entry(payload, timeout=50)
    else:
        result = await call_node(target.node_id, payload, timeout=50)
        if result.get("error") != "offline":
            result["node_id"] = target.node_id
        else:
            result = await call_entry(payload, timeout=50)
    if not result.get("ok"):
        return {"ok": False, "label": label, "message": explain(result)}
    finished = await complete_import(owner_id, str(result.get("node_id") or ""), result)
    if not finished.get("ok"):
        return {"ok": False, "label": label, "message": finished.get("message") or explain(finished)}
    account = finished.get("account") if isinstance(finished.get("account"), dict) else {}
    name = str(account.get("name") or "").strip()
    try:
        user_id = int(account.get("user_id") or item.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    return {"ok": True, "label": name or label, "fresh": True, "user_id": user_id}


async def import_pack(owner_id: int, blob: bytes) -> dict:
    items, failed = read_pack(blob)
    found: dict[int, int] = {}
    if items:
        found, offline = await lookup_holders([int(item["user_id"]) for item in items if item.get("user_id")])
        if offline:
            return {"ok": False, "error": "offline"}
    results = []
    added = 0
    for item in items:
        user_id = int(item.get("user_id") or 0)
        label = str(item["label"])
        if user_id and user_id in found:
            if found[user_id] == owner_id:
                results.append({"ok": True, "label": label, "fresh": False})
            else:
                results.append({"ok": False, "label": label, "message": "这个账号已在系统里"})
            continue
        row = await import_one(owner_id, item)
        placed = int(row.pop("user_id", 0) or 0)
        if row.get("fresh") and placed:
            found[placed] = owner_id
        if row.get("fresh"):
            added += 1
        results.append(row)
    results.extend(failed)
    log.info("pack owner=%s added=%s rows=%s", owner_id, added, len(results))
    return {"ok": True, "results": results, "added": added}


def _sample_session() -> str:
    sample = StringSession()
    sample.set_dc(1, "149.154.175.53", 443)
    sample.auth_key = AuthKey(b"\x11" * 256)
    return StringSession.save(sample)


def _sample_pack() -> bytes:
    session = _sample_session()
    body = {
        "app_id": 8,
        "app_hash": "a" * 32,
        "device": "iPhone 14",
        "sdk": "16.3.1",
        "app_version": "13.0 (35303)",
        "lang_code": "en",
        "system_lang_code": "en-CA",
        "lang_pack": "ios",
        "session_str": session,
        "first_name": "Ada",
        "user_id": 42,
        "dc": 1,
        "twoFA": "secret",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("+100/../evil.json", b"{}")
        archive.writestr("+100/+100.json", json.dumps(body).encode())
        archive.writestr("+100/+100.session", b"not-sqlite")
    return buf.getvalue()


_sample_items, _sample_failed = read_pack(_sample_pack())
assert len(_sample_items) == 1
assert _sample_items[0]["label"] == "Ada"
assert _sample_items[0]["app"]["api_id"] == 8
assert _sample_items[0]["app"]["device_model"] == "iPhone 14"
assert _sample_items[0]["app"]["lang_pack"] == "ios"
assert "twoFA" not in _sample_items[0]["app"]
assert _sample_items[0]["session"].startswith("1")
assert _sample_failed == []
