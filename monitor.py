"""
NBG prospectus monitor v2.
Detects new prospectuses on https://nbg.gov.ge/supervision/public-companies
including PRE-ISIN entries (bookbuilding-stage prospectuses published before
an ISIN is assigned). Detection is keyed on the prospectus PDF URL, with
ISIN as a secondary key for old rows that have no PDF.

State is stored in seen.json (auto-migrates from the old ISIN-list format
without re-alerting on everything).
"""

import json
import os
import re
import sys
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

ISIN_RE = re.compile(r"GE\s?\d{10}")
DATE_RE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
PDF_RE = re.compile(r"https://nbg\.gov\.ge/fm/cm/issuers/[^\s\"'<>]+\.pdf[^\s\"'<>]*")
ISSUER_ID_RE = re.compile(r"([ა-ჰ][ა-ჰ0-9\s\-\.„“\"]{3,80}?)\s*(\d{9})")


def fetch_html() -> str:
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if PDF_RE.search(r.text) or ISIN_RE.search(r.text):
            return r.text
        print("Table not in raw HTML, falling back to Playwright...")
    except Exception as e:
        print(f"requests failed ({e}), falling back to Playwright...")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(URL, wait_until="networkidle", timeout=60000)
        html = page.content()
        browser.close()
    return html


def clean_issuer(name: str) -> str:
    name = re.sub(r"^ვებგვერდი\s*", "", name.strip())
    return name or "უცნობი ემიტენტი"


def pdf_key(url: str) -> str:
    """Stable key for a PDF: path without the ?v= cache-buster."""
    return "pdf:" + url.split("?")[0]


def parse_entries(html: str) -> list[dict]:
    """Extract rows anchored on the prospectus PDF link, so entries WITHOUT
    an ISIN (pre-bookbuilding publications) are captured too. A second pass
    picks up legacy rows that have an ISIN but no PDF."""
    text = re.sub(r'<a\s[^>]*?href="([^"]+)"[^>]*>', r" \1 ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    entries = []
    used_isins = set()
    used_pdfs = set()

    # Pass 1: PDF-anchored (catches pre-ISIN rows).
    for m in PDF_RE.finditer(text):
        pdf = m.group(0)
        k = pdf_key(pdf)
        if k in used_pdfs:
            continue
        used_pdfs.add(k)

        before = text[max(0, m.start() - 400) : m.start()]
        after = text[m.end() : m.end() + 200]
        # Truncate at next row's start (issuer+ID pattern or next PDF) so a
        # dateless row can't steal the following row's date.
        boundary = ISSUER_ID_RE.search(after)
        next_pdf = PDF_RE.search(after)
        cut = min(x.start() for x in (boundary, next_pdf) if x) if (boundary or next_pdf) else len(after)
        after = after[:cut]

        issuer_matches = list(ISSUER_ID_RE.finditer(before))
        if issuer_matches:
            im = issuer_matches[-1]
            issuer = clean_issuer(im.group(1))
            # ISIN belongs to this row only if it appears AFTER this row's
            # 9-digit ID (row order: issuer, id, type, isin, pdf). Searching
            # the whole window would steal the previous row's ISIN.
            isin_m = ISIN_RE.search(before[im.end():])
        else:
            issuer = "უცნობი ემიტენტი"
            isin_m = None

        isin = isin_m.group(0).replace(" ", "") if isin_m else ""
        if isin:
            used_isins.add(isin)

        date_m = DATE_RE.search(after)
        entries.append({
            "key": k,
            "issuer": issuer,
            "isin": isin,
            "date": date_m.group(0) if date_m else "",
            "pdf": pdf,
        })

    # Pass 2: ISIN-anchored, for rows with no PDF (legacy listings).
    for m in ISIN_RE.finditer(text):
        isin = m.group(0).replace(" ", "")
        if isin in used_isins:
            continue
        used_isins.add(isin)

        before = text[max(0, m.start() - 300) : m.start()]
        after = text[m.end() : m.end() + 300]
        issuer_matches = ISSUER_ID_RE.findall(before)
        issuer = clean_issuer(issuer_matches[-1][0]) if issuer_matches else "უცნობი ემიტენტი"
        date_m = DATE_RE.search(after)
        entries.append({
            "key": "isin:" + isin,
            "issuer": issuer,
            "isin": isin,
            "date": date_m.group(0) if date_m else "",
            "pdf": "",
        })

    return entries


def load_state():
    """Returns (keys:set, legacy_isins:set|None). Old format = plain list of
    ISIN strings; we migrate it silently."""
    if not STATE_FILE.exists():
        return set(), None
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return set(data.get("keys", [])), None
    return set(), set(data)  # old format


def save_state(keys: set):
    STATE_FILE.write_text(
        json.dumps({"keys": sorted(keys)}, indent=2), encoding="utf-8"
    )


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; printing instead:\n" + msg)
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


def main():
    html = fetch_html()
    entries = parse_entries(html)

    if not entries:
        send_telegram("⚠️ NBG monitor: no entries parsed. Check page structure.")
        sys.exit(1)

    seen_keys, legacy_isins = load_state()
    current_keys = {e["key"] for e in entries}

    if not seen_keys and legacy_isins is None:
        save_state(current_keys)
        print(f"Baseline saved: {len(current_keys)} entries.")
        return

    if legacy_isins is not None:
        # Migrating from old ISIN-list state: alert only on ISINs that are
        # genuinely new; baseline everything else (incl. all PDFs) silently.
        new = [e for e in entries if e["isin"] and e["isin"] not in legacy_isins]
        print(f"Migrated old state ({len(legacy_isins)} ISINs) to key format.")
    else:
        new = [e for e in entries if e["key"] not in seen_keys]

    for e in new:
        stage = "" if e["isin"] else "\n⏳ ISIN ჯერ არ არის მინიჭებული (სავარაუდოდ bookbuilding ეტაპი)"
        msg = (
            "🆕 <b>ახალი პროსპექტი გამოქვეყნდა NBG-ზე</b>\n\n"
            f"ემიტენტი: {e['issuer']}\n"
            f"ISIN: {e['isin'] or '—'}\n"
            f"თარიღი: {e['date'] or '—'}\n"
            f"პროსპექტი: {e['pdf'] or 'ლინკი ვერ მოიძებნა'}"
            f"{stage}\n\n{URL}"
        )
        send_telegram(msg)
        print(f"Alerted: {e['key']} ({e['issuer']})")

    save_state(seen_keys | current_keys)
    if not new:
        print(f"No new prospectuses. {len(current_keys)} entries on page.")


if __name__ == "__main__":
    main()
