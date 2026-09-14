import hashlib
from unittest.mock import Mock, patch

import pytest
import requests

import monitor
import on_demand
import telegram_relay as relay
from test_monitor import baseline, NOW

ENV = {"TELEGRAM_WORKER_URL":"https://nbg-telegram-requests.example.workers.dev",
       "TELEGRAM_TOKEN":"test-token", "TELEGRAM_CHAT_ID":"123"}
REQUEST = "1234567890abcdef12345678"

def response(code=200):
    return Mock(status_code=code, json=lambda:{"ok":True})

def test_relay_auth_and_payload_do_not_contain_recipient():
    with patch.dict("os.environ", ENV, clear=True), patch.object(relay.requests,"post",return_value=response()) as post:
        assert relay.enqueue("Bond terms", "request:opaque:0", REQUEST)
    kwargs = post.call_args.kwargs
    assert set(kwargs["json"]) == {"id","text","request_id"}
    assert kwargs["headers"]["X-NBG-Relay-Secret"] == hashlib.sha256(b"nbg-relay-v1:test-token").hexdigest()
    assert kwargs["allow_redirects"] is False

def test_only_legacy_broadcast_can_fall_back_during_rollout():
    with patch.dict("os.environ", ENV, clear=True), patch.object(relay.requests,"post",return_value=response(404)):
        assert relay.enqueue("Alert","event:0") is False
        with pytest.raises(RuntimeError):
            relay.enqueue("Private result","request:0",REQUEST)
    with patch.dict("os.environ", {}, clear=True):
        assert relay.enqueue("Alert","event:0") is False
        with pytest.raises(RuntimeError):
            relay.enqueue("Private result","request:0",REQUEST)

@pytest.mark.parametrize("status", [403,409,429,500,503])
def test_relay_failure_is_retained_without_owner_fallback(status):
    with patch.dict("os.environ", ENV, clear=True), patch.object(relay.requests,"post",return_value=response(status)):
        with pytest.raises(RuntimeError):
            relay.enqueue("Alert","event:0")

def test_network_failure_sanitises_token():
    with patch.dict("os.environ", ENV, clear=True), patch.object(relay.requests,"post",side_effect=requests.RequestException("test-token")):
        with pytest.raises(RuntimeError) as error:
            relay.enqueue("Alert","event:0")
    assert "test-token" not in str(error.value)

def test_private_job_routes_result_using_only_opaque_id(tmp_path):
    with patch.object(relay,"enqueue",return_value=True) as send:
        state = on_demand.run_request(baseline(), "overview", REQUEST, now=NOW,
                                      path=tmp_path/"state.json", private_reply=True)
    assert send.call_args.args[2] == REQUEST
    assert not state["outbox"]
    assert state["on_demand"]["receipts"][REQUEST]["status"] == "complete"

def test_failed_private_delivery_retries_without_repeating_work(tmp_path):
    path = tmp_path/"state.json"
    with patch.object(relay,"enqueue",side_effect=RuntimeError("offline")):
        with pytest.raises(RuntimeError):
            on_demand.run_request(baseline(),"overview",REQUEST,now=NOW,path=path,private_reply=True)
    state=monitor.load_state(path)
    assert state["outbox"][0]["private_reply"] is True
    with patch.object(relay,"enqueue",return_value=True) as send:
        state=on_demand.run_request(state,"overview",REQUEST,now=NOW,path=path,private_reply=True)
    assert send.call_count == 1 and not state["outbox"]

def test_digest_uses_stable_keys_and_does_not_send_twice(tmp_path):
    state=baseline()
    with patch.object(relay,"enqueue",return_value=True) as send:
        monitor.run_digest(state,NOW,tmp_path/"state.json")
        count=send.call_count
        monitor.run_digest(state,NOW,tmp_path/"state.json")
    assert count and send.call_count == count
    assert send.call_args.args[1].startswith("digest:")
