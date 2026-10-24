"""Kpay 按钮关键词。"""

from meiguan.grab.catalog import plug
from meiguan.grab.chat import claim_kpay


def match(rp) -> bool:
    return (rp.bot or "").lower() == "kpay"


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    return await claim_kpay(client, user_id, event, chat_id, msg_id, rp)


plug("kpay", "Kpay", "direct", match, claim, 5, 40)
