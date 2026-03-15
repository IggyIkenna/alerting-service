"""
Telegram notifier using the Bot API sendMessage endpoint.

Reads bot token and chat ID from config (backed by env vars
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) and sends messages
with HTML parse mode.

Best-effort: logs failures, returns False, never crashes.
"""

import logging

import httpx
from unified_events_interface import log_event

logger = logging.getLogger(__name__)

_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """Send a message to a Telegram chat via the Bot API.

    Uses HTML parse mode for rich formatting support.

    Args:
        message: The message text to send (HTML formatting allowed).
        bot_token: Telegram Bot API token.
        chat_id: Target Telegram chat ID.

    Returns:
        True when Telegram accepted the message (HTTP 200 + ok=true), False otherwise.
    """
    url = _TELEGRAM_API_URL.format(token=bot_token)
    payload: dict[str, str] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        response = httpx.post(url, json=payload, timeout=10.0)
        if response.status_code == 200:
            log_event(
                "TELEGRAM_MESSAGE_SENT",
                details={"chat_id": chat_id, "message_length": str(len(message))},
            )
            return True
        logger.error(
            "Telegram API returned unexpected status %d: %s",
            response.status_code,
            response.text,
        )
        return False
    except httpx.HTTPError as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        return False
