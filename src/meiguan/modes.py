"""领取开关。按操作者保存，并推到节点。"""

from meiguan.node_rpc import call_nodes
from meiguan.store import load_policies

LABELS = (
    ("phrase", "口令红包"),
    ("captcha", "算式计算"),
    ("webapp", "网页验证码"),
    ("direct", "按钮关键词"),
    ("cloudflare", "网页验证码"),
    ("shared", "共享抢包"),
)


async def push_policies() -> list[str]:
    policies = load_policies()
    if policies is None:
        return []
    results = await call_nodes({"op": "modes", "policies": dict(policies)}, timeout=8)
    return [node_id for node_id, result in results.items() if not result.get("ok")]
