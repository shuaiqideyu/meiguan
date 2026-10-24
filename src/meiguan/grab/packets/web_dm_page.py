"""私信里带页面的网页验证。"""

from meiguan.grab.catalog import plug
from meiguan.grab.webapp import claim_simple


def match(rp) -> bool:
    return rp.place == "dm" and bool(rp.page)


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_simple(client, user_id, rp)


plug("web-dm-page", "私信页面", "webapp", match, claim, 1, 75)
