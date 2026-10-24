"""账号列表、资料和余额。"""

import asyncio
import logging
import re
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from meikai.login import _wait
from meikai.node import NODES
from meikai.node_rpc import call_node, call_nodes
from meikai.store import load_record, note_nodes, save_record

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_AMOUNT = re.compile(r"([A-Z]{2,12})\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")
_AVAILABLE = re.compile(r"可用余额\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Z]{2,12})")
_AIDU = re.compile(r"aidu币\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)")
_TAIL = re.compile(r"余额\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Z]{2,12})")
_SKIP = {"ID", "TGID"}
WALLETS = (
    ("okpay", "@OKPAY"),
    ("kkpay", "@KKPAY"),
    ("dlqb", "@DLQB"),
    ("fllqb", "@FLLQB"),
    ("wlqb", "@WLQB"),
    ("xvip", "@XVIP"),
    ("myqb", "@MYQB"),
    ("fjqb", "@FJQB"),
    ("lpay", "@LPAY"),
    ("hgqb", "@HGQB"),
    ("kpay", "@KPAY"),
    ("ftpay", "@FTPAY"),
    ("tcqb", "@TCQB"),
)
WALLET_COINS = {
    "fllqb": frozenset({"USDT"}),
    "tcqb": frozenset({"USDT"}),
    "fjqb": frozenset({"USDT"}),
}
_refreshing: set[int] = set()
_seen_accounts: dict[tuple[int, str], list[dict]] = {}
_roster: dict[int, tuple[float, list[dict], list[str]]] = {}
log = logging.getLogger(__name__)


def remembered(owner_id: int) -> tuple[list[dict], list[str]]:
    hit = _roster.get(owner_id)
    if hit is not None:
        return list(hit[1]), list(hit[2])
    found = []
    for (oid, node_id), people in _seen_accounts.items():
        if oid != owner_id:
            continue
        for person in people:
            item = dict(person)
            item["node_id"] = node_id
            found.append(item)
    found.sort(key=lambda item: (item.get("added_at") or "9999", int(item.get("user_id") or 0)))
    return found, []


def nickname(person: dict) -> str:
    return person.get("name") or ""


def added_text(value: str) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(_SHANGHAI).strftime("%Y-%m-%d %H:%M")


def username_text(person: dict) -> str:
    name = str(person.get("username") or "").strip().lstrip("@")
    if not name:
        raw = person.get("usernames") or []
        name = str(raw[0] if raw else "").strip().lstrip("@")
    if not name:
        return ""
    return f"@{name}"


_LANG = {
    "zh-hans": "简体中文",
    "zh-cn": "简体中文",
    "zh": "中文",
    "zh-hant": "繁体中文",
    "zh-tw": "繁体中文",
    "zh-hk": "繁体中文",
    "en": "英文",
    "ru": "俄文",
    "uk": "乌克兰文",
    "ja": "日文",
    "ko": "韩文",
    "es": "西班牙文",
    "pt": "葡萄牙文",
    "pt-br": "葡萄牙文",
    "de": "德文",
    "fr": "法文",
    "it": "意大利文",
    "id": "印尼文",
    "vi": "越南文",
    "th": "泰文",
    "ar": "阿拉伯文",
    "fa": "波斯文",
    "tr": "土耳其文",
    "hi": "印地文",
}


def lang_text(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _LANG.get(raw, raw)


async def collect(owner_id: int, on_node=None, max_age: float = 2) -> tuple[list[dict], list[str]]:
    now = time.monotonic()
    hit = _roster.get(owner_id)
    if hit is not None and now - hit[0] < max_age:
        if on_node is not None:
            for node_id in NODES:
                await on_node(node_id)
        return hit[1], hit[2]
    found: list[dict] = []
    have: list[str] = []
    empty: list[str] = []

    async def one(node_id: str) -> tuple[str, dict]:
        result = await call_node(node_id, {"op": "list", "owner_id": owner_id}, timeout=12)
        if on_node is not None:
            await on_node(node_id)
        return node_id, result

    if on_node is not None:
        results = dict(await asyncio.gather(*(one(node_id) for node_id in NODES)))
    else:
        results = await call_nodes({"op": "list", "owner_id": owner_id}, timeout=12)
    for node_id in NODES:
        result = results.get(node_id) or {"ok": False, "error": "offline"}
        if result.get("ok"):
            people = result.get("accounts") or []
            _seen_accounts[(owner_id, node_id)] = [dict(person) for person in people]
            if people:
                have.append(node_id)
            else:
                empty.append(node_id)
        else:
            people = _seen_accounts.get((owner_id, node_id), [])
            people = [dict(person, online=False) for person in people]
        for person in people:
            item = dict(person)
            item["node_id"] = node_id
            found.append(item)
    note_nodes(owner_id, have, empty)
    found.sort(key=lambda item: (item.get("added_at") or "9999", int(item.get("user_id") or 0)))
    _roster[owner_id] = (now, found, [])
    return found, []


def drop_account(owner_id: int, user_id: int) -> None:
    number = int(user_id)
    hit = _roster.get(owner_id)
    if hit is not None:
        people = [item for item in hit[1] if int(item.get("user_id") or 0) != number]
        _roster[owner_id] = (hit[0], people, hit[2])
    for key, people in list(_seen_accounts.items()):
        if key[0] != owner_id:
            continue
        _seen_accounts[key] = [item for item in people if int(item.get("user_id") or 0) != number]


def parse_amounts(text: str) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for match in _AVAILABLE.finditer(text):
        found[match.group(2)] = Decimal(match.group(1))
    for match in _AMOUNT.finditer(text):
        code = match.group(1)
        if code in _SKIP or code in found:
            continue
        found[code] = Decimal(match.group(2))
    for match in _TAIL.finditer(text):
        code = match.group(2)
        if code not in _SKIP and code not in found:
            found[code] = Decimal(match.group(1))
    for match in _AIDU.finditer(text):
        if "AIDU" not in found:
            found["AIDU"] = Decimal(match.group(1))
    return found


def format_amount(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def load_balances(owner_id: int) -> dict:
    return load_record(owner_id) or {}


def save_snapshot(owner_id: int, at: str, accounts: dict, pending: str = "") -> None:
    save_record(owner_id, at, accounts, pending)


def balance_error(name: str, result: dict) -> str:
    code = result.get("error")
    if code == "flood":
        return f"Telegram：请求过于频繁，请 {_wait(int(result.get('seconds') or 0))}后再试"
    if code == "peer_flood":
        return f"Telegram：暂时不能给{name}发消息"
    if code == "empty":
        return f"{name}：未返回余额"
    if code == "no_bot":
        return f"{name}：钱包不存在"
    if code == "offline":
        return "节点：未连接"
    if code == "not_found":
        return "节点：账号不存在"
    if code == "revoked":
        return "Telegram：登录已失效，请重新登录"
    return f"{name}：查询失败"


def apply_wallet(entry: dict, username: str, label: str, item: dict) -> None:
    entry["errors"] = [error for error in entry.get("errors") or [] if not error.startswith(label)]
    entry["wallets"].pop(label, None)
    if not item.get("ok"):
        if item.get("error") != "flood":
            entry["errors"].append(balance_error(label, item))
        return
    amounts = parse_amounts(item.get("text") or "")
    allowed = WALLET_COINS.get(username)
    if allowed is not None:
        amounts = {code: amount for code, amount in amounts.items() if code in allowed}
    if not amounts:
        if "设置密码" in (item.get("text") or ""):
            entry["errors"].append(f"{label}：需要先设置支付密码")
        else:
            entry["errors"].append(f"{label}：未识别余额")
        return
    kept = {
        code.upper(): format_amount(amount)
        for code, amount in amounts.items()
        if amount != 0
    }
    if kept:
        entry["wallets"][label] = kept


async def query_one(owner_id: int, node_id: str, user_id: int, username: str) -> dict:
    result = await call_node(
        node_id,
        {"op": "wallets", "user_id": user_id, "owner_id": owner_id, "bot": username},
        timeout=30,
    )
    if not result.get("ok"):
        return result
    return (result.get("wallets") or {}).get(username) or {"ok": False, "error": "empty"}


async def take_wallet(owner_id: int, node_id: str, user_id: int, username: str) -> dict:
    item = {"ok": False, "error": "empty"}
    for _ in range(3):
        try:
            item = await query_one(owner_id, node_id, user_id, username)
        except Exception as exc:
            log.warning("wallet %s %s", username, type(exc).__name__)
            return {"ok": False, "error": "internal"}
        if item.get("error") != "flood":
            return item
        await asyncio.sleep(min(int(item.get("seconds") or 1), 20))
    return item


async def persist_scan(owner_id: int, accounts: dict, pending: str) -> None:
    from datetime import timezone

    at = "" if pending else datetime.now(timezone.utc).isoformat()
    save_snapshot(owner_id, at, accounts, pending)


async def run_wallet_scan(owner_id: int, targets: list[tuple[str, int]]) -> None:
    if owner_id in _refreshing:
        return
    _refreshing.add(owner_id)
    try:
        saved = load_balances(owner_id)
        accounts = dict(saved.get("accounts") or {})
        queue: asyncio.Queue = asyncio.Queue()
        entries: dict[str, dict] = {}
        for node_id, user_id in targets:
            entry: dict = {"wallets": {}, "errors": []}
            entries[str(user_id)] = entry
            accounts[str(user_id)] = entry
            for username, label in WALLETS:
                queue.put_nowait((node_id, user_id, username, label))
        await persist_scan(owner_id, accounts, "正在刷新")
        while True:
            try:
                node_id, user_id, username, label = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            item = await take_wallet(owner_id, node_id, user_id, username)
            if item.get("error") != "flood":
                apply_wallet(entries[str(user_id)], username, label, item)
            accounts[str(user_id)] = entries[str(user_id)]
            await persist_scan(owner_id, accounts, "正在刷新")
        await persist_scan(owner_id, accounts, "")
    finally:
        _refreshing.discard(owner_id)


def scanning(owner_id: int) -> bool:
    return owner_id in _refreshing


assert lang_text("zh-hans") == "简体中文"
assert lang_text("") == ""
assert lang_text("xx") == "xx"
