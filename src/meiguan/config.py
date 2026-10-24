"""主体配置。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str


def load_config(env: Mapping[str, str] | None = None) -> Config:
    src = os.environ if env is None else env
    token = (src.get("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("缺少 BOT_TOKEN")
    return Config(bot_token=token)
