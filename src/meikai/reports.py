"""投稿审核和系统屏蔽。"""

import html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata

from meikai.policy import parse_target, wallet_key

log = logging.getLogger("meikai.reports")


def _author_id() -> int:
    raw = (os.environ.get("AUTHOR_ID") or "").strip()
    if raw.isdigit():
        return int(raw)
    return 0


# 审核人。只从主体环境变量读取。
AUTHOR_ID = _author_id()
KIND_LABEL = {"user": "用户", "bot": "机器人", "group": "群组", "channel": "频道"}
_CAP = 500


def kind_label(kind: str) -> str:
    return KIND_LABEL.get(kind, "")


def kind_title(kind: str) -> str:
    return kind_label(kind)


_PATH: str | None = None
_ids: set[str] = set()
_by_name: dict[str, str] = {}
_watch_ids: set[str] = set()
_blocks_ready = False
_brands: dict[str, dict] = {}
_db_ready: set[str] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS system_block (
    user_id INTEGER PRIMARY KEY,
    username TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    report_id INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS wallet_brand (
    nick TEXT PRIMARY KEY,
    names TEXT NOT NULL DEFAULT '[]',
    ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS report_name_id ON report(username, id DESC);
CREATE INDEX IF NOT EXISTS report_uid_id ON report(user_id, id DESC);
CREATE INDEX IF NOT EXISTS report_owner_time ON report(owner_id, created_at);
CREATE INDEX IF NOT EXISTS report_owner_status ON report(owner_id, status);
"""


def _connect() -> sqlite3.Connection:
    if _PATH is None:
        from meikai.store import DB

        DB.parent.mkdir(parents=True, exist_ok=True)
        target = str(DB)
    else:
        target = _PATH
    conn = sqlite3.connect(target, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if target not in _db_ready:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _db_ready.add(target)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _primary(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})") if row[5]]


def _migrate(conn: sqlite3.Connection) -> None:
    report = _columns(conn, "report")
    if report and "kind" not in report:
        conn.execute("ALTER TABLE report ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    if report and "note" not in report:
        conn.execute("ALTER TABLE report ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    block = _columns(conn, "system_block")
    if block and "kind" not in block:
        conn.execute("ALTER TABLE system_block ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
    if block and _primary(conn, "system_block") != ["user_id"]:
        _blocks_by_id(conn)
    conn.execute("DROP INDEX IF EXISTS block_user")
    conn.execute("DROP INDEX IF EXISTS report_username")
    conn.execute("DROP INDEX IF EXISTS report_user")


def _face_name(username, user_id: int = 0) -> str:
    name = _key(username)
    if not name or name.lstrip("-").isdigit() or name == _key(user_id):
        return ""
    return name


def _blocks_by_id(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE system_block_id (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                report_id INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT ''
            )
            """
        )
        rows = conn.execute(
            "SELECT username, user_id, title, report_id, kind FROM system_block"
        ).fetchall()
        for row in rows:
            number = int(row["user_id"] or 0)
            raw_name = str(row["username"] or "")
            if number == 0 and raw_name.lstrip("-").isdigit():
                number = int(raw_name)
            if number == 0:
                log.warning("主体：屏蔽没有 id，已跳过 %s", raw_name[:32])
                continue
            conn.execute(
                """
                INSERT INTO system_block_id (user_id, username, title, report_id, kind)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE username END,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE title END,
                    report_id = CASE WHEN excluded.report_id != 0 THEN excluded.report_id ELSE report_id END,
                    kind = CASE WHEN excluded.kind != '' THEN excluded.kind ELSE kind END
                """,
                (
                    number,
                    _face_name(raw_name, number),
                    str(row["title"] or ""),
                    int(row["report_id"] or 0),
                    str(row["kind"] or ""),
                ),
            )
        conn.execute("DROP TABLE system_block")
        conn.execute("ALTER TABLE system_block_id RENAME TO system_block")
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _key(value) -> str:
    text = str(value or "").strip().lstrip("@")
    if not text:
        return ""
    sign = ""
    bare = text
    if text[0] == "-" and len(text) > 1:
        sign, bare = "-", text[1:]
    if bare.isdigit() and 1 <= len(bare) <= 20:
        return sign + bare
    if sign or len(text) > 32:
        return ""
    return wallet_key(text)


def _probe(text: str):
    raw = " ".join((text or "").split())
    if raw.startswith("投稿"):
        raw = raw.removeprefix("投稿").strip()
    if not raw or len(raw) > 200:
        return None
    lower = raw.lower()
    if raw.startswith("@") or "t.me/" in lower or "telegram.me/" in lower or raw.lstrip("-").isdigit():
        return parse_target(raw)
    return parse_target(f"@{raw}")


def typed_name(text: str, *, bare: bool) -> dict | None:
    raw = " ".join((text or "").split())
    if not raw or " " in raw:
        return None
    lower = raw.lower()
    explicit = raw.startswith("@") or "t.me/" in lower or "telegram.me/" in lower
    if not bare and not explicit:
        return None
    parsed = parse_subject("user", raw)
    if not parsed or not parsed.get("username"):
        return None
    return parsed


def parse_subject(kind: str, text: str) -> dict | None:
    raw = " ".join((text or "").split())
    if kind in ("user", "bot") and raw.isdigit() and 5 <= len(raw) <= 15:
        return {"user_id": int(raw)}
    parsed = _probe(raw)
    if not parsed:
        return None
    if kind in ("user", "bot"):
        if parsed.get("username") and parsed.get("chat_id") is None:
            return {"username": str(parsed["username"])}
        return None
    if parsed.get("chat_id") is not None and not parsed.get("username"):
        return {"chat_id": int(parsed["chat_id"])}
    if parsed.get("username") and parsed.get("chat_id") is None:
        return {"username": str(parsed["username"])}
    return None


def parse_report(text: str) -> str:
    parsed = _probe(text)
    if not parsed:
        return ""
    if parsed.get("username") and parsed.get("chat_id") is None:
        return _key(parsed.get("username"))
    if parsed.get("chat_id") is not None:
        return str(int(parsed["chat_id"]))
    return ""


def _id_key(value) -> str:
    key = _key(value)
    if key and key.lstrip("-").isdigit():
        return key
    return ""


def apply_blocks(items) -> list[str]:
    nxt = set()
    for item in items or []:
        ident = _id_key(item)
        if not ident:
            continue
        nxt.add(ident)
        if len(nxt) >= 1000:
            break
    _ids.clear()
    _ids.update(nxt)
    return sorted(_ids)


def apply_alias(items) -> None:
    nxt = {}
    pairs = items.items() if isinstance(items, dict) else []
    for name, ident in pairs:
        key = _key(name)
        number = _id_key(ident)
        if not key or not number or key.lstrip("-").isdigit():
            continue
        nxt[key] = number
        if len(nxt) >= 1000:
            break
    _by_name.clear()
    _by_name.update(nxt)


_AT_NAME = re.compile(r"[@＠]([A-Za-z][A-Za-z0-9_]{3,31})")


def account_names(entity) -> list[str]:
    found = []
    seen = set()

    def add(value) -> None:
        name = str(value or "").strip().lstrip("@")
        key = name.lower()
        if not name or key in seen:
            return
        seen.add(key)
        found.append(name)

    add(getattr(entity, "username", None))
    for item in getattr(entity, "usernames", None) or []:
        if not getattr(item, "active", False):
            continue
        add(getattr(item, "username", None))
    return found


def _owned(username) -> set[str]:
    values = [username] if isinstance(username, str) else list(username or [])
    found = set()
    for value in values:
        name = str(value or "").strip().lstrip("@").lower()
        if name:
            found.add(name)
    return found


def poseur(display: str, username="") -> bool:
    """机器人显示名里写了用户名，但这个号的用户名里没有它。"""
    owned = _owned(username)
    raw = str(display or "")
    mentions = {item.lower() for item in _AT_NAME.findall(raw)}
    if mentions:
        return not mentions <= owned
    latin = "".join(ch for ch in raw.lower() if "a" <= ch <= "z" or ch.isdigit() or ch == "_")
    if len(latin) < 4 or not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", latin):
        return False
    for actual in owned:
        if len(actual) >= 4 and (actual == latin or latin.startswith(actual)):
            return False
    return True


def _latin(display: str) -> str:
    return "".join(ch for ch in str(display or "").lower() if "a" <= ch <= "z" or ch.isdigit() or ch == "_")


def _shaped(token: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{3,31}", token))


def _title_key(display: str) -> str:
    title = "".join(_AT_NAME.sub(" ", str(display or "")).split()).lower()
    if any("\u4e00" <= ch <= "\u9fff" for ch in title) and 2 <= len(title) <= 32:
        return title
    return ""


def brand_keys(display: str) -> set[str]:
    keys = set()
    raw = str(display or "")
    latin = _latin(raw)
    if _shaped(latin):
        keys.add(latin)
    for mention in _AT_NAME.findall(raw):
        keys.add(mention.lower())
    title = _title_key(raw)
    if title:
        keys.add(title)
    return keys


def seal_text(description: str) -> bool:
    text = str(description or "").lower()
    return "hold" in text or "major" in text


def learn_brand(display: str, usernames, user_id: int = 0) -> list[dict]:
    owned = _owned(usernames)
    try:
        number = int(user_id or 0)
    except (TypeError, ValueError):
        number = 0
    keys = set(brand_keys(display))
    latin = _latin(display)
    for name in owned:
        if len(name) >= 4 and latin.startswith(name):
            keys.add(name)
    changed = []
    for key in keys:
        slot = _brands.setdefault(key, {"names": set(), "ids": set()})
        before_names = set(slot["names"])
        before_ids = set(slot["ids"])
        slot["names"].update(owned)
        if number:
            slot["ids"].add(number)
        if slot["names"] == before_names and slot["ids"] == before_ids:
            continue
        changed.append({"nick": key, "names": sorted(slot["names"]), "ids": sorted(slot["ids"])})
    return changed


def apply_brands(items) -> bool:
    changed = False
    for item in items or []:
        if not isinstance(item, dict):
            continue
        nick = str(item.get("nick") or "").strip().lower()
        if not nick or len(nick) > 32:
            continue
        names = set()
        ids = set()
        for name in item.get("names") or []:
            key = str(name or "").strip().lstrip("@").lower()
            if key:
                names.add(key)
        for raw in item.get("ids") or []:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number:
                ids.add(number)
        if not names and not ids:
            continue
        slot = _brands.get(nick)
        if slot is None:
            _brands[nick] = {"names": names, "ids": ids}
            changed = True
            continue
        if names <= slot["names"] and ids <= slot["ids"]:
            continue
        slot["names"].update(names)
        slot["ids"].update(ids)
        changed = True
    return changed


def dump_brands() -> list[dict]:
    return [
        {"nick": key, "names": sorted(slot["names"]), "ids": sorted(slot["ids"])}
        for key, slot in sorted(_brands.items())
    ]


def _claimed(display: str):
    keys = brand_keys(display)
    latin = _latin(display)
    title = _title_key(display)
    found = []
    for brand, slot in _brands.items():
        if brand in keys:
            claimed = True
        elif _shaped(brand) and latin.startswith(brand):
            claimed = True
        elif title and not _shaped(brand) and (title == brand or title.startswith(brand)):
            claimed = True
        else:
            continue
        found.append((brand, slot))
    return found


def impostor(display: str, username="", user_id: int = 0) -> dict | None:
    """同名假钱包时返回正版昵称和用户名。正版或认不出则没有。"""
    if not _brands:
        return None
    owned = _owned(username)
    try:
        number = int(user_id or 0)
    except (TypeError, ValueError):
        number = 0
    matched = []
    for brand, slot in _claimed(display):
        if number and number in slot["ids"]:
            continue
        if owned & slot["names"]:
            continue
        matched.append((brand, slot))
    if not matched:
        return None
    shown = display.lower()
    latin = _latin(display)

    def rank(item) -> tuple:
        nick = item[0]
        chinese = any("\u4e00" <= ch <= "\u9fff" for ch in nick)
        visible = nick in shown or nick in latin
        return (0 if chinese else 1, 0 if visible else 1, len(nick))

    matched.sort(key=rank)
    names: set[str] = set()
    ids: set[int] = set()
    for _, slot in matched:
        names.update(slot["names"])
        ids.update(slot["ids"])
    return {"nick": matched[0][0], "names": sorted(names), "ids": sorted(ids)}


def brand_fake(display: str, username="", user_id: int = 0) -> bool:
    """已经记下的认证昵称，再出现同名但不是这个号，就是假钱包。"""
    return impostor(display, username, user_id) is not None


def ensure_brands() -> None:
    if not _brands:
        load_brands()


def release_known_fakes() -> list[int]:
    """能认出的假钱包不占系统屏蔽。只移机器人，正版和群频道不动。"""
    ensure_brands()
    if not _brands:
        return []
    conn = _connect()
    gone = []
    try:
        rows = conn.execute(
            "SELECT user_id, username, title, kind FROM system_block WHERE kind = 'bot'"
        ).fetchall()
        for row in rows:
            number = int(row["user_id"] or 0)
            if number and impostor(row["title"], row["username"], number):
                gone.append(number)
        if not gone:
            return []
        conn.execute("BEGIN IMMEDIATE")
        for number in gone:
            conn.execute("DELETE FROM system_block WHERE user_id = ?", (number,))
        conn.execute("COMMIT")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    refresh()
    log.info("主体：假钱包移出屏蔽 %s", " ".join(str(item) for item in gone))
    return gone


def fake_notice(row: dict, hit: dict) -> str:
    nick = html.escape(str(hit.get("nick") or "已认证钱包"))
    names = [
        f"@{html.escape(name)}"
        for name in hit.get("names") or []
        if name and not str(name).lstrip("-").isdigit()
    ]
    if names:
        real = f"正版是 {'、'.join(names)}。"
    else:
        ids = "、".join(str(item) for item in hit.get("ids") or [])
        real = f"正版编号是 {ids}。" if ids else "正版已经记下。"
    return "\n".join([
        "<b>已识别为假钱包</b>",
        f"仿冒「{nick}」。{real}",
        "之后所有人都不抢。",
        "",
        _quote(row, note=bool(str(row.get("note") or "").strip())),
    ])


def load_brands() -> list[dict]:
    try:
        conn = _connect()
    except OSError:
        return dump_brands()
    try:
        rows = conn.execute("SELECT nick, names, ids FROM wallet_brand").fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        try:
            names = json.loads(row["names"] or "[]")
            ids = json.loads(row["ids"] or "[]")
        except json.JSONDecodeError:
            continue
        items.append({"nick": row["nick"], "names": names, "ids": ids})
    apply_brands(items)
    return dump_brands()


def save_brands(items) -> list[dict]:
    apply_brands(items)
    conn = _connect()
    stored = []
    try:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            nick = str(item.get("nick") or "").strip().lower()
            slot = _brands.get(nick)
            if not nick or slot is None:
                continue
            names = json.dumps(sorted(slot["names"]))
            ids = json.dumps(sorted(slot["ids"]))
            row = conn.execute(
                "SELECT names, ids FROM wallet_brand WHERE nick = ?",
                (nick,),
            ).fetchone()
            if row is not None and row["names"] == names and row["ids"] == ids:
                continue
            conn.execute(
                """
                INSERT INTO wallet_brand (nick, names, ids) VALUES (?, ?, ?)
                ON CONFLICT(nick) DO UPDATE SET names = excluded.names, ids = excluded.ids
                """,
                (nick, names, ids),
            )
            stored.append({"nick": nick, "names": sorted(slot["names"]), "ids": sorted(slot["ids"])})
    finally:
        conn.close()
    return stored


def watched(name: str) -> bool:
    return "监控" in "".join(str(name or "").split())


def steward_text(about: str) -> bool:
    text = "".join(str(about or "").split())
    return "群管" in text or "智能" in text


def note_steward(username="", user_id: int = 0) -> None:
    fresh = False
    values = list(username) if isinstance(username, (list, tuple, set)) else [username]
    values.append(user_id)
    for value in values:
        key = _key(value)
        if not key or key in _watch_ids:
            continue
        _watch_ids.add(key)
        fresh = True
    if fresh:
        source = "节点" if os.getenv("NODE_ID") else "主体"
        log.info("%s：简介像群管，已屏蔽 %s", source, _key(user_id) or _key(username))


def note_watch(name: str = "", *, chat_id: int = 0, username: str = "", user_id: int = 0) -> bool:
    if not watched(name):
        return False
    fresh = False
    for value in (chat_id, username, user_id):
        if not value:
            continue
        key = _key(value)
        if not key or key in _watch_ids:
            continue
        _watch_ids.add(key)
        fresh = True
    if fresh:
        source = "节点" if os.getenv("NODE_ID") else "主体"
        log.info("%s：名字含监控，已屏蔽 %s", source, "".join(str(name).split())[:40])
    return True


def entity_watched(entity) -> bool:
    if entity is None:
        return False
    return any(watched(getattr(entity, attr, None)) for attr in ("title", "first_name", "last_name", "username"))


def _marked(value) -> bool:
    key = _key(value)
    return bool(key and key in _watch_ids)


def remember(username: str = "", user_id: int = 0) -> None:
    ident = _id_key(user_id)
    if not ident:
        return
    _ids.add(ident)
    name = _key(username)
    if name and not name.lstrip("-").isdigit():
        _by_name[name] = ident


def _listed(value) -> bool:
    ident = _id_key(value)
    if ident and ident in _ids:
        return True
    name = _key(value)
    if not name or name.lstrip("-").isdigit():
        return False
    linked = _by_name.get(name, "")
    return bool(linked and linked in _ids)


def hits(bots=None, chat_id: int = 0, username: str = "", title: str = "") -> bool:
    if note_watch(title, chat_id=chat_id, username=username) or note_watch(username, chat_id=chat_id, username=username):
        return True
    if _marked(chat_id) or _marked(username):
        return True
    for item in bots or ():
        if watched(item) or _marked(item):
            note_watch(str(item), username=str(item))
            return True
    if not _ids:
        return False
    if _listed(chat_id) or _listed(username):
        return True
    return any(_listed(item) for item in bots or ())


def refresh() -> list[str] | None:
    global _blocks_ready
    try:
        conn = _connect()
    except OSError:
        log.warning("主体：屏蔽名单读失败")
        return None
    try:
        rows = conn.execute("SELECT username, user_id FROM system_block").fetchall()
    finally:
        conn.close()
    items = []
    alias = {}
    for row in rows:
        ident = _id_key(row["user_id"])
        if not ident:
            continue
        items.append(ident)
        name = _face_name(row["username"], int(row["user_id"] or 0))
        if name:
            alias[name] = ident
    _blocks_ready = True
    apply_alias(alias)
    return apply_blocks(items)


def _item(row, status: str) -> dict:
    return {
        "id": int(row["id"]),
        "owner_id": int(row["owner_id"]),
        "username": str(row["username"]),
        "user_id": int(row["user_id"] or 0),
        "title": str(row["title"] or ""),
        "name": str(row["name"] or ""),
        "kind": str(row["kind"] or ""),
        "note": str(row["note"] or ""),
        "status": status,
    }


def face(row: dict) -> str:
    username = str(row.get("username") or "")
    if username and not username.lstrip("-").isdigit():
        return f"@{username}"
    try:
        number = int(row.get("user_id") or 0)
    except (TypeError, ValueError):
        number = 0
    if number:
        return str(number)
    return username


def _visible(value, limit: int = 200) -> str:
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch) not in {"Cf", "Cc"})
    return " ".join(text.split())[:limit]


def _anchor(label: str, href: str) -> str:
    text = html.escape(label or "")
    if not href:
        return text
    return f'<a href="{html.escape(href, quote=True)}">{text}</a>'


def _handle(value: str) -> str:
    name = _visible(value).lstrip("@")
    key = wallet_key(name)
    return key if key else ""


def _reporter(row: dict) -> str:
    raw = _visible(row.get("name"), 64)
    handle = ""
    nick = raw
    bits = raw.split(" ")
    if bits and bits[-1].startswith("@") and len(bits[-1]) > 1:
        handle = _handle(bits[-1])
        nick = " ".join(bits[:-1])
    elif raw.startswith("@"):
        handle = _handle(raw)
        nick = ""
    try:
        owner = int(row.get("owner_id") or 0)
    except (TypeError, ValueError):
        owner = 0
    href = f"https://t.me/{handle}" if handle else ""
    if not href and owner:
        href = f"tg://user?id={owner}"
    return _anchor(nick or (f"@{handle}" if handle else str(owner)), href)


def _subject(row: dict) -> str:
    username = _handle(row.get("username"))
    title = _visible(row.get("title"), 40)
    kind = str(row.get("kind") or "")
    try:
        number = int(row.get("user_id") or 0)
    except (TypeError, ValueError):
        number = 0
    if title and (title == str(number) or title.lstrip("-") == str(number).lstrip("-")):
        title = ""
    label = title or (f"@{username}" if username else "")
    if username:
        href = f"https://t.me/{username}"
    elif kind in ("user", "bot") and number > 0:
        href = f"tg://user?id={number}"
    else:
        href = ""
    if not label:
        if href:
            return _anchor("未命名", href)
        return "未命名"
    return _anchor(label, href)


def _number(row: dict) -> int:
    try:
        return int(row.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


def counts(owner_id: int) -> tuple[int, int]:
    if not owner_id:
        return 0, 0
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'yes' THEN 1 ELSE 0 END) AS passed
            FROM report WHERE owner_id = ?
            """,
            (int(owner_id),),
        ).fetchone()
    finally:
        conn.close()
    return int(row["total"] or 0), int(row["passed"] or 0)


def attach_counts(row) -> dict:
    item = dict(row)
    total, passed = counts(int(item.get("owner_id") or 0))
    item["reports"] = total
    item["passed"] = passed
    return item


def _record(row: dict) -> str:
    if "reports" not in row:
        return ""
    return f"记录：投稿 {int(row['reports'])} 次，通过 {int(row['passed'])} 次。"


def _quote(row: dict, *, reporter: bool = False, note: bool = False) -> str:
    bits = []
    if reporter:
        bits.append(f"投稿人：{_reporter(row)}")
    record = _record(row)
    if record:
        bits.append(record)
    bits.append(f"类型：{kind_title(str(row.get('kind') or ''))}")
    bits.append(f"内容：{_subject(row)}")
    number = _number(row)
    if number:
        bits.append(f"编号：<code>{number}</code>")
    if note:
        bits.append(f"原因：{html.escape(_visible(row.get('note')) or '未填写')}")
    return "<blockquote>" + "\n".join(bits) + "</blockquote>"


def lines(row: dict, head: str) -> str:
    title = {
        "投稿": "<b>新投稿</b>",
        "已确认": "<b>已通过</b>",
        "已拒绝": "<b>未通过</b>",
        "已移出": "<b>已移出</b>",
    }.get(head, html.escape(head or "投稿"))
    return "\n".join([title, "", _quote(row, reporter=True, note=True)])


def preview(kind: str, title: str, username: str, user_id: int) -> str:
    row = {"kind": kind, "title": title, "username": username, "user_id": user_id}
    return "\n".join([
        "请发送提交原因：",
        "",
        _quote(row),
    ])


def ready(kind: str, title: str, username: str, user_id: int, note: str) -> str:
    row = {"kind": kind, "title": title, "username": username, "user_id": user_id, "note": note}
    return "\n".join([
        "请确认投稿：",
        "",
        _quote(row, note=True),
    ])


def filed(row: dict) -> str:
    return "\n".join([
        "已提交，等待审核。",
        "",
        _quote(row, note=True),
    ])


ASK = "\n".join([
    "请选择投稿对象：",
    "从下方列表选择，或发送 @用户名。",
])


OPEN = "\n".join([
    "🛡 <b>红包</b>",
    "账号、屏蔽和偏好，都在管理页。",
    "假钱包、诈骗号，可以在这里投稿。",
])


def taken(username: str, user_id: int, conn=None) -> str:
    key = _key(username) or _key(user_id)
    number = int(user_id or 0)
    if not key and number == 0:
        return ""
    own = conn is None
    if own:
        conn = _connect()
    try:
        blocked = None
        if key:
            blocked = conn.execute(
                "SELECT 1 FROM system_block WHERE username = ?",
                (key,),
            ).fetchone()
        if blocked is None and number:
            blocked = conn.execute(
                "SELECT 1 FROM system_block WHERE user_id = ?",
                (number,),
            ).fetchone()
        row = None
        row_id = -1
        if key:
            found = conn.execute(
                "SELECT id, status FROM report WHERE username = ? ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if found is not None:
                row = found
                row_id = int(found["id"])
        if number:
            found = conn.execute(
                "SELECT id, status FROM report WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (number,),
            ).fetchone()
            if found is not None and int(found["id"]) > row_id:
                row = found
    finally:
        if own:
            conn.close()
    if blocked:
        return "主体：已在屏蔽名单"
    if row is None or row["status"] == "no":
        return ""
    if row["status"] == "pending":
        return "主体：已提交，等待审核"
    return "主体：已经投稿过"


def receipt(row: dict, status: str) -> str:
    shown = face(row)
    if status == "yes":
        return f"已通过 {shown}"
    if status == "no":
        return f"未通过 {shown}"
    if status == "out":
        return f"已移出 {shown}"
    return f"已提交 {shown}，等待审核。"


def verdict(row: dict, status: str) -> str:
    head = {"yes": "已通过", "no": "未通过", "out": "已移出"}.get(status, "已提交")
    extra = {
        "yes": "已加入系统屏蔽，之后所有人都不抢。",
        "no": "没有加入屏蔽。",
        "out": "已从系统屏蔽移出。",
    }.get(status, "")
    return "\n".join([
        f"<b>{head}</b>",
        extra,
        "",
        _quote(row, note=True),
    ])


def clean_note(text: str) -> tuple[str, str]:
    note = " ".join((text or "").split())
    if len(note) < 2:
        return "", "主体：原因至少 2 个字"
    if len(note) > 200:
        return "", "主体：原因最多 200 字"
    return note, ""


def submit(
    owner_id: int, username: str, user_id: int, title: str, name: str, kind: str, note: str,
) -> tuple[dict | None, str]:
    key = _key(username) or _key(user_id)
    number = int(user_id or 0)
    if kind not in KIND_LABEL or not key or number == 0:
        return None, "主体：无法识别"
    note, error = clean_note(note)
    if error:
        return None, error
    title = " ".join(str(title or "").split())[:40]
    name = " ".join(str(name or "").split())[:64]
    now = int(time.time())
    earlier = taken(key, number)
    if earlier:
        return None, earlier
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        again = taken(key, number, conn)
        if again:
            _rollback(conn)
            return None, again
        waiting = conn.execute(
            "SELECT COUNT(*) AS n FROM report WHERE owner_id = ? AND status = 'pending'",
            (owner_id,),
        ).fetchone()
        if int(waiting["n"]) >= 5:
            _rollback(conn)
            return None, "主体：还有投稿在等审核"
        burst = conn.execute(
            "SELECT COUNT(*) AS n FROM report WHERE owner_id = ? AND created_at > ?",
            (owner_id, now - 3600),
        ).fetchone()
        if int(burst["n"]) >= 8:
            _rollback(conn)
            return None, "主体：投稿过多，请稍后再试"
        cur = conn.execute(
            """
            INSERT INTO report (owner_id, username, user_id, title, name, kind, note, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (owner_id, key, number, title, name, kind, note, now),
        )
        conn.execute("COMMIT")
        report_id = int(cur.lastrowid)
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    log.info("主体：投稿 id=%s user=%s kind=%s name=%s", report_id, owner_id, kind, key)
    return _item(
        {
            "id": report_id,
            "owner_id": owner_id,
            "username": key,
            "user_id": number,
            "title": title,
            "name": name,
            "kind": kind,
            "note": note,
        },
        "pending",
    ), ""


def cancel(report_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM report WHERE id = ? AND status = 'pending'", (report_id,))
    finally:
        conn.close()


def decide(report_id: int, accept: bool) -> tuple[dict | None, str]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM report WHERE id = ?", (report_id,)).fetchone()
        if row is None or row["status"] != "pending":
            _rollback(conn)
            return None, "主体：已经处理"
        status = "yes" if accept else "no"
        known = None
        if accept:
            number = int(row["user_id"] or 0)
            if number == 0:
                _rollback(conn)
                return None, "主体：无法识别"
            if str(row["kind"] or "") == "bot":
                ensure_brands()
                known = impostor(row["title"], row["username"], number)
            if known:
                conn.execute("DELETE FROM system_block WHERE user_id = ?", (number,))
            else:
                count = conn.execute("SELECT COUNT(*) AS n FROM system_block").fetchone()
                exists = conn.execute(
                    "SELECT 1 FROM system_block WHERE user_id = ?",
                    (number,),
                ).fetchone()
                if not exists and int(count["n"]) >= _CAP:
                    _rollback(conn)
                    return None, "主体：屏蔽名单已满"
                conn.execute(
                    """
                    INSERT INTO system_block (user_id, username, title, report_id, kind)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = excluded.username,
                        title = excluded.title,
                        report_id = excluded.report_id,
                        kind = excluded.kind
                    """,
                    (number, _face_name(row["username"], number), row["title"], report_id, row["kind"] or ""),
                )
        updated = conn.execute(
            "UPDATE report SET status = ? WHERE id = ? AND status = 'pending'",
            (status, report_id),
        )
        if updated.rowcount != 1:
            _rollback(conn)
            return None, "主体：已经处理"
        conn.execute("COMMIT")
        item = _item(row, status)
        if known:
            item["known_fake"] = known
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    refresh()
    log.info("主体：投稿审核 id=%s status=%s", report_id, item["status"])
    return item, ""


def remove(report_id: int) -> tuple[dict | None, str]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM report WHERE id = ?", (report_id,)).fetchone()
        if row is None or row["status"] != "yes":
            _rollback(conn)
            return None, "主体：已经处理"
        number = int(row["user_id"] or 0)
        deleted = conn.execute("DELETE FROM system_block WHERE user_id = ?", (number,))
        if number == 0 or deleted.rowcount != 1:
            _rollback(conn)
            return None, "主体：不在屏蔽名单"
        conn.execute("COMMIT")
        item = _item(row, "yes")
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()
    refresh()
    log.info("主体：移出屏蔽 id=%s name=%s", report_id, item["username"])
    return item, ""


def list_blocks() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT username, user_id, title, kind, report_id FROM system_block"
        ).fetchall()
    finally:
        conn.close()
    order = {"group": 0, "channel": 1, "bot": 2, "user": 3}
    items = []
    for row in rows:
        kind = str(row["kind"] or "")
        items.append({
            "username": str(row["username"] or ""),
            "user_id": int(row["user_id"] or 0),
            "title": str(row["title"] or ""),
            "kind": kind,
            "kind_title": kind_title(kind) or "其他",
            "report_id": int(row["report_id"] or 0),
        })
    items.sort(key=lambda item: (order.get(item["kind"], 9), item["title"], item["username"]))
    return items


def drop_name(username: str) -> str:
    key = _key(username)
    if not key:
        return "主体：无法识别"
    conn = _connect()
    try:
        if key.lstrip("-").isdigit():
            deleted = conn.execute("DELETE FROM system_block WHERE user_id = ?", (int(key),))
        else:
            deleted = conn.execute("DELETE FROM system_block WHERE username = ?", (key,))
        gone = deleted.rowcount
    finally:
        conn.close()
    refresh()
    if gone != 1:
        return "主体：不在屏蔽名单"
    log.info("主体：移出屏蔽 name=%s", key)
    return ""


async def find_target(kind: str, parsed: dict) -> tuple[dict | None, str]:
    from meikai.login import _wait
    from meikai.node_rpc import call_nodes

    payload = {"op": "lookup"}
    if parsed.get("username"):
        payload["username"] = parsed["username"]
    else:
        payload["peer"] = int(parsed.get("chat_id") or parsed.get("user_id") or 0)
    results = await call_nodes(payload, timeout=28)
    found = None
    other = ""
    flood = None
    answered = False
    for result in results.values():
        if not isinstance(result, dict) or result.get("error") == "offline":
            continue
        answered = True
        if result.get("error") == "flood":
            flood = result
            continue
        if not result.get("ok"):
            continue
        got = str(result.get("kind") or "")
        if got not in KIND_LABEL:
            continue
        if not kind or got == kind:
            found = result
            kind = got
            break
        other = got
    if found:
        try:
            number = int(found.get("user_id") or 0)
        except (TypeError, ValueError):
            number = 0
        key = _key(found.get("username")) or _key(number)
        if number == 0 or not key:
            miss = "节点：没有这个窗口" if kind in ("group", "channel") else "节点：没有这个用户名"
            return None, miss
        title = " ".join(str(found.get("title") or "").split())[:40]
        return {"username": key, "user_id": number, "title": title, "kind": kind}, ""
    if other:
        return None, f"主体：这是{KIND_LABEL[other]}，不是{KIND_LABEL.get(kind, '')}"
    if flood:
        return None, f"Telegram：请求过于频繁，请 {_wait(int(flood.get('seconds') or 0))}后再试"
    if not answered:
        return None, "节点：未连接"
    if kind in ("group", "channel"):
        return None, "节点：没有这个窗口"
    return None, "节点：没有这个用户名"


async def push_blocks(only: str | None = None) -> list[str]:
    from meikai.node_rpc import call_node, call_nodes

    names = sorted(_ids) if _blocks_ready else refresh()
    if names is None:
        return []
    payload = {"op": "blocks", "names": names, "alias": dict(_by_name)}
    if only:
        result = await call_node(only, payload, timeout=8)
        failed = [] if result.get("ok") else [only]
    else:
        results = await call_nodes(payload, timeout=8)
        failed = [node_id for node_id, result in results.items() if not result.get("ok")]
    if failed:
        log.warning("主体：屏蔽名单没有送到 %s", " ".join(failed))
    return failed


async def push_brand_delta(items) -> list[str]:
    from meikai.node_rpc import call_nodes

    if not items:
        return []
    results = await call_nodes({"op": "brands", "items": items}, timeout=8)
    failed = [node_id for node_id, result in results.items() if not result.get("ok")]
    if failed:
        log.warning("主体：认证钱包没有送到 %s", " ".join(failed))
    return failed


async def push_brands(only: str | None = None) -> list[str]:
    from meikai.node_rpc import call_node, call_nodes

    items = load_brands()
    payload = {"op": "brands", "items": items}
    if only:
        result = await call_node(only, payload, timeout=8)
        failed = [] if result.get("ok") else [only]
    else:
        results = await call_nodes(payload, timeout=8)
        failed = [node_id for node_id, result in results.items() if not result.get("ok")]
    if failed:
        log.warning("主体：认证钱包没有送到 %s", " ".join(failed))
    return failed


async def push_glyphs(only: str | None = None) -> list[str]:
    from meikai.node_rpc import call_node, call_nodes
    from meikai.store import load_doc_map, load_glyph_map

    glyphs = load_glyph_map()
    docs = load_doc_map()
    payload = {"op": "glyphs", "glyphs": glyphs, "docs": docs}
    if only:
        result = await call_node(only, payload, timeout=25)
        failed = [] if result.get("ok") else [only]
    else:
        results = await call_nodes(payload, timeout=25)
        failed = [node_id for node_id, result in results.items() if not result.get("ok")]
    if failed:
        log.warning("主体：表情缓存没有送到 %s", " ".join(failed))
    return failed


def _check() -> None:
    from meikai.policy import marked_channel_id

    saved_watch = set(_watch_ids)
    assert watched("青念红包监控")
    assert watched(" UU雷达监控 ")
    assert not watched("未来娱乐")
    assert not watched("")
    assert steward_text("专业群管，欢迎进群")
    assert steward_text("社群管理助手")
    assert steward_text("首家AI智能反广告引擎")
    assert not steward_text("红包钱包")
    assert not steward_text("")
    note_steward("guanbot", 424242)
    assert hits(bots=["guanbot"])
    assert hits(bots=[424242])
    assert not hits(username="walletbot")

    class _Named:
        title = "红包监控"
        first_name = ""
        last_name = ""
        username = "radar"

    assert entity_watched(_Named())
    assert hits(title="红包监控", chat_id=-1001, username="radar")
    assert hits(chat_id=-1001)
    assert hits(username="radar")
    assert note_watch("监控机器人", username="monwatchbot", user_id=9000000000005)
    assert hits(bots=["monwatchbot"])
    assert hits(bots=[9000000000005])
    assert not hits(chat_id=-1002)
    _watch_ids.clear()
    _watch_ids.update(saved_watch)
    assert parse_report("@okpvy") == "okpvy"
    assert parse_report("@OKPVY") == "okpvy"
    assert parse_report("https://t.me/okpvy/3") == "okpvy"
    assert parse_report("投稿 @okpvy") == "okpvy"
    assert parse_report("@nn33") == "nn33"
    assert parse_report("https://t.me/c/5/1") == str(marked_channel_id(5))
    assert parse_report("nope nope") == ""
    assert parse_report("@s") == ""
    assert typed_name("@okpvy", bare=False) == {"username": "okpvy"}
    assert typed_name("okpvy", bare=True) == {"username": "okpvy"}
    assert typed_name("okpvy", bare=False) is None
    assert typed_name("假的", bare=True) is None
    assert typed_name("https://t.me/okpvy/3", bare=False) == {"username": "okpvy"}
    assert typed_name("看 https://t.me/okpvy", bare=False) is None
    assert typed_name("@okpvy 是假的", bare=True) is None
    assert parse_subject("user", "@okpvy") == {"username": "okpvy"}
    assert parse_subject("bot", "123456789") == {"user_id": 123456789}
    assert parse_subject("user", "https://t.me/c/5/1") is None
    assert parse_subject("group", "https://t.me/c/5/1") == {"chat_id": marked_channel_id(5)}
    assert clean_note("字")[1] == "主体：原因至少 2 个字"
    assert preview("user", "雨", "yuuu", 1).startswith("请发送提交原因：")
    assert ready("user", "雨", "yuuu", 1, "假的").startswith("请确认投稿：")
    assert clean_note("假的")[0] == "假的"
    assert poseur("okpay", "okpvy")
    assert poseur("OK Pay", "okpvy")
    assert poseur("OKPay钱包", "okpvy")
    assert poseur("kpay", "kpayy")
    assert poseur("达利钱包 @DLQB", "dlqbobot")
    assert not poseur("达利钱包 @DLQB", "dlqb")
    assert not poseur("达利钱包 @DLQB", "DLQB")
    assert not poseur("OKPay", "okpay")
    assert not poseur("OKPay钱包", "okpay")
    assert not poseur("OKPay Official", "okpay")
    assert not poseur("红包助手", "hongbao")
    assert not poseur("OKPay", ["okqb", "okpay"])
    assert not poseur("OK Pay", ["okqb", "okpay"])
    assert not poseur("钱包 @okpay", ["okqb", "okpay"])
    assert not poseur("钱包 @okpay @okqb", ["okqb", "okpay"])
    assert poseur("钱包 @okpay", ["okqb"])
    assert poseur("okpay", ["okqb"])
    assert seal_text('verified by the organization "Hold".')
    assert seal_text('verified by the organization "Major".')
    assert not seal_text("")
    learn_brand("OKPay Official", ["okpay"], 1)
    assert brand_fake("OKPay", ["okpvy"], 2)
    assert brand_fake("OK Pay", ["okpvycbot"], 3)
    assert brand_fake("OKPay钱包", ["okpvy"], 4)
    assert brand_fake("OKPay Official", ["okpayofficial"], 5)
    assert not brand_fake("OKPay", ["okpay"], 1)
    assert not brand_fake("OKPay Official", ["okpay"], 1)
    assert not brand_fake("OKPay", ["okqb", "okpay"], 1)
    learn_brand("汤臣钱包 @tcqb", ["lpay", "tcqb"], 9)
    assert not brand_fake("汤臣钱包 @tcqb", ["lpay"], 9)
    assert brand_fake("汤臣钱包", ["fakebot"], 8)
    assert not brand_fake("tmqb", ["tmqb"], 7)
    hit = impostor("汤臣钱包", "fakebot", 8)
    assert hit is not None and hit["nick"] == "汤臣钱包" and "tcqb" in hit["names"]
    assert impostor("汤臣钱包 @tcqb", "tcqb", 9) is None
    notice = fake_notice(
        {"kind": "bot", "title": "汤臣钱包", "username": "fakebot", "user_id": 8},
        hit,
    )
    assert notice.startswith("<b>已识别为假钱包</b>")
    assert "仿冒「汤臣钱包」" in notice and "@tcqb" in notice and "之后所有人都不抢" in notice
    assert "系统屏蔽" not in notice
    _brands.clear()
    card = {
        "username": "okpvy",
        "title": "假钱包",
        "name": "甲",
        "owner_id": 1,
        "user_id": 99,
        "kind": "bot",
        "note": "冒充",
    }
    posted = lines(card, "投稿")
    assert posted.startswith("<b>新投稿</b>")
    assert "<blockquote>" in posted and "投稿人：" in posted
    assert 'href="tg://user?id=1"' in posted and ">甲</a>" in posted
    assert 'href="https://t.me/okpvy"' in posted and ">假钱包</a>" in posted
    assert "类型：机器人" in posted and "<code>99</code>" in posted and "原因：冒充" in posted
    marked = dict(card, reports=3, passed=1)
    assert "记录：投稿 3 次，通过 1 次。" in lines(marked, "投稿")
    named = dict(card, name="雨 @yuuu", owner_id=755, note="a<b>")
    text = lines(named, "已确认")
    assert text.startswith("<b>已通过</b>")
    assert 'href="https://t.me/yuuu"' in text and ">雨</a>" in text
    assert "原因：a&lt;b&gt;" in text
    bare = dict(card, username="1000000001", title="", user_id=1000000001, kind="user", note="不知道")
    bare_text = lines(bare, "投稿")
    assert 'href="tg://user?id=1000000001"' in bare_text
    assert "<code>1000000001</code>" in bare_text
    assert receipt(card, "yes") == "已通过 @okpvy"
    assert not hits({"okpvy"})
    import os
    import tempfile

    global _PATH
    saved_path = _PATH
    saved_ids = set(_ids)
    saved_alias = dict(_by_name)
    fd, path = tempfile.mkstemp(prefix="meikai-report-")
    os.close(fd)
    old_fd, old_path = tempfile.mkstemp(prefix="meikai-block-")
    os.close(old_fd)
    try:
        old = sqlite3.connect(old_path)
        old.execute(
            """
            CREATE TABLE system_block (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                report_id INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT ''
            )
            """
        )
        old.execute("INSERT INTO system_block VALUES ('okpvy', 99, '假', 1, 'bot')")
        old.execute("INSERT INTO system_block VALUES ('-1009', -1009, '频道', 2, 'channel')")
        old.commit()
        old.close()
        _PATH = old_path
        _db_ready.discard(old_path)
        moved = _connect()
        try:
            kept = moved.execute("SELECT user_id, username FROM system_block WHERE user_id = 99").fetchone()
            channel = moved.execute("SELECT username FROM system_block WHERE user_id = -1009").fetchone()
            assert kept is not None and kept["username"] == "okpvy"
            assert channel is not None and channel["username"] == ""
            assert _primary(moved, "system_block") == ["user_id"]
        finally:
            moved.close()
        _db_ready.discard(old_path)
        _PATH = path
        row, error = submit(1, "okpvy", 99, "假钱包", "甲", "bot", "冒充")
        assert error == "" and row is not None and row["username"] == "okpvy" and row["note"] == "冒充"
        assert counts(1) == (1, 0)
        again, error = submit(2, "okpvy", 99, "", "乙", "bot", "也是假的")
        assert again is None and error == "主体：已提交，等待审核"
        assert not hits({"okpvy"})
        done, error = decide(int(row["id"]), True)
        assert error == "" and done is not None and done["status"] == "yes"
        assert counts(1) == (1, 1)
        assert hits({"OKPVY"}) and hits({99}) and not hits({"kpay"})
        blocked, error = submit(3, "other", 99, "", "", "user", "同一个")
        assert blocked is None and error == "主体：已在屏蔽名单"
        removed, error = remove(int(row["id"]))
        assert error == "" and removed is not None and not hits({"okpvy"}) and not hits({"99"})
        fresh, error = submit(1, "okpvy", 99, "", "甲", "bot", "再投一次")
        assert fresh is None and error == "主体：已经投稿过"
        other, error = submit(1, "nopebot", 100, "", "甲", "bot", "另一条")
        assert error == "" and other is not None
        rejected, error = decide(int(other["id"]), False)
        assert error == "" and rejected is not None and "<b>未通过</b>" in verdict(rejected, "no")
        late, error = decide(int(other["id"]), True)
        assert late is None and error == "主体：已经处理"
        repeat, error = submit(1, "nopebot", 100, "", "甲", "bot", "再来")
        assert error == "" and repeat is not None
        chat, error = submit(4, "-1009", -1009, "频道", "甲", "channel", "假频道")
        assert error == "" and chat is not None
        assert decide(int(chat["id"]), True)[1] == ""
        assert hits(set(), chat_id=-1009)
        assert drop_name("-1009") == "" and not hits(set(), chat_id=-1009)
        assert drop_name("okpvy") == "主体：不在屏蔽名单"
        learn_brand("汤臣钱包 @tcqb", ["lpay", "tcqb"], 9)
        conn = _connect()
        conn.executemany(
            "INSERT INTO system_block (user_id, username, title, report_id, kind) VALUES (?, ?, ?, 0, ?)",
            [
                (8, "fakebot", "汤臣钱包", "bot"),
                (9, "tcqb", "汤臣钱包", "bot"),
                (-1008, "", "汤臣钱包", "group"),
            ],
        )
        conn.close()
        refresh()
        assert release_known_fakes() == [8]
        assert not hits({8}) and hits({9}) and hits(set(), chat_id=-1008)
        filed, error = submit(5, "fakebot", 8, "汤臣钱包", "甲", "bot", "冒充")
        assert error == "" and filed is not None
        done, error = decide(int(filed["id"]), True)
        assert error == "" and done is not None and done.get("known_fake") and not hits({8})
        _brands.clear()
    finally:
        _PATH = saved_path
        apply_blocks(saved_ids)
        apply_alias(saved_alias)
        for gone in (path, old_path):
            for suffix in ("", "-wal", "-shm"):
                extra = gone + suffix
                if os.path.exists(extra):
                    os.remove(extra)


_check()
