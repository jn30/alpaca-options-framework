"""
Discord notifications for individual trades.

Fire-and-forget: the message is posted from a daemon thread with a short
timeout, and every error is swallowed, so Discord being down can never delay or
break an order. Disabled when config.DISCORD_WEBHOOK_URL is empty.
"""

import json
import logging
import threading
import urllib.request

import config

logger = logging.getLogger(__name__)


def _post(text: str) -> None:
    try:
        req = urllib.request.Request(
            config.DISCORD_WEBHOOK_URL,
            data=json.dumps({"content": text[:1900]}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     # Discord (Cloudflare) rejects the default urllib user agent.
                     "User-Agent": "alpaca-options-framework/1.0"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except Exception as exc:
        logger.debug("Discord notify failed: %s", exc)


def notify(text: str) -> None:
    if not getattr(config, "DISCORD_WEBHOOK_URL", ""):
        return
    threading.Thread(target=_post, args=(text,), daemon=True).start()
