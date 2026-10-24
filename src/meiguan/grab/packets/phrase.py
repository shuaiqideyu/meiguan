"""口令红包。"""

from meiguan.grab.catalog import plug
from meiguan.grab.chat import claim_phrase


def match(rp) -> bool:
    return True


async def claim(client, user_id, rp, event=None, chat_id: int = 0, msg_id: int = 0):
    if user_id and rp.start and not rp.data:
        import asyncio

        from meiguan.grab.loop import policy_of
        from meiguan.policy import phrase_say_for, pick_delay

        bounds = phrase_say_for(policy_of(user_id))
        if bounds:
            wait = pick_delay(*bounds)
            if wait > 0:
                await asyncio.sleep(wait)
    return await claim_phrase(client, event, chat_id, msg_id, rp, user_id)


plug("phrase", "口令红包", "phrase", match, claim, 10, 40)
