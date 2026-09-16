import hashlib
from unittest.mock import Mock, patch

import pytest
import configure_telegram as setup

URL = "https://nbg-telegram-requests.example.workers.dev"
ENV = {"WORKER_URL":URL, "TELEGRAM_TOKEN":"test-token", "TELEGRAM_CHAT_ID":"123"}

def response(data=None, code=200):
    return Mock(status_code=code, json=lambda: data or {}, raise_for_status=lambda: None)

def test_setup_authenticates_worker_before_changing_webhook():
    calls = []
    health = response({"service":"nbg-telegram-requests","configured":True,"jobs_enabled":True,"friends_enabled":True,"relay_version":1,"preliminary_enabled":True})
    def post(url, json, **kwargs):
        calls.append((url,json,kwargs))
        if url == URL+"/telegram":
            return response()
        if url.endswith("/getWebhookInfo"):
            return response({"ok":True,"result":{"url":URL+"/telegram"}})
        return response({"ok":True,"result":True})
    with patch.dict("os.environ", ENV, clear=True), patch.object(setup.requests,"get",return_value=health), patch.object(setup.requests,"post",side_effect=post):
        setup.configure()
    assert calls[0][0] == URL+"/telegram"
    assert calls[0][2]["headers"]["X-Telegram-Bot-Api-Secret-Token"] == hashlib.sha256(b"nbg-webhook-v1:test-token").hexdigest()
    webhook = next(payload for url,payload,_ in calls if url.endswith("/setWebhook"))
    assert webhook["max_connections"] == 1 and not webhook["drop_pending_updates"]
    assert not any(url.endswith("/sendMessage") for url,_,_ in calls)
    menus = [payload for url,payload,_ in calls if url.endswith("/setMyCommands")]
    common = next(m for m in menus if m["scope"]["type"] == "all_private_chats")
    owner = next(m for m in menus if m["scope"]["type"] == "chat")
    assert "stop" in {c["command"] for c in common["commands"]}
    assert "invite" not in {c["command"] for c in common["commands"]}
    assert "invite" in {c["command"] for c in owner["commands"]}

def test_bad_worker_auth_does_not_touch_telegram():
    health = response({"service":"nbg-telegram-requests","configured":True,"jobs_enabled":True,"friends_enabled":True,"relay_version":1,"preliminary_enabled":True})
    with patch.dict("os.environ", ENV, clear=True), patch.object(setup.requests,"get",return_value=health), patch.object(setup.requests,"post",return_value=response(code=403)) as post:
        with pytest.raises(ValueError):
            setup.configure()
    assert post.call_count == 1

@pytest.mark.parametrize("url", ["http://nbg-telegram-requests.example.workers.dev", "https://example.org", URL+"/path"])
def test_unexpected_deployment_origin_rejected(url):
    with patch.dict("os.environ", dict(ENV, WORKER_URL=url), clear=True), patch.object(setup.requests,"get") as get:
        with pytest.raises(ValueError):
            setup.configure()
    get.assert_not_called()


def test_setup_waits_for_updated_worker_before_installing_commands():
    old = response({'service':'nbg-telegram-requests','configured':True,'jobs_enabled':True,'friends_enabled':True,'relay_version':1})
    ready = response({'service':'nbg-telegram-requests','configured':True,'jobs_enabled':True,'friends_enabled':True,'relay_version':1,'preliminary_enabled':True})
    def post(url, json, **kwargs):
        if url == URL+'/telegram':
            return response()
        if url.endswith('/getWebhookInfo'):
            return response({'ok':True,'result':{'url':URL+'/telegram'}})
        return response({'ok':True,'result':True})
    with patch.dict('os.environ',ENV,clear=True), patch.object(setup.requests,'get',side_effect=[old,ready]) as get, patch.object(setup.requests,'post',side_effect=post), patch.object(setup.time,'sleep') as sleep:
        setup.configure()
    assert get.call_count == 2
    sleep.assert_called_once_with(2)
