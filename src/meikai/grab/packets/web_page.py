"""窗口里带页面的网页验证。"""

from meikai.grab.catalog import plug
from meikai.grab.webapp import claim_chat_page


def match(rp) -> bool:
    return bool(rp.page)


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_chat_page(client, user_id, rp, event=event, chat_id=chat_id)


plug("web-page", "窗口页面", "webapp", match, claim, 3, 75)
