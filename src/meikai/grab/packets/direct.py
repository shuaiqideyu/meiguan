"""按钮关键词。"""

from meikai.grab.catalog import plug
from meikai.grab.chat import claim_direct


def match(rp) -> bool:
    return True


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_direct(client, user_id, event, chat_id, msg_id, rp)


plug("direct", "按钮关键词", "direct", match, claim, 10, 40)
