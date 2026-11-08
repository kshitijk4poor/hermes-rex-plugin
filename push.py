"""Phone push notifications through Pushover (https://pushover.net/api).

Keys come from the environment (``~/.hermes/.env``): ``PUSHOVER_USER_KEY`` and
``PUSHOVER_APP_TOKEN``. When either key is missing nothing is sent. Each push is one HTTPS POST with
a 5-second timeout, sent from the plugin's worker thread. Failures are logged at debug level and
never raised.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

API_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT_S = 5


def keys() -> tuple[str, str]:
    return os.environ.get("PUSHOVER_USER_KEY", "").strip(), os.environ.get("PUSHOVER_APP_TOKEN", "").strip()


def configured() -> bool:
    user, token = keys()
    return bool(user and token)


def send(title: str, message: str, *, priority: int = 0) -> bool:
    """POST one message; True when Pushover accepted it."""
    user, token = keys()
    if not (user and token):
        return False
    data = urllib.parse.urlencode({
        "token": token, "user": user, "title": title[:250],
        "message": (message or title)[:1024], "priority": priority,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(API_URL, data=data), timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read() or b"{}").get("status") == 1
    except Exception:
        logger.debug("rex plugin: pushover send failed", exc_info=True)
        return False
