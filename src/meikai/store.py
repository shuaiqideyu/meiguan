"""余额和表情缓存。Redis 供读取，SQLite 落盘。"""

import copy
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

log = logging.getLogger(__name__)
MODE_KINDS = ("direct", "captcha", "webapp", "phrase")

DB = Path("/opt/meikai/data/meikai.db")
LEGACY = Path("/opt/meikai/data/balances.json")
_redis = None
_redis_ready = False
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_memory: dict[int, dict] = {}
_policies: dict[str, dict] | None = None
_sessions: dict[str, tuple[int, int]] = {}
_used: dict[int, set[str]] | None = None


class _Held:
    def __init__(self, raw: sqlite3.Connection, lock: threading.Lock):
        self._raw = raw
        self._lock = lock
        self._open = True

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        try:
            if self._raw.in_transaction:
                self._raw.rollback()
        except sqlite3.Error:
            pass
        self._lock.release()

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


def _connect() -> _Held:
    global _conn
    _lock.acquire()
    try:
        if _conn is None:
            DB.parent.mkdir(parents=True, exist_ok=True)
            raw = sqlite3.connect(DB, check_same_thread=False, timeout=5)
            raw.execute("PRAGMA journal_mode=WAL")
            raw.execute("PRAGMA synchronous=NORMAL")
            raw.execute("PRAGMA busy_timeout=5000")
            raw.execute("PRAGMA temp_store=MEMORY")
            raw.execute("PRAGMA cache_size=-32768")
            raw.execute("PRAGMA mmap_size=67108864")
            raw.executescript(
        """
        CREATE TABLE IF NOT EXISTS balance_account (
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (owner_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS balance_meta (
            owner_id INTEGER PRIMARY KEY,
            updated_at TEXT NOT NULL,
            pending TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grab_mode (
            kind TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS owner_policy (
            owner_id INTEGER PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS glyph (
            digest TEXT PRIMARY KEY,
            char TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS glyph_doc (
            doc_id INTEGER PRIMARY KEY,
            char TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_session (
            token_hash TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hot_sent (
            owner_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            next_at INTEGER NOT NULL,
            last_line TEXT NOT NULL,
            PRIMARY KEY (owner_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS hot_slot (
            owner_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            next_at INTEGER NOT NULL,
            last_line TEXT NOT NULL,
            PRIMARY KEY (owner_id, chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS hot_day (
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            sent INTEGER NOT NULL,
            PRIMARY KEY (owner_id, user_id, day)
        );
        CREATE TABLE IF NOT EXISTS hot_sign (
            owner_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            due_at INTEGER NOT NULL,
            done INTEGER NOT NULL,
            PRIMARY KEY (owner_id, chat_id, user_id, day)
        );
        CREATE TABLE IF NOT EXISTS claim (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            ok INTEGER NOT NULL,
            amount TEXT NOT NULL,
            wallet TEXT NOT NULL,
            place TEXT NOT NULL,
            detail TEXT NOT NULL,
            slots TEXT NOT NULL DEFAULT '',
            at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS claim_owner ON claim (owner_id, id DESC);
        CREATE TABLE IF NOT EXISTS chat_face (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS chat_face_username ON chat_face(username);
        CREATE TABLE IF NOT EXISTS face_photo (
            peer INTEGER PRIMARY KEY,
            photo_id INTEGER NOT NULL,
            md5 TEXT NOT NULL,
            checked_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS face_photo_md5 ON face_photo(md5);
        CREATE INDEX IF NOT EXISTS web_session_expires ON web_session(expires_at);
        CREATE TABLE IF NOT EXISTS owner_node (
            owner_id INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            PRIMARY KEY (owner_id, node_id)
        );
        CREATE TABLE IF NOT EXISTS claim_skip (
            owner_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            reason TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (owner_id, day, reason)
        );
        CREATE TABLE IF NOT EXISTS claim_skip_note (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            reason TEXT NOT NULL,
            detail TEXT NOT NULL,
            at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS claim_skip_note_owner ON claim_skip_note (owner_id, day, id DESC);
        CREATE TABLE IF NOT EXISTS session_notice (
            user_id INTEGER PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notify_bot (
            owner_id INTEGER PRIMARY KEY,
            token TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            bot_id INTEGER NOT NULL DEFAULT 0
        );
        """
    )
            columns = {row[1] for row in raw.execute("PRAGMA table_info(claim)")}
            if "slots" not in columns:
                raw.execute("ALTER TABLE claim ADD COLUMN slots TEXT NOT NULL DEFAULT ''")
            if "chat_id" not in columns:
                raw.execute("ALTER TABLE claim ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0")
            if "msg_id" not in columns:
                raw.execute("ALTER TABLE claim ADD COLUMN msg_id INTEGER NOT NULL DEFAULT 0")
            if "elapsed_ms" not in columns:
                raw.execute("ALTER TABLE claim ADD COLUMN elapsed_ms INTEGER NOT NULL DEFAULT 0")
            _conn = raw
        return _Held(_conn, _lock)
    except Exception:
        _lock.release()
        raise


def _client():
    global _redis, _redis_ready
    if _redis_ready:
        return _redis
    _redis_ready = True
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        from redis import Redis

        client = Redis.from_url(url, socket_connect_timeout=0.3, socket_timeout=0.3)
        client.ping()
        _redis = client
    except Exception as exc:
        log.warning("redis unavailable %s", type(exc).__name__)
        _redis = None
    return _redis


def _key(owner_id: int) -> str:
    return f"meikai:balance:{owner_id}"


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


def _hash_get(key: str) -> dict[str, str] | None:
    client = _client()
    if client is None:
        return None
    try:
        if not client.exists(key):
            return None
        raw = client.hgetall(key)
    except Exception as exc:
        log.warning("redis read %s", type(exc).__name__)
        return None
    found = {}
    for field, value in raw.items():
        name = _text(field)
        char = _text(value)
        if name and char:
            found[name] = char
    return found


def _hash_fill(key: str, mapping: dict[str, str]) -> None:
    client = _client()
    if client is None or not mapping:
        return
    tmp = f"{key}:next"
    try:
        client.delete(tmp)
        items = list(mapping.items())
        for start in range(0, len(items), 1000):
            client.hset(tmp, mapping=dict(items[start:start + 1000]))
        client.rename(tmp, key)
    except Exception as exc:
        log.warning("redis write %s", type(exc).__name__)
        try:
            client.delete(tmp)
        except Exception:
            pass


def _hash_patch(key: str, updates: dict[str, str], deletes: list[str]) -> None:
    client = _client()
    if client is None or not (updates or deletes):
        return
    try:
        if not client.exists(key):
            return
        if deletes:
            client.hdel(key, *deletes)
        if updates:
            client.hset(key, mapping=updates)
    except Exception as exc:
        log.warning("redis write %s", type(exc).__name__)
        try:
            client.delete(key)
        except Exception:
            pass


def _import_legacy(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) FROM balance_meta").fetchone()
    if row and row[0]:
        return
    if not LEGACY.exists():
        return
    try:
        data = json.loads(LEGACY.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for owner, record in data.items():
        if not str(owner).isdigit():
            continue
        owner_id = int(owner)
        for user, entry in (record.get("accounts") or {}).items():
            if not str(user).isdigit():
                continue
            conn.execute(
                "INSERT OR REPLACE INTO balance_account (owner_id, user_id, payload) VALUES (?, ?, ?)",
                (owner_id, int(user), json.dumps(entry, ensure_ascii=False)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO balance_meta (owner_id, updated_at, pending) VALUES (?, ?, ?)",
            (owner_id, record.get("at") or "", record.get("pending") or ""),
        )
    conn.commit()


def _read_db(owner_id: int) -> dict:
    conn = _connect()
    try:
        _import_legacy(conn)
        meta = conn.execute(
            "SELECT updated_at, pending FROM balance_meta WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT user_id, payload FROM balance_account WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    finally:
        conn.close()
    accounts = {}
    for user_id, payload in rows:
        try:
            accounts[str(user_id)] = json.loads(payload)
        except json.JSONDecodeError:
            continue
    if meta is None and not accounts:
        return {}
    return {
        "at": meta[0] if meta else "",
        "pending": meta[1] if meta else "",
        "accounts": accounts,
    }


_DIGEST = re.compile(r"^[0-9a-f]{32}$")
_GLYPH = re.compile(r"^[0-9A-Za-z]{1,2}$")
_DOC_CHAR = re.compile(r"^.{1,4}$", re.DOTALL)


def load_glyph_map() -> dict[str, str]:
    cached = _hash_get("meikai:glyph")
    if cached is not None:
        return cached
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return {}
    try:
        rows = conn.execute("SELECT digest, char FROM glyph").fetchall()
    finally:
        conn.close()
    found = {str(digest): str(char) for digest, char in rows if digest and char}
    _hash_fill("meikai:glyph", found)
    return found


def merge_glyphs(learned: dict) -> tuple[int, int]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return 0, 0
    added = 0
    deleted = 0
    updates: dict[str, str] = {}
    deletes: list[str] = []
    try:
        conn.execute("BEGIN")
        for raw_digest, raw_char in learned.items():
            digest = str(raw_digest or "").strip().lower()
            char = str(raw_char or "").strip()
            if not _DIGEST.fullmatch(digest):
                continue
            if char == "":
                cur = conn.execute("DELETE FROM glyph WHERE digest = ?", (digest,))
                deleted += cur.rowcount
                deletes.append(digest)
                continue
            if not _GLYPH.fullmatch(char):
                continue
            row = conn.execute("SELECT char FROM glyph WHERE digest = ?", (digest,)).fetchone()
            if row and row[0] == char:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO glyph (digest, char) VALUES (?, ?)",
                (digest, char),
            )
            added += 1
            updates[digest] = char
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return 0, 0
    finally:
        conn.close()
    _hash_patch("meikai:glyph", updates, deletes)
    return added, deleted


def load_doc_map() -> dict[str, str]:
    cached = _hash_get("meikai:glyph:doc")
    if cached is not None:
        return cached
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return {}
    try:
        rows = conn.execute("SELECT doc_id, char FROM glyph_doc").fetchall()
    finally:
        conn.close()
    found = {str(doc_id): str(char) for doc_id, char in rows if doc_id and char}
    _hash_fill("meikai:glyph:doc", found)
    return found


def merge_docs(learned: dict) -> tuple[int, int]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return 0, 0
    added = 0
    deleted = 0
    updates: dict[str, str] = {}
    deletes: list[str] = []
    try:
        conn.execute("BEGIN")
        for raw_doc, raw_char in learned.items():
            text = str(raw_doc or "").strip()
            if not text.isdigit():
                continue
            doc_id = int(text)
            char = str(raw_char or "").strip()
            if char == "":
                cur = conn.execute("DELETE FROM glyph_doc WHERE doc_id = ?", (doc_id,))
                deleted += cur.rowcount
                deletes.append(str(doc_id))
                continue
            if not _DOC_CHAR.fullmatch(char):
                continue
            row = conn.execute("SELECT char FROM glyph_doc WHERE doc_id = ?", (doc_id,)).fetchone()
            if row and row[0] == char:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO glyph_doc (doc_id, char) VALUES (?, ?)",
                (doc_id, char),
            )
            added += 1
            updates[str(doc_id)] = char
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return 0, 0
    finally:
        conn.close()
    _hash_patch("meikai:glyph:doc", updates, deletes)
    return added, deleted


def load_modes() -> dict[str, bool]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return {kind: True for kind in MODE_KINDS}
    try:
        rows = conn.execute("SELECT kind, enabled FROM grab_mode").fetchall()
    finally:
        conn.close()
    saved = {kind: bool(enabled) for kind, enabled in rows}
    if "webapp" not in saved and "app" in saved:
        saved["webapp"] = saved["app"]
    if "webapp" not in saved and ("cloudflare" in saved or "yypay" in saved):
        saved["webapp"] = bool(saved.get("cloudflare", saved.get("yypay")))
    if "captcha" not in saved and "dm" in saved:
        saved["captcha"] = saved["dm"]
    flags = {kind: True for kind in MODE_KINDS}
    for kind in MODE_KINDS:
        if kind in saved:
            flags[kind] = saved[kind]
    return flags


def save_modes(flags: dict[str, bool]) -> dict[str, bool]:
    current = {kind: bool(flags.get(kind)) for kind in MODE_KINDS}
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return current
    try:
        conn.execute("BEGIN")
        for kind, enabled in current.items():
            conn.execute(
                "INSERT OR REPLACE INTO grab_mode (kind, enabled) VALUES (?, ?)",
                (kind, 1 if enabled else 0),
            )
        conn.execute("DELETE FROM grab_mode WHERE kind IN (?, ?, ?)", ("yypay", "app", "dm"))
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()
    return current


def load_policies() -> dict[str, dict] | None:
    global _policies
    from meikai.policy import normalize

    if _policies is not None:
        return _policies
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        rows = conn.execute("SELECT owner_id, payload FROM owner_policy").fetchall()
    finally:
        conn.close()
    found = {}
    for owner_id, payload in rows:
        try:
            found[str(int(owner_id))] = normalize(json.loads(payload))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    _policies = found
    return _policies


def load_policy(owner_id: int) -> dict:
    from meikai.policy import GRAB_KINDS, default_policy

    saved = load_policies() or {}
    if str(owner_id) in saved:
        return copy.deepcopy(saved[str(owner_id)])
    base = default_policy()
    flags = load_modes()
    for kind in GRAB_KINDS:
        base["modes"][kind] = bool(flags.get(kind, True))
    return base


def close_joins() -> int:
    saved = load_policies()
    if not saved:
        return 0
    changed = 0
    for raw, policy in saved.items():
        if not policy.get("joins") and not policy.get("join_legacy"):
            continue
        policy["joins"] = []
        policy["join_legacy"] = False
        save_policy(int(raw), policy)
        changed += 1
    return changed


def save_policy(owner_id: int, policy: dict) -> dict:
    from meikai.policy import normalize

    current = normalize(policy)
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return current
    saved = False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO owner_policy (owner_id, payload) VALUES (?, ?)",
            (owner_id, json.dumps(current, ensure_ascii=False, separators=(",", ":"))),
        )
        conn.commit()
        saved = True
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()
    if saved and _policies is not None:
        _policies[str(int(owner_id))] = copy.deepcopy(current)
    return current


def load_record(owner_id: int) -> dict:
    cached = _memory.get(owner_id)
    if cached is not None:
        return cached
    client = _client()
    if client is not None:
        try:
            raw = client.get(_key(owner_id))
            if raw:
                record = json.loads(raw)
                _memory[owner_id] = record
                return record
        except Exception as exc:
            log.warning("redis read %s", type(exc).__name__)
    try:
        record = _read_db(owner_id)
    except OSError as exc:
        log.warning("sqlite read %s", type(exc).__name__)
        return {}
    if record:
        _memory[owner_id] = record
        if client is not None:
            try:
                client.set(_key(owner_id), json.dumps(record, ensure_ascii=False))
            except Exception as exc:
                log.warning("redis write %s", type(exc).__name__)
    return record


def save_record(owner_id: int, at: str, accounts: dict, pending: str = "") -> None:
    previous = {}
    client = _client()
    if client is not None:
        try:
            raw = client.get(_key(owner_id))
            if raw:
                previous = json.loads(raw)
        except Exception:
            previous = {}
    record = {
        "at": at or previous.get("at") or "",
        "pending": pending,
        "accounts": accounts,
    }
    _memory[owner_id] = record
    if client is not None:
        try:
            client.set(_key(owner_id), json.dumps(record, ensure_ascii=False))
        except Exception as exc:
            log.warning("redis write %s", type(exc).__name__)
    if pending:
        return
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM balance_account WHERE owner_id = ?", (owner_id,))
        for user, entry in accounts.items():
            if not str(user).isdigit():
                continue
            conn.execute(
                "INSERT INTO balance_account (owner_id, user_id, payload) VALUES (?, ?, ?)",
                (owner_id, int(user), json.dumps(entry, ensure_ascii=False)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO balance_meta (owner_id, updated_at, pending) VALUES (?, ?, ?)",
            (owner_id, record["at"], ""),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def put_session(token_hash: str, owner_id: int, expires_at: int) -> bool:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return False
    try:
        conn.execute("DELETE FROM web_session WHERE expires_at < ?", (int(expires_at) - 86400,))
        conn.execute(
            "INSERT OR REPLACE INTO web_session (token_hash, owner_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, int(owner_id), int(expires_at)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return False
    finally:
        conn.close()
    _sessions[token_hash] = (int(owner_id), int(expires_at))
    if len(_sessions) > 400:
        now = int(time.time())
        for key, (_owner, exp) in list(_sessions.items()):
            if exp < now:
                _sessions.pop(key, None)
    return True


def take_session(token_hash: str) -> int | None:
    now = int(time.time())
    hit = _sessions.get(token_hash)
    if hit is not None:
        if hit[1] >= now:
            return hit[0]
        _sessions.pop(token_hash, None)
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        row = conn.execute(
            "SELECT owner_id, expires_at FROM web_session WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or int(row[1]) < now:
        return None
    owner = int(row[0])
    _sessions[token_hash] = (owner, int(row[1]))
    return owner


def hot_slots(owner_id: int, chat_id: int) -> dict[int, tuple[int, str]]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return {}
    try:
        rows = conn.execute(
            "SELECT user_id, next_at, last_line FROM hot_slot WHERE owner_id = ? AND chat_id = ?",
            (int(owner_id), int(chat_id)),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(row[0]): (int(row[1]), str(row[2] or "")) for row in rows}


def hot_slot_save(owner_id: int, chat_id: int, user_id: int, next_at: int, last_line: str) -> None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO hot_slot (owner_id, chat_id, user_id, next_at, last_line)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(owner_id), int(chat_id), int(user_id), int(next_at), last_line[:80]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def hot_day_counts(owner_id: int, day: str) -> dict[int, int]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return {}
    try:
        rows = conn.execute(
            "SELECT user_id, sent FROM hot_day WHERE owner_id = ? AND day = ?",
            (int(owner_id), day),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(row[0]): int(row[1]) for row in rows}


def hot_day_add(owner_id: int, user_id: int, day: str) -> int | None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        conn.execute("DELETE FROM hot_day WHERE day < ?", (day,))
        row = conn.execute(
            """
            INSERT INTO hot_day (owner_id, user_id, day, sent) VALUES (?, ?, ?, 1)
            ON CONFLICT(owner_id, user_id, day) DO UPDATE SET sent = sent + 1
            RETURNING sent
            """,
            (int(owner_id), int(user_id), day),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0])


def hot_when(owner_id: int, chat_id: int) -> tuple[int | None, str]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None, ""
    try:
        row = conn.execute(
            "SELECT next_at, last_line FROM hot_sent WHERE owner_id = ? AND chat_id = ?",
            (int(owner_id), int(chat_id)),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, ""
    return int(row[0]), str(row[1] or "")


def hot_sign_get(owner_id: int, chat_id: int, user_id: int, day: str) -> tuple[int, int] | None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        row = conn.execute(
            """
            SELECT due_at, done FROM hot_sign
            WHERE owner_id = ? AND chat_id = ? AND user_id = ? AND day = ?
            """,
            (int(owner_id), int(chat_id), int(user_id), day),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), int(row[1])


def hot_sign_plan(owner_id: int, chat_id: int, user_id: int, day: str, due_at: int) -> tuple[int, int] | None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO hot_sign (owner_id, chat_id, user_id, day, due_at, done)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (int(owner_id), int(chat_id), int(user_id), day, int(due_at)),
        )
        row = conn.execute(
            """
            SELECT due_at, done FROM hot_sign
            WHERE owner_id = ? AND chat_id = ? AND user_id = ? AND day = ?
            """,
            (int(owner_id), int(chat_id), int(user_id), day),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), int(row[1])


def hot_sign_finish(owner_id: int, chat_id: int, user_id: int, day: str) -> bool:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return False
    try:
        conn.execute(
            """
            UPDATE hot_sign SET done = 1
            WHERE owner_id = ? AND chat_id = ? AND user_id = ? AND day = ?
            """,
            (int(owner_id), int(chat_id), int(user_id), day),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return False
    finally:
        conn.close()
    return True


def hot_sign_delay(owner_id: int, chat_id: int, user_id: int, day: str, due_at: int) -> None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute(
            """
            UPDATE hot_sign SET due_at = ?
            WHERE owner_id = ? AND chat_id = ? AND user_id = ? AND day = ? AND done = 0
            """,
            (int(due_at), int(owner_id), int(chat_id), int(user_id), day),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def hot_sign_purge(day: str) -> None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute("DELETE FROM hot_sign WHERE day < ?", (day,))
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def hot_save(owner_id: int, chat_id: int, next_at: int, last_line: str) -> None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO hot_sent (owner_id, chat_id, next_at, last_line) VALUES (?, ?, ?, ?)",
            (int(owner_id), int(chat_id), int(next_at), last_line[:80]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def _next_face(stored: tuple[str, str] | None, title: str, username: str) -> tuple[str, str] | None:
    title = " ".join(str(title or "").split())[:40]
    username = str(username or "").strip().lstrip("@")[:40]
    if stored is None:
        if not title and not username:
            return None
        return title, username
    kept_title = title or stored[0]
    kept_user = username or stored[1]
    if kept_title == stored[0] and kept_user == stored[1]:
        return None
    return kept_title, kept_user


assert _next_face(None, "", "") is None
assert _next_face(None, "雨的群", "rain") == ("雨的群", "rain")
assert _next_face(("雨的群", "rain"), "", "") is None
assert _next_face(("雨的群", "rain"), "新群", "") == ("新群", "rain")
assert _next_face(("雨的群", ""), "", "@rain") == ("雨的群", "rain")


def _remember_chat(conn, chat_id: int, title: str, username: str) -> str:
    if not chat_id:
        return ""
    row = conn.execute(
        "SELECT title, username FROM chat_face WHERE chat_id = ?",
        (int(chat_id),),
    ).fetchone()
    stored = None if row is None else (str(row[0] or ""), str(row[1] or ""))
    nxt = _next_face(stored, title, username)
    if nxt is None:
        return stored[0] if stored else ""
    if stored is None:
        conn.execute(
            "INSERT INTO chat_face (chat_id, title, username) VALUES (?, ?, ?)",
            (int(chat_id), nxt[0], nxt[1]),
        )
    else:
        conn.execute(
            "UPDATE chat_face SET title = ?, username = ? WHERE chat_id = ?",
            (nxt[0], nxt[1], int(chat_id)),
        )
    return nxt[0]


def notifier_of(owner_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT token, username, bot_id FROM notify_bot WHERE owner_id = ?",
            (int(owner_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not str(row[0] or ""):
        return None
    return {"token": str(row[0]), "username": str(row[1] or ""), "bot_id": int(row[2] or 0)}


def notifier_face(owner_id: int) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT username FROM notify_bot WHERE owner_id = ?",
            (int(owner_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"bound": False, "username": ""}
    return {"bound": True, "username": str(row[0] or "")}


def save_notifier(owner_id: int, token: str, username: str, bot_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO notify_bot (owner_id, token, username, bot_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET
                token = excluded.token,
                username = excluded.username,
                bot_id = excluded.bot_id
            """,
            (int(owner_id), str(token), str(username or "")[:32], int(bot_id or 0)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_notifier(owner_id: int) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM notify_bot WHERE owner_id = ?", (int(owner_id),))
        conn.commit()
    finally:
        conn.close()


def save_claim(item: dict) -> None:
    try:
        owner_id = int(item.get("owner_id") or 0)
        user_id = int(item.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    if not owner_id or not user_id:
        return
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        try:
            chat_id = int(item.get("chat_id") or 0)
            msg_id = int(item.get("msg_id") or 0)
            elapsed_ms = int(item.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            chat_id, msg_id, elapsed_ms = 0, 0, 0
        if elapsed_ms < 0:
            elapsed_ms = 0
        title = _remember_chat(conn, chat_id, str(item.get("title") or ""), str(item.get("username") or ""))
        conn.execute(
            """
            INSERT INTO claim (
                owner_id, user_id, name, kind, ok, amount, wallet, place, detail, slots,
                chat_id, msg_id, elapsed_ms, at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_id,
                user_id,
                str(item.get("name") or "")[:40],
                str(item.get("kind") or "")[:20],
                1 if item.get("ok") else 0,
                str(item.get("amount") or "")[:40],
                str(item.get("wallet") or "")[:40],
                title[:40],
                str(item.get("detail") or "")[:80],
                str(item.get("slots") or "")[:20],
                chat_id,
                msg_id,
                elapsed_ms,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.execute(
            """
            DELETE FROM claim WHERE owner_id = ? AND id < (
                SELECT id FROM claim WHERE owner_id = ? ORDER BY id DESC LIMIT 1 OFFSET 199
            )
            """,
            (owner_id, owner_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


_AMT = re.compile(r"([\d,]+(?:\.\d+)?)\s*([A-Za-z]+)")


def _coin_name(unit: str) -> str:
    name = (unit or "").upper()
    if name == "U":
        return "USDT"
    if name == "RMB":
        return "CNY"
    if name == "GRAM":
        return "TON"
    if name == "USD":
        return "USDT"
    return name


def sum_text(amounts) -> str:
    from decimal import Decimal

    totals: dict[str, Decimal] = {}
    order: list[str] = []
    for raw in amounts:
        matched = _AMT.search(str(raw or ""))
        if not matched:
            continue
        coin = _coin_name(matched.group(2))
        if not coin:
            continue
        try:
            number = Decimal(matched.group(1).replace(",", ""))
        except Exception:
            continue
        if coin not in totals:
            order.append(coin)
            totals[coin] = Decimal(0)
        totals[coin] += number
    parts = []
    for coin in order:
        text = format(totals[coin], "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        parts.append(f"{text} {coin}")
    return " · ".join(parts)


assert sum_text(["1.20 USDT", "0.8 USDT", "3 TRX"]) == "2 USDT · 3 TRX"
assert sum_text(["0.001 USDT"]) == "0.001 USDT"
assert sum_text(["能量红包"]) == ""


def message_link(chat_id: int, msg_id: int, username: str) -> str:
    if msg_id <= 0:
        return ""
    name = str(username or "").strip().lstrip("@")
    if name and "/" not in name and " " not in name:
        return f"https://t.me/{name}/{msg_id}"
    if chat_id < -(10**12):
        inner = -chat_id - 10**12
        if inner > 0:
            return f"https://t.me/c/{inner}/{msg_id}"
    return ""


assert message_link(-1001234567890, 566, "") == "https://t.me/c/1234567890/566"
assert message_link(-1001234567890, 566, "rain") == "https://t.me/rain/566"
assert message_link(9, 3, "") == ""
assert message_link(0, 0, "rain") == ""


def _group_claims(rows: list[dict], limit: int = 80) -> list[dict]:
    groups: list[dict] = []
    index: dict = {}
    for row in rows:
        chat_id = int(row.get("chat_id") or 0)
        msg_id = int(row.get("msg_id") or 0)
        key = (chat_id, msg_id) if chat_id and msg_id else ("row", row.get("id"))
        slot = index.get(key)
        if slot is None:
            if len(groups) >= limit:
                continue
            slot = len(groups)
            index[key] = slot
            groups.append({
                "place": row.get("place") or "",
                "chat_id": chat_id,
                "msg_id": msg_id,
                "link": message_link(chat_id, msg_id, str(row.get("username") or "")),
                "at": row.get("at") or "",
                "kind": row.get("kind") or "",
                "wallet": row.get("wallet") or "",
                "got": 0,
                "sum": "",
                "accounts": [],
            })
        group = groups[slot]
        if not group["place"] and row.get("place"):
            group["place"] = row["place"]
        if not group["link"]:
            group["link"] = message_link(chat_id, msg_id, str(row.get("username") or ""))
        if not group["kind"] and row.get("kind"):
            group["kind"] = row["kind"]
        if not group["wallet"] and row.get("wallet"):
            group["wallet"] = row["wallet"]
        if row.get("ok"):
            group["got"] += 1
        group["accounts"].append({
            "user_id": int(row.get("user_id") or 0),
            "name": row.get("name") or "",
            "ok": bool(row.get("ok")),
            "amount": row.get("amount") or "",
            "detail": row.get("detail") or "",
            "elapsed_ms": int(row.get("elapsed_ms") or 0),
            "at": row.get("at") or "",
        })
    for group in groups:
        picked = []
        seen = set()
        for account in group["accounts"]:
            if account["user_id"] in seen:
                continue
            seen.add(account["user_id"])
            picked.append(account)
        group["accounts"] = list(reversed(picked))
        group["got"] = sum(1 for account in group["accounts"] if account["ok"])
        if group["accounts"]:
            group["at"] = group["accounts"][0]["at"]
        group["sum"] = sum_text(item["amount"] for item in group["accounts"] if item["ok"] and item["amount"])
    return groups


assert _group_claims([
    {"id": 2, "chat_id": 9, "msg_id": 3, "user_id": 2, "name": "乙", "ok": 0, "amount": "", "detail": "已抢完", "elapsed_ms": 400, "place": "群", "at": "t2", "kind": "direct", "wallet": "kpay"},
    {"id": 1, "chat_id": 9, "msg_id": 3, "user_id": 1, "name": "甲", "ok": 1, "amount": "1.00 USDT", "detail": "", "elapsed_ms": 200, "place": "群", "at": "t1", "kind": "direct", "wallet": "kpay"},
], 80) == [{
    "place": "群",
    "chat_id": 9,
    "msg_id": 3,
    "link": "",
    "at": "t1",
    "kind": "direct",
    "wallet": "kpay",
    "got": 1,
    "sum": "1 USDT",
    "accounts": [
        {"user_id": 1, "name": "甲", "ok": True, "amount": "1.00 USDT", "detail": "", "elapsed_ms": 200, "at": "t1"},
        {"user_id": 2, "name": "乙", "ok": False, "amount": "", "detail": "已抢完", "elapsed_ms": 400, "at": "t2"},
    ],
}]
assert len(_group_claims([
    {"id": 1, "chat_id": 0, "msg_id": 0, "user_id": 1, "name": "甲", "ok": 1, "amount": "", "detail": "", "elapsed_ms": 0, "place": "", "at": "t1"},
    {"id": 2, "chat_id": 0, "msg_id": 0, "user_id": 1, "name": "甲", "ok": 1, "amount": "", "detail": "", "elapsed_ms": 0, "place": "", "at": "t2"},
], 80)) == 2
assert _group_claims([
    {"id": 2, "chat_id": 9, "msg_id": 3, "user_id": 1, "name": "甲", "ok": 0, "amount": "", "detail": "已领取过", "elapsed_ms": 200, "place": "群", "at": "t2", "kind": "webapp", "wallet": "pcyl"},
    {"id": 1, "chat_id": 9, "msg_id": 3, "user_id": 1, "name": "甲", "ok": 0, "amount": "", "detail": "领取失败", "elapsed_ms": 600, "place": "群", "at": "t1", "kind": "webapp", "wallet": "pcyl"},
], 80)[0]["accounts"] == [
    {"user_id": 1, "name": "甲", "ok": False, "amount": "", "detail": "已领取过", "elapsed_ms": 200, "at": "t2"},
]
assert _group_claims([{
    "id": 1, "chat_id": -1001234567890, "msg_id": 566, "user_id": 1, "name": "甲",
    "ok": 1, "amount": "1 USDT", "detail": "", "elapsed_ms": 1, "place": "群",
    "at": "t", "kind": "direct", "wallet": "", "username": "",
}], 80)[0]["link"] == "https://t.me/c/1234567890/566"


_SKIP_REASONS = ("floor", "list", "busy")


def _beijing_day() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _beijing_start() -> str:
    start = datetime.now(ZoneInfo("Asia/Shanghai")).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat()


def list_claims(owner_id: int, limit: int = 80) -> list[dict]:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return []
    try:
        found = conn.execute(
            """
            SELECT c.id, c.user_id, c.name, c.kind, c.wallet, c.ok, c.amount, c.detail, c.elapsed_ms,
                   c.chat_id, c.msg_id, c.at,
                   COALESCE(
                       NULLIF(f.title, ''),
                       (
                           SELECT title FROM chat_face
                           WHERE c.chat_id = 0 AND username != '' AND username = c.place AND title != ''
                           LIMIT 1
                       ),
                       ''
                   ),
                   COALESCE(f.username, '')
            FROM claim c
            LEFT JOIN chat_face f ON c.chat_id != 0 AND f.chat_id = c.chat_id
            WHERE c.owner_id = ? ORDER BY c.id DESC LIMIT 200
            """,
            (int(owner_id),),
        ).fetchall()
    finally:
        conn.close()
    rows = [
        {
            "id": int(row[0]),
            "user_id": int(row[1]),
            "name": row[2],
            "kind": row[3] or "",
            "wallet": row[4] or "",
            "ok": bool(row[5]),
            "amount": row[6],
            "detail": row[7],
            "elapsed_ms": int(row[8] or 0),
            "chat_id": int(row[9] or 0),
            "msg_id": int(row[10] or 0),
            "at": row[11],
            "place": row[12] or "",
            "username": row[13] or "",
        }
        for row in found
    ]
    return _group_claims(rows, max(1, min(int(limit), 80)))


def today_text(owner_id: int) -> str:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return ""
    try:
        found = conn.execute(
            "SELECT amount FROM claim WHERE owner_id = ? AND ok = 1 AND at >= ?",
            (int(owner_id), _beijing_start()),
        ).fetchall()
    finally:
        conn.close()
    return sum_text(row[0] for row in found)


def save_skip(item: dict) -> None:
    reason = str(item.get("reason") or "")
    if reason not in _SKIP_REASONS:
        return
    try:
        owner_id = int(item.get("owner_id") or 0)
    except (TypeError, ValueError):
        return
    if not owner_id:
        return
    detail = " ".join(str(item.get("detail") or "").split())[:40]
    day = _beijing_day()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute(
            """
            INSERT INTO claim_skip (owner_id, day, reason, n) VALUES (?, ?, ?, 1)
            ON CONFLICT(owner_id, day, reason) DO UPDATE SET n = n + 1
            """,
            (owner_id, day, reason),
        )
        if detail:
            conn.execute(
                "INSERT INTO claim_skip_note (owner_id, day, reason, detail, at) VALUES (?, ?, ?, ?, ?)",
                (owner_id, day, reason, detail, now),
            )
            kept = conn.execute(
                "SELECT id FROM claim_skip_note WHERE owner_id = ? AND day = ? ORDER BY id DESC LIMIT 5",
                (owner_id, day),
            ).fetchall()
            ids = [int(row[0]) for row in kept]
            if ids:
                marks = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM claim_skip_note WHERE owner_id = ? AND day = ? AND id NOT IN ({marks})",
                    (owner_id, day, *ids),
                )
        conn.execute("DELETE FROM claim_skip WHERE day < ?", (day,))
        conn.execute("DELETE FROM claim_skip_note WHERE day < ?", (day,))
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def skip_board(owner_id: int) -> dict:
    empty = {"floor": 0, "list": 0, "busy": 0, "notes": []}
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return empty
    day = _beijing_day()
    try:
        counts = conn.execute(
            "SELECT reason, n FROM claim_skip WHERE owner_id = ? AND day = ?",
            (int(owner_id), day),
        ).fetchall()
        notes = conn.execute(
            """
            SELECT reason, detail FROM claim_skip_note
            WHERE owner_id = ? AND day = ? ORDER BY id DESC LIMIT 5
            """,
            (int(owner_id), day),
        ).fetchall()
    finally:
        conn.close()
    board = {"floor": 0, "list": 0, "busy": 0}
    for reason, count in counts:
        if reason in board:
            board[reason] = int(count or 0)
    board["notes"] = [{"reason": reason, "detail": detail} for reason, detail in notes]
    return board


def begin_expiry(owner_id: int, user_id: int, note: str) -> bool:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return False
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO session_notice (user_id, owner_id, note, at) VALUES (?, ?, ?, ?)",
            (int(user_id), int(owner_id), str(note or "")[:80], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return False
    finally:
        conn.close()


def clear_expiry(user_id: int) -> None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        conn.execute("DELETE FROM session_notice WHERE user_id = ?", (int(user_id),))
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
    finally:
        conn.close()


def _next_nodes(known: set[str], have: list[str], empty: list[str]) -> set[str] | None:
    nxt = (set(known) - set(empty)) | set(have)
    if nxt == known:
        return None
    return nxt


assert _next_nodes({"mia"}, ["mia"], []) is None
assert _next_nodes(set(), ["mia"], []) == {"mia"}
assert _next_nodes({"mia", "ams"}, [], ["ams"]) == {"mia"}


def _load_used() -> dict[int, set[str]] | None:
    global _used
    if _used is not None:
        return _used
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        rows = conn.execute("SELECT owner_id, node_id FROM owner_node").fetchall()
    finally:
        conn.close()
    found: dict[int, set[str]] = {}
    for owner_id, node_id in rows:
        found.setdefault(int(owner_id), set()).add(str(node_id))
    _used = found
    return _used


def note_nodes(owner_id: int, have: list[str], empty: list[str]) -> None:
    global _used
    known_map = _load_used()
    if known_map is None:
        return
    owner_id = int(owner_id)
    known = known_map.get(owner_id, set())
    nxt = _next_nodes(known, have, empty)
    if nxt is None:
        return
    added = nxt - known
    removed = known - nxt
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return
    try:
        if added:
            conn.executemany(
                "INSERT OR IGNORE INTO owner_node (owner_id, node_id) VALUES (?, ?)",
                [(owner_id, node_id) for node_id in added],
            )
        if removed:
            marks = ",".join("?" for _ in removed)
            conn.execute(
                f"DELETE FROM owner_node WHERE owner_id = ? AND node_id IN ({marks})",
                (owner_id, *removed),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return
    finally:
        conn.close()
    if _used is None:
        _used = {}
    _used[owner_id] = nxt


def used_nodes(owner_id: int) -> set[str]:
    known_map = _load_used()
    if known_map is None:
        return set()
    return set(known_map.get(int(owner_id), ()))


def _face_md5(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) != 32 or any(ch not in "0123456789abcdef" for ch in text):
        return ""
    return text


def read_face(peer: int) -> tuple[int, str, int] | None:
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return None
    try:
        row = conn.execute(
            "SELECT photo_id, md5, checked_at FROM face_photo WHERE peer = ?",
            (int(peer),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), _face_md5(str(row[1] or "")), int(row[2])


def write_face(peer: int, photo_id: int, md5: str, checked_at: int) -> bool:
    digest = _face_md5(md5)
    if md5 and not digest:
        return False
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return False
    try:
        conn.execute(
            """
            INSERT INTO face_photo (peer, photo_id, md5, checked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(peer) DO UPDATE SET
                photo_id = excluded.photo_id,
                md5 = excluded.md5,
                checked_at = excluded.checked_at
            """,
            (int(peer), int(photo_id), digest, int(checked_at)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.warning("sqlite write failed")
        return False
    finally:
        conn.close()
    return True


def face_used(md5: str, peer: int) -> bool:
    digest = _face_md5(md5)
    if not digest:
        return False
    try:
        conn = _connect()
    except OSError as exc:
        log.warning("sqlite open %s", type(exc).__name__)
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM face_photo WHERE md5 = ? AND peer != ? LIMIT 1",
            (digest, int(peer)),
        ).fetchone()
    finally:
        conn.close()
    return row is not None
