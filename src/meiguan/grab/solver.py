"""打码 token。管理员用节点上的系统密钥，其他人用自己填写的。"""

import contextvars
import os

from meiguan.reports import AUTHOR_ID

_owner: contextvars.ContextVar[int] = contextvars.ContextVar("solver_owner", default=0)
_ENV = {"fll": "TWOCAPTCHA_KEY", "cf": "CAPMONSTER_KEY"}


def bind_solver(user_id: int) -> None:
    from meiguan.node_host import accounts

    owner = int((accounts.get(str(int(user_id))) or {}).get("owner_id") or 0)
    _owner.set(owner)


def choose_key(owner: int, stored: str, system: str) -> str:
    if int(owner) == AUTHOR_ID:
        return system
    if not int(owner):
        return ""
    return stored


def solver_key(bucket: str) -> str:
    system = os.environ.get(_ENV.get(bucket, ""), "").strip()
    owner = _owner.get()
    if int(owner) == AUTHOR_ID:
        return system
    if not int(owner):
        return ""
    from meiguan.grab.loop import _policies

    policy = _policies.get(int(owner)) or {}
    keys = policy.get("pay_keys") if isinstance(policy.get("pay_keys"), dict) else {}
    stored = str(keys.get(bucket) or "").strip()
    return choose_key(owner, stored, system)


def pay_missing(name: str) -> str:
    if _owner.get() == AUTHOR_ID:
        return f"节点：{name}未配置打码密钥"
    return "节点：请先填写打码 token"


def _self_check() -> None:
    assert choose_key(AUTHOR_ID, "user-token", "system-token") == "system-token"
    assert choose_key(1, "user-token", "system-token") == "user-token"
    assert choose_key(1, "", "system-token") == ""
    assert choose_key(0, "user-token", "system-token") == ""
    mark = _owner.set(0)
    try:
        assert solver_key("fll") == ""
        assert solver_key("cf") == ""
        assert pay_missing("滑块") == "节点：请先填写打码 token"
    finally:
        _owner.reset(mark)
    mark = _owner.set(AUTHOR_ID)
    try:
        assert pay_missing("滑块") == "节点：滑块未配置打码密钥"
    finally:
        _owner.reset(mark)


_self_check()
