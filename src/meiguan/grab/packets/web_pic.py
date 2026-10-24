"""带图的网页验证。"""

from meiguan.grab.catalog import plug
from meiguan.grab.webapp import claim_app


def match(rp) -> bool:
    return (rp.short or "").lower() == "hbpic"


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_app(client, user_id, rp, event=event, chat_id=chat_id)


plug("web-pic", "图片验证", "webapp", match, claim, 70, 180)
