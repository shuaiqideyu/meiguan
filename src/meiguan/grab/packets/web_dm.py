"""私信里的网页验证。"""

from meiguan.grab.catalog import plug
from meiguan.grab.webapp import claim_web


def match(rp) -> bool:
    return rp.place == "dm"


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_web(client, user_id, rp)


plug("web-dm", "私信网页", "webapp", match, claim, 2, 75)
