"""
NBG public-securities monitor v4.

Parses the full row for every entry on
https://nbg.gov.ge/supervision/public-companies and classifies what changed:

  NEW ISSUER          — an identification number never seen before
  NEW SECURITY        — new ISIN / new prospectus for a known issuer
  DOCUMENT REPLACED   — prospectus PDF swapped on an existing row
  ISIN ASSIGNED       — row that had no ISIN now has one
  REMOVED             — row disappeared from the page
  UPDATED             — status date or website changed (low priority, digested)

State: seen.json holds a full snapshot of every row, so removals and
in-place edits are detectable. Migrates silently from older formats.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

URL = "https://nbg.gov.ge/supervision/public-companies"
STATE_FILE = Path(__file__).parent / "seen.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ka,en;q=0.9",
}

TBILISI = timezone(timedelta(hours=4))  # Georgia is UTC+4 year-round, no DST


def now_tbs() -> datetime:
    return datetime.now(TBILISI)


ISIN_RE = re.compile(r"GE\s?\d{10}")
ID_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
PDF_RE = re.compile(r"https://nbg\.gov\.ge/fm/cm/issuers/[^\s\"'<>]+\.pdf[^\s\"'<>]*")
SITE_RE = re.compile(r"https?://(?!nbg\.gov\.ge)[^\s\"'<>]+")
TYPE_RE = re.compile(r"(ობლიგაცია|ჩვ\.\s*აქცია|პრ\.\s*აქცია)")

# --------------------------------------------------------------------------


def _fetch_once() -> str:
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if PDF_RE.search(r.text) or ISIN_RE.search(r.text):
            return r.text
        print(f"Table not in raw HTML ({len(r.text)} bytes), trying Playwright...")
    except Exception as e:
        print(f"requests failed ({e}), trying Playwright...")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(URL, wait_until="networkidle", timeout=60000)
        html = page.content()
        browser.close()
    return html


def fetch_html() -> str:
    """Retry within the run. NBG occasionally serves a maintenance/WAF page
    with HTTP 200 and no table — transient, not a structure change."""
    last = ""
    for attempt in range(3):
        if attempt:
            time.sleep(20)
        try:
            last = _fetch_once()
        except Exception as e:
            print(f"Attempt {attempt + 1} failed entirely: {e}")
            continue
        if PDF_RE.search(last) or ISIN_RE.search(last):
            return last
        print(f"Attempt {attempt + 1}: page had no prospectus rows.")
    return last


def parse_rows(html: str) -> list[dict]:
    """Anchor on the 9-digit identification number, which every row has.
    Everything from one ID to the next belongs to that row."""
    text = re.sub(r'<a\s[^>]*?href="([^"]+)"[^>]*>', r" \1 ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    # Mask ISINs so their digits can't be mistaken for ID numbers.
    isins = {}

    def _mask(m):
        tok = f" @I{len(isins)}@ "
        isins[tok.strip()] = m.group(0).replace(" ", "")
        return tok

    masked = ISIN_RE.sub(_mask, text)

    hits = list(ID_RE.finditer(masked))
    rows = []
    for i, m in enumerate(hits):
        idnum = m.group(1)
        before = masked[hits[i - 1].end() if i else 0 : m.start()]
        after = masked[m.end() : hits[i + 1].start() if i + 1 < len(hits) else len(masked)]

        issuer = ""
        names = re.findall(r"([ა-ჰ][ა-ჰa-zA-Z0-9\s\-\.,„“\"'()]{2,90})", before)
        if names:
            issuer = names[-1].strip()
        issuer = re.sub(r"^(ვებგვერდი|Eng)\s*", "", issuer).strip(" .,-")

        tm = TYPE_RE.search(after)
        sec_type = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""

        isin = ""
        itok = re.search(r"@I\d+@", after)
        if itok:
            isin = isins.get(itok.group(0), "")

        pdfs = PDF_RE.findall(after)
        pdf = pdfs[0] if pdfs else ""

        dm = DATE_RE.search(after)
        date = dm.group(0) if dm else ""
        invalid_date = bool(re.search(r"Invalid\s*Date", after)) or not date

        sites = [u for u in SITE_RE.findall(after) if ".pdf" not in u]
        site = sites[0].rstrip(" .,") if sites else ""

        rows.append({
            "id": idnum,
            "issuer": issuer or "(unnamed)",
            "type": sec_type,
            "isin": isin,
            "pdf": pdf,
            "pdf_key": pdf.split("?")[0],
            "date": date,
            "invalid_date": invalid_date,
            "site": site,
        })
    return rows


def row_key(r: dict) -> str:
    """Stable identity for a row across page updates."""
    if r["isin"]:
        return "isin:" + r["isin"]
    if r["pdf_key"]:
        return "pdf:" + r["pdf_key"]
    return f"row:{r['id']}:{r['type']}"


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    seen, out, dropped = set(), [], 0
    for r in rows:
        sig = (r["id"], r["isin"], r["pdf_key"], r["type"], r["date"])
        if sig in seen:
            dropped += 1
            continue
        seen.add(sig)
        out.append(r)
    return out, dropped


# --------------------------------------------------------------------------


def load_full() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_state() -> tuple[dict, int, dict]:
    data = load_full()
    if not data:
        return {}, 0, {"fresh": True}
    if isinstance(data, dict) and data.get("v") in (4, 5):
        return data.get("rows", {}), int(data.get("fails", 0)), {"fresh": False}
    return {}, 0, {"migrating": True, "fresh": False}


def save_state(rows: dict, fails: int = 0, log=None, last_ok=None):
    prev = load_full() if isinstance(load_full(), dict) else {}
    cutoff = (now_tbs() - timedelta(days=30)).isoformat()
    merged_log = [e for e in (prev.get("log") or []) if e.get("ts", "") >= cutoff]
    if log:
        merged_log.extend(log)
    STATE_FILE.write_text(
        json.dumps({
            "v": 5,
            "fails": fails,
            "last_ok": last_ok or prev.get("last_ok", ""),
            "rows": rows,
            "log": merged_log,
        }, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; message:\n" + msg + "\n")
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    ).raise_for_status()


# --------------------------------------------------------------------------

HEADLINE = {
    "new_issuer": "🏢 <b>NEW ISSUER — first NBG prospectus</b>",
    "new_security": "🆕 <b>NEW SECURITY / PROSPECTUS</b>",
    "isin_assigned": "✅ <b>ISIN ASSIGNED</b>",
    "doc_replaced": "📄 <b>PROSPECTUS DOCUMENT REPLACED</b>",
    "removed": "❌ <b>ENTRY REMOVED FROM NBG PAGE</b>",
}


def format_alert(kind: str, r: dict, prev: dict | None = None) -> str:
    lines = [HEADLINE[kind], ""]
    lines.append(f"Issuer: {r['issuer']}")
    lines.append(f"ID: {r['id']}")
    if r["type"]:
        lines.append(f"Type: {r['type']}")
    lines.append(f"ISIN: {r['isin'] or '— not yet assigned'}")
    lines.append(f"Status date: {'⚠️ not set' if r['invalid_date'] else r['date']}")

    if kind == "doc_replaced" and prev:
        lines.append(f"Previous doc: {prev.get('pdf') or '—'}")
    if r["pdf"]:
        lines.append(f"Prospectus: {r['pdf']}")
    if r["site"]:
        lines.append(f"Website: {r['site']}")

    flags = []
    if not r["isin"]:
        flags.append("no ISIN yet — likely bookbuilding stage")
    if r["invalid_date"]:
        flags.append("no status date on page")
    if flags:
        lines += ["", "⚠️ " + "; ".join(flags)]

    lines += ["", URL]
    return "\n".join(lines)


def main():
    html = fetch_html()
    rows = parse_rows(html)
    rows, dupes = dedupe(rows)

    prev_rows, fails, meta = load_state()

    if not rows:
        fails += 1
        print(f"No rows parsed. Consecutive failures: {fails}")
        if fails == 3:
            send_telegram(
                "⚠️ NBG monitor: page unreadable for ~45 minutes (3 runs). "
                "Structure may have changed."
            )
        save_state(prev_rows, fails)
        return

    current = {row_key(r): r for r in rows}
    print(f"Parsed {len(current)} rows ({dupes} duplicates collapsed).")

    if meta.get("fresh") or meta.get("migrating"):
        save_state(current, 0, last_ok=now_tbs().isoformat())
        print("Baseline saved — no alerts on first run of this version.")
        return

    known_ids = {r.get("id") for r in prev_rows.values()}
    urgent, digest = [], []

    for key, r in current.items():
        old = prev_rows.get(key)
        if old is None:
            twin = next(
                (o for o in prev_rows.values()
                 if not o.get("isin") and o.get("pdf_key")
                 and o["pdf_key"] == r["pdf_key"] and r["isin"]),
                None,
            )
            if twin:
                urgent.append(("isin_assigned", r, twin))
            elif r["id"] not in known_ids:
                urgent.append(("new_issuer", r, None))
            else:
                urgent.append(("new_security", r, None))
            continue

        if r["pdf_key"] and old.get("pdf_key") and r["pdf_key"] != old["pdf_key"]:
            urgent.append(("doc_replaced", r, old))
            continue

        changed = []
        if r["date"] != old.get("date"):
            changed.append(f"status date {old.get('date') or '—'} → {r['date'] or '—'}")
        if r["site"] != old.get("site"):
            changed.append("website updated")
        if changed:
            digest.append(f"• {r['issuer']} ({r['isin'] or r['id']}): " + "; ".join(changed))

    superseded = {t["pdf_key"] for k, t, p in
                  [(a, b, c) for a, b, c in urgent if a == "isin_assigned" and c]
                  for t in [p]}
    for key, old in prev_rows.items():
        if key not in current and old.get("pdf_key") not in superseded:
            urgent.append(("removed", old, None))

    events = []
    ts = now_tbs().isoformat()
    for kind, r, prev in urgent:
        send_telegram(format_alert(kind, r, prev))
        print(f"Alert [{kind}]: {r['issuer']} {r['isin'] or r['id']}")
        events.append({"ts": ts, "kind": kind, "issuer": r["issuer"], "id": r["id"],
                       "isin": r["isin"], "type": r["type"], "date": r["date"],
                       "pdf": r["pdf"], "site": r["site"]})

    if digest:
        send_telegram("📋 <b>Minor updates on NBG page</b>\n\n" + "\n".join(digest[:20]))
        print(f"Minor-update digest sent: {len(digest)} items.")
        for line in digest:
            events.append({"ts": ts, "kind": "minor", "text": line.lstrip("• ")})

    save_state(current, 0, log=events, last_ok=ts)
    if not urgent and not digest:
        print("No changes.")


KIND_LABEL = {
    "new_issuer": "New issuers",
    "new_security": "New securities",
    "isin_assigned": "ISINs assigned",
    "doc_replaced": "Documents replaced",
    "removed": "Entries removed",
}


def daily_digest():
    """Summarise everything logged since 00:00 Tbilisi today. Reads state
    only — no page fetch — so it can never race the checker."""
    data = load_full()
    rows = data.get("rows", {}) or {}
    log = data.get("log", []) or []
    today = now_tbs().date()
    todays = [e for e in log
              if e.get("ts", "")[:10] == today.isoformat()]

    header = f"📊 <b>NBG DAILY DIGEST</b>\n{today.strftime('%d %B %Y')}"

    last_ok = data.get("last_ok", "")
    if last_ok:
        try:
            stale_h = (now_tbs() - datetime.fromisoformat(last_ok)).total_seconds() / 3600
            checked = datetime.fromisoformat(last_ok).strftime("%H:%M")
            freshness = (f"Last successful check: {checked}"
                         + (f"  ⚠️ {stale_h:.0f}h ago" if stale_h > 2 else ""))
        except Exception:
            freshness = "Last successful check: unknown"
    else:
        freshness = "Last successful check: unknown"

    no_isin = sum(1 for r in rows.values() if not r.get("isin"))
    bad_date = sum(1 for r in rows.values() if r.get("invalid_date"))
    quality = []
    if no_isin:
        quality.append(f"{no_isin} entr{'y' if no_isin == 1 else 'ies'} without ISIN")
    if bad_date:
        quality.append(f"{bad_date} without a valid status date")

    major = [e for e in todays if e.get("kind") in KIND_LABEL]
    minor = [e for e in todays if e.get("kind") == "minor"]

    if not major and not minor:
        msg = [header, "", "✅ No new issuers, securities or document changes today.",
               "", f"Tracking {len(rows)} entries.", freshness]
        if quality:
            msg += ["", "Data quality: " + "; ".join(quality)]
        send_telegram("\n".join(msg))
        print("Quiet-day digest sent.")
        return

    counts = {}
    for e in major:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1

    msg = [header, "", "<b>Today's activity</b>"]
    for k, label in KIND_LABEL.items():
        if counts.get(k):
            msg.append(f"{label}: {counts[k]}")
    if minor:
        msg.append(f"Minor updates: {len(minor)}")

    for k, label in KIND_LABEL.items():
        items = [e for e in major if e["kind"] == k]
        if not items:
            continue
        msg += ["", f"<b>{label}</b>"]
        for e in items:
            msg.append(f"• {e['issuer']} — {e.get('type') or 'n/a'}")
            msg.append(f"  ISIN: {e.get('isin') or '— not assigned'}"
                       + (f"  ·  {e['date']}" if e.get("date") else ""))
            if e.get("pdf"):
                msg.append(f"  {e['pdf']}")

    if minor:
        msg += ["", "<b>Minor updates</b>"]
        msg += [f"• {e['text']}" for e in minor[:10]]

    if quality:
        msg += ["", "⚠️ <b>Data quality</b>", "; ".join(quality)]

    msg += ["", f"Tracking {len(rows)} entries.", freshness, "", URL]
    send_telegram("\n".join(msg))
    print(f"Daily digest sent: {len(major)} major, {len(minor)} minor.")


if __name__ == "__main__":
    if "--digest" in sys.argv:
        daily_digest()
    else:
        main()
