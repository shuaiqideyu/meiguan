"""其余网页验证。"""

from meikai.grab.catalog import plug
from meikai.grab.webapp import claim_web


def match(rp) -> bool:
    return True


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_web(client, user_id, rp)


plug("web", "网页验证", "webapp", match, claim, 100, 75)
