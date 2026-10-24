"""节点。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    node_id: str
    city_zh: str
    dcs: tuple[int, ...]


NODES: dict[str, Node] = {
    "mia": Node("mia", "迈阿密", (1, 3)),
    "ams": Node("ams", "阿姆斯特丹", (2, 4)),
    "sgp": Node("sgp", "新加坡", (5,)),
}


ENTRY_ORDER: tuple[str, ...] = ("sgp", "ams", "mia")


def place_text(node: Node | None, dc: int = 0) -> str:
    if node is None:
        return ""
    if dc:
        return f"{node.city_zh}-DC{dc}"
    return node.city_zh


def node_for_dc(dc: int) -> Node | None:
    for node in NODES.values():
        if dc in node.dcs:
            return node
    return None


def ssh_host(node_id: str) -> str:
    if node_id not in NODES:
        raise KeyError(node_id)
    return f"meikai-{node_id}"
