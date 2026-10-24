"""私信里的验证码。"""

from meikai.grab.catalog import plug
from meikai.grab.chat import claim_dm


def match(rp) -> bool:
    return bool(rp.start)


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_dm(client, user_id, rp)


plug("captcha-dm", "私信验证码", "captcha", match, claim, 10, 40)
