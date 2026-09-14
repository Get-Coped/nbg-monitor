from datetime import timedelta
from unittest.mock import Mock

import pytest

import monitor
from on_demand import run_request
from test_monitor import baseline, source, FIXTURE, NOW

REQUEST = "abcdef0123456789abcdef01"
NEXT = "abcdef0123456789abcdef02"
KEY = "nbg:645"
READY = {"status": "ready", "terms": {
    "stage": "Indicative terms", "fields": {
        "Coupon": {"value": "Fixed 7–7.25%", "page": 3}},
    "restrictions": [], "pages_reviewed": 60, "total_pages": 84}}

def options(tmp_path, **kwargs):
    return dict(now=NOW, path=tmp_path/"state.json", **kwargs)

def test_overview_is_offline_and_does_not_advance_digest(tmp_path):
    before = baseline()
    sent = []
    no_network = Mock(side_effect=AssertionError("unexpected NBG request"))
    result = run_request(before, "overview", REQUEST, **options(
        tmp_path, sender=sent.append, page_fetcher=no_network, fetcher=no_network))
    assert "Bond entries: 47" in "".join(sent)
    assert "Share entries excluded: 10" in "".join(sent)
    assert result["last_digest_at"] == before["last_digest_at"]
    assert result["log"] == before["log"] == []
    no_network.assert_not_called()

def test_duplicate_request_does_not_send_twice(tmp_path):
    sent = []
    state = run_request(baseline(), "overview", REQUEST, **options(tmp_path, sender=sent.append))
    run_request(state, "overview", REQUEST, **options(tmp_path, sender=sent.append))
    assert len(sent) == 1

def test_recent_refresh_reuses_saved_snapshot(tmp_path):
    fetch = Mock(side_effect=AssertionError("too soon"))
    sent = []
    state = run_request(baseline(), "refresh", REQUEST, **options(
        tmp_path, sender=sent.append, page_fetcher=fetch))
    fetch.assert_not_called()
    assert state["last_ok"] == NOW.isoformat()
    assert "Reusing" in "".join(sent)

def test_fresh_refresh_fetches_once_and_queues_real_changes(tmp_path):
    fetch = Mock(return_value=FIXTURE.read_text().replace("GE 2700605613", "GE 2700605614").replace("GE2700605613", "GE2700605614"))
    sent = []
    result = run_request(baseline(), "refresh", REQUEST, now=NOW+timedelta(hours=4),
                         path=tmp_path/"state.json", sender=sent.append, page_fetcher=fetch)
    fetch.assert_called_once_with()
    assert "isin_changed" in [e["kind"] for e in result["log"]]
    assert "Fresh check completed" in "".join(sent)
    assert result["rows"]["nbg:642"]["isin"] == "GE2700605614"
    assert not result["outbox"]

def test_failed_refresh_retains_rows_and_throttles_another_attempt(tmp_path):
    fetch = Mock(side_effect=RuntimeError("unavailable"))
    state = run_request(baseline(), "refresh", REQUEST, now=NOW+timedelta(hours=4),
                        path=tmp_path/"state.json", sender=lambda _: None, page_fetcher=fetch)
    assert state["rows"] == baseline()["rows"] and state["last_error"]
    run_request(state, "refresh", NEXT, now=NOW+timedelta(hours=4,minutes=1),
                path=tmp_path/"state.json", sender=lambda _: None, page_fetcher=fetch)
    assert fetch.call_count == 1

def test_pending_isin_terms_are_cached_and_cited(tmp_path):
    sent = []
    fetch = Mock(return_value=READY)
    state = run_request(baseline(), "terms", REQUEST, KEY, **options(
        tmp_path, sender=sent.append, fetcher=fetch))
    assert "not yet assigned" in "".join(sent)
    assert "Fixed 7–7.25%" in "".join(sent) and "#page=3" in "".join(sent)
    run_request(state, "terms", NEXT, KEY, **options(tmp_path, sender=sent.append, fetcher=fetch))
    fetch.assert_called_once()

def test_failed_delivery_retries_saved_message_without_refetch(tmp_path):
    fetch = Mock(return_value=READY)
    with pytest.raises(RuntimeError):
        run_request(baseline(), "terms", REQUEST, KEY, **options(tmp_path, fetcher=fetch,
                    sender=Mock(side_effect=RuntimeError("delivery failed"))))
    state = monitor.load_state(tmp_path/"state.json")
    assert state["on_demand"]["receipts"][REQUEST]["status"] == "pending"
    sent = []
    state = run_request(state, "terms", REQUEST, KEY, **options(tmp_path, sender=sent.append, fetcher=fetch))
    fetch.assert_called_once()
    assert sent and not state["outbox"]
    assert state["on_demand"]["receipts"][REQUEST]["status"] == "complete"

def test_pdf_retry_is_limited_and_spaced(tmp_path):
    fetch = Mock(side_effect=RuntimeError("cannot extract"))
    state = baseline()
    for i, hours in enumerate([0,1,12,24,48]):
        state = run_request(state, "terms", f"{i:024x}", KEY, now=NOW+timedelta(hours=hours),
                            path=tmp_path/"state.json", sender=lambda _: None, fetcher=fetch)
    assert fetch.call_count == 3
    assert state["documents"][source()[KEY]["documents"][0]]["status"] == "needs_review"

def test_at_most_two_documents_per_request(tmp_path):
    state = baseline()
    state["rows"][KEY]["documents"] = [f"https://nbg.gov.ge/fm/{i}.pdf" for i in range(4)]
    fetch = Mock(return_value=READY)
    run_request(state, "terms", REQUEST, KEY, **options(tmp_path, sender=lambda _: None, fetcher=fetch))
    assert fetch.call_count == 2

@pytest.mark.parametrize("key", ["https://example.org/a.pdf", "nbg:123; echo x", "../seen.json"])
def test_arbitrary_terms_target_rejected_without_side_effects(tmp_path, key):
    fetch = Mock()
    with pytest.raises(ValueError):
        run_request(baseline(), "terms", REQUEST, key, **options(tmp_path, sender=fetch, fetcher=fetch))
    fetch.assert_not_called()
    assert not (tmp_path/"state.json").exists()

def test_share_terms_cannot_be_requested(tmp_path):
    fetch = Mock()
    sent = []
    run_request(baseline(), "terms", REQUEST, "nbg:385", **options(tmp_path, sender=sent.append, fetcher=fetch))
    fetch.assert_not_called()
    assert "select a current entry" in "".join(sent)
