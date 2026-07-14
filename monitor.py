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
    """Returns (keys:set, version:int).
    version 0 = no state (first run), 1 = old ISIN list, 2 = dict without
    version tag (pdf-keyed), 3 = current format (pdf+isin keyed)."""
    if not STATE_FILE.exists():
        return set(), 0
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return set("isin:" + i for i in data), 1
    if data.get("v") == 3:
        return set(data.get("keys", [])), 3
    return set(data.get("keys", [])), 2


def save_state(keys: set):
    STATE_FILE.write_text(
        json.dumps({"v": 3, "keys": sorted(keys)}, indent=2), encoding="utf-8"
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

    seen, version = load_state()

    all_keys = set(seen)
    alerts = []  # (entry, kind) where kind: "new" | "isin_assigned"

    for e in entries:
        k_pdf = e["key"] if e["key"].startswith("pdf:") else ""
        k_isin = ("isin:" + e["isin"]) if e["isin"] else ""

        new_pdf = bool(k_pdf) and k_pdf not in seen
        new_isin = bool(k_isin) and k_isin not in seen

        if version == 0:
            pass  # first run: baseline everything silently
        elif version == 1:
            # old ISIN-list state: alert only genuinely new ISINs
            if new_isin:
                alerts.append((e, "new"))
        elif version == 2:
            # pdf-keyed state: alert on new PDFs; absorb all ISIN keys
            # silently this one time (they were never tracked before)
            if new_pdf:
                alerts.append((e, "new"))
        else:
            if new_pdf:
                alerts.append((e, "new"))
            elif new_isin:
                alerts.append((e, "isin_assigned"))

        if k_pdf:
            all_keys.add(k_pdf)
        if k_isin:
            all_keys.add(k_isin)

    for e, kind in alerts:
        if kind == "isin_assigned":
            msg = (
                "✅ <b>ISIN მიენიჭა / საბოლოო ეტაპი</b>\n\n"
                f"ემიტენტი: {e['issuer']}\n"
                f"ISIN: {e['isin']}\n"
                f"თარიღი: {e['date'] or '—'}\n"
                f"პროსპექტი: {e['pdf'] or '—'}\n\n{URL}"
            )
        else:
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
        print(f"Alerted ({kind}): {e['issuer']} {e['isin'] or e['key']}")

    save_state(all_keys)
    if version == 0:
        print(f"Baseline saved: {len(all_keys)} keys.")
    elif not alerts:
        print(f"No new prospectuses. {len(all_keys)} keys tracked.")


if __name__ == "__main__":
    main()
