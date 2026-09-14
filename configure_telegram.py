"""Wire the deployed owner-only Worker to the existing bot; never print secrets."""
import hashlib
import os
import sys
from urllib.parse import urlsplit

import requests

COMMANDS = [
    ("overview", "Bond count, issuers and pending ISINs"),
    ("changes", "Recorded changes; for example /changes 7"),
    ("issuer", "Find issuer; for example /issuer Nikora"),
    ("terms", "View prospectus terms by issuer or ISIN"),
    ("digest", "Get a digest now"),
    ("refresh", "Request a fresh NBG check"),
    ("menu", "Show the main menu"),
]
MENU = {"inline_keyboard": [
    [{"text":"Overview","callback_data":"overview"},{"text":"Recent changes","callback_data":"changes:7"}],
    [{"text":"Find issuer","callback_data":"issuer"},{"text":"Bond terms","callback_data":"list:0"}],
    [{"text":"Digest now","callback_data":"digest"},{"text":"Check NBG now","callback_data":"refresh"}],
]}

def configure():
    url = os.environ["WORKER_URL"].rstrip("/")
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.path not in ("","/") or p.query or p.fragment or p.username or p.port or not p.hostname.startswith("nbg-telegram-requests.") or not p.hostname.endswith(".workers.dev"):
        raise ValueError("Worker URL must be the deployed nbg-telegram-requests workers.dev origin")
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    owner = os.environ.get("TELEGRAM_USER_ID") or chat_id
    if not owner.isdecimal():
        raise ValueError("A group bot needs an explicit positive TELEGRAM_USER_ID")
    health = requests.get(url+"/health", timeout=20)
    health.raise_for_status()
    status = health.json()
    if status.get("service") != "nbg-telegram-requests" or not status.get("configured") or not status.get("jobs_enabled"):
        raise ValueError("Worker configuration is incomplete")
    secret = hashlib.sha256(("nbg-webhook-v1:"+token).encode()).hexdigest()
    probe = requests.post(url+"/telegram", json={"update_id":0},
                          headers={"X-Telegram-Bot-Api-Secret-Token":secret}, timeout=20)
    if probe.status_code != 200:
        raise ValueError("Worker authentication check failed")
    def api(method, payload=None):
        r = requests.post("https://api.telegram.org/bot"+token+"/"+method,
                          json=payload or {}, timeout=25)
        if r.status_code != 200 or not r.json().get("ok"):
            raise ValueError("Telegram configuration failed")
        return r.json()["result"]
    before = api("getWebhookInfo")
    api("setWebhook", {"url":url+"/telegram", "secret_token":secret,
                      "max_connections":1, "allowed_updates":["message","callback_query"],
                      "drop_pending_updates":False})
    api("setMyCommands", {"commands":[{"command":c,"description":d} for c,d in COMMANDS],
                         "scope":{"type":"chat","chat_id":chat_id}})
    if chat_id.isdecimal():
        api("setChatMenuButton", {"chat_id":chat_id, "menu_button":{"type":"commands"}})
    after = api("getWebhookInfo")
    if after.get("url") != url+"/telegram":
        raise ValueError("Webhook verification failed")
    if before.get("url") != url+"/telegram":
        api("sendMessage", {"chat_id":chat_id, "text":"Your NBG on-demand assistant is ready. Choose an option below.",
                            "reply_markup":MENU})
    print("Telegram menu and webhook configured successfully.")

if __name__ == "__main__":
    try:
        configure()
    except Exception as exc:
        print("Telegram setup failed:", type(exc).__name__)
        sys.exit(1)
