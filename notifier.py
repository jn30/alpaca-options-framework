"""
Discord notifications for individual trades.

Fire-and-forget: the message is posted from a daemon thread with a short
timeout, and every error is swallowed, so Discord being down can never delay or
break an order. Disabled when config.DISCORD_WEBHOOK_URL is empty.
"""

import atexit
import json
import logging
import threading
import time
import urllib.request

import config

logger = logging.getLogger(__name__)
_pending: list[threading.Thread] = []


def flush(timeout: float = 10.0) -> None:
    """Wait (bounded) for queued messages. Runs at exit: daemon threads are
    killed when the interpreter ends, so a one-shot run (e.g. trend_bot --now)
    would otherwise exit before its message is sent."""
    end = time.time() + timeout
    for t in list(_pending):
        t.join(max(0.0, end - time.time()))


atexit.register(flush)


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
        logger.warning("Discord notify failed: %s", exc)


def notify(text: str) -> None:
    if not getattr(config, "DISCORD_WEBHOOK_URL", ""):
        return
    _pending[:] = [t for t in _pending if t.is_alive()]
    t = threading.Thread(target=_post, args=(text,), daemon=True)
    t.start()
    _pending.append(t)
