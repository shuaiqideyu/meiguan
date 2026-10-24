"""节点调用。"""

import asyncio
import json
import logging
import secrets

from meiguan.node import NODES, ssh_host

log = logging.getLogger(__name__)
_SSH = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
)

_up: set[str] = set()


def set_node_up(node_id: str, up: bool) -> None:
    if up:
        _up.add(node_id)
    else:
        _up.discard(node_id)


def node_up(node_id: str) -> bool:
    return node_id in _up


_links: dict[str, tuple] = {}
_waiters: dict[str, dict[str, asyncio.Future]] = {}


def attach_link(node_id: str, proc, lock: asyncio.Lock) -> None:
    _links[node_id] = (proc, lock)


def detach_link(node_id: str, proc) -> None:
    current = _links.get(node_id)
    if current is None or current[0] is not proc:
        return
    _links.pop(node_id, None)
    for fut in _waiters.pop(node_id, {}).values():
        if not fut.done():
            fut.set_result({"ok": False, "error": "offline"})


def deliver(node_id: str, msg: dict) -> bool:
    rid = str(msg.get("id") or "")
    fut = _waiters.get(node_id, {}).pop(rid, None)
    if fut is None or fut.done():
        return False
    fut.set_result(msg)
    return True


async def call_over_link(node_id: str, payload: dict, timeout: float) -> dict | None:
    link = _links.get(node_id)
    if link is None:
        return None
    proc, lock = link
    if proc.stdin is None:
        return None
    rid = secrets.token_hex(4)
    fut = asyncio.get_running_loop().create_future()
    _waiters.setdefault(node_id, {})[rid] = fut
    body = dict(payload)
    body["id"] = rid
    data = (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    async def _send() -> None:
        async with lock:
            proc.stdin.write(data)
            await proc.stdin.drain()

    try:
        await asyncio.wait_for(_send(), timeout)
        return await asyncio.wait_for(fut, timeout)
    except Exception:
        _waiters.get(node_id, {}).pop(rid, None)
        if not fut.done():
            fut.cancel()
        return {"ok": False, "error": "offline"}


async def _call_once(node_id: str, payload: dict, timeout: float) -> dict:
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_SSH,
            ssh_host(node_id),
            "/opt/meiguan/.venv/bin/python",
            "-m",
            "meiguan.rpc_client",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(data), timeout)
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        return {"ok": False, "error": "offline"}
    except OSError:
        return {"ok": False, "error": "offline"}
    text = stdout.decode(errors="replace").strip()
    if text:
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError:
            pass
    log.info("node %s rpc missed code=%s", node_id, proc.returncode)
    return {"ok": False, "error": "offline"}


async def call_node(node_id: str, payload: dict, timeout: float = 45) -> dict:
    linked = await call_over_link(node_id, payload, timeout)
    if linked is not None:
        return linked
    return await _call_once(node_id, payload, timeout)


async def call_nodes(payload: dict, timeout: float = 20) -> dict[str, dict]:
    async def one(node_id: str) -> tuple[str, dict]:
        return node_id, await call_node(node_id, payload, timeout=timeout)

    pairs = await asyncio.gather(*(one(node_id) for node_id in NODES))
    return dict(pairs)


_BRIDGE = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=8",
    "-o",
    "ControlMaster=no",
)


async def start_bridge(node_id: str):
    return await asyncio.create_subprocess_exec(
        *_BRIDGE,
        ssh_host(node_id),
        "/opt/meiguan/.venv/bin/python",
        "-m",
        "meiguan.rpc_bridge",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=4 * 1024 * 1024,
    )
