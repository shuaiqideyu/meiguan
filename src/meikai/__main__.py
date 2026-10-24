"""主体进程。"""

import logging

from telegram import Update

from meikai.bot import build_application
from meikai.config import load_config
from meikai.fresh import mark_started


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx 日志含 token。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    from meikai.reports import AUTHOR_ID
    from meikai.webapi import ORIGIN

    if not ORIGIN:
        raise SystemExit("缺少 WEB_ORIGIN")
    if not AUTHOR_ID:
        raise SystemExit("缺少 AUTHOR_ID")
    cfg = load_config()
    logging.getLogger("meikai").info("role=main")
    app = build_application(cfg)
    mark_started()
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
    )


if __name__ == "__main__":
    main()
