"""启动前的消息。"""

from datetime import datetime, timezone

STARTED_AT: datetime | None = None


def mark_started() -> None:
    global STARTED_AT
    STARTED_AT = datetime.now(timezone.utc)


def is_stale_message(update) -> bool:
    if getattr(update, "callback_query", None) is not None:
        return False
    message = getattr(update, "message", None)
    if STARTED_AT is None or message is None or message.date is None:
        return False
    return message.date < STARTED_AT
