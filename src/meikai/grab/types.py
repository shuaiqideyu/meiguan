"""领取结果。"""

from dataclasses import dataclass, field

@dataclass
class RedPacket:
    kind: str
    data: bytes | None = None
    expr: str | None = None
    buttons: list = field(default_factory=list)
    needs_emoji: bool = False
    start: str | None = None
    bot: str | None = None
    short: str | None = None
    premium_only: bool = False
    follows: str | None = None
    emoji_map: dict | None = None
    place: str = ""
    page: str | None = None
    register_bot: str | None = None
    register_start: str | None = None


@dataclass
class Result:
    ok: bool
    amount: str | None = None
    retryable: bool = True
    feedback: str = ""
    wrong: bool = False
    blocked: bool = False
    pause: bool = False
    corrections: dict = field(default_factory=dict)
    hold: bool = False
    url: str = ""


class Live:
    def __init__(self) -> None:
        self.done: set = set()
        self.inflight: set = set()
        self.attempts: dict = {}
        self.blocked: dict = {}
        self.pause_until = 0.0
