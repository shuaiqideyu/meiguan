"""表情缓存。"""

import asyncio
import hashlib
import json
import logging

from telethon import TelegramClient
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
from telethon.tl.types import MessageEntityCustomEmoji

from meikai.charset import buttons_are_digit, buttons_keep_case
from meikai.grab.detect import expr_line, raw_text
from meikai.grab.vision import glyph_prompt, parse_glyph, vision_chat, vision_settings, warn_once

log = logging.getLogger("meikai.grab")

_glyphs: dict[str, str] = {}


_glyph_pending: dict[str, str] = {}


_glyph_locks: dict[str, asyncio.Lock] = {}


_doc_char: dict[int, str] = {}


_doc_hash: dict[int, str] = {}


_doc_pending: dict[str, str] = {}


async def glyph_of(thumb: bytes, digest: str, want_digit: bool | None, keep_case: bool = False) -> str:
    load_glyphs()
    cached = _glyphs.get(digest)
    if cached:
        return cached
    lock = _glyph_locks.setdefault(digest, asyncio.Lock())
    async with lock:
        cached = _glyphs.get(digest)
        if cached:
            return cached
        if not vision_settings()[0]:
            warn_once("glyph-key", "节点：窗口验证码表情未配置视觉识别")
            return ""
        text = await vision_chat(thumb, glyph_prompt(want_digit, keep_case), "image/jpeg", "", 10)
        char = parse_glyph(text, want_digit, keep_case)
        if char:
            store_glyph(digest, char)
        return char


def glyph_file():
    from pathlib import Path

    from meikai.node_host import SESSIONS
    return Path(SESSIONS) / "glyphs.json"


_glyphs_loaded = False


def load_glyphs() -> None:
    global _glyphs_loaded
    if _glyphs_loaded:
        return
    _glyphs_loaded = True
    path = glyph_file()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict):
        for key, value in data.items():
            digest = str(key or "")
            char = str(value or "")
            if digest and char:
                _glyphs[digest] = char


def save_glyphs() -> None:
    path = glyph_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_glyphs, ensure_ascii=False, separators=(",", ":")) + "\n")
    path.chmod(0o600)


_glyph_hook = None


_glyph_dirty = False


_doc_dirty = False


def bind_glyph_change(fn) -> None:
    global _glyph_hook
    _glyph_hook = fn


def note_glyph() -> None:
    global _glyph_dirty
    _glyph_dirty = True
    _kick_glyph()


def note_doc() -> None:
    global _doc_dirty
    _doc_dirty = True
    _kick_glyph()


def _kick_glyph() -> None:
    global _glyph_dirty, _doc_dirty
    hook = _glyph_hook
    if hook is None:
        if _glyph_dirty:
            save_glyphs()
        if _doc_dirty:
            save_docs()
        _glyph_dirty = False
        _doc_dirty = False
        return
    hook()


def take_glyph_snapshot() -> tuple[dict | None, dict | None, dict[str, str], dict[str, str]]:
    global _glyph_dirty, _doc_dirty
    glyphs = dict(_glyphs) if _glyph_dirty else None
    docs = {str(doc_id): char for doc_id, char in _doc_char.items() if char} if _doc_dirty else None
    _glyph_dirty = False
    _doc_dirty = False
    return glyphs, docs, pending_glyphs(), pending_docs()


def glyph_dirty() -> bool:
    return _glyph_dirty or _doc_dirty


def store_glyph(digest: str, char: str, replace: bool = False) -> None:
    if not digest or not char:
        return
    current = _glyphs.get(digest)
    if current == char or (current and not replace):
        return
    _glyphs[digest] = char
    _glyph_pending[digest] = char
    log.info("节点：表情缓存 %s %s", digest[:8], char)
    note_glyph()


def forget_glyph(digest: str) -> None:
    if not digest:
        return
    _glyphs.pop(digest, None)
    _glyph_pending[digest] = ""
    note_glyph()


def drop_glyphs(mapping: dict | None) -> None:
    if not mapping:
        return


def pending_glyphs() -> dict[str, str]:
    return dict(_glyph_pending)


def pending_docs() -> dict[str, str]:
    return dict(_doc_pending)


def apply_remote_glyphs(incoming: dict) -> None:
    if not incoming:
        return
    clean: dict[str, str] = {}
    for key, value in incoming.items():
        digest = str(key or "").strip().lower()
        char = str(value or "").strip()
        if digest and char:
            clean[digest] = char
    for digest, char in list(_glyph_pending.items()):
        if clean.get(digest, "") == char:
            _glyph_pending.pop(digest, None)
    changed = False
    for digest, char in clean.items():
        if _glyphs.get(digest) == char and digest not in _glyph_pending:
            continue
        _glyphs[digest] = char
        _glyph_pending[digest] = char
        changed = True
    if changed:
        note_glyph()


def doc_file():
    from pathlib import Path

    from meikai.node_host import SESSIONS
    return Path(SESSIONS) / "docs.json"


_docs_loaded = False


def load_docs() -> None:
    global _docs_loaded
    if _docs_loaded:
        return
    _docs_loaded = True
    path = doc_file()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        text = str(key or "").strip()
        char = str(value or "").strip()
        if text.isdigit() and char:
            _doc_char[int(text)] = char


def save_docs() -> None:
    path = doc_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {str(doc_id): char for doc_id, char in _doc_char.items() if char}
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def apply_remote_docs(incoming: dict) -> None:
    if not incoming:
        return
    clean: dict[str, str] = {}
    for key, value in incoming.items():
        text = str(key or "").strip()
        char = str(value or "").strip()
        if text.isdigit() and char:
            clean[text] = char
    for doc_id, char in list(_doc_pending.items()):
        if clean.get(doc_id, "") == char:
            _doc_pending.pop(doc_id, None)
    changed = False
    for doc_id, char in clean.items():
        number = int(doc_id)
        if _doc_char.get(number) == char and doc_id not in _doc_pending:
            continue
        _doc_char[number] = char
        _doc_pending[doc_id] = char
        changed = True
    if changed:
        note_doc()


def remember_glyph(doc_id: int, digest: str, char: str, replace: bool = False) -> None:
    if digest:
        _doc_hash[doc_id] = digest
    if not char:
        return
    if replace or doc_id not in _doc_char:
        _doc_char[doc_id] = char
        _doc_pending[str(doc_id)] = char
        note_doc()
    store_glyph(digest, char, replace=replace)


def apply_corrections(emoji_map: dict | None, corrections: dict) -> None:
    if not emoji_map or not corrections:
        return
    pending = dict(corrections)
    for doc_id, char in emoji_map.items():
        fixed = pending.get(char)
        if not fixed:
            continue
        digest = _doc_hash.get(doc_id)
        if digest:
            store_glyph(digest, fixed, replace=True)
        _doc_char[doc_id] = fixed
        _doc_pending[str(doc_id)] = fixed
        note_doc()
        del pending[char]
        if not pending:
            break


def utf16_to_py(text: str) -> list[int]:
    table = []
    for index, ch in enumerate(text):
        table.append(index)
        if ord(ch) > 0xFFFF:
            table.append(index)
    return table


async def decode_expr(client: TelegramClient, msg, buttons, force: bool = False) -> tuple[str | None, dict]:
    body = raw_text(msg)
    marker = body.rfind("💡")
    if marker < 0:
        line = expr_line(body)
        marker = body.rfind(line) if line else -1
    if marker < 0:
        return None, {}
    marker_u = 0
    for ch in body[:marker]:
        marker_u += 2 if ord(ch) > 0xFFFF else 1
    entities = [
        (entity.offset, entity.length, entity.document_id)
        for entity in (getattr(msg, "entities", None) or [])
        if isinstance(entity, MessageEntityCustomEmoji) and entity.offset >= marker_u
    ]
    if not entities:
        return None, {}
    labels = [label for label, _data in buttons] if buttons else []
    want = buttons_are_digit(labels) if labels else None
    keep_case = buttons_keep_case(labels)
    mapping: dict[int, str] = {}
    missing = []
    for _offset, _length, doc_id in entities:
        if not force and doc_id in _doc_char:
            mapping[doc_id] = _doc_char[doc_id]
        else:
            missing.append(doc_id)
    if missing:
        try:
            docs = await client(GetCustomEmojiDocumentsRequest(document_id=list(set(missing))))
        except Exception as exc:
            log.info("Telegram：表情文档获取失败 %s", type(exc).__name__)
            return None, mapping
        thumbs = await asyncio.gather(*(download_thumb(client, doc) for doc in docs))
        ready = [item for item in thumbs if item[1] and item[2]]

        async def identify(item):
            doc_id, digest, blob = item
            char = await glyph_of(blob, digest, want, keep_case)
            return doc_id, digest, char

        found_chars = list(await asyncio.gather(*(identify(item) for item in ready)))
        missed = [item for item, (_doc, _digest, char) in zip(ready, found_chars) if not char]
        if missed:
            again = await asyncio.gather(*(identify(item) for item in missed))
            by_id = {doc_id: (digest, char) for doc_id, digest, char in found_chars}
            for doc_id, digest, char in again:
                if char:
                    by_id[doc_id] = (digest, char)
            found_chars = [(doc_id, digest, char) for doc_id, (digest, char) in by_id.items()]
        for doc_id, digest, char in found_chars:
            remember_glyph(doc_id, digest, char)
            if char:
                mapping[doc_id] = char
    if not mapping:
        return None, {}
    table = utf16_to_py(body)
    chars = list(body)
    for offset, length, doc_id in sorted(entities, key=lambda item: item[0], reverse=True):
        char = mapping.get(doc_id)
        if not char:
            continue
        start = table[offset] if offset < len(table) else len(body)
        end_at = offset + length
        end = table[end_at] if end_at < len(table) else len(body)
        chars[start:end] = list(char)
    return "".join(chars), mapping


async def download_thumb(client: TelegramClient, doc) -> tuple[int, str | None, bytes | None]:
    if not getattr(doc, "thumbs", None):
        return doc.id, None, None
    try:
        blob = await client.download_media(doc, file=bytes, thumb=-1)
    except Exception as exc:
        log.info("Telegram：表情缩略图下载失败 %s", type(exc).__name__)
        return doc.id, None, None
    if not blob:
        return doc.id, None, None
    return doc.id, hashlib.md5(blob).hexdigest(), blob


async def emoji_info(client: TelegramClient, msg) -> list[dict]:
    load_glyphs()
    entities = [
        entity for entity in (getattr(msg, "entities", None) or [])
        if isinstance(entity, MessageEntityCustomEmoji)
    ]
    if not entities:
        return []
    try:
        docs = await client(GetCustomEmojiDocumentsRequest(
            document_id=list({entity.document_id for entity in entities}),
        ))
    except Exception as exc:
        log.info("Telegram：表情文档获取失败 %s", type(exc).__name__)
        return []
    thumbs = await asyncio.gather(*(download_thumb(client, doc) for doc in docs))
    found = []
    for doc_id, digest, _blob in thumbs:
        if not digest:
            continue
        found.append({"doc_id": doc_id, "md5": digest, "char": _glyphs.get(digest, "")})
    return found
