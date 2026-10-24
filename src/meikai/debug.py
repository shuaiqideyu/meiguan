"""查看一条消息。"""

import re

from meikai.modes import LABELS
from meikai.node import NODES
from meikai.node_rpc import call_nodes

_KIND = dict(LABELS)
_LINK = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z][A-Za-z0-9_]{2,}))/(\d+)", re.I)


def parse_link(text: str) -> dict | None:
    matched = _LINK.search(text or "")
    if not matched:
        return None
    msg_id = int(matched.group(3))
    if matched.group(1):
        return {"chat_id": int("-100" + matched.group(1)), "msg_id": msg_id}
    return {"username": matched.group(2), "msg_id": msg_id}


async def peek_message(owner_id: int, text: str) -> dict:
    parsed = parse_link(text)
    if parsed is None:
        return {"ok": False, "error": "bad_link", "message": "主体：链接无法识别"}
    payload = {"op": "peek", "msg_id": parsed["msg_id"], "owner_id": int(owner_id)}
    if parsed.get("chat_id"):
        payload["chat_id"] = parsed["chat_id"]
    else:
        payload["username"] = parsed["username"]
    results = await call_nodes(payload, timeout=40)
    offline = []
    for node_id, result in results.items():
        if result.get("ok"):
            kind = result.get("kind")
            return {
                "ok": True,
                "node": NODES[node_id].city_zh,
                "date": result.get("date") or "",
                "text": str(result.get("text") or "")[:1500],
                "buttons": result.get("buttons") or [],
                "emojis": result.get("emojis") or [],
                "is_red": bool(result.get("is_red")),
                "state": result.get("state") or "",
                "kind": _KIND.get(kind) or "未识别",
                "wallet": result.get("wallet") or "",
                "via": result.get("via") or "",
                "via_id": int(result.get("via_id") or 0),
                "via_user_id": int(result.get("via_user_id") or 0),
                "via_bot_flag": bool(result.get("via_bot_flag")),
                "via_names": result.get("via_names") or [],
                "via_title": result.get("via_title") or "",
                "via_err": result.get("via_err") or "",
                "via_type": result.get("via_type") or "",
                "sender": result.get("sender") or "",
                "sender_id": int(result.get("sender_id") or 0),
                "sender_bot": bool(result.get("sender_bot")),
            }
        if result.get("error") == "offline":
            offline.append(NODES[node_id].city_zh)
    message = "节点：没有读到这条消息"
    if offline:
        message += "。未连接：" + "、".join(offline)
    return {"ok": False, "error": "not_found", "message": message}
