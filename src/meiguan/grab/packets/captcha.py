"""算式计算。"""

from meiguan.grab.catalog import plug
from meiguan.grab.chat import claim_captcha


def match(rp) -> bool:
    return True


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_captcha(client, event, chat_id, msg_id, rp)


plug("captcha", "算式计算", "captcha", match, claim, 20, 40)
