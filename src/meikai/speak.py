"""热群发言。主体决定谁说，节点只发送。"""

import asyncio
import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from meikai.hot_copy import lines_for, pick_line
from meikai.node_rpc import call_node
from meikai.policy import hours_open, sign_hours
from meikai.reports import hits
from meikai.store import (
    hot_sign_delay,
    hot_sign_finish,
    hot_sign_get,
    hot_sign_plan,
    hot_sign_purge,
    hot_slot_save,
    hot_slots,
    load_policies,
)

log = logging.getLogger(__name__)

SPEAK_OK = 0.0
SIGN_TEXT = "签到"
_roster: dict[int, list[dict]] = {}
_roster_at = 0.0
_sign_purged = ""


async def _accounts() -> dict[int, list[dict]]:
    global _roster_at
    now = time.time()
    if _roster and now - _roster_at < 60:
        return _roster
    from meikai.node_rpc import call_nodes

    results = await call_nodes({"op": "list"}, timeout=20)
    found: dict[int, list[dict]] = {}
    for node_id, result in results.items():
        if not result.get("ok"):
            continue
        for person in result.get("accounts") or []:
            owner = int(person.get("owner_id") or 0)
            if not owner:
                continue
            item = dict(person)
            item["node_id"] = node_id
            found.setdefault(owner, []).append(item)
    _roster.clear()
    _roster.update(found)
    _roster_at = now
    return _roster


async def _speak_one(person: dict, owner_id: int, chat_id: int, username: str, text: str, delete_ms: int) -> bool:
    result = await call_node(
        person["node_id"],
        {
            "op": "speak",
            "owner_id": owner_id,
            "user_id": int(person["user_id"]),
            "chat_id": chat_id,
            "username": username,
            "text": text,
            "delete_ms": delete_ms,
        },
        timeout=40,
    )
    if result.get("ok"):
        log.info("主体：热群已发言 owner=%s chat=%s user=%s", owner_id, chat_id, person["user_id"])
        return True
    return False


async def _send(
    owner_id: int,
    chat_id: int,
    username: str,
    packs,
    extra,
    low: int,
    high: int,
    delete_ms: int,
    stagger: bool,
    skip: set[int],
) -> None:
    people = (await _accounts()).get(owner_id) or []
    eligible = [
        person for person in people
        if person.get("online") and int(person.get("user_id") or 0) not in skip
    ]
    eligible.sort(key=lambda person: int(person.get("user_id") or 0))
    if not eligible:
        return
    now = int(time.time())
    slots = hot_slots(owner_id, chat_id)
    used = {line for _nxt, line in slots.values() if line}
    for index, person in enumerate(eligible):
        user_id = int(person["user_id"])
        nxt, previous = slots.get(user_id, (None, ""))
        if nxt is None:
            start = random.randint(low, high) + index * random.randint(180, 600)
            hot_slot_save(owner_id, chat_id, user_id, now + start, "")
            continue
        if now < nxt:
            continue
        # 时段外积压的档不补发，错开后再说，避免一开时段齐发。
        if stagger and now - nxt > high:
            hot_slot_save(
                owner_id,
                chat_id,
                user_id,
                now + random.randint(60, 300) + index * random.randint(15, 45),
                previous,
            )
            continue
        text = pick_line(packs, used | ({previous} if previous else set()), extra)
        if not text:
            continue
        if not await _speak_one(person, owner_id, chat_id, username, text, delete_ms):
            hot_slot_save(owner_id, chat_id, user_id, now + random.randint(180, 600), previous)
            continue
        used.add(text)
        hot_slot_save(owner_id, chat_id, user_id, now + random.randint(low, high), text)


def _sign_due(now: int, end_ts: int) -> int:
    span = end_ts - now
    if span <= 150:
        return now
    return now + random.randint(60, span - 90)


assert _sign_due(1000, 1150) == 1000
assert 1060 <= _sign_due(1000, 5000) <= 4910


async def _sign_owner(owner_id: int, policy: dict) -> None:
    global _sign_purged
    windows = [item for item in policy.get("hot_windows") or [] if item.get("sign") and item.get("chat_id")]
    if not windows:
        return
    now_dt = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not sign_hours(now_dt):
        return
    day = now_dt.date().isoformat()
    end_ts = int(now_dt.replace(hour=6, minute=0, second=0, microsecond=0).timestamp())
    now = int(time.time())
    if now >= end_ts:
        return
    if _sign_purged != day:
        hot_sign_purge(day)
        _sign_purged = day
    people = (await _accounts()).get(owner_id) or []
    online = {
        int(person["user_id"]): person
        for person in people
        if person.get("online") and int(person.get("user_id") or 0)
    }
    for window in windows:
        chat_id = int(window["chat_id"])
        if hits(
            chat_id=chat_id,
            username=str(window.get("username") or ""),
            title=str(window.get("title") or ""),
        ):
            continue
        sent_one = False
        rush = end_ts - now <= 180
        for user_id in sorted(online):
            row = hot_sign_get(owner_id, chat_id, user_id, day)
            if row is None:
                row = hot_sign_plan(owner_id, chat_id, user_id, day, _sign_due(now, end_ts))
            if row is None or row[1]:
                continue
            if row[0] > now:
                continue
            if sent_one and not rush:
                hot_sign_delay(owner_id, chat_id, user_id, day, now + random.randint(25, 90))
                continue
            if not await _speak_one(online[user_id], owner_id, chat_id, str(window.get("username") or ""), SIGN_TEXT, 0):
                nxt = now + random.randint(120, 300)
                if nxt >= end_ts:
                    nxt = min(now + 30, end_ts - 1)
                if nxt > now:
                    hot_sign_delay(owner_id, chat_id, user_id, day, nxt)
                continue
            sent_one = True
            if hot_sign_finish(owner_id, chat_id, user_id, day):
                log.info("主体：已签到 owner=%s chat=%s user=%s", owner_id, chat_id, user_id)
            else:
                log.warning("主体：签到已发出但没记下 owner=%s chat=%s user=%s", owner_id, chat_id, user_id)


async def tick() -> None:
    policies = load_policies()
    if not policies:
        return
    for key, policy in policies.items():
        if not str(key).lstrip("-").isdigit():
            continue
        owner_id = int(key)
        await _sign_owner(owner_id, policy)
        packs = list(policy.get("hot_packs") or [])
        extra = list(policy.get("hot_lines") or []) if "custom" in packs else None
        windows = [item for item in policy.get("hot_windows") or [] if item.get("speak")]
        if not lines_for(packs, extra) or not windows:
            continue
        if not hours_open(policy, start_key="hot_open", end_key="hot_close"):
            continue
        delete_ms = 1000 if int(policy.get("hot_drop") or 0) > 0 else 0
        low = int(policy.get("hot_low") or 1800)
        high = int(policy.get("hot_high") or 5400)
        if high < low:
            high = low
        stagger = str(policy.get("hot_open") or "00:00") != str(policy.get("hot_close") or "00:00")
        for window in windows:
            chat_id = int(window.get("chat_id") or 0)
            if not chat_id:
                continue
            if hits(
                chat_id=chat_id,
                username=str(window.get("username") or ""),
                title=str(window.get("title") or ""),
            ):
                continue
            await _send(
                owner_id,
                chat_id,
                str(window.get("username") or ""),
                packs,
                extra,
                low,
                high,
                delete_ms,
                stagger,
                set(),
            )


async def run() -> None:
    global SPEAK_OK
    while True:
        try:
            await tick()
            SPEAK_OK = time.time()
        except Exception as exc:
            log.warning("主体：热群循环 %s", type(exc).__name__)
        await asyncio.sleep(30)
