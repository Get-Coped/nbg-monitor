"""
NBG prospectus monitor.
Scrapes https://nbg.gov.ge/supervision/public-companies, detects new
approved prospectuses (new ISINs), and sends a Telegram alert.

State (seen ISINs) is stored in seen.json and committed back to the repo
by the GitHub Actions workflow.
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


def fetch_html() -> str:
    """Try plain requests first; fall back to Playwright if the table
    isn't in the raw HTML (client-side rendering)."""
    try:
        r = requests.get(URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if ISIN_RE.search(r.text):
            return r.text
        print("Table not found in raw HTML, falling back to Playwright...")
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


def parse_entries(html: str) -> list[dict]:
    """Extract (issuer, isin, date, pdf_url) tuples.

    Strategy: locate each ISIN in the page, then look in a window of
    surrounding text for the issuer name (before), approval date and
    PDF link (after). This is layout-tolerant.
    """
    # Pull hrefs out of their tags first so PDF links survive tag stripping.
    text = re.sub(r'<a\s[^>]*?href="([^"]+)"[^>]*>', r" \1 ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    entries = []
    for m in ISIN_RE.finditer(text):
        isin = m.group(0).replace(" ", "")
        before = text[max(0, m.start() - 300) : m.start()]
        after = text[m.end() : m.end() + 600]

        # Issuer: last "Georgian name + 9-digit ID" pair before the ISIN
        # (security type text like "ობლიგაცია" may sit between ID and ISIN)
        issuer_matches = re.findall(
            r"([ა-ჰ][ა-ჰ0-9\s\-\.]{3,80}?)\s*(\d{9})", before
        )
        issuer = issuer_matches[-1][0].strip() if issuer_matches else "უცნობი ემიტენტი"
        issuer = re.sub(r"^ვებგვერდი\s*", "", issuer)

        pdf_m = PDF_RE.search(after)
        pdf = pdf_m.group(0) if pdf_m else ""

        date_m = DATE_RE.search(after)
        date = date_m.group(0) if date_m else ""

        entries.append({"isin": isin, "issuer": issuer, "date": date, "pdf": pdf})

    # Deduplicate by ISIN, keep first occurrence
    seen = set()
    unique = []
    for e in entries:
        if e["isin"] not in seen:
            seen.add(e["isin"])
            unique.append(e)
    return unique


def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(isins: set):
    STATE_FILE.write_text(json.dumps(sorted(isins), indent=2))


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; printing instead:\n" + msg)
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    resp.raise_for_status()


def main():
    html = fetch_html()
    entries = parse_entries(html)

    if not entries:
        # Page structure changed or fetch failed silently — alert once so
        # the monitor doesn't die quietly.
        send_telegram("⚠️ NBG monitor: no entries parsed. Check page structure.")
        sys.exit(1)

    seen = load_state()
    current = {e["isin"] for e in entries}

    if not seen:
        # First run: baseline only, no alerts.
        save_state(current)
        print(f"Baseline saved: {len(current)} ISINs.")
        return

    new = [e for e in entries if e["isin"] not in seen]
    for e in new:
        msg = (
            "🆕 <b>ახალი პროსპექტი დამტკიცდა NBG-ზე</b>\n\n"
            f"ემიტენტი: {e['issuer']}\n"
            f"ISIN: {e['isin']}\n"
            f"დამტკიცების თარიღი: {e['date']}\n"
            f"პროსპექტი: {e['pdf'] or 'ლინკი ვერ მოიძებნა'}\n\n"
            f"{URL}"
        )
        send_telegram(msg)
        print(f"Alerted: {e['isin']} ({e['issuer']})")

    if new:
        save_state(seen | current)
    else:
        print(f"No new prospectuses. {len(current)} ISINs on page.")


if __name__ == "__main__":
    main()
