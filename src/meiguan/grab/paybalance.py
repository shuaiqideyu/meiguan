"""查询打码账户余额。只返回金额，不记录密钥。"""

import hashlib
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

import aiohttp

_URL = {
    "fll": "https://api.2captcha.com/getBalance",
    "cf": "https://api.capmonster.cloud/getBalance",
}
_cache: dict[str, tuple[float, str, str]] = {}
_TTL = 60


def show_balance(value) -> str:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return ""
    if not number.is_finite():
        return ""
    quant = number.quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
    text = format(quant, "f")
    whole, frac = text.split(".")
    frac = frac.rstrip("0")
    if len(frac) < 2:
        frac = frac.ljust(2, "0")
    return f"{whole}.{frac}"


async def ask_balance(bucket: str, key: str) -> tuple[str, str]:
    secret = "".join(str(key or "").split())
    if bucket not in _URL or not secret:
        return "empty", ""
    cache_id = bucket + ":" + hashlib.sha256(secret.encode()).hexdigest()[:16]
    now = time.monotonic()
    hit = _cache.get(cache_id)
    if hit and now - hit[0] < _TTL:
        return hit[1], hit[2]
    state, text = "down", ""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_URL[bucket], json={"clientKey": secret}) as resp:
                data = await resp.json(content_type=None)
        if isinstance(data, dict) and data.get("errorId") in (0, "0") and "balance" in data:
            text = show_balance(data.get("balance"))
            state = "ok" if text else "down"
        else:
            code = str((data or {}).get("errorCode") or "") if isinstance(data, dict) else ""
            state = "invalid" if "KEY" in code else "down"
    except Exception:
        state, text = "down", ""
    if state in ("ok", "invalid"):
        _cache[cache_id] = (now, state, text)
    return state, text


def _self_check() -> None:
    assert show_balance(0) == "0.00"
    assert show_balance(19.2) == "19.20"
    assert show_balance("345.678") == "345.678"
    assert show_balance("0.93958") == "0.93958"
    assert show_balance("") == ""
    assert show_balance(None) == ""


_self_check()
