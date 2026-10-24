"""小程序网页验证。"""

from meikai.grab.catalog import plug
from meikai.grab.webapp import claim_app


def match(rp) -> bool:
    return bool(rp.short) or (rp.place == "chat" and bool(rp.start))


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_app(client, user_id, rp, event=event, chat_id=chat_id)


plug("web-app", "小程序", "webapp", match, claim, 80, 75)
