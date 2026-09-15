"""Queue notifications privately in the Worker. No subscriber IDs enter public state."""
import hashlib
import os
from urllib.parse import urlsplit

import requests


def enqueue(message, delivery_key, request_id=None):
    url = os.environ.get("TELEGRAM_WORKER_URL", "").rstrip("/")
    if not url:
        if request_id:
            raise RuntimeError("Private reply relay is not configured")
        return False
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or
            not parsed.hostname.endswith(".workers.dev") or parsed.path or
            parsed.query or parsed.fragment or parsed.username or parsed.port):
        raise RuntimeError("Invalid Telegram relay origin")
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Telegram relay credentials are missing")
    payload = {"id": hashlib.sha256(delivery_key.encode()).hexdigest(), "text": message}
    if request_id:
        payload["request_id"] = request_id
    secret = hashlib.sha256(("nbg-relay-v1:" + token).encode()).hexdigest()
    try:
        result = requests.post(url + "/relay", json=payload,
                               headers={"X-NBG-Relay-Secret": secret}, timeout=30,
                               allow_redirects=False)
        # During the first rollout, the old Worker has no /relay endpoint.
        # Only broadcast messages may fall back to the original owner delivery.
        if result.status_code == 404 and not request_id:
            return False
        if result.status_code != 200 or result.json().get("ok") is not True:
            raise RuntimeError("Telegram relay rejected delivery; retained for retry")
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram relay unavailable; retained for retry") from None
    return True
