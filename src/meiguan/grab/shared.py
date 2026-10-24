"""共享抢包。开启后加入其他操作者看到的公开窗口再领。"""

import asyncio
import logging
import time

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChannelsTooMuchError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.tl.functions.channels import JoinChannelRequest

log = logging.getLogger("meiguan.grab")
_next_join: dict[int, float] = {}


def schedule(owners: list[int], chat_id: int, msg_id: int, username: str, bots: list[str], kind: str, invite: str = "") -> None:
    from meiguan.node_host import accounts, clients

    want = {int(item) for item in owners}
    for user_id, client in list(clients.items()):
        owner = int((accounts.get(str(user_id)) or {}).get("owner_id") or 0)
        if owner not in want or not client.is_connected():
            continue
        asyncio.create_task(_run(client, user_id, chat_id, msg_id, username, bots, kind, invite))


async def _run(client, user_id: int, chat_id: int, msg_id: int, username: str, bots: list[str], kind: str, invite: str = "") -> None:
    from meiguan.grab.loop import paused, policy_of, safe
    from meiguan.policy import account_joins, account_paused, allow, hours_open, mode_key
    from meiguan.reports import hits

    try:
        policy = policy_of(user_id)
        if not hours_open(policy):
            return
        joining = account_joins(policy, user_id)
        if account_paused(policy, user_id) and not joining:
            return
        names = {str(item) for item in bots if item}
        blocked = hits(names, chat_id, username)
        if blocked or not allow(policy, chat_id=chat_id, username=username, bots=names):
            return
        peer = await open_chat(client, user_id, chat_id, username, invite, joining)
        if peer is None:
            return
        modes = policy.get("modes") or {}
        if paused(user_id) or account_paused(policy, user_id) or not modes.get(mode_key(kind)):
            return
        msg = await asyncio.wait_for(client.get_messages(peer, ids=msg_id), timeout=20)
        if msg is None:
            return

        class _Event:
            foreign = True

            def __init__(self):
                self.message = msg
                self.chat_id = chat_id
                self._username = username
                self._bots = names

            async def get_input_chat(self):
                return peer

        await safe(client, user_id, _Event(), kind)
    except Exception as exc:
        log.info("节点：共享领取失败 user=%s %s", user_id, type(exc).__name__)


async def open_chat(client, user_id: int, chat_id: int, username: str, invite: str = "", join: bool = False):
    from meiguan.reports import entity_watched, hits

    if hits(chat_id=chat_id, username=username):
        return None
    try:
        return await asyncio.wait_for(client.get_input_entity(chat_id), timeout=12)
    except Exception:
        pass
    name = (username or "").strip().lstrip("@")
    if name:
        try:
            entity = await asyncio.wait_for(client.get_entity(name), timeout=12)
            if entity_watched(entity):
                return None
            return await asyncio.wait_for(client.get_input_entity(entity), timeout=12)
        except Exception:
            pass
    if not join:
        return None
    # ponytail: 每个号成功加入后至少隔 20 秒，FloodWait 按秒数推迟，避免连加群
    if time.time() < _next_join.get(user_id, 0):
        return None
    if invite:
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

        from meiguan.reports import entity_watched, watched

        try:
            preview = await asyncio.wait_for(client(CheckChatInviteRequest(invite)), timeout=12)
        except Exception:
            preview = None
        if preview is not None and (
            watched(getattr(preview, "title", None)) or entity_watched(getattr(preview, "chat", None))
        ):
            return None
        from meiguan.node_roster import touch_active

        touch_active(user_id)
        try:
            await asyncio.wait_for(client(ImportChatInviteRequest(invite)), timeout=20)
        except UserAlreadyParticipantError:
            pass
        except InviteRequestSentError:
            log.info("节点：已申请加入 user=%s", user_id)
            return None
        except FloodWaitError as exc:
            _next_join[user_id] = time.time() + int(exc.seconds)
            return None
        except Exception as exc:
            log.info("节点：加入窗口失败 user=%s %s", user_id, type(exc).__name__)
            return None
        _next_join[user_id] = time.time() + 20
        try:
            return await asyncio.wait_for(client.get_input_entity(chat_id), timeout=12)
        except Exception:
            return None
    if not name:
        return None
    try:
        entity = await asyncio.wait_for(client.get_entity(name), timeout=12)
    except Exception:
        entity = None
    if entity_watched(entity):
        return None
    from meiguan.node_roster import touch_active

    touch_active(user_id)
    try:
        await asyncio.wait_for(client(JoinChannelRequest(entity or name)), timeout=20)
    except UserAlreadyParticipantError:
        pass
    except FloodWaitError as exc:
        _next_join[user_id] = time.time() + int(exc.seconds)
        log.info("节点：加入窗口受限 user=%s 等待 %s 秒", user_id, int(exc.seconds))
        return None
    except ChannelsTooMuchError:
        _next_join[user_id] = time.time() + 6 * 3600
        log.info("节点：加入窗口已满 user=%s", user_id)
        return None
    except (InviteRequestSentError, ChannelPrivateError, ChannelInvalidError, UserBannedInChannelError, ValueError):
        log.info("节点：加入窗口失败 user=%s", user_id)
        return None
    except Exception as exc:
        log.info("节点：加入窗口失败 user=%s %s", user_id, type(exc).__name__)
        return None
    _next_join[user_id] = time.time() + 20
    try:
        return await asyncio.wait_for(client.get_input_entity(name), timeout=12)
    except Exception:
        return None
