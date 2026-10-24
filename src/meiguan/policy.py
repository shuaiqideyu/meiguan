"""操作者的模块开关、屏蔽名单和金额。"""

import random
import re
import secrets
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

from meiguan.hot_copy import PACKS

_PACK_IDS = tuple(name for name, _title in PACKS)
_LEGACY_PACKS = ("xiehouyu", "joke", "hello")
_CUSTOM = "custom"

GRAB_KINDS = ("direct", "captcha", "webapp", "phrase")
ALL_KINDS = GRAB_KINDS + ("shared",)
AXIS = ("off", "white", "black")
COINS = (
    "USDT", "CNY", "TRX", "TON", "KKCOIN", "WLCOIN", "FTCOIN", "JIBA", "AIDU",
    "DDCOIN", "JJCOIN",
)
_COIN_ALIAS = {"GRAM": "TON", "USD": "USDT", "U": "USDT", "RMB": "CNY"}
_PAY_FLL = frozenset({"fllqb"})
_PAY_CF = frozenset({"fuguihbbot", "sfylc888bot", "sdrp_bot", "nlhbbot"})
DEFAULT_COUNTS = {"USDT": "10", "CNY": "10", "TRX": "10", "TON": "10"}
DEFAULT_TOTALS: dict[str, str] = {}
_COIN_EACH = "100000"
DEFAULT_EACHES = {
    "USDT": "5",
    "CNY": "18",
    "TRX": "10",
    "TON": "5",
    "JIBA": "100000",
    "AIDU": "100000",
}
for _code in COINS:
    if _code.endswith("COIN"):
        DEFAULT_EACHES[_code] = _COIN_EACH
_SPAN = re.compile(r"^(\d+)\s*[-~～—到至]\s*(\d+)$")
_LINKISH = re.compile(r"(https?://|t\.me/|telegram\.me/)", re.I)
_ID = re.compile(r"^[0-9a-f]{8}$")
_USER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_LINK = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)(?:/\d+)?|([A-Za-z][A-Za-z0-9_]{0,31})(?:/\d+)?)",
    re.I,
)
_INVITE_LINK = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]{8,64})",
    re.I,
)
_SKIP_NAMES = {"joinchat", "share", "addstickers", "addtheme", "invoice", "bg", "s"}
_PAY_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
DELAY_MAX = 60
HOT_MIN = 60
HOT_MAX = 86400
DROP_MAX = 600
HOT_CAP_MAX = 500
# 自动添加窗口。False 时各账号只在自己的窗口领取，页面开关不能打开。
AUTO_JOIN = False


def default_policy() -> dict:
    return {
        "modes": {kind: kind != "shared" for kind in ALL_KINDS},
        "delay_unit": "s",
        "mode_delays": {kind: {"low": 0, "high": 0} for kind in ALL_KINDS},
        "window_mode": "off",
        "windows": [],
        "bot_mode": "off",
        "bots": [],
        "burn": False,
        "burns": {"captcha": False, "phrase": False},
        "delays": [],
        "hot_on": False,
        "hot_windows": [],
        "hot_lines": [],
        "hot_low": 1800,
        "hot_high": 5400,
        "hot_source": "pack",
        "hot_packs": list(_PACK_IDS),
        "hot_rev": 2,
        "hot_drop": 0,
        "hot_open": "00:00",
        "hot_close": "00:00",
        "hot_cap": 0,
        "hot_off": [],
        "coins": {code: True for code in COINS},
        "min_counts": dict(DEFAULT_COUNTS),
        "min_totals": dict(DEFAULT_TOTALS),
        "min_eaches": dict(DEFAULT_EACHES),
        "amount_custom": False,
        "paused": [],
        "unlinked": [],
        "joins": [],
        "join_legacy": False,
        "unknown_coin": True,
        "count_over": True,
        "web_pay": {"fll": True, "cf": True},
        "auto_receive": True,
        "window_block_keywords": [],
        "window_block_minutes": 0,
        "pay_keys": {},
        "phrase_say": {"low": 0, "high": 0},
        "watch": [],
        "hide_zero": True,
        "show_min": {},
        "open_at": "00:00",
        "close_at": "00:00",
    }


def marked_channel_id(channel_id: int) -> int:
    if channel_id < 0:
        return channel_id
    return -(10**12 + channel_id)


def _entry_id(value) -> str:
    text = str(value or "")
    if _ID.fullmatch(text):
        return text
    return secrets.token_hex(4)


def _username(value) -> str:
    name = str(value or "").strip().lstrip("@")
    if name and _USER.fullmatch(name):
        return name
    return ""


def _number(value) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def _user_ids(raw, limit: int = 200) -> list[int]:
    found = []
    seen = set()
    for item in raw or []:
        number = _number(item)
        if number is None or number in seen:
            continue
        seen.add(number)
        found.append(number)
        if len(found) >= limit:
            break
    return found


def _clean(entry: dict, *, numeric: str) -> dict | None:
    if not isinstance(entry, dict):
        return None
    username = _username(entry.get("username"))
    number = _number(entry.get(numeric))
    if not username and number is None:
        return None
    title = str(entry.get("title") or "").strip()[:40]
    item = {"id": _entry_id(entry.get("id")), "title": title}
    if username:
        item["username"] = username
    if number is not None:
        item[numeric] = number
    kind = str(entry.get("kind") or "")
    if kind in {"user", "group", "channel", "bot"}:
        item["kind"] = kind
    if not item["title"]:
        item["title"] = f"@{username}" if username else "私密窗口"
    return item


def _shield_kind(entry: dict) -> str:
    kind = str(entry.get("kind") or "")
    if kind == "bot":
        return "bot"
    if kind in {"user", "group", "channel"}:
        return kind
    if entry.get("user_id") and not entry.get("chat_id"):
        return "bot"
    return "chat"


def _window_bucket(kind: str) -> str:
    return "bot" if kind == "bot" else "window"


def _clean_shield(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    kind = _shield_kind(entry)
    username = _username(entry.get("username"))
    number = _number(entry.get("user_id") if kind == "bot" else entry.get("chat_id"))
    if not username and number is None:
        return None
    title = str(entry.get("title") or "").strip()[:40]
    item = {"id": _entry_id(entry.get("id")), "title": title, "kind": kind}
    if username:
        item["username"] = username
    if number is not None:
        item["user_id" if kind == "bot" else "chat_id"] = number
    if not item["title"]:
        item["title"] = f"@{username}" if username else ("机器人" if kind == "bot" else "私密窗口")
    return item


def _merged_mode(window_mode: str, bot_mode: str) -> str:
    if window_mode not in AXIS:
        window_mode = "off"
    if bot_mode not in AXIS:
        bot_mode = "off"
    if window_mode == "off":
        return bot_mode
    if bot_mode in ("off", window_mode):
        return window_mode
    return "black"


def _shield_list(raw) -> list[dict]:
    cleaned = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        item = _clean_shield(entry)
        if item is not None:
            cleaned.append(item)
    return _dedupe_windows(cleaned)


def _legacy_shields(raw: dict) -> list[dict]:
    rows = [entry for entry in (raw.get("windows") or []) if isinstance(entry, dict)]
    for entry in raw.get("bots") or []:
        if isinstance(entry, dict):
            item = dict(entry)
            item["kind"] = "bot"
            rows.append(item)
    return _shield_list(rows)


def _read_lists(raw: dict, mode: str) -> dict[str, list]:
    stored = raw.get("window_lists")
    if isinstance(stored, dict):
        return {axis: _shield_list(stored.get(axis)) for axis in AXIS}
    seeded = {axis: [] for axis in AXIS}
    seeded[mode] = _legacy_shields(raw)
    return seeded


def _dedupe_windows(items: list[dict]) -> list[dict]:
    kept = []
    seen = set()
    for item in items:
        kind = _window_bucket(str(item.get("kind") or "chat"))
        number = _number(item.get("user_id") if kind == "bot" else item.get("chat_id"))
        if number is not None:
            mark = (kind, "id", number)
        else:
            mark = (kind, "name", str(item.get("username") or "").lower())
        if mark in seen or mark[1:] == ("name", ""):
            continue
        seen.add(mark)
        kept.append(item)
    return kept


def _dedupe_hot(items: list[dict]) -> list[dict]:
    kept = []
    seen_id = set()
    seen_name = set()
    for item in _dedupe_windows(items):
        number = _number(item.get("chat_id"))
        title = " ".join(str(item.get("title") or "").split()).casefold()
        username = str(item.get("username") or "").strip().lstrip("@").lower()
        name = ("u", username) if username else ("t", title) if title else ("id", number)
        if number in seen_id or name in seen_name:
            continue
        if number is not None:
            seen_id.add(number)
        seen_name.add(name)
        kept.append(item)
    return kept


def normalize(raw: dict | None) -> dict:
    base = default_policy()
    raw = raw or {}
    unit = str(raw.get("delay_unit") or "")
    base["delay_unit"] = "s"
    modes = raw.get("modes") if isinstance(raw.get("modes"), dict) else {}
    for kind in ALL_KINDS:
        if kind in modes:
            base["modes"][kind] = bool(modes[kind])
    raw_spans = raw.get("mode_delays") if isinstance(raw.get("mode_delays"), dict) else {}
    for kind in ALL_KINDS:
        item = raw_spans.get(kind) if isinstance(raw_spans.get(kind), dict) else {}
        span = _keep_span(_seconds(item.get("low")), _seconds(item.get("high")), unit)
        if span is None:
            continue
        base["mode_delays"][kind] = {"low": span[0], "high": span[1]}
    say = raw.get("phrase_say") if isinstance(raw.get("phrase_say"), dict) else {}
    said = _keep_span(_seconds(say.get("low")), _seconds(say.get("high")), unit)
    if said is not None:
        base["phrase_say"] = {"low": said[0], "high": said[1]}
    pay = raw.get("web_pay") if isinstance(raw.get("web_pay"), dict) else {}
    base["web_pay"] = {
        "fll": bool(pay.get("fll", True)),
        "cf": bool(pay.get("cf", True)),
    }
    base["pay_keys"] = _pay_keys(raw)
    if "count_over" in raw:
        base["count_over"] = bool(raw.get("count_over"))
    if "auto_receive" in raw:
        base["auto_receive"] = bool(raw.get("auto_receive"))
    if "window_block_keywords" in raw:
        raw_kw = raw.get("window_block_keywords")
        if isinstance(raw_kw, str):
            base["window_block_keywords"] = [p.strip() for p in raw_kw.replace("，", ",").replace("\n", ",").split(",") if p.strip()]
        elif isinstance(raw_kw, list):
            base["window_block_keywords"] = [str(p).strip() for p in raw_kw if str(p).strip()]
        else:
            base["window_block_keywords"] = []
    if "window_block_minutes" in raw:
        try:
            base["window_block_minutes"] = max(0, min(int(raw.get("window_block_minutes")), 1440))
        except (TypeError, ValueError):
            base["window_block_minutes"] = 0
    base["watch"] = _user_ids(raw.get("watch"))
    window_mode = str(raw.get("window_mode") or "off")
    bot_mode = str(raw.get("bot_mode") or "off")
    mode = _merged_mode(window_mode, bot_mode)
    base["window_mode"] = mode
    base["bot_mode"] = mode
    lists = _read_lists(raw, mode)
    base["window_lists"] = lists
    base["windows"] = list(lists[mode])
    base["bots"] = []
    base["burn"] = bool(raw.get("burn"))
    stored_burns = raw.get("burns") if isinstance(raw.get("burns"), dict) else None
    if stored_burns is None:
        phrase_on = base["burn"]
    else:
        phrase_on = bool(stored_burns.get("phrase")) or bool(stored_burns.get("captcha"))
    base["burns"] = {"captcha": False, "phrase": phrase_on}
    base["burn"] = phrase_on
    delays = []
    seen = set()
    for entry in raw.get("delays") or []:
        item = _clean_delay(entry, unit)
        if item is None or item["wallet"] in seen:
            continue
        seen.add(item["wallet"])
        delays.append(item)
    base["delays"] = delays
    coins = {code: True for code in COINS}
    raw_coins = raw.get("coins") if isinstance(raw.get("coins"), dict) else {}
    for code in COINS:
        if code in raw_coins:
            coins[code] = bool(raw_coins[code])
    base["coins"] = coins
    counts, totals, eaches, custom = _amount_floors(raw)
    if (_number(raw.get("coin_rev")) or 0) < 1:
        for code in COINS:
            if code.endswith("COIN") and not eaches.get(code):
                eaches[code] = _COIN_EACH
    base["coin_rev"] = 1
    base["min_counts"] = counts
    base["min_totals"] = totals
    base["min_eaches"] = eaches
    base["amount_custom"] = custom
    paused = []
    seen_ids = set()
    for item in raw.get("paused") or []:
        number = _number(item)
        if number is None or number in seen_ids:
            continue
        seen_ids.add(number)
        paused.append(number)
        if len(paused) >= 200:
            break
    base["paused"] = paused
    unlinked = []
    seen_off = set()
    for item in raw.get("unlinked") or []:
        number = _number(item)
        if number is None or number in seen_off:
            continue
        seen_off.add(number)
        unlinked.append(number)
        if len(unlinked) >= 200:
            break
    base["unlinked"] = unlinked
    joins = []
    seen_joins = set()
    for item in raw.get("joins") or []:
        number = _number(item)
        if number is None or number in seen_joins:
            continue
        seen_joins.add(number)
        joins.append(number)
        if len(joins) >= 200:
            break
    base["joins"] = joins
    if "join_legacy" in raw:
        base["join_legacy"] = bool(raw.get("join_legacy"))
    elif "joins" not in raw:
        base["join_legacy"] = bool(base["modes"].get("shared"))
    else:
        base["join_legacy"] = False
    base["unknown_coin"] = True if "unknown_coin" not in (raw or {}) else bool(raw.get("unknown_coin"))
    base["hide_zero"] = True if "hide_zero" not in raw else bool(raw.get("hide_zero"))
    base["open_at"] = parse_clock(raw.get("open_at")) or "00:00"
    base["close_at"] = parse_clock(raw.get("close_at")) or "00:00"
    show_min = {}
    raw_min = raw.get("show_min") if isinstance(raw.get("show_min"), dict) else {}
    for code in COINS:
        floor = _floor(raw_min.get(code))
        if floor:
            show_min[code] = floor
    base["show_min"] = show_min
    base["hot_on"] = bool(raw.get("hot_on"))
    hot_windows = []
    for entry in raw.get("hot_windows") or []:
        item = _clean(entry, numeric="chat_id")
        if item is not None and item.get("chat_id") is not None:
            off = _user_ids(entry.get("off") if isinstance(entry, dict) else None)
            if off:
                item["off"] = off
            if isinstance(entry, dict) and entry.get("sign"):
                item["sign"] = True
            if isinstance(entry, dict) and "speak" in entry:
                item["speak"] = bool(entry.get("speak"))
            else:
                item["speak"] = bool(raw.get("hot_on"))
            hot_windows.append(item)
    base["hot_windows"] = _dedupe_hot(hot_windows)
    base["hot_lines"] = _clean_lines(raw.get("hot_lines"))
    low = _number(raw.get("hot_low"))
    high = _number(raw.get("hot_high"))
    if low is None:
        low = 1800
    elif unit != "s":
        low = int(low) * 60
    if high is None:
        high = 5400
    elif unit != "s":
        high = int(high) * 60
    base["hot_low"], base["hot_high"] = _hot_span(int(low), int(high))
    source = str(raw.get("hot_source") or "pack")
    base["hot_source"] = "lines" if source == "lines" else "pack"
    raw_packs = raw.get("hot_packs")
    if isinstance(raw_packs, list):
        packs = _chosen_packs(raw_packs)
        # 旧配置只开了歇后语、段子、打招呼时，补上后来加的类。保存过就不再补。
        rev = _number(raw.get("hot_rev"))
        if rev is None:
            rev = 1
        if rev < 2 and set(packs) == set(_LEGACY_PACKS) and len(packs) == len(_LEGACY_PACKS):
            packs = list(_PACK_IDS)
        base["hot_packs"] = packs
    else:
        base["hot_packs"] = list(_PACK_IDS)
    base["hot_rev"] = 2
    if unit == "s":
        drop = _number(raw.get("hot_drop") if "hot_drop" in raw else raw.get("hot_drop_ms"))
    else:
        drop = _as_seconds(_number(raw.get("hot_drop_ms")) or 0)
    base["hot_drop"] = 0 if drop is None else max(0, min(int(drop), DROP_MAX))
    base["hot_open"] = parse_clock(raw.get("hot_open")) or "00:00"
    base["hot_close"] = parse_clock(raw.get("hot_close")) or "00:00"
    cap = _number(raw.get("hot_cap"))
    base["hot_cap"] = 0 if cap is None else max(0, min(int(cap), HOT_CAP_MAX))
    base["hot_off"] = _user_ids(raw.get("hot_off"))
    return base


def _stored_floors(raw: dict, key: str, legacy: str) -> dict:
    if key not in raw:
        if legacy not in raw:
            return {}
        old = _floor(raw.get(legacy))
        return {code: old for code in COINS} if old else {}
    source = raw.get(key) if isinstance(raw.get(key), dict) else {}
    found = {}
    for code in COINS:
        floor = _floor(source.get(code))
        if not floor and code == "TON":
            floor = _floor(source.get("GRAM"))
        if not floor and code == "USDT":
            floor = _floor(source.get("USD"))
        if floor:
            found[code] = floor
    return found


def _amount_floors(raw: dict) -> tuple[dict, dict, dict, bool]:
    counts = _stored_floors(raw, "min_counts", "min_count")
    totals = _stored_floors(raw, "min_totals", "min_total")
    eaches = _stored_floors(raw, "min_eaches", "min_each")
    custom = bool(raw.get("amount_custom"))
    legacy = "min_total" in raw or "min_each" in raw
    if custom or legacy or counts or totals or eaches:
        return counts, totals, eaches, custom
    return dict(DEFAULT_COUNTS), dict(DEFAULT_TOTALS), dict(DEFAULT_EACHES), False


def _floor(value) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return ""
    number = Decimal(text)
    if number <= 0:
        return ""
    shown = format(number, "f")
    if "." in shown:
        shown = shown.rstrip("0").rstrip(".")
    return shown


def packet_value(shown: str) -> tuple[Decimal | None, str]:
    matched = re.search(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)?", shown or "")
    if matched is None:
        return None, ""
    amount = Decimal(matched.group(1).replace(",", ""))
    unit = (matched.group(2) or "").upper()
    unit = _COIN_ALIAS.get(unit, unit)
    if unit not in COINS:
        unit = ""
    return amount, unit


def slot_count(slots: str) -> int | None:
    matched = re.search(r"(\d+)\s*/\s*(\d+)", slots or "")
    if matched is None:
        return None
    count = int(matched.group(2))
    return count or None


def below_amount(policy: dict, shown: str, slots: str = "") -> bool:
    current = normalize(policy)
    amount, unit = packet_value(shown)
    if unit and not current["coins"].get(unit, True):
        return True
    if not unit and not current.get("unknown_coin", True):
        return True
    if amount is None or not unit:
        return False
    count = slot_count(slots)
    need = current["min_counts"].get(unit, "")
    if need and count is not None and count < int(need):
        return True
    floor = current["min_totals"].get(unit, "")
    if floor and amount < Decimal(floor):
        return True
    each = current["min_eaches"].get(unit, "")
    if each and count and amount / count < Decimal(each):
        return True
    if count and current.get("count_over", True) and count > int(amount.to_integral_value(ROUND_DOWN)):
        return True
    return False


def wants_slots(policy: dict) -> bool:
    current = normalize(policy)
    if current.get("count_over", True):
        return True
    return any(current["min_counts"].values()) or any(current["min_eaches"].values())


def parse_floors(text: str) -> tuple[str, str, str, str]:
    raw = (text or "").replace("，", " ").replace(",", " ").replace("/", " ")
    parts = raw.split()
    if len(parts) != 3:
        return "", "", "", "主体：请写个数、金额和人均，例如 5 10 2。"
    count, error = _parse_count(parts[0])
    if error:
        return "", "", "", error
    total, error = parse_floor(parts[1])
    if error:
        return "", "", "", error
    each, error = parse_floor(parts[2])
    if error:
        return "", "", "", error
    return count, total, each, ""


def _parse_count(text: str) -> tuple[str, str]:
    value, error = parse_floor(text)
    if error or not value:
        return value, error
    if not value.isdigit():
        return "", "主体：个数要写整数。"
    return value, ""


def floor_preview(code: str, count: str, total: str, each: str) -> str:
    if str(code or "").upper() not in COINS:
        return ""
    parts = []
    if count:
        parts.append(f"少于 {count} 个")
    if total:
        parts.append(f"少于 {total}")
    if each:
        parts.append(f"人均少于 {each}")
    if not parts:
        return ""
    return "，或".join(parts) + "，不抢。"


def parse_floor(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if raw in {"0", "关", "关闭"}:
        return "", ""
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return "", "主体：这不是数字。"
    number = Decimal(raw)
    if number <= 0:
        return "", ""
    return _floor(number), ""


def _id_list(policy: dict, key: str) -> list | None:
    if not isinstance(policy, dict):
        return None
    value = policy.get(key)
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        return None
    return value


def account_paused(policy: dict, user_id: int) -> bool:
    paused = _id_list(policy, "paused")
    if paused is None:
        paused = normalize(policy).get("paused") or []
    return int(user_id) in paused


def account_linked(policy: dict, user_id: int) -> bool:
    unlinked = _id_list(policy, "unlinked")
    if unlinked is None:
        unlinked = normalize(policy).get("unlinked") or []
    return int(user_id) not in unlinked


def set_link(policy: dict, user_ids, linked: bool) -> dict:
    current = normalize(policy)
    unlinked = [item for item in current["unlinked"]]
    seen = set(unlinked)
    ids = []
    for item in user_ids:
        number = _number(item)
        if number is None:
            continue
        ids.append(number)
        if linked:
            unlinked = [one for one in unlinked if one != number]
            seen.discard(number)
            continue
        if number not in seen and len(unlinked) < 200:
            unlinked.append(number)
            seen.add(number)
    current["unlinked"] = unlinked
    if not linked:
        current = set_join(current, ids, False)
        return set_running(current, ids, False)
    return current


def account_watches(policy: dict, user_id: int) -> bool:
    watched = _id_list(policy, "watch")
    if watched is None:
        watched = normalize(policy).get("watch") or []
    return int(user_id) in watched


def set_watch(policy: dict, user_ids, watching: bool) -> dict:
    current = normalize(policy)
    watched = [item for item in current["watch"]]
    seen = set(watched)
    for item in user_ids:
        number = _number(item)
        if number is None:
            continue
        if watching:
            if number not in seen and len(watched) < 200:
                watched.append(number)
                seen.add(number)
            continue
        watched = [one for one in watched if one != number]
        seen.discard(number)
    current["watch"] = watched
    return current


def _pay_keys(raw: dict) -> dict:
    incoming = raw.get("pay_keys") if isinstance(raw.get("pay_keys"), dict) else {}
    kept = {}
    for bucket in ("fll", "cf"):
        text = "".join(str(incoming.get(bucket) or "").split())
        if _PAY_TOKEN.fullmatch(text):
            kept[bucket] = text
    return kept


def set_pay_keys(policy: dict, incoming: dict) -> tuple[dict, str]:
    current = normalize(policy)
    keys = dict(current.get("pay_keys") or {})
    for bucket in ("fll", "cf"):
        if bucket not in incoming:
            continue
        text = "".join(str(incoming.get(bucket) or "").split())
        pay = dict(current.get("web_pay") or {})
        if not text:
            keys.pop(bucket, None)
            pay[bucket] = False
            current["web_pay"] = pay
            continue
        if not _PAY_TOKEN.fullmatch(text):
            return current, "token 无效"
        fresh = not keys.get(bucket)
        keys[bucket] = text
        if fresh:
            pay[bucket] = False
            current["web_pay"] = pay
    current["pay_keys"] = keys
    return current, ""


def present_policy(policy: dict) -> dict:
    shown = dict(policy)
    keys = shown.pop("pay_keys", None)
    if not isinstance(keys, dict):
        keys = {}
    shown["pay_ready"] = {
        "fll": bool(str(keys.get("fll") or "").strip()),
        "cf": bool(str(keys.get("cf") or "").strip()),
    }
    return shown


def pay_bucket(bot: str, kind: str) -> str:
    name = str(bot or "").lower().lstrip("@")
    if name in _PAY_FLL:
        return "fll"
    if kind == "cloudflare" or name in _PAY_CF:
        return "cf"
    return ""


def pay_open(policy: dict, bot: str, kind: str, owner_id: int = 0) -> bool:
    bucket = pay_bucket(bot, kind)
    if not bucket:
        return True
    flags = normalize(policy).get("web_pay") or {}
    if not flags.get(bucket, True):
        return False
    from meiguan.reports import AUTHOR_ID

    if int(owner_id or 0) == AUTHOR_ID:
        return True
    keys = policy.get("pay_keys") if isinstance(policy.get("pay_keys"), dict) else {}
    return bool(str(keys.get(bucket) or "").strip())


def account_brief(policy: dict, user_id: int) -> dict:
    current = normalize(policy)
    if account_watches(current, user_id):
        state = "只通知，不抢"
    elif account_paused(current, user_id):
        state = "抢包关着"
    else:
        state = "会领"
    return {"state": state}


def account_joins(policy: dict, user_id: int) -> bool:
    if not isinstance(policy, dict) or "join_legacy" not in policy:
        policy = normalize(policy)
    if policy.get("join_legacy"):
        return True
    joins = _id_list(policy, "joins")
    if joins is None:
        joins = normalize(policy).get("joins") or []
    return int(user_id) in joins


def set_join(policy: dict, user_ids, joining: bool, known: list[int] | None = None) -> dict:
    current = normalize(policy)
    if current.get("join_legacy"):
        current["joins"] = [int(item) for item in (known or [])][:200]
        current["join_legacy"] = False
    joins = [item for item in current["joins"]]
    seen = set(joins)
    for item in user_ids:
        number = _number(item)
        if number is None:
            continue
        if joining:
            if number not in seen and len(joins) < 200:
                joins.append(number)
                seen.add(number)
            current["unlinked"] = [one for one in current["unlinked"] if one != number]
            continue
        joins = [one for one in joins if one != number]
        seen.discard(number)
    current["joins"] = joins
    current["join_legacy"] = False
    return current


def toggle_account(policy: dict, user_id: int) -> dict:
    current = normalize(policy)
    uid = int(user_id)
    paused = [item for item in current["paused"] if item != uid]
    if uid not in current["paused"]:
        paused.append(uid)
    current["paused"] = paused
    return current


def clear_axis(policy: dict, axis: str) -> dict:
    current = normalize(policy)
    if axis in ("window", "bot"):
        current["window_lists"][current["window_mode"]] = []
        return normalize(current)
    return current


def toggle_coin(policy: dict, code: str) -> dict:
    current = normalize(policy)
    if code not in COINS:
        return current
    current["coins"][code] = not current["coins"].get(code, True)
    return current


def toggle_auto_receive(policy: dict) -> dict:
    current = normalize(policy)
    current["auto_receive"] = not current.get("auto_receive", True)
    return current


def set_floor(policy: dict, kind: str, code: str, value: str) -> dict:
    current = normalize(policy)
    field = {"count": "min_counts", "total": "min_totals", "each": "min_eaches"}.get(kind)
    coin = str(code or "").upper()
    if field is None or coin not in COINS:
        return current
    floor = _floor(value)
    if field == "min_counts" and floor and not floor.isdigit():
        return current
    current["amount_custom"] = True
    if floor:
        current[field][coin] = floor
    else:
        current[field].pop(coin, None)
    return current


def set_show_min(policy: dict, code: str, value: str) -> dict:
    current = normalize(policy)
    coin = str(code or "").upper()
    if coin not in COINS:
        return current
    floor = _floor(value)
    if floor:
        current["show_min"][coin] = floor
    else:
        current["show_min"].pop(coin, None)
    return current


def balance_hidden(policy: dict, code: str, total: Decimal) -> bool:
    if not isinstance(policy, dict) or "hide_zero" not in policy or not isinstance(policy.get("show_min"), dict):
        policy = normalize(policy)
    if total == 0 and policy["hide_zero"]:
        return True
    floor = policy["show_min"].get(str(code or "").upper(), "")
    return bool(floor) and total < Decimal(floor)


def wallet_key(value: str) -> str:
    name = str(value or "").strip().lstrip("@").lower()
    if name in _SKIP_NAMES or not _USER.fullmatch(name):
        return ""
    return name


def parse_wallet(text: str) -> tuple[str, str]:
    raw = " ".join((text or "").split())
    if not raw or len(raw) > 200:
        return "", "主体：无法识别"
    candidate = raw
    if not raw.startswith("@"):
        parsed = parse_target(raw)
        if parsed and parsed.get("username") and parsed.get("chat_id") is None:
            candidate = str(parsed["username"])
    name = wallet_key(candidate)
    if not name:
        return "", "主体：无法识别"
    return name, ""


def _as_seconds(value: int) -> int:
    if value <= 0:
        return 0
    return (int(value) + 500) // 1000


def _delay_span(low: int, high: int, unit: str) -> tuple[int, int] | None:
    if low < 0 or high < 0 or low > high:
        return None
    if unit == "ms" or (unit not in ("s", "ms") and high > 60):
        low, high = _as_seconds(low), _as_seconds(high)
        if high < low:
            high = low
    if low > DELAY_MAX or high > DELAY_MAX:
        return None
    if high < 1 and (low, high) != (0, 0):
        return None
    return low, high


def _clean_delay(entry, unit: str = "s") -> dict | None:
    if not isinstance(entry, dict):
        return None
    wallet = wallet_key(entry.get("wallet"))
    if not wallet:
        return None
    low = _number(entry.get("low"))
    high = _number(entry.get("high"))
    if low is None or high is None:
        return None
    span = _delay_span(int(low), int(high), unit)
    if span is None or span[1] < 1:
        return None
    return {"wallet": wallet, "low": span[0], "high": span[1]}


def wallet_label(key: str) -> str:
    name = wallet_key(key)
    return f"@{name}" if name else ""


def delay_text(low: int, high: int) -> str:
    if low == high:
        return f"{low} 秒"
    return f"{low}–{high} 秒"


def parse_span(text: str) -> tuple[int, int] | None:
    raw = "".join((text or "").strip().lower().split())
    unit = "s"
    if "毫秒" in raw or "ms" in raw:
        unit = "ms"
    raw = raw.replace("毫秒", "").replace("ms", "").replace("秒", "").replace("s", "")
    matched = _SPAN.fullmatch(raw)
    if matched:
        low, high = int(matched.group(1)), int(matched.group(2))
    elif raw.isdigit():
        low = high = int(raw)
    else:
        return None
    if unit == "ms":
        low, high = _as_seconds(low), _as_seconds(high)
    return low, high


def _seconds(value) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    if number < 0:
        return None
    return round(number, 3)


def _shown_span(low: float, high: float) -> tuple[int | float, int | float]:
    def one(number: float) -> int | float:
        number = round(float(number), 3)
        if number == int(number):
            return int(number)
        return number

    return one(low), one(high)


def _keep_span(low: float | None, high: float | None, unit: str) -> tuple[int | float, int | float] | None:
    if low is None or high is None or high < low:
        return None
    if unit == "ms" or (unit not in ("s", "ms") and high > DELAY_MAX):
        low, high = round(low / 1000, 3), round(high / 1000, 3)
        if high < low:
            high = low
    if low > DELAY_MAX or high > DELAY_MAX:
        return None
    if high < 0.001 and (low, high) != (0, 0):
        return None
    return _shown_span(low, high)


def span_error(low: float, high: float) -> str:
    low, high = float(low), float(high)
    if low < 0 or high < 0 or low > DELAY_MAX or high > DELAY_MAX:
        return "主体：延迟请写在 0 到 60 秒"
    if high < 0.001:
        return "主体：延迟至少 1 秒"
    if low > high:
        return "主体：前面不能大于后面"
    return ""


def pick_delay(low: float, high: float) -> float:
    low, high = float(low), float(high)
    if high < 0.001:
        return 0
    if low < 0:
        low = 0
    if high > low:
        return round(random.uniform(low, high), 3)
    return high


_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")


def parse_clock(value) -> str:
    matched = _CLOCK.fullmatch(str(value or "").strip())
    if not matched:
        return ""
    hour, minute = int(matched.group(1)), int(matched.group(2))
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def hours_open(
    policy: dict | None,
    moment: datetime | None = None,
    *,
    start_key: str = "open_at",
    end_key: str = "close_at",
) -> bool:
    raw = policy or {}
    start = parse_clock(raw.get(start_key)) or "00:00"
    end = parse_clock(raw.get(end_key)) or "00:00"
    if start == end:
        return True
    now = moment or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    minute = now.hour * 60 + now.minute
    begin = int(start[:2]) * 60 + int(start[3:])
    stop = int(end[:2]) * 60 + int(end[3:])
    if begin < stop:
        return begin <= minute < stop
    return minute >= begin or minute < stop


def sign_hours(moment: datetime | None = None) -> bool:
    return hours_open(
        {"sign_open": "01:00", "sign_close": "06:00"},
        moment,
        start_key="sign_open",
        end_key="sign_close",
    )


def set_hours(policy: dict, open_at, close_at) -> tuple[dict, str]:
    current = normalize(policy)
    start = parse_clock(open_at)
    end = parse_clock(close_at)
    if not start or not end:
        return current, "主体：时间写成 10:00 这样"
    current["open_at"] = start
    current["close_at"] = end
    return current, ""


def set_delay(policy: dict, wallet: str, low: int, high: int) -> tuple[dict, str]:
    current = normalize(policy)
    name = wallet_key(wallet)
    if not name:
        return current, "主体：无法识别"
    error = span_error(low, high)
    if error:
        return current, error
    current["delays"] = [item for item in current["delays"] if item["wallet"] != name]
    current["delays"].append({"wallet": name, "low": low, "high": high})
    return normalize(current), ""


def clear_delay(policy: dict, wallet: str) -> dict:
    current = normalize(policy)
    wallet = wallet.strip().lstrip("@").lower()
    current["delays"] = [item for item in current["delays"] if item["wallet"] != wallet]
    return current


def _chosen_packs(raw) -> list[str]:
    chosen = []
    for item in raw or []:
        name = str(item or "")
        if name != _CUSTOM and name not in _PACK_IDS:
            continue
        if name in chosen:
            continue
        chosen.append(name)
    return chosen


def line_error(text: str) -> str:
    if _LINKISH.search(text or ""):
        return "主体：句子里不要带链接"
    if len(text) > 80:
        return "主体：一句最长 80 字"
    return ""


def _clean_lines(raw) -> list[str]:
    lines = []
    seen = set()
    for item in raw or []:
        text = " ".join(str(item or "").split())
        if not text or line_error(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        lines.append(text)
        if len(lines) >= 50:
            break
    return lines


def _hot_span(low: int, high: int) -> tuple[int, int]:
    low = max(HOT_MIN, min(int(low), HOT_MAX))
    high = max(HOT_MIN, min(int(high), HOT_MAX))
    if high < low:
        high = low
    return low, high


def _store_span(policy: dict, key: str, low: float, high: float) -> tuple[dict, str]:
    if float(low) == 0 and float(high) == 0:
        policy[key] = {"low": 0, "high": 0}
        return policy, ""
    error = span_error(low, high)
    if error:
        return policy, error
    kept = _keep_span(float(low), float(high), "s")
    if kept is None:
        return policy, "主体：延迟请写在 0 到 60 秒"
    policy[key] = {"low": kept[0], "high": kept[1]}
    return policy, ""


def set_mode_delay(policy: dict, kind: str, low: float, high: float) -> tuple[dict, str]:
    current = normalize(policy)
    if kind not in ALL_KINDS:
        return current, "主体：没有这个模块"
    if float(low) == 0 and float(high) == 0:
        current["mode_delays"][kind] = {"low": 0, "high": 0}
        return current, ""
    error = span_error(low, high)
    if error:
        return current, error
    kept = _keep_span(float(low), float(high), "s")
    if kept is None:
        return current, "主体：延迟请写在 0 到 60 秒"
    current["mode_delays"][kind] = {"low": kept[0], "high": kept[1]}
    return current, ""


def set_phrase_say(policy: dict, low: float, high: float) -> tuple[dict, str]:
    current = normalize(policy)
    return _store_span(current, "phrase_say", low, high)


def mode_key(kind: str) -> str:
    return "webapp" if kind == "cloudflare" else kind


def _span_for(item: dict) -> tuple[float, float] | None:
    low = _seconds(item.get("low")) or 0
    high = _seconds(item.get("high")) or 0
    if high < 0.001:
        return None
    return low, high


def mode_delay_for(policy: dict, kind: str) -> tuple[float, float] | None:
    item = (normalize(policy).get("mode_delays") or {}).get(mode_key(kind)) or {}
    return _span_for(item)


def phrase_say_for(policy: dict) -> tuple[float, float] | None:
    return _span_for(normalize(policy).get("phrase_say") or {})


def set_running(policy: dict, user_ids, running: bool) -> dict:
    current = normalize(policy)
    paused = [item for item in current["paused"]]
    seen = set(paused)
    for item in user_ids:
        number = _number(item)
        if number is None:
            continue
        if running:
            paused = [one for one in paused if one != number]
            seen.discard(number)
            current["unlinked"] = [one for one in current["unlinked"] if one != number]
            continue
        if number in seen or len(paused) >= 200:
            continue
        paused.append(number)
        seen.add(number)
    current["paused"] = paused
    return current


def set_hot_seat(policy: dict, chat_id, user_id, on: bool) -> tuple[dict, str]:
    current = normalize(policy)
    chat = _number(chat_id)
    user = _number(user_id)
    if chat is None or user is None:
        return current, "主体：无法识别"
    found = False
    for item in current["hot_windows"]:
        if int(item.get("chat_id") or 0) != chat:
            continue
        found = True
        off = [number for number in item.get("off") or [] if number != user]
        if not on:
            off.append(user)
        if off:
            item["off"] = off
        else:
            item.pop("off", None)
    if not found:
        return current, "主体：没有这个窗口"
    return current, ""


def set_hot_sign(policy: dict, chat_id, on: bool) -> tuple[dict, str]:
    current = normalize(policy)
    chat = _number(chat_id)
    if chat is None:
        return current, "主体：无法识别"
    found = False
    for item in current["hot_windows"]:
        if int(item.get("chat_id") or 0) != chat:
            continue
        found = True
        if on:
            item["sign"] = True
        else:
            item.pop("sign", None)
    if not found:
        return current, "主体：没有这个窗口"
    return current, ""


def set_hot_speak(policy: dict, chat_id, on: bool) -> tuple[dict, str]:
    current = normalize(policy)
    chat = _number(chat_id)
    if chat is None:
        return current, "主体：无法识别"
    found = False
    for item in current["hot_windows"]:
        if int(item.get("chat_id") or 0) != chat:
            continue
        found = True
        item["speak"] = bool(on)
    if not found:
        return current, "主体：没有这个窗口"
    return current, ""


def assign_hot(policy: dict, *, on: bool | None = None, low: int | None = None, high: int | None = None, lines=None, windows=None, drop: int | None = None, packs=None, start: str | None = None, stop: str | None = None, cap: int | None = None, muted=None) -> tuple[dict, str]:
    current = normalize(policy)
    if on is not None:
        current["hot_on"] = bool(on)
    if start is not None or stop is not None:
        begin = parse_clock(current["hot_open"] if start is None else start)
        end = parse_clock(current["hot_close"] if stop is None else stop)
        if not begin or not end:
            return current, "主体：时间写成 10:00 这样"
        current["hot_open"] = begin
        current["hot_close"] = end
    if cap is not None:
        number = int(cap)
        if number < 0 or number > HOT_CAP_MAX:
            return current, "主体：每天上限请写在 0 到 500"
        current["hot_cap"] = number
    if muted is not None:
        current["hot_off"] = _user_ids(muted)
    if low is not None or high is not None:
        current["hot_low"], current["hot_high"] = _hot_span(
            current["hot_low"] if low is None else int(low),
            current["hot_high"] if high is None else int(high),
        )
    if drop is not None:
        current["hot_drop"] = max(0, min(int(drop), DROP_MAX))
    if packs is not None:
        current["hot_packs"] = _chosen_packs(packs)
        current["hot_source"] = "pack"
    if lines is not None:
        cleaned = []
        seen = set()
        for item in lines:
            text = " ".join(str(item or "").split())
            if not text:
                continue
            error = line_error(text)
            if error:
                return current, error
            if text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
            if len(cleaned) > 50:
                return current, "主体：最多 50 句"
        current["hot_lines"] = cleaned
        current["hot_source"] = "lines"
    if windows is not None:
        items = []
        for entry in windows:
            number = _number(entry.get("chat_id") if isinstance(entry, dict) else None)
            if number is None:
                continue
            title = str((entry.get("title") if isinstance(entry, dict) else "") or "").strip()[:40]
            username = _username(entry.get("username") if isinstance(entry, dict) else "")
            item = {"id": _entry_id(entry.get("id") if isinstance(entry, dict) else ""), "title": title or (f"@{username}" if username else str(number)), "chat_id": number}
            kind = str(entry.get("kind") or "") if isinstance(entry, dict) else ""
            if kind in {"user", "group", "channel", "bot"}:
                item["kind"] = kind
            if username:
                item["username"] = username
            off = _user_ids(entry.get("off") if isinstance(entry, dict) else None)
            if off:
                item["off"] = off
            if isinstance(entry, dict) and entry.get("sign"):
                item["sign"] = True
            if isinstance(entry, dict) and "speak" in entry:
                item["speak"] = bool(entry.get("speak"))
            else:
                item["speak"] = False
            items.append(item)
        current["hot_windows"] = _dedupe_hot(items)
    return normalize(current), ""


def delay_for(policy: dict, wallet: str) -> tuple[int, int] | None:
    name = wallet_key(wallet)
    if not name:
        return None
    for item in normalize(policy).get("delays") or []:
        if item["wallet"] == name:
            return int(item["low"]), int(item["high"])
    return None


def window_line(entry: dict) -> str:
    title = str(entry.get("title") or "").strip()
    number = _number(entry.get("chat_id"))
    if number is None:
        return title or "未命名"
    if title and title != str(number):
        return f"{title} {number}"
    return str(number)


def parse_target(text: str) -> dict | None:
    raw = " ".join((text or "").split())
    if not raw or len(raw) > 200:
        return None
    raw = re.sub(
        r"^(?:https?://)?(?:t\.me|telegram\.me)/s/",
        "https://t.me/",
        raw,
        count=1,
        flags=re.I,
    )
    if raw.startswith("@"):
        name = raw[1:]
        if not _USER.fullmatch(name) or name.lower() in _SKIP_NAMES:
            return None
        return {"username": name, "title": f"@{name}"}
    if re.fullmatch(r"-?\d{3,}", raw):
        number = marked_channel_id(int(raw))
        return {"chat_id": number, "title": str(number)}
    matched = _LINK.search(raw)
    if matched is None:
        return None
    if matched.group(1):
        return {"chat_id": marked_channel_id(int(matched.group(1))), "title": "私密窗口"}
    name = matched.group(2) or ""
    if not _USER.fullmatch(name) or name.lower() in _SKIP_NAMES:
        return None
    return {"username": name, "title": f"@{name}"}


def lookup_query(text: str) -> dict | None:
    raw = " ".join((text or "").split())
    if not raw or len(raw) > 200 or " " in raw:
        return None
    invite = _INVITE_LINK.search(raw)
    if invite:
        return {"invite": invite.group(1)}
    parsed = parse_target(raw)
    if parsed and parsed.get("username"):
        return parsed
    if parsed and parsed.get("chat_id") is not None:
        digits = raw.lstrip("-")
        if raw.startswith("-") or len(digits) >= 6:
            return parsed
        return None
    name = raw[1:] if raw.startswith("@") else raw
    if len(name) >= 3 and _USER.fullmatch(name) and name.lower() not in _SKIP_NAMES:
        return {"username": name, "title": f"@{name}"}
    return None


def _same(item: dict, parsed: dict, numeric: str) -> bool:
    username = _username(parsed.get("username"))
    if username and _username(item.get("username")).lower() == username.lower():
        return True
    number = _number(parsed.get(numeric))
    if number is not None and item.get(numeric) is not None and int(item[numeric]) == number:
        return True
    return False


def shield_picks(dialogs: list, picks: list) -> tuple[list[dict], str]:
    if not isinstance(picks, list) or not picks:
        return [], "主体：无法识别"
    index = {}
    for item in dialogs or []:
        if not isinstance(item, dict):
            continue
        kind = _window_bucket(str(item.get("kind") or ""))
        number = _number(item.get("user_id") if kind == "bot" else item.get("chat_id"))
        if number is None:
            continue
        index[(kind, number)] = item
    chosen = []
    seen = set()
    for raw in picks:
        if not isinstance(raw, dict):
            return [], "主体：无法识别"
        kind = _window_bucket(str(raw.get("kind") or ""))
        number = _number(raw.get("user_id") if kind == "bot" else raw.get("chat_id"))
        if number is None:
            return [], "主体：无法识别"
        mark = (kind, number)
        if mark in seen:
            continue
        seen.add(mark)
        item = index.get(mark) or raw
        stored = _shield_kind(item)
        entry = {"kind": stored, "title": str(item.get("title") or "").strip()[:40]}
        username = _username(item.get("username"))
        if username:
            entry["username"] = username
        entry["user_id" if stored == "bot" else "chat_id"] = number
        chosen.append(entry)
    if not chosen:
        return [], "主体：无法识别"
    return chosen, ""


def append_entry(policy: dict, axis: str, parsed: dict) -> tuple[dict, str]:
    current = normalize(policy)
    kind = _shield_kind(parsed)
    if axis == "bot" and kind != "bot":
        return current, "主体：这是窗口，不是机器人"
    if axis not in ("window", "bot"):
        return current, "主体：无法识别"
    mode = current["window_mode"]
    items = list(current["window_lists"][mode])
    numeric = "user_id" if kind == "bot" else "chat_id"
    bucket = _window_bucket(kind)
    if any(_same(item, parsed, numeric) for item in items if _window_bucket(str(item.get("kind") or "chat")) == bucket):
        return current, "主体：已经在名单里"
    entry = {"id": secrets.token_hex(4), "title": str(parsed.get("title") or ""), "kind": kind}
    if parsed.get("username"):
        entry["username"] = parsed["username"]
    if parsed.get(numeric) is not None:
        entry[numeric] = int(parsed[numeric])
    items.append(entry)
    current["window_lists"][mode] = items
    return normalize(current), ""


def remove_entry(policy: dict, axis: str, entry_id: str) -> dict:
    current = normalize(policy)
    if axis not in ("window", "bot"):
        return current
    mode = current["window_mode"]
    current["window_lists"][mode] = [item for item in current["window_lists"][mode] if item.get("id") != entry_id]
    return normalize(current)


def set_axis(policy: dict, axis: str, mode: str) -> dict:
    current = normalize(policy)
    if axis not in ("window", "bot") or mode not in AXIS:
        return current
    current["window_mode"] = mode
    current["bot_mode"] = mode
    return normalize(current)


def _window_marks(entry: dict) -> set[str]:
    number = _number(entry.get("chat_id"))
    if number is not None:
        return {str(number)}
    username = _username(entry.get("username"))
    return {username.lower()} if username else set()


def _chat_keys(chat_id: int, username: str) -> set[str]:
    keys = set()
    if chat_id:
        keys.add(str(int(chat_id)))
    name = _username(username)
    if name:
        keys.add(name.lower())
    return keys


def _bot_keys(bots: set[str]) -> set[str]:
    keys = set()
    for item in bots:
        text = str(item or "").strip().lstrip("@")
        if text:
            keys.add(text.lower())
    return keys


def _shield_hit(entry: dict, chat_keys: set[str], bot_keys: set[str]) -> bool:
    if (entry.get("kind") or "chat") == "bot":
        marks = set()
        username = _username(entry.get("username"))
        if username:
            marks.add(username.lower())
        number = _number(entry.get("user_id"))
        if number is not None:
            marks.add(str(number))
        return bool(marks & bot_keys)
    return bool(_window_marks(entry) & chat_keys)


def window_on(policy: dict, *, chat_id: int = 0, username: str = "") -> bool:
    current = normalize(policy)
    mode = current["window_mode"]
    if mode == "off":
        return True
    chats = [item for item in current["windows"] if (item.get("kind") or "chat") != "bot"]
    hit = any(_shield_hit(item, _chat_keys(chat_id, username), set()) for item in chats)
    if mode == "white":
        return hit
    if mode == "black":
        return not hit
    return True


def allow(policy: dict, *, chat_id: int, username: str, bots: set[str]) -> bool:
    from meiguan.reports import locked_chat

    if locked_chat(chat_id, username, bots):
        return False
    current = normalize(policy)
    mode = current["window_mode"]
    if mode == "off":
        return True
    chat_keys = _chat_keys(chat_id, username)
    bot_keys = _bot_keys(bots)
    hit = any(_shield_hit(item, chat_keys, bot_keys) for item in current["windows"])
    if mode == "white":
        return hit
    if mode == "black":
        return not hit
    return True


def axis_line(axis: str, mode: str, count: int) -> str:
    if mode == "white":
        text = "白名单：只抢名单里的。"
        if count == 0:
            text += "当前没有名单，不会抢。"
        return text
    if mode == "black":
        return "黑名单：群是整群不抢，机器人是任何群都不抢。"
    return "关闭：不限制。"


def _check() -> None:
    policy = default_policy()
    assert allow(policy, chat_id=1, username="a", bots={"kpay"})
    assert window_on(policy, chat_id=1, username="a")
    policy["window_mode"] = "white"
    assert not window_on(policy, chat_id=1, username="a")
    assert not allow(policy, chat_id=1, username="a", bots=set())
    policy["windows"] = [{"id": "abcd1234", "username": "a", "title": "@a"}]
    assert allow(policy, chat_id=9, username="A", bots=set())
    assert not allow(policy, chat_id=9, username="b", bots=set())
    policy["window_mode"] = "black"
    assert not allow(policy, chat_id=9, username="a", bots={"okpay"})
    assert allow(policy, chat_id=3, username="b", bots={"okpay"})
    blocked = {
        "window_mode": "black",
        "windows": [{"id": "abcd1235", "kind": "bot", "username": "kpay", "title": "@kpay"}],
    }
    assert not allow(blocked, chat_id=3, username="b", bots={"KPAY"})
    assert allow(blocked, chat_id=3, username="b", bots={"okpay"})
    assert allow(blocked, chat_id=8, username="room", bots=set())
    opened = dict(blocked)
    opened["window_mode"] = "white"
    assert allow(opened, chat_id=3, username="b", bots={"kpay"})
    assert not allow(opened, chat_id=3, username="b", bots={"okpay"})
    mixed = {
        "window_mode": "white",
        "windows": [
            {"id": "abcd1234", "kind": "chat", "chat_id": 9, "title": "群"},
            {"id": "abcd1235", "kind": "bot", "username": "kpay", "title": "@kpay"},
        ],
    }
    assert allow(mixed, chat_id=9, username="", bots={"other"})
    assert allow(mixed, chat_id=3, username="", bots={"kpay"})
    assert not allow(mixed, chat_id=3, username="", bots={"other"})
    legacy = {"bot_mode": "black", "bots": [{"id": "abcd1235", "username": "kpay", "title": "@kpay"}]}
    assert normalize(legacy)["window_mode"] == "black"
    assert normalize(legacy)["bots"] == []
    assert normalize(legacy)["windows"][0]["kind"] == "bot"
    assert not allow(legacy, chat_id=1, username="x", bots={"kpay"})
    assert parse_target("https://t.me/c/123/9")["chat_id"] == marked_channel_id(123)
    assert parse_target("-1001234567890")["chat_id"] == -1001234567890
    assert parse_target("1234567890")["chat_id"] == marked_channel_id(1234567890)
    named = {
        "window_mode": "white",
        "windows": [{"id": "abcd1234", "chat_id": -1001234567890, "username": "old", "title": "群"}],
    }
    assert allow(named, chat_id=-1001234567890, username="other", bots=set())
    assert not allow(named, chat_id=-100999, username="old", bots=set())
    assert parse_target("@ab")["username"] == "ab"
    assert parse_target("https://t.me/kpay/3")["username"] == "kpay"
    assert parse_target("https://t.me/s/kpay")["username"] == "kpay"
    assert parse_target("nope") is None
    assert lookup_query("https://t.me/+AbcdEfgh")["invite"] == "AbcdEfgh"
    assert lookup_query("t.me/joinchat/AbcdEfgh12")["invite"] == "AbcdEfgh12"
    assert lookup_query("kpay")["username"] == "kpay"
    assert lookup_query("nft")["username"] == "nft"
    assert lookup_query("@ab")["username"] == "ab"
    assert lookup_query("ab") is None
    assert lookup_query("红包群") is None
    assert lookup_query("12345") is None
    assert lookup_query("-1001234567890")["chat_id"] == -1001234567890
    assert lookup_query("https://t.me/kpay")["username"] == "kpay"
    dialogs = [
        {"kind": "chat", "chat_id": -1001, "title": "甲群", "username": "jia"},
        {"kind": "bot", "user_id": 42, "title": "甲", "username": "jiabot"},
    ]
    chosen, error = shield_picks(dialogs, [{"kind": "chat", "chat_id": -1001}, {"kind": "bot", "user_id": 42}])
    assert error == "" and chosen[0]["title"] == "甲群" and chosen[1]["user_id"] == 42
    typed = [
        {"kind": "channel", "chat_id": -1002, "title": "甲频道", "username": "news"},
        {"kind": "user", "chat_id": 7, "title": "甲", "username": "jia"},
        {"kind": "group", "chat_id": -1003, "title": "乙群"},
    ]
    picked, error = shield_picks(typed, [{"kind": "channel", "chat_id": -1002}, {"kind": "user", "chat_id": 7}])
    assert error == "" and [item["kind"] for item in picked] == ["channel", "user"]
    kept = normalize({"windows": picked, "window_mode": "white"})
    assert [item["kind"] for item in kept["windows"]] == ["channel", "user"]
    assert allow(kept, chat_id=7, username="", bots=set())
    assert not allow(kept, chat_id=8, username="", bots=set())
    assert allow(kept, chat_id=-1002, username="", bots=set())
    saved = normalize({
        "window_mode": "white",
        "windows": [
            {"id": "abcd1234", "kind": "group", "chat_id": 1, "title": "甲"},
            {"id": "abcd1235", "kind": "group", "chat_id": 2, "title": "乙"},
        ],
    })
    assert [item["chat_id"] for item in saved["windows"]] == [1, 2]
    assert saved["window_lists"]["black"] == [] and saved["window_lists"]["off"] == []
    black = set_axis(saved, "window", "black")
    assert black["windows"] == [] and len(black["window_lists"]["white"]) == 2
    black, error = append_entry(black, "window", {"kind": "group", "chat_id": 3, "title": "丙"})
    assert error == "" and [item["chat_id"] for item in black["windows"]] == [3]
    assert [item["chat_id"] for item in black["window_lists"]["white"]] == [1, 2]
    assert not allow(black, chat_id=3, username="", bots=set())
    assert allow(black, chat_id=1, username="", bots=set())
    back = set_axis(black, "window", "white")
    assert [item["chat_id"] for item in back["windows"]] == [1, 2]
    assert allow(back, chat_id=1, username="", bots=set())
    assert not allow(back, chat_id=3, username="", bots=set())
    off = set_axis(back, "window", "off")
    assert off["windows"] == []
    assert allow(off, chat_id=1, username="", bots=set())
    off, error = append_entry(off, "window", {"kind": "group", "chat_id": 9, "title": "丁"})
    assert error == "" and [item["chat_id"] for item in off["windows"]] == [9]
    assert allow(off, chat_id=9, username="", bots=set())
    assert [item["chat_id"] for item in set_axis(off, "window", "white")["windows"]] == [1, 2]
    assert [item["chat_id"] for item in set_axis(off, "window", "black")["windows"]] == [3]
    cleared = clear_axis(back, "window")
    assert cleared["windows"] == [] and [item["chat_id"] for item in set_axis(cleared, "window", "black")["windows"]] == [3]
    hot, error = assign_hot(default_policy(), windows=[{"chat_id": -1002, "title": "甲频道", "kind": "channel"}])
    assert error == "" and hot["hot_windows"][0]["kind"] == "channel"
    loose, error = shield_picks([], [{"kind": "group", "chat_id": -9, "title": "外来", "username": "wai"}])
    assert error == "" and loose[0]["title"] == "外来" and loose[0]["username"] == "wai" and loose[0]["chat_id"] == -9
    assert shield_picks(dialogs, [])[1] == "主体：无法识别"
    assert len(shield_picks(dialogs, [{"kind": "chat", "chat_id": -1001}] * 51)[0]) == 1
    full = {"windows": [{"id": f"{i:08x}", "kind": "chat", "chat_id": -2000 - i, "title": str(i)} for i in range(50)]}
    more, error = append_entry(full, "window", {"kind": "chat", "chat_id": -9, "title": "新"})
    assert error == "" and len(more["windows"]) == 51
    assert shield_picks(dialogs, [{"kind": "bot", "user_id": 42}, {"kind": "bot", "user_id": 42}])[0][0]["username"] == "jiabot"
    added, error = append_entry(default_policy(), "window", parse_target("@ab"))
    assert error == "" and added["windows"][0]["username"] == "ab"
    again, error = append_entry(added, "window", parse_target("@AB"))
    assert error == "主体：已经在名单里"
    bot_added, error = append_entry(default_policy(), "bot", parse_target("https://t.me/c/5/1"))
    assert error == "主体：这是窗口，不是机器人" and bot_added["bots"] == []
    assert parse_span("1000ms-5000ms") == (1, 5)
    assert parse_span("3000ms-3000ms") == (3, 3)
    assert parse_span("1-5") == (1, 5)
    assert parse_span("2到5秒") == (2, 5)
    assert parse_span("0") == (0, 0)
    assert parse_span("abc") is None
    assert span_error(5, 2) == "主体：前面不能大于后面"
    assert span_error(0, 0) == "主体：延迟至少 1 秒"
    assert pick_delay(3, 3) == 3
    assert pick_delay(0, 0) == 0
    assert 1 <= pick_delay(1, 5) <= 5
    noon = datetime(2026, 10, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    night = datetime(2026, 10, 17, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    late = datetime(2026, 10, 17, 1, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    edge = datetime(2026, 10, 17, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert hours_open({"open_at": "00:00", "close_at": "00:00"}, night)
    assert hours_open({"open_at": "10:00", "close_at": "02:00"}, noon)
    assert hours_open({"open_at": "10:00", "close_at": "02:00"}, late)
    assert not hours_open({"open_at": "10:00", "close_at": "02:00"}, night)
    assert not hours_open({"open_at": "10:00", "close_at": "02:00"}, edge)
    assert hours_open({"open_at": "09:00", "close_at": "18:00"}, datetime(2026, 10, 17, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert not hours_open({"open_at": "09:00", "close_at": "18:00"}, datetime(2026, 10, 17, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert hours_open({"open_at": "00:00", "close_at": "00:00", "hot_open": "10:00", "hot_close": "11:00"}, night)
    mid = datetime(2026, 10, 17, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert hours_open({"hot_open": "10:00", "hot_close": "18:00"}, mid, start_key="hot_open", end_key="hot_close")
    assert not hours_open({"hot_open": "10:00", "hot_close": "18:00"}, night, start_key="hot_open", end_key="hot_close")
    assert hours_open({"hot_open": "22:00", "hot_close": "02:00"}, late, start_key="hot_open", end_key="hot_close")
    saved, error = set_hours(default_policy(), "10:00:00", "2:00")
    assert error == "" and saved["open_at"] == "10:00" and saved["close_at"] == "02:00"
    assert set_hours(default_policy(), "25:00", "01:00")[1] == "主体：时间写成 10:00 这样"
    old = normalize({"mode_delays": {"direct": {"low": 2, "high": 5}}, "delays": [{"wallet": "kpay", "low": 1, "high": 2}]})
    assert old["delay_unit"] == "s"
    assert old["mode_delays"]["direct"] == {"low": 2, "high": 5}
    assert delay_for(old, "kpay") == (1, 2)
    assert normalize(old)["mode_delays"]["direct"]["high"] == 5
    migrated = normalize({
        "delay_unit": "ms",
        "mode_delays": {"direct": {"low": 2000, "high": 5000}},
        "delays": [{"wallet": "kpay", "low": 1000, "high": 2000}],
        "hot_low": 30,
        "hot_high": 90,
        "hot_drop_ms": 3000,
    })
    assert migrated["mode_delays"]["direct"] == {"low": 2, "high": 5}
    assert delay_for(migrated, "kpay") == (1, 2)
    assert migrated["hot_low"] == 1800 and migrated["hot_high"] == 5400 and migrated["hot_drop"] == 3
    assert normalize(migrated)["hot_drop"] == 3
    assert parse_wallet("@OKPAY") == ("okpay", "")
    assert parse_wallet("https://t.me/kpay/3") == ("kpay", "")
    assert parse_wallet("@ab") == ("ab", "")
    assert parse_wallet("@adpay") == ("adpay", "")
    assert parse_wallet("nope nope")[0] == ""
    delayed, error = set_delay(default_policy(), "kpay", 2, 5)
    assert error == "" and delay_for(delayed, "KPAY") == (2, 5)
    custom, error = set_delay(default_policy(), "NewQB", 1, 2)
    assert error == "" and delay_for(custom, "newqb") == (1, 2)
    kept, error = set_delay(default_policy(), "adpay", 1, 2)
    assert error == "" and delay_for(kept, "ADPAY") == (1, 2)
    assert delay_for(clear_delay(delayed, "kpay"), "kpay") is None
    assert normalize({"burn": 1})["burns"] == {"captcha": False, "phrase": True}
    assert normalize({"burns": {"captcha": True, "phrase": 0}})["burns"] == {"captcha": False, "phrase": True}
    assert normalize({"burns": {"captcha": False, "phrase": False}})["burns"]["phrase"] is False
    assert below_amount({"coins": {"CNY": False}}, "10 CNY", "1/2")
    assert not below_amount({"coins": {"CNY": False}}, "100 USDT", "10/10")
    assert below_amount({}, "10 USDT", "1/2")
    assert below_amount({}, "58 USDT", "1/1")
    assert not below_amount({}, "50 USDT", "10/10")
    assert default_policy()["min_counts"] == {"USDT": "10", "CNY": "10", "TRX": "10", "TON": "10"}
    assert default_policy()["min_totals"] == {}
    assert default_policy()["min_eaches"]["CNY"] == "18"
    assert default_policy()["min_eaches"]["TRX"] == "10"
    sized = set_floor(default_policy(), "count", "USDT", "3")
    assert sized["min_counts"]["USDT"] == "3"
    assert below_amount(sized, "58 USDT", "1/2")
    assert not below_amount(sized, "58 USDT", "1/3")
    assert not below_amount(sized, "58 USDT", "")
    assert floor_preview("USDT", "5", "58", "5") == "少于 5 个，或少于 58，或人均少于 5，不抢。"
    assert below_amount({}, "58 USDT", "12/12")
    assert below_amount({}, "87 CNY", "1/1")
    assert below_amount({}, "88 CNY", "1/1")
    assert not below_amount({}, "180 CNY", "10/10")
    assert below_amount({}, "100 CNY", "11/11")
    assert below_amount({}, "87 TRX", "1/1")
    assert below_amount({}, "88 TRX", "1/1")
    assert not below_amount({}, "100 TRX", "10/10")
    assert below_amount({}, "57 TON", "1/1")
    assert below_amount({}, "58 TON", "1/1")
    assert not below_amount({}, "50 TON", "10/10")
    assert not below_amount({}, "100000 KKCOIN", "1/1")
    assert below_amount({}, "100000 KKCOIN", "1/2")
    assert below_amount({}, "99999 JIBA", "1/1")
    assert below_amount({"min_totals": {}, "min_eaches": {}}, "10 USDT", "1/1")
    blank = {"amount_custom": True, "min_totals": {}, "min_eaches": {}}
    assert not below_amount(blank, "1 USDT", "1/1")
    assert below_amount(blank, "1 AIDU", "1/2")
    assert not below_amount({**blank, "count_over": False}, "1 AIDU", "1/2")
    assert below_amount({"min_total": "20"}, "10 USDT", "2/5")
    assert below_amount({"min_total": "20"}, "10 CNY", "1/2")
    assert below_amount({"min_totals": {"USDT": "20"}}, "10 USDT", "1/2")
    assert not below_amount({"min_totals": {"CNY": "20"}}, "10 USDT", "1/2")
    assert below_amount({"min_each": "3"}, "10 USDT", "2/5")
    assert below_amount({"min_eaches": {"CNY": "3"}}, "10 CNY", "2/5")
    assert not below_amount({"min_eaches": {"CNY": "3"}}, "10 USDT", "2/5")
    assert not below_amount({"min_each": "1"}, "10 USDT", "2/5")
    assert not below_amount({"min_total": "5"}, "", "")
    assert not below_amount({"min_total": "20"}, "10", "")
    updated = set_floor(default_policy(), "total", "CNY", "8")
    assert updated["min_totals"]["CNY"] == "8" and "USDT" not in updated["min_totals"]
    assert updated["amount_custom"] is True
    cleared = set_floor({"min_totals": {"CNY": "8"}}, "total", "CNY", "0")
    assert cleared["min_totals"] == {} and cleared["amount_custom"] is True
    opened = set_floor(default_policy(), "total", "USDT", "0")
    opened = set_floor(opened, "each", "USDT", "0")
    assert "USDT" not in opened["min_totals"] and "USDT" not in opened["min_eaches"]
    assert below_amount(opened, "10 USDT", "1/1")
    assert not below_amount(opened, "50 USDT", "10/10")
    assert below_amount(opened, "10 CNY", "1/1")
    assert parse_floors("5 10 2") == ("5", "10", "2", "")
    assert parse_floors("0 0 0") == ("", "", "", "")
    assert parse_floors("10 2")[3] == "主体：请写个数、金额和人均，例如 5 10 2。"
    assert parse_floors("5.5 10 2")[3] == "主体：个数要写整数。"
    assert floor_preview("USDT", "", "10", "2") == "少于 10，或人均少于 2，不抢。"
    assert floor_preview("AIDU", "", "", "100000") == "人均少于 100000，不抢。"
    assert floor_preview("USDT", "", "", "") == ""
    assert parse_floor("0.5") == ("0.5", "")
    assert parse_floor("0") == ("", "")
    assert balance_hidden({"show_min": {"USDT": "10"}}, "USDT", Decimal("9"))
    assert not balance_hidden({"show_min": {"USDT": "10"}}, "USDT", Decimal("10"))
    assert balance_hidden({}, "USDT", Decimal("0"))
    assert not balance_hidden({"hide_zero": False}, "USDT", Decimal("0"))
    assert set_show_min(default_policy(), "USDT", "10")["show_min"]["USDT"] == "10"
    assert set_show_min(default_policy(), "USDT", "0")["show_min"] == {}
    quiet = set_link(default_policy(), [7], False)
    assert not account_linked(quiet, 7) and account_paused(quiet, 7)
    opened = set_running(quiet, [7], True)
    assert account_linked(opened, 7) and not account_paused(opened, 7)
    joined = set_join(default_policy(), [9], True)
    assert account_joins(joined, 9) and account_linked(joined, 9)
    assert not account_joins(set_join(joined, [9], False), 9)
    merged = normalize({"hot_windows": [
        {"id": "abcd1234", "chat_id": 1, "title": "test红包"},
        {"id": "abcd1235", "chat_id": 2, "title": "test红包"},
    ]})
    assert len(merged["hot_windows"]) == 1
    many, error = assign_hot(default_policy(), windows=[{"chat_id": i + 1, "title": str(i)} for i in range(51)])
    assert error == "" and len(many["hot_windows"]) == 51
    delayed_many = default_policy()
    for i in range(51):
        delayed_many, error = set_delay(delayed_many, f"w{i:02d}bot", 1, 2)
        assert error == ""
    assert len(delayed_many["delays"]) == 51
    assert account_paused(toggle_account(default_policy(), 7), 7)
    assert not account_paused(toggle_account(toggle_account(default_policy(), 7), 7), 7)
    assert below_amount({"unknown_coin": False}, "10", "")
    assert not below_amount({}, "", "")
    filled = default_policy()
    filled["windows"] = [{"id": "abcd1234", "username": "a", "title": "@a"}]
    assert clear_axis(filled, "window")["windows"] == []
    delayed, error = set_mode_delay(default_policy(), "direct", 2, 5)
    assert error == "" and mode_delay_for(delayed, "direct") == (2, 5)
    immediate, error = set_mode_delay(delayed, "direct", 0, 0)
    assert error == "" and mode_delay_for(immediate, "direct") is None
    tied, error = set_mode_delay(default_policy(), "webapp", 3, 3)
    assert error == "" and mode_delay_for(tied, "cloudflare") == (3, 3)
    assert normalize({})["hot_low"] == 1800 and normalize({})["hot_high"] == 5400
    clamped, error = assign_hot(default_policy(), low=1, high=5, lines=["在", "https://t.me/a"])
    assert error.startswith("主体：")
    kept, error = assign_hot(default_policy(), low=1, high=400, lines=["在呢", "在呢", ""])
    assert error == "" and kept["hot_low"] == 60 and kept["hot_high"] == 400
    zeroed, error = assign_hot(default_policy(), low=0, high=0)
    assert error == "" and zeroed["hot_low"] == 60 and zeroed["hot_high"] == 60
    capped, error = assign_hot(default_policy(), low=10, high=100000)
    assert error == "" and capped["hot_low"] == 60 and capped["hot_high"] == 86400
    assert kept["hot_lines"] == ["在呢"] and kept["hot_source"] == "lines"
    assert normalize({})["hot_packs"] == list(_PACK_IDS)
    assert normalize({})["hot_drop"] == 0
    assert normalize({})["hot_open"] == "00:00" and normalize({})["hot_close"] == "00:00" and normalize({})["hot_cap"] == 0
    shaped, error = assign_hot(default_policy(), start="10:00:00", stop="2:00", cap=20)
    assert error == "" and shaped["hot_open"] == "10:00" and shaped["hot_close"] == "02:00" and shaped["hot_cap"] == 20
    assert assign_hot(default_policy(), start="25:00", stop="01:00")[1] == "主体：时间写成 10:00 这样"
    assert assign_hot(default_policy(), cap=501)[1] == "主体：每天上限请写在 0 到 500"
    assert assign_hot(default_policy(), cap=0)[0]["hot_cap"] == 0
    assert normalize({})["hot_off"] == []
    muted, error = assign_hot(default_policy(), muted=[7, 7, "8"])
    assert error == "" and muted["hot_off"] == [7, 8]
    seated, error = set_hot_seat({"hot_windows": [{"chat_id": 3, "title": "甲"}]}, 3, 7, False)
    assert error == "" and seated["hot_windows"][0]["off"] == [7]
    back, error = set_hot_seat(seated, 3, 7, True)
    assert error == "" and "off" not in back["hot_windows"][0]
    assert set_hot_seat(default_policy(), 3, 7, False)[1] == "主体：没有这个窗口"
    kept_off, error = assign_hot(seated, windows=seated["hot_windows"])
    assert error == "" and kept_off["hot_windows"][0]["off"] == [7]
    marked, error = set_hot_sign({"hot_windows": [{"chat_id": 3, "title": "甲"}]}, 3, True)
    assert error == "" and marked["hot_windows"][0]["sign"] is True
    cleared, error = set_hot_sign(marked, 3, False)
    assert error == "" and "sign" not in cleared["hot_windows"][0]
    assert set_hot_sign(default_policy(), 3, True)[1] == "主体：没有这个窗口"
    kept_sign, error = assign_hot(marked, windows=marked["hot_windows"])
    assert error == "" and kept_sign["hot_windows"][0].get("sign") is True
    assert normalize({"hot_windows": [{"chat_id": 3, "title": "甲", "sign": False}]})["hot_windows"][0].get("sign") is not True
    shanghai = ZoneInfo("Asia/Shanghai")
    assert not sign_hours(datetime(2026, 10, 18, 0, 59, tzinfo=shanghai))
    assert sign_hours(datetime(2026, 10, 18, 1, 0, tzinfo=shanghai))
    assert sign_hours(datetime(2026, 10, 18, 5, 59, tzinfo=shanghai))
    assert not sign_hours(datetime(2026, 10, 18, 6, 0, tzinfo=shanghai))
    legacy = normalize({"hot_on": True, "hot_windows": [{"chat_id": 3, "title": "甲"}]})
    assert legacy["hot_windows"][0]["speak"] is True
    quiet = normalize({"hot_windows": [{"chat_id": 3, "title": "甲"}]})
    assert quiet["hot_windows"][0]["speak"] is False
    closed, error = set_hot_speak(legacy, 3, False)
    assert error == "" and closed["hot_windows"][0]["speak"] is False
    assert normalize(closed)["hot_windows"][0]["speak"] is False
    opened, error = set_hot_speak(closed, 3, True)
    assert error == "" and opened["hot_windows"][0]["speak"] is True
    kept_speak, error = assign_hot(opened, windows=opened["hot_windows"])
    assert error == "" and kept_speak["hot_windows"][0]["speak"] is True
    fresh, error = assign_hot(default_policy(), windows=[{"chat_id": 9, "title": "乙"}])
    assert error == "" and fresh["hot_windows"][0]["speak"] is False
    added, error = assign_hot(opened, windows=[*opened["hot_windows"], {"chat_id": 9, "title": "乙"}])
    assert error == "" and [item["speak"] for item in added["hot_windows"]] == [True, False]
    assert set_hot_speak(default_policy(), 3, True)[1] == "主体：没有这个窗口"
    assert normalize({"hot_cap": 9000})["hot_cap"] == 500
    assert normalize({"hot_packs": ["xiehouyu", "joke", "hello"]})["hot_packs"] == list(_PACK_IDS)
    assert normalize({"hot_packs": ["hello"]})["hot_packs"] == ["hello"]
    assert normalize({"hot_packs": ["hello"], "hot_rev": 2})["hot_packs"] == ["hello"]
    packed, error = assign_hot(default_policy(), drop=3, packs=["hello", "nope", "hello"])
    assert error == "" and packed["hot_drop"] == 3 and packed["hot_packs"] == ["hello"]
    assert assign_hot(default_policy(), drop=9000)[0]["hot_drop"] == 600
    from meiguan.hot_copy import lines_for, pick_line
    hello = lines_for(["hello"])
    assert hello and all(5 <= len(item) <= 60 for item in hello)
    for name in _PACK_IDS:
        rows = lines_for([name])
        assert rows, name
        assert all(5 <= len(item) <= 60 and "答案：" not in item and "答案:" not in item for item in rows)
    assert "做砖的坯子、插刀的鞘子——框框套套" in lines_for(["xiehouyu"])
    assert "阿斗当皇帝——软弱无能" in lines_for(["xiehouyu"])
    assert lines_for(["nope"]) == []
    assert pick_line(["hello", "pun"], set(hello)) in set(lines_for(["pun"]))
    assert pick_line(["nope"], set()) == ""
    assert normalize({"hot_packs": ["hello", "custom"], "hot_rev": 2})["hot_packs"] == ["hello", "custom"]
    custom, error = assign_hot(default_policy(), packs=["custom", "nope", "custom"], lines=["你好呀", "你好呀", ""])
    assert error == "" and custom["hot_packs"] == ["custom"] and custom["hot_lines"] == ["你好呀"]
    assert assign_hot(default_policy(), lines=["https://t.me/a"])[1] == "主体：句子里不要带链接"
    assert assign_hot(default_policy(), lines=[f"句子{i:02d}呀" for i in range(51)])[1] == "主体：最多 50 句"
    assert pick_line(["hello"], set(hello), ["只此一句"]) == "只此一句"
    assert lines_for(["nope"], ["只此一句"]) == ["只此一句"]
    assert pick_line(["nope"], set(), None) == ""
    running = set_running(default_policy(), [7, 8], False)
    assert account_paused(running, 7) and account_paused(running, 8)
    assert not account_paused(set_running(running, [7], True), 7)
    watched = set_watch(default_policy(), [7], True)
    assert account_watches(watched, 7) and not account_watches(set_watch(watched, [7], False), 7)
    assert pay_bucket("fllqb", "webapp") == "fll"
    assert pay_bucket("yypaybot", "cloudflare") == "cf"
    assert pay_open({"web_pay": {"fll": False}}, "fllqb", "webapp") is False
    assert pay_open({}, "kkpay", "direct") is True
    fine = {"amount_custom": True, "count_over": True}
    assert below_amount(fine, "5 USDT", "1/8")
    assert not below_amount({"amount_custom": True, "count_over": False}, "5 USDT", "1/8")
    whole = {"amount_custom": True, "count_over": True}
    assert below_amount(whole, "1 USDT", "0/100")
    assert below_amount(whole, "1.11 USDT", "0/100")
    assert below_amount(whole, "1.11u", "0/100")
    assert below_amount(whole, "50 USDT", "0/100")
    assert not below_amount(whole, "100 USDT", "0/100")
    assert not below_amount(whole, "100.9 USDT", "0/100")
    assert not below_amount(fine, "5 USDT", "")
    milli, error = set_mode_delay(default_policy(), "direct", 0.3, 0.5)
    assert error == "" and mode_delay_for(milli, "direct") == (0.3, 0.5)
    said, error = set_phrase_say(default_policy(), 0.5, 0.5)
    assert error == "" and phrase_say_for(said) == (0.5, 0.5)
    eachs = default_policy()["min_eaches"]
    coin_each = {code: eachs[code] for code in COINS if code.endswith("COIN")}
    assert coin_each and set(coin_each.values()) == {"100000"}
    assert below_amount({}, "1 DDCOIN", "1/1")
    assert not below_amount({}, "100000 JJCOIN", "1/1")
    kept = normalize({"amount_custom": True, "min_eaches": {"USDT": "1"}, "coin_rev": 1})
    assert "DDCOIN" not in kept["min_eaches"]
    assert "GRAM" not in default_policy()["coins"] and "USD" not in default_policy()["coins"]
    assert below_amount({}, "5 GRAM", "1/1")
    assert not below_amount({}, "50 GRAM", "10/10")
    assert below_amount({}, "5 USD", "1/1")
    assert not below_amount({}, "50 USD", "10/10")
    assert account_brief(watched, 7)["state"] == "只通知，不抢"
    sample = "a" * 32
    saved, error = set_pay_keys({}, {"fll": sample, "cf": "短"})
    assert error == "token 无效" and saved["pay_keys"] == {}
    saved, error = set_pay_keys({"web_pay": {"fll": True, "cf": True}}, {"fll": f"  {sample}  "})
    assert error == "" and saved["pay_keys"] == {"fll": sample} and saved["web_pay"]["fll"] is False
    saved["web_pay"]["fll"] = True
    replaced, error = set_pay_keys(saved, {"fll": "b" * 32})
    assert error == "" and replaced["web_pay"]["fll"] is True
    shown = present_policy(saved)
    assert "pay_keys" not in shown and shown["pay_ready"] == {"fll": True, "cf": False}
    assert "pay_keys" in saved
    cleared, error = set_pay_keys(replaced, {"fll": ""})
    assert error == "" and cleared["pay_keys"] == {} and cleared["web_pay"]["fll"] is False
    assert pay_open({"web_pay": {"fll": True}}, "fllqb", "webapp") is False
    assert pay_open({"web_pay": {"fll": True}, "pay_keys": {"fll": sample}}, "fllqb", "webapp") is True
    assert normalize({"pay_keys": {"fll": sample, "cf": "nope", "extra": sample}})["pay_keys"] == {"fll": sample}


if __name__ == "__main__":
    _check()
