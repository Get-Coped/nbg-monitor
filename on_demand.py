"""Bounded jobs requested by the owner through Telegram. No new schedule."""
import argparse
import copy
import re
import sys
from datetime import datetime, timedelta

import monitor
from prospectus import fetch_terms
from registry import bond_counts

def overview(state, prefix=""):
    counts = bond_counts(state.get("observed_rows", state["rows"]))
    lines = [prefix, "<b>NBG bond overview</b>",
             f"Bond entries: {counts['bonds']} · Issuers: {counts['issuers']}",
             f"With ISIN: {counts['assigned']} · Awaiting ISIN: {counts['awaiting_isin']}",
             f"Share entries excluded: {counts['shares']}",
             "Last successful check: " + datetime.fromisoformat(state["last_ok"]).strftime("%d %b %H:%M") + " Tbilisi"]
    if state.get("last_error"):
        lines.append("Latest list not verified; figures are from the last successful check.")
    lines.append("Counts describe NBG page entries, not all outstanding bonds in Georgia.")
    return lines

def run_request(state, mode, request_id, row_key="", now=None, path=monitor.STATE_FILE,
                sender=monitor.send_telegram, fetcher=fetch_terms, page_fetcher=monitor.fetch_html):
    now = now or monitor.now_tbs()
    if state.get("v") != 6:
        raise ValueError("A successful v6 baseline is required")
    if mode not in {"overview", "refresh", "terms"} or not re.fullmatch(r"[a-f0-9]{24}", request_id):
        raise ValueError("Invalid request")
    if mode == "terms" and not re.fullmatch(r"nbg:\d+", row_key):
        raise ValueError("Select an NBG record ID, not an arbitrary URL")
    state = copy.deepcopy(state)
    meta = state.setdefault("on_demand", {})
    receipts = meta.setdefault("receipts", {})
    old = receipts.get(request_id)
    if old:
        if old["status"] == "complete":
            return state
        monitor.flush_outbox(state, path, sender)
        receipts[request_id]["status"] = "complete"
        monitor.save_state(state, path)
        return state
    documents = []
    if mode == "overview":
        lines = overview(state)
    elif mode == "refresh":
        recent = max(state.get("last_ok", ""), meta.get("last_refresh_attempt", ""))
        if recent and (now-datetime.fromisoformat(recent)).total_seconds() < 15*60:
            lines = overview(state, "A check was made recently. Reusing it to avoid repeated NBG requests.")
        else:
            meta["last_refresh_attempt"] = now.isoformat()
            monitor.save_state(state, path)
            try:
                rows = monitor.parse_rows(page_fetcher())
                state, _ = monitor.reconcile(state, rows, now)
                lines = overview(state, "Fresh check completed." if not state.get("last_error") else "The changed list still needs confirmation.")
            except Exception:
                monitor.record_failure(state)
                lines = overview(state, "NBG could not be checked successfully. The saved list has been retained.")
            meta = state["on_demand"]
            receipts = meta["receipts"]
    else:
        row = state.get("observed_rows", state["rows"]).get(row_key)
        if not row or row["kind"] != "bond":
            lines = ["That bond is no longer in the saved list. Please select a current entry."]
        else:
            lines = ["<b>Requested prospectus terms</b>", monitor.h(row["issuer"]),
                     "ISIN: " + monitor.h(row["isin"] or "not yet assigned")]
            documents = row["documents"]
            attempts = 0
            for url in documents:
                item = state["documents"].setdefault(url, {"status": "baseline"})
                item.setdefault("row", copy.deepcopy(row))
                recent = item.get("last_attempt")
                eligible = (item["status"] != "ready" and item.get("attempts",0) < 3 and
                            (not recent or (now-datetime.fromisoformat(recent)).total_seconds() >= 12*3600))
                if eligible and attempts < 2:
                    attempts += 1
                    item.update(status="retry", attempts=item.get("attempts",0)+1, last_attempt=now.isoformat())
                    monitor.save_state(state, path)
                    try:
                        item.update(fetcher(url))
                    except Exception:
                        item.update(status="needs_review" if item["attempts"] >= 3 else "retry",
                                    reason="PDF extraction unavailable; document review required.")
                lines.extend(["", monitor.link(url)])
                lines.extend(monitor.format_terms(item, url))
            if not documents:
                lines.append("No prospectus link is listed for this bond.")
    payload = {"id": "request:" + request_id, "kind": "request_reply",
               "documents": documents, "messages": monitor.chunk_lines(lines)}
    state["outbox"].append(payload)
    receipts[request_id] = {"status": "pending", "ts": now.isoformat()}
    cutoff = (now-timedelta(days=7)).isoformat()
    for key, value in list(receipts.items()):
        if value["status"] == "complete" and value["ts"] < cutoff:
            del receipts[key]
    monitor.save_state(state, path)
    monitor.flush_outbox(state, path, sender)
    receipts[request_id]["status"] = "complete"
    monitor.save_state(state, path)
    return state

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["overview","refresh","terms"], required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--row-key", default="")
    args = p.parse_args()
    try:
        run_request(monitor.load_state(), args.mode, args.request_id, args.row_key)
        print("On-demand request completed.")
    except Exception as exc:
        print("On-demand request failed:", type(exc).__name__)
        sys.exit(1)
