"""炒群文案。内置每句 5 到 60 个字。自定义句子由外面传入。"""

import random
from pathlib import Path

PACKS = (
    ("xiehouyu", "歇后语"),
    ("riddle", "脑筋急转弯"),
    ("pun", "谐音梗"),
    ("joke", "段子"),
    ("flirt", "土味情话"),
    ("hello", "打招呼"),
)

MIN_LEN = 5
MAX_LEN = 60

_DIR = Path(__file__).resolve().parent / "hot_lines"
_CACHE: dict[str, list[str]] = {}
_IDS = {name for name, _title in PACKS}


def lines_for(packs, extra=None) -> list[str]:
    found = []
    for rows in _buckets(packs, extra):
        found.extend(rows)
    return found


def pick_line(packs, used: set[str] | None = None, extra=None) -> str:
    used = used or set()
    fresh = []
    stale = []
    for rows in _buckets(packs, extra):
        choices = [item for item in rows if item not in used]
        if choices:
            fresh.append(choices)
        else:
            stale.append(rows)
    pool = fresh or stale
    if not pool:
        return ""
    return random.choice(random.choice(pool))


def _buckets(packs, extra=None) -> list[list[str]]:
    found = []
    for name in packs or []:
        if name not in _IDS:
            continue
        rows = _load(name)
        if rows:
            found.append(rows)
    custom = []
    seen = set()
    for item in extra or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        custom.append(text)
    if custom:
        found.append(custom)
    return found


def _load(name: str) -> list[str]:
    cached = _CACHE.get(name)
    if cached is not None:
        return cached
    path = _DIR / f"{name}.txt"
    rows = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not (MIN_LEN <= len(text) <= MAX_LEN) or text in seen:
            continue
        if "答案：" in text or "答案:" in text:
            continue
        seen.add(text)
        rows.append(text)
    _CACHE[name] = rows
    return rows
