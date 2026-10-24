"""把窗口解析成 ID。"""

from meikai.login import _wait
from meikai.node_rpc import call_nodes


async def resolve_window(owner_id: int, parsed: dict) -> tuple[dict | None, str]:
    payload = {"op": "resolve", "owner_id": owner_id}
    if parsed.get("username"):
        payload["username"] = parsed["username"]
    else:
        payload["chat_id"] = int(parsed["chat_id"])
    results = await call_nodes(payload, timeout=8)
    found = None
    answered = False
    not_chat = False
    flood = None
    for result in results.values():
        if result.get("error") == "offline":
            continue
        answered = True
        if result.get("ok") and (result.get("chat_id") or result.get("kind") == "bot"):
            found = result
            break
        if result.get("error") == "not_chat":
            not_chat = True
        elif result.get("error") == "flood":
            flood = result
    if found:
        username = str(found.get("username") or parsed.get("username") or "").strip().lstrip("@")
        if found.get("kind") == "bot":
            title = str(found.get("title") or "").strip() or (f"@{username}" if username else str(found.get("user_id") or ""))
            out = {"kind": "bot", "user_id": int(found.get("user_id") or 0), "title": title}
            if username:
                out["username"] = username
            return out, ""
        chat_id = int(found["chat_id"])
        title = str(found.get("title") or "").strip() or str(chat_id)
        out = {"kind": "chat", "chat_id": chat_id, "title": title}
        if username:
            out["username"] = username
        return out, ""
    if parsed.get("chat_id") is not None and not parsed.get("username"):
        return {"kind": "chat", "chat_id": int(parsed["chat_id"]), "title": str(parsed["chat_id"])}, ""
    if flood:
        return None, f"Telegram：请求过于频繁，请 {_wait(int(flood.get('seconds') or 0))}后再试"
    if not_chat:
        return None, "主体：这是用户，不是窗口"
    if not answered:
        return None, "节点：未连接"
    return None, "节点：没有这个窗口"


def _found_row(found: dict) -> dict:
    kind = str(found.get("kind") or "group")
    if kind not in {"user", "group", "channel", "bot"}:
        kind = "group"
    number = int(found.get("chat_id") or 0)
    username = str(found.get("username") or "").strip().lstrip("@")
    title = str(found.get("title") or "").strip()[:40]
    row = {
        "kind": kind,
        "chat_id": number,
        "title": title or (f"@{username}" if username else str(number)),
        "username": username,
    }
    if kind in {"bot", "user"}:
        row["user_id"] = int(found.get("user_id") or number)
    return row


async def find_window(owner_id: int, parsed: dict) -> tuple[dict | None, str]:
    payload = {"op": "find", "owner_id": owner_id}
    if parsed.get("invite"):
        payload["invite"] = str(parsed["invite"])[:64]
    elif parsed.get("username"):
        payload["username"] = parsed["username"]
    else:
        payload["chat_id"] = int(parsed["chat_id"])
    results = await call_nodes(payload, timeout=16)
    found = None
    answered = False
    not_joined = False
    flood = None
    numeric = parsed.get("chat_id") is not None and not parsed.get("username") and not parsed.get("invite")
    for result in results.values():
        if result.get("error") == "offline":
            continue
        answered = True
        if result.get("ok") and result.get("chat_id"):
            found = result
            break
        if result.get("error") == "not_joined":
            not_joined = True
        elif result.get("error") == "flood":
            flood = result
    if found:
        return _found_row(found), ""
    if not_joined:
        return None, "节点：还没加入这个窗口"
    if flood:
        return None, f"Telegram：请求过于频繁，请 {_wait(int(flood.get('seconds') or 0))}后再试"
    if not answered:
        return None, "节点：未连接"
    if numeric:
        return None, "节点：没有这个窗口，这个号里还没有这个对话"
    return None, "节点：没有这个窗口"
