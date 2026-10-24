"""节点钱包查询。"""

import asyncio
import logging
import re
import time

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from meikai.node_host import accounts, clients, error_code, wallet_lock

log = logging.getLogger("meikai.node")

WALLETS = (
    "okpay",
    "kkpay",
    "dlqb",
    "fllqb",
    "wlqb",
    "xvip",
    "myqb",
    "fjqb",
    "lpay",
    "hgqb",
    "kpay",
    "ftpay",
    "tcqb",
)


WALLET_PROMPT = {"fllqb": "余额"}


_peers: dict[tuple[int, str], object] = {}


async def sleep_flood(exc: FloodWaitError) -> bool:
    seconds = int(exc.seconds)
    if seconds > 20:
        return False
    await asyncio.sleep(max(seconds, 0))
    return True


_BALANCE_RE = re.compile(
    r"(?:USDT|TRX|CNY|TON|KKCOIN|WLCOIN|FTCOIN|JIBA)\s*[:：]|余额\s*[:：]|aidu币\s*[:：]|可用余额",
    re.I,
)


def looks_like_balance(text: str) -> bool:
    return _BALANCE_RE.search(text or "") is not None


async def recent_incoming(client: TelegramClient, entity) -> list[tuple[int, str]]:
    try:
        messages = await client.get_messages(entity, limit=8)
    except FloodWaitError as exc:
        if not await sleep_flood(exc):
            return []
        messages = await client.get_messages(entity, limit=8)
    rows = []
    for message in messages:
        if message.out:
            continue
        text = (message.message or "").strip()
        if text:
            rows.append((message.id, text))
    return rows


async def wait_incoming(client: TelegramClient, entity, after_id: int) -> str:
    deadline = time.monotonic() + 4
    rows = []
    fresh: list[str] = []
    while True:
        rows = await recent_incoming(client, entity)
        fresh = [text for message_id, text in rows if message_id > after_id]
        if any(looks_like_balance(text) for text in fresh) or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.4)
    picked = [text for text in fresh if looks_like_balance(text)]
    if picked:
        return "\n".join(picked)
    for _message_id, text in rows:
        if looks_like_balance(text):
            return text
    return "\n".join(fresh)


async def send_wallet(client: TelegramClient, entity, text: str):
    try:
        return await client.send_message(entity, text)
    except FloodWaitError as exc:
        if not await sleep_flood(exc):
            raise
        return await client.send_message(entity, text)


async def wallet_text(client: TelegramClient, user_id: int, username: str) -> dict:
    key = (user_id, username)
    try:
        entity = _peers.get(key)
        if entity is None:
            entity = await client.get_entity(username)
            _peers[key] = entity
        sent = await send_wallet(client, entity, WALLET_PROMPT.get(username, "/start"))
        text = await wait_incoming(client, entity, sent.id)
    except Exception as exc:
        if not isinstance(exc, FloodWaitError):
            _peers.pop(key, None)
        mapped = error_code(exc)
        if mapped:
            return mapped
        log.warning("wallet %s %s", username, type(exc).__name__)
        return {"ok": False, "error": "internal"}
    if not text:
        return {"ok": False, "error": "empty"}
    return {"ok": True, "text": text}


async def op_wallets(req: dict) -> dict:
    user_id = int(req.get("user_id") or 0)
    owner_id = int(req.get("owner_id") or 0)
    record = accounts.get(str(user_id))
    if record is None or int(record.get("owner_id") or 0) != owner_id:
        return {"ok": False, "error": "not_found"}
    client = clients.get(user_id)
    if client is None:
        return {"ok": False, "error": "offline"}
    bot = str(req.get("bot") or "").lstrip("@").lower()
    names = (bot,) if bot else WALLETS
    if any(name not in WALLETS for name in names):
        return {"ok": False, "error": "unknown"}
    found = {}
    async with wallet_lock(user_id):
        for name in names:
            found[name] = await wallet_text(client, user_id, name)
    return {"ok": True, "wallets": found}


async def op_okpay(req: dict) -> dict:
    req = dict(req)
    req["bot"] = "okpay"
    result = await op_wallets(req)
    if not result.get("ok"):
        return result
    return result["wallets"]["okpay"]
