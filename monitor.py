"""Publication-focused NBG bond monitor. See README for semantics and limits."""

import argparse
import copy
import hashlib
import html
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

from prospectus import HEADERS, enrich
from registry import URL, TBILISI, SourceError, bond_counts, parse_rows

STATE_FILE = Path(__file__).with_name("seen.json")
VERSION = 6
LABELS = {
    "prospectus_added": "NEW PROSPECTUS ADDED",
    "documents_replaced": "PROSPECTUS DOCUMENT REPLACED",
    "documents_removed": "DOCUMENT REMOVED FROM NBG PAGE",
    "bond_added": "BOND ENTRY ADDED",
    "bond_removed": "BOND ENTRY REMOVED FROM NBG PAGE",
    "isin_assigned": "ISIN ASSIGNED",
    "isin_changed": "ISIN UPDATED",
    "terms_ready": "PROSPECTUS TERMS AVAILABLE",
}


def now_tbs():
    return datetime.now(TBILISI)


def load_state(path=STATE_FILE):
    if not path.exists():
        return {}
    # Corrupt state is an error, never a silent reset that can miss publications.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("State must be a JSON object")
    return data


def save_state(state, path=STATE_FILE):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def new_state(rows, now):
    return {"v": VERSION, "rows": rows, "observed_rows": rows, "last_ok": now.isoformat(),
            "baseline_at": now.isoformat(), "fails": 0, "last_error": "",
            "pending_removals": {}, "pending_documents": {}, "documents": {
                url: {"status": "baseline"} for r in rows.values() for url in r["documents"]},
            "log": [], "outbox": [], "last_digest_at": now.isoformat(),
            "last_digest_date": "", "digest_counts": bond_counts(rows)}


def event(state, kind, row, now, documents=None, previous=None):
    if row["kind"] != "bond":
        return
    payload = {"ts": now.isoformat(), "kind": kind, "row": copy.deepcopy(row),
               "documents": documents or [], "previous": previous or {}}
    payload["id"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    state["log"].append(payload)
    state["outbox"].append(copy.deepcopy(payload))
    if kind in {"prospectus_added", "documents_replaced"}:
        for url in payload["documents"]:
            state["documents"].setdefault(url, {"status": "pending", "row": copy.deepcopy(row)})


def same_record(old, row):
    if old["id"] != row["id"] or old["kind"] != row["kind"]:
        return False
    if old["isin"] and old["isin"] == row["isin"]:
        return True
    return bool(set(old["documents"]) & set(row["documents"]))


def reconcile(previous, rows, now):
    """Pure state transition. Only valid independent snapshots confirm removals."""
    if previous.get("v") != VERSION:
        # v5 is corrupted by the previous parser. Do not compare it to repaired data.
        state = new_state(rows, now)
        return state, "Clean baseline saved; historical records are not new publications."
    state = copy.deepcopy(previous)
    old_rows = state["rows"]
    old_count = len(state.get("observed_rows", old_rows))
    if len(rows) < old_count * 0.8:
        signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        if state.get("large_drop_candidate") != signature:
            state.update(large_drop_candidate=signature, last_error="Large change awaiting confirmation", fails=state.get("fails", 0)+1)
            # A failure interrupts consecutive removal confirmations.
            state["pending_removals"] = {}
            state["pending_documents"] = {}
            return state, "Unusually small dataset held for confirmation; previous counts retained."
    state.pop("large_drop_candidate", None)
    state.update(last_ok=now.isoformat(), last_error="", fails=0, observed_rows=copy.deepcopy(rows))
    unmatched = set(old_rows)
    matched = {}
    # First match NBG record IDs; a website migration can recreate IDs, so allow
    # a unique issuer+ISIN/document match without reporting removal/republication.
    for key, row in rows.items():
        if key in old_rows:
            matched[key] = key
            unmatched.discard(key)
    for key, row in rows.items():
        if key in matched:
            continue
        candidates = [k for k in unmatched if same_record(old_rows[k], row)]
        if len(candidates) == 1:
            matched[key] = candidates[0]
            unmatched.remove(candidates[0])
    accepted = {}
    for key, row in rows.items():
        row = copy.deepcopy(row)
        old_key = matched.get(key)
        old = old_rows.get(old_key) if old_key else None
        state["pending_removals"].pop(key, None)
        if old_key:
            state["pending_removals"].pop(old_key, None)
        if not old:
            if row["kind"] == "bond":
                new_docs = [url for url in row['documents'] if url not in state['documents']]
                event(state, "prospectus_added" if new_docs else "bond_added", row, now, new_docs or row['documents'])
            accepted[key] = row
            continue
        if old["id"] != row["id"] or old["kind"] != row["kind"]:
            raise SourceError("An existing NBG record changed issuer/type; manual review needed")
        if row["isin"] and row["isin"] != old["isin"]:
            event(state, "isin_assigned" if not old["isin"] else "isin_changed", row, now,
                  previous={"isin": old["isin"]})
        # A temporarily blank ISIN is not a new unassigned security.
        if not row["isin"] and old["isin"]:
            row["isin"] = old["isin"]
        added = sorted(set(row["documents"]) - set(old["documents"]))
        removed = sorted(set(old["documents"]) - set(row["documents"]))
        pending = state["pending_documents"].pop(old_key, {})
        kept_pending = {}
        confirmed = []
        for url in removed:
            count = pending.get(url, 0) + 1
            if count >= 2:
                confirmed.append(url)
            else:
                kept_pending[url] = count
        if kept_pending:
            state["pending_documents"][key] = kept_pending
        if added:
            if removed:
                # A replacement link is immediately useful; do not also notify
                # removal of its predecessor at the following check.
                event(state, "documents_replaced", row, now, added, {"documents": removed})
                state["pending_documents"].pop(key, None)
                kept_pending = {}
            else:
                event(state, "prospectus_added", row, now, added)
        elif confirmed:
            event(state, "documents_removed", row, now, confirmed)
        row["documents"] = sorted(set(row["documents"]) | set(kept_pending))
        accepted[key] = row
    for key in unmatched:
        count = state["pending_removals"].get(key, 0) + 1
        if count >= 2:
            old = old_rows[key]
            event(state, "bond_removed", old, now, old["documents"])
            state["pending_removals"].pop(key, None)
            state["pending_documents"].pop(key, None)
        else:
            state["pending_removals"][key] = count
            accepted[key] = old_rows[key]
    state["rows"] = accepted
    cutoff = (now - timedelta(days=90)).isoformat()
    # Never drop events not yet covered by a digest, even after a long outage.
    state["log"] = [e for e in state["log"] if e["ts"] >= min(cutoff, state["last_digest_at"])]
    return state, "Snapshot checked."


def record_failure(state):
    state["fails"] = state.get("fails", 0) + 1
    state["last_error"] = "Latest check could not verify the NBG list"
    state["pending_removals"] = {}
    state["pending_documents"] = {}
    state.pop("large_drop_candidate", None)


def h(value):
    return html.escape(str(value), quote=True)


def link(url, label="Open prospectus"):
    return f'<a href="{h(url)}">{h(label)}</a>'


def format_terms(item, url):
    if item.get("status") != "ready":
        return ["Terms: " + h(item.get("reason", "Extraction pending; the prospectus is available at the link."))]
    terms = item["terms"]
    lines = [h(terms["stage"])]
    for name in ("Placement agent", "Currency and amount", "Coupon", "Tenor", "Coupon payments"):
        value = terms["fields"].get(name)
        if value:
            lines.append(f"{name}: {h(value['value'])} ({link(url + '#page=' + str(value['page']), 'p. ' + str(value['page']))})")
        else:
            lines.append(f"{name}: not reliably extracted — review document")
    restrictions = terms["restrictions"]
    lines.append("Restrictions identified (review cited clauses):")
    if restrictions:
        for restriction in restrictions[:3]:
            value = restriction['value']
            if len(value) > 330:
                value = value[:330].rsplit(' ', 1)[0] + '…'
            lines.append("• " + h(value) + " (" + link(url + '#page=' + str(restriction['page']), 'p. ' + str(restriction['page'])) + ")")
    else:
        lines.append("Not reliably extracted — review eligibility, transfer and instrument conditions.")
    lines.append(f"Automatic extraction: first {terms['pages_reviewed']} of {terms.get('total_pages', terms['pages_reviewed'])} PDF pages; restrictions are not exhaustive.")
    return lines


def format_alert(e, state):
    row = e["row"]
    lines = ["<b>" + LABELS[e["kind"]] + "</b>", h(row["issuer"]),
             "ISIN: " + h(row["isin"] or "not yet assigned")]
    if e["kind"] == "isin_changed":
        lines.append("Previous ISIN: " + h(e["previous"].get("isin")))
    if e["kind"] in {"bond_removed", "documents_removed"}:
        lines.append("Absent on two successful checks. Removal from this page does not establish redemption or cancellation.")
    for url in e["documents"]:
        lines.extend(["", link(url)])
        if e["kind"] in {"prospectus_added", "documents_replaced", "terms_ready"}:
            lines.extend(format_terms(state["documents"].get(url, {}), url))
    for url in e.get("previous", {}).get("documents", []):
        lines.append(link(url, "Previous document"))
    lines.extend(["", link(URL, "NBG securities page")])
    return chunk_lines(lines)


def units(text):
    return len(text.encode("utf-16-le")) // 2


def chunk_lines(lines, limit=3800):
    # Split between complete HTML lines, never in the middle of an anchor/entity.
    out, chunk = [], ""
    for line in lines:
        if units(line) > limit:
            raise ValueError("One notification line exceeds Telegram limit")
        candidate = chunk + ("\n" if chunk else "") + line
        if units(candidate) > limit:
            out.append(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        out.append(chunk)
    return out


def send_telegram(message):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Telegram credentials are missing; use --dry-run to preview")
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": message, "parse_mode": "HTML",
                                       "disable_web_page_preview": True}, timeout=30)
        ok = response.status_code == 200 and response.json().get("ok") is True
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram delivery failed; notification retained for retry") from None
    if not ok:
        raise RuntimeError("Telegram rejected notification; retained for retry")


def flush_outbox(state, path, sender=send_telegram):
    for e in list(state["outbox"]):
        # Freeze chunks so retrying a partially delivered message cannot reorder
        # chunks after a later extraction succeeds.
        e.setdefault("messages", format_alert(e, state))
        save_state(state, path)
        for i in range(e.get("sent_chunks", 0), len(e["messages"])):
            sender(e["messages"][i])
            e["sent_chunks"] = i + 1
            save_state(state, path)
        for url in e["documents"]:
            if url in state["documents"]:
                state["documents"][url]["announced"] = True
        state["outbox"].remove(e)
        save_state(state, path)


def digest_messages(state, now):
    rows = state.get("observed_rows", state["rows"])
    counts = bond_counts(rows)
    old_counts = state.get("digest_counts", counts)
    delta = counts["bonds"] - old_counts["bonds"]
    events = [e for e in state["log"] if state.get("last_digest_at", "") < e["ts"] <= now.isoformat() and e["kind"] != "terms_ready"]
    last_ok = datetime.fromisoformat(state["last_ok"])
    lines = ["<b>NBG DAILY BOND DIGEST</b>", now.strftime("%d %B %Y"), "",
             f"Bond entries on the NBG page: <b>{counts['bonds']}</b> ({delta:+d} since previous digest)",
             f"Bond issuers: {counts['issuers']}",
             f"With ISIN: {counts['assigned']} · Awaiting ISIN: {counts['awaiting_isin']}",
             f"Share entries excluded: {counts['shares']}", ""]
    if events:
        lines.append("<b>Since the previous digest</b>")
        for kind, label in LABELS.items():
            selected = [e for e in events if e["kind"] == kind]
            if not selected:
                continue
            lines.append(f"{label.capitalize()}: {len(selected)}")
            for e in selected:
                lines.append("• " + h(e["row"]["issuer"]) + " — " + h(e["row"]["isin"] or "ISIN pending"))
                for url in e["documents"]:
                    lines.append("  " + link(url))
    else:
        lines.append("No bond publications, confirmed removals or ISIN updates detected since the previous digest.")
    if state.get("pending_removals") or state.get("pending_documents"):
        lines.append("A possible removal is awaiting the next successful check; it is not confirmed yet.")
    if state.get("last_error") or (now-last_ok).total_seconds() > 8*3600:
        lines.append("Latest list not verified: counts are from the last successful check.")
    lines.extend(["", "Last successful check: " + last_ok.strftime("%d %b %H:%M") + " Tbilisi",
                  "Counts describe NBG page entries, not all outstanding bonds in Georgia."])
    if now.date() == datetime.fromisoformat(state["baseline_at"]).date():
        lines.append("The corrected baseline was established today; earlier alerts are excluded.")
    return chunk_lines(lines), counts


def run_digest(state, now, path, dry_run=False, sender=send_telegram):
    if state.get("v") != VERSION:
        raise RuntimeError("Run one successful check to establish the corrected baseline before the digest")
    if state.get("last_digest_date") == now.date().isoformat() and not dry_run:
        print("Daily digest already delivered.")
        return
    if dry_run:
        for msg in digest_messages(state, now)[0]:
            print(msg + "\n")
        return
    if not state.get("pending_digest"):
        messages, counts = digest_messages(state, now)
        state["pending_digest"] = {"messages": messages, "counts": counts, "cutoff": now.isoformat(), "sent_chunks": 0}
        save_state(state, path)
    pending = state["pending_digest"]
    for i in range(pending["sent_chunks"], len(pending["messages"])):
        sender(pending["messages"][i])
        pending["sent_chunks"] = i+1
        save_state(state, path)
    state.update(last_digest_at=pending["cutoff"], last_digest_date=now.date().isoformat(), digest_counts=pending["counts"])
    state.pop("pending_digest")
    save_state(state, path)


def fetch_html():
    # One page request per scheduled check. Do not bypass WAF/maintenance pages.
    response = requests.get(URL, headers=HEADERS, timeout=(10, 40), allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise SourceError("NBG did not return the securities page")
    return response.text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="No messages, state writes or PDF downloads")
    parser.add_argument("--html", type=Path, help="Read an offline NBG HTML fixture instead of the network")
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    args = parser.parse_args()
    state = load_state(args.state)
    now = now_tbs()
    if args.digest:
        run_digest(state, now, args.state, dry_run=args.dry_run)
        return
    try:
        rows = parse_rows(args.html.read_text(encoding="utf-8") if args.html else fetch_html())
        updated, message = reconcile(state, rows, now)
    except (SourceError, requests.RequestException) as exc:
        print("Check failed:", type(exc).__name__)
        if not args.dry_run:
            record_failure(state)
            save_state(state, args.state)
        raise RuntimeError("NBG list could not be verified; previous data retained") from None
    print(message, bond_counts(rows))
    if args.dry_run:
        for e in updated["outbox"]:
            for msg in format_alert(e, updated):
                print(msg + "\n")
        for msg in digest_messages(updated, now)[0]:
            print(msg + "\n")
        return
    save_state(updated, args.state)
    enrich(updated, now)
    for url, item in updated["documents"].items():
        if item.pop("followup_due", False):
            event(updated, "terms_ready", item["row"], now, [url])
    save_state(updated, args.state)
    flush_outbox(updated, args.state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Sanitise exceptions from HTTP libraries: bot tokens can occur in URLs.
        print("Monitor stopped:", str(exc) if isinstance(exc, (RuntimeError, SourceError)) else type(exc).__name__)
        sys.exit(1)
