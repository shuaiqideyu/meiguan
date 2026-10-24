"""红包模块登记。管理页只显示四类，不列出模块名。"""

import asyncio
import importlib
import inspect
import pkgutil

_PLUGS: list[dict] = []
_LOADED = False


def plug(module_id: str, title: str, kind: str, match, claim, order: int, seconds: float) -> None:
    _PLUGS[:] = [item for item in _PLUGS if item["id"] != module_id]
    _PLUGS.append({
        "id": module_id,
        "title": title,
        "kind": kind,
        "order": order,
        "match": match,
        "claim": claim,
        "seconds": seconds,
    })


def load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import meiguan.grab.packets as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"meiguan.grab.packets.{info.name}")


# 匹配面盖住一整类。认得出机器人或参数的模块先试。
_BROAD = frozenset({
    "captcha-dm",
    "captcha",
    "web-dm-page",
    "web-dm",
    "web-page",
    "web-app",
    "web",
    "direct",
    "phrase",
    "yypay",
})

# 分类成验证码、实际按网页接口领的模块。
_RETARGET = frozenset({
    "fll",
    "bmzy",
    "fugui",
    "sfyl",
    "pcyl",
    "jbyl",
    "kkpoker",
    "web-pic",
})


def listing() -> list[dict]:
    load()
    rows = sorted(_PLUGS, key=lambda item: (item["order"], item["id"]))
    return [
        {"id": item["id"], "title": item["title"], "kind": "webapp" if item["kind"] == "cloudflare" else item["kind"]}
        for item in rows
    ]


def _ranked() -> list[dict]:
    load()

    def key(item: dict) -> tuple:
        broad = 1 if item["id"] in _BROAD else 0
        return (broad, item["order"], item["id"])

    return sorted(_PLUGS, key=key)


def _first(kind: str, rp) -> dict | None:
    for item in _ranked():
        if item["kind"] != kind or not item["match"](rp):
            continue
        return item
    return None


def apply_kind(rp):
    """普通 start 链接被分成验证码时，改成专模块登记的种类。"""
    if rp is None:
        return None
    for item in _ranked():
        if item["id"] in _BROAD or item["kind"] != rp.kind:
            continue
        if item["match"](rp):
            return rp
    if rp.kind != "captcha":
        return rp
    for item in _ranked():
        if item["id"] not in _RETARGET or item["kind"] == "captcha":
            continue
        if item["match"](rp):
            rp.kind = item["kind"]
            return rp
    return rp


def limit_for(rp) -> float | None:
    found = _first(rp.kind, rp)
    if found is None:
        return None
    return float(found["seconds"])


_GROUP_EXEMPT = {"okpay", "kkpay", "dlqb", "kpay"}  # AI 识图网页验证，不在群也抢


def _need_in_group(rp) -> bool:
    """无成本（算式/AI识图）不在群也试；打码类（cloudflare/hc）不在群抢不了。"""
    kind = getattr(rp, "kind", "")
    bot = (getattr(rp, "bot", "") or "").lower().lstrip("@")
    if kind == "phrase":
        return True
    if kind == "captcha":
        return bot == "nlhbbot"  # 奶龙（Turnstile 打码）要查；算式类不查
    if kind == "webapp":
        return bot not in _GROUP_EXEMPT
    return False


async def _in_group(client, user_id: int, chat_id: int) -> bool:
    if not chat_id:
        return True
    from telethon.errors import UserNotParticipantError

    try:
        await client.get_participant(chat_id, user_id)
        return True
    except UserNotParticipantError:
        return False
    except Exception:
        return True


async def _register(client, rp) -> None:
    """领取前点「一键注册」按钮：给注册 bot 发 /start。"""
    bot = getattr(rp, "register_bot", None)
    if not bot:
        return
    start = (getattr(rp, "register_start", None) or "").strip()
    try:
        entity = await client.get_entity(bot)
        await client.send_message(entity, f"/start {start}" if start else "/start")
        await asyncio.sleep(2)
    except Exception:
        pass


async def claim_kind(kind: str, client, user_id: int, rp, event, chat_id: int, msg_id: int = 0):
    found = _first(kind, rp)
    if found is None:
        return None
    if _need_in_group(rp) and not await _in_group(client, user_id, chat_id):
        from meiguan.grab.types import Result

        return Result(False, retryable=False, feedback="节点：不在群")
    await _register(client, rp)
    claim = found["claim"]
    kwargs = {"event": event, "chat_id": chat_id}
    if "msg_id" in inspect.signature(claim).parameters:
        kwargs["msg_id"] = msg_id
    return await claim(client, user_id, rp, **kwargs)


def _self_check() -> None:
    from meiguan.grab.types import RedPacket

    nl = apply_kind(RedPacket("captcha", start="grab_abc_v1", bot="nlhbbot", place="dm"))
    assert nl.kind == "captcha" and _first("captcha", nl)["id"] == "nlhb"
    ferr = apply_kind(RedPacket("captcha", start="pkt_hash=ab12", bot="fllqb", place="dm"))
    assert ferr.kind == "webapp" and _first("webapp", ferr)["id"] == "fll"
    plain = apply_kind(RedPacket("captcha", start="abc", bot="somebot", place="dm"))
    assert plain.kind == "captcha" and _first("captcha", plain)["id"] == "captcha-dm"
    page = apply_kind(RedPacket("webapp", bot="fuguihbbot", page="https://receive.example/r", place="chat"))
    assert page.kind == "webapp" and _first("webapp", page)["id"] == "fugui"
    kept = apply_kind(RedPacket("direct", data=b"x", bot="kpay"))
    assert kept.kind == "direct" and _first("direct", kept)["id"] == "kpay"
    quiz = apply_kind(RedPacket("captcha", start="abc", bot="kpay", place="dm"))
    assert quiz.kind == "captcha" and _first("captcha", quiz)["id"] == "captcha-dm"
    web = RedPacket("webapp", bot="kpay", place="dm", start="web_abc")
    assert _first("webapp", web)["id"] == "web-kpay"
    gold = apply_kind(RedPacket("captcha", start="abc", bot="jbhongbao666_bot", place="dm"))
    assert gold.kind == "webapp" and _first("webapp", gold)["id"] == "jbyl"


_self_check()
