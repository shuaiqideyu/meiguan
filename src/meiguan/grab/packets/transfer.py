"""OKPay 转账收款（自动点「收款」按钮）。"""

from meiguan.grab.catalog import plug
from meiguan.grab.chat import claim_direct
from meiguan.grab.types import RedPacket, Result


def match(rp: RedPacket) -> bool:
    return bool(rp.data) and rp.data.lower().startswith(b"transferil--")


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    from meiguan.grab.loop import policy_of

    if not policy_of(user_id).get("auto_receive", True):
        return Result(False, retryable=False, feedback="节点：自动收款已关闭")
    return await claim_direct(client, user_id, event, chat_id, msg_id, rp)


plug("transfer", "转账收款", "direct", match, claim, 5, 40)
