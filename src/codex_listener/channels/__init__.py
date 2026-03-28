"""External bot notification channels."""

import logging

from codex_listener.channels.feishu import send_feishu_notification
from codex_listener.channels.telegram import send_telegram_notification

logger = logging.getLogger(__name__)

try:
    from codex_listener.channels.qq import send_qq_notification
except ModuleNotFoundError as e:
    logger.warning("QQ channel unavailable: %s", e)

    async def send_qq_notification(*args, **kwargs):  # type: ignore[override]
        raise RuntimeError("QQ channel unavailable: missing qq-botpy dependency")

__all__ = [
    "send_feishu_notification",
    "send_qq_notification",
    "send_telegram_notification",
]
