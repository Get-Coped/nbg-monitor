"""Bounded, local PDF extraction. No AI API, OCR service, or paid dependency.

Only report values supported by text and attach physical PDF page references.
Unrecognised layouts stay explicitly unresolved instead of inventing terms.
"""

import hashlib
import io
import re
from datetime import datetime

import requests
from pypdf import PdfReader

from registry import clean, document_url

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 60
HEADERS = {"User-Agent": "NBG-Publication-Monitor/6 (+https://github.com/Get-Coped/nbg-monitor)"}
NUMBER = r"(?:\d{1,3}(?:[, ]\d{3})+|\d{6,})"
CURRENCY = r"აშშ\s*დოლარ\w*|ლარ(?:ი|ის|ში|ამდე)?|ევრო\w*|USD|GEL|EUR"
PERCENT = r"\d{1,2}(?:[.,]\d+)?\s*%(?:\s*[-–—]\s*\d{1,2}(?:[.,]\d+)?\s*%)?"


def currency(value):
    if re.search(r"აშშ|USD", value, re.I):
        return "USD"
    if re.search(r"ევრო|EUR", value, re.I):
        return "EUR"
    return "GEL"


def number(value):
    return f"{int(re.sub(r'[, ]', '', value)):,}"


def field(value, page, evidence):
    return {"value": value, "page": page, "evidence": clean(evidence)[:650]}


def extract_terms(pages):
    """Accept a list of page strings, making financial extraction testable offline."""
    texts = [clean(p) for p in pages]
    result = {"fields": {}, "restrictions": [], "pages_reviewed": len(pages),
              "note": "Automatic text extraction; missing fields need document review. Restrictions are not exhaustive."}
    fields = result["fields"]
    cover = " ".join(texts[:2])
    # Use the heading, not boilerplate about an earlier preliminary prospectus.
    stage = re.search(r"წინასწარი|საბოლოო|preliminary|indicative|final", cover[:400], re.I)
    if stage and re.search(r"წინასწარი|preliminary|indicative", stage[0], re.I):
        result["stage"] = "Preliminary / indicative"
    elif stage:
        result["stage"] = "Final terms / prospectus"
    else:
        result["stage"] = "Check document for final/indicative status"

    # Offer size: cover-page total nominal amount, never financial-statement data.
    for p, text in enumerate(texts[:3], 1):
        candidates = list(re.finditer(rf"({NUMBER})\s*(?:\([^)]{{1,80}}\)\s*)?({CURRENCY})", text, re.I))
        offers = []
        for m in candidates:
            context = text[max(0, m.start()-100):m.end()+140]
            if re.search(r"ჯამური\s*ნომინალურ|aggregate\s*(?:principal|nominal)|issue\s*(?:size|amount)", context, re.I):
                offers.append((m, context))
        if offers:
            unique = {(number(m[1]), currency(m[2])) for m, _ in offers}
            if len(unique) == 1:
                m, context = offers[0]
                up_to = "Up to " if re.search(r"ამდე|up to", context, re.I) else ""
                fields["Currency and amount"] = field(up_to + currency(m[2]) + " " + number(m[1]), p, context)
            else:
                fields["Currency and amount"] = field("Multiple amounts/tranches — review source", p, " ".join(c for _, c in offers))
            break

    for p, text in enumerate(texts, 1):
        if "Placement agent" not in fields:
            anchor = re.search(r"განთავსების\s*(?:აგენტ|თანააგენტ)|placement agent|joint lead manager", text, re.I)
            if anchor:
                context = text[max(0, anchor.start()-220):anchor.end()+450]
                names = []
                for pattern, name in [(r"თიბისი\s*კაპიტალ|TBC\s*Capital", "TBC Capital"),
                                      (r"გალტ\s*(?:ენდ|&|და)\s*თაგარტ|Galt\s*(?:&|and)\s*Taggart", "Galt & Taggart")]:
                    if re.search(pattern, context, re.I):
                        names.append(name)
                if names:
                    fields["Placement agent"] = field(" / ".join(names), p, context)
                else:
                    fields["Placement agent"] = field("Review source: " + context[:250], p, context)
        if "Coupon" not in fields:
            for anchor in re.finditer(r"განაკვეთი\s*\(?კუპონი|საპროცენტო\s*განაკვეთი|coupon\s*(?:rate)?", text, re.I):
                context = text[max(0, anchor.start()-90):anchor.end()+500]
                rate = re.search(PERCENT, context)
                if not rate:
                    continue
                base = re.search(r"(?:TIBR\s*\d?\s*M?|SOFR|EURIBOR|NBG\s*(?:refinancing|policy)\s*rate)", context, re.I)
                if base:
                    value = clean(base[0]).upper() + " + " + clean(rate[0])
                    # A spread is explicit only when the source shows '+' or 'დამატ'.
                    if not re.search(r"\+|დამატ|plus", context, re.I):
                        value = "Floating — review source formula: " + context[:250]
                    else:
                        value = "Floating: " + value
                elif re.search(r"ფიქსირებული|fixed", cover + " " + context, re.I) and not re.search(r"ცვლადი|floating", context, re.I):
                    value = "Fixed: " + clean(rate[0])
                else:
                    value = clean(rate[0]) + " (fixed/floating not reliably extracted)"
                fields["Coupon"] = field(value, p, context)
                break
        if "Tenor" not in fields:
            tenor = re.search(r"გამოშვებ\w*\s*(?:თარიღიდან\s*)?(\d+)\s*(?:\([^)]*\)\s*)?(თვის|თვე|წლის|წელი)", text)
            if not tenor:
                tenor = re.search(r"(?:დაფარვის\s*ვადა|tenor|maturity)\s*[:—–-]?\s*(\d+)\s*(months?|years?|თვე|წელი)", text, re.I)
            if tenor:
                unit = "months" if re.search(r"თვ|month", tenor[2], re.I) else "years"
                fields["Tenor"] = field(f"{tenor[1]} {unit}", p, text[max(0, tenor.start()-50):tenor.end()+100])
            elif re.search(r"უვადო\s*ობლიგაცი|perpetual\s*(?:notes|bonds)", text, re.I):
                fields["Tenor"] = field("Perpetual", p, text[:500])
        if "Coupon payments" not in fields:
            for pattern, value in [
                (r"წელიწადში\s*ორჯერ|ყოველ\s*(?:ექვს|6)\s*თვე|semi[- ]?annual", "Semiannual"),
                (r"წელიწადში\s*ოთხჯერ|ყოველ\s*(?:სამ|3)\s*თვე|კვარტალურ|quarterly", "Quarterly"),
                (r"წელიწადში\s*ერთხელ|annually", "Annual"),
                (r"ყოველთვიურ|monthly", "Monthly"),
            ]:
                for match in re.finditer(pattern, text, re.I):
                    context = text[max(0, match.start()-200):match.end()+130]
                    if re.search(r"პროცენტ|სარგებ|კუპონ|coupon|interest", context, re.I):
                        fields["Coupon payments"] = field(value, p, context)
                        break
                if "Coupon payments" in fields:
                    break

    # Restrictions are selected source passages, not a claim of a complete legal review.
    # Preserve the Georgian wording, exceptions and scope rather than translating
    # complex eligibility/transfer clauses using fragile numeric heuristics.
    restriction_patterns = [
        r"მინიმუმ\s*" + NUMBER,
        r"minimum\s*(?:investment|holding|denomination)",
        r"აკრძალულია\s*.{0,40}თანასაკუთრება",
        r"მხოლოდ\s*.{0,40}(?:გათვითცნობიერებულ|კვალიფიციურ)",
        r"(?:permanent\s*write[- ]down|მუდმივ\w*\s*ჩამოწერ)",
        r"(?:transfer\s*restrictions|გადაცემის\s*შეზღუდვ)",
    ]
    for pattern in restriction_patterns:
        for p, text in enumerate(texts, 1):
            m = re.search(pattern, text, re.I)
            if m:
                start = max(0, m.start()-75)
                if start:
                    start = text.find(' ', start) + 1
                end = min(len(text), m.end()+300)
                if end < len(text):
                    end = text.rfind(' ', start, end)
                excerpt = text[start:end]
                value = excerpt
                if 'თანასაკუთრება' in m[0]:
                    value = 'Joint ownership of an individual bond is prohibited.'
                elif re.match(r'მინიმუმ\s*' + NUMBER, m[0]):
                    detail = text[m.start():m.end()+180]
                    amount = re.search(NUMBER, detail)
                    if amount and re.search(r'ლარის\s*ექვივალენტ', detail) and re.search(r'აშშ\s*დოლარ', detail):
                        value = 'Minimum investment: GEL ' + number(amount[0]) + ' equivalent in USD. See source for holding and transfer conditions.'
                if excerpt not in [r["value"] for r in result["restrictions"]]:
                    result["restrictions"].append(field(value, p, excerpt))
                break
    return result


def read_pdf(content):
    if not content.startswith(b"%PDF-"):
        raise ValueError("Response is not a PDF")
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF needs manual review")
    pages = [(page.extract_text(extraction_mode="layout") or "") if '/Contents' in page else ''
             for page in reader.pages[:MAX_PAGES]]
    if len(clean(" ".join(pages))) < 100:
        return {"status": "needs_review", "reason": "Scanned/image PDF: text extraction unavailable; OCR not enabled."}
    terms = extract_terms(pages)
    terms["total_pages"] = len(reader.pages)
    return {"status": "ready", "terms": terms, "sha256": hashlib.sha256(content).hexdigest()}


def fetch_terms(url):
    # No redirects to arbitrary sites and no unbounded retries/downloads.
    url = document_url(url)
    with requests.get(url, headers=HEADERS, timeout=(10, 45), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("PDF redirect needs review")
        if int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
            raise ValueError("PDF exceeds 20 MB extraction limit")
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("PDF exceeds 20 MB extraction limit")
    return read_pdf(bytes(data))


def enrich(state, now, fetcher=fetch_terms, limit=2):
    """Only new document URLs. A failed extraction never prevents a publication alert."""
    processed = 0
    for url, item in state.get("documents", {}).items():
        if item.get("status") not in {"pending", "retry"}:
            continue
        attempts = item.get("attempts", 0)
        if attempts >= 3:
            continue
        if item.get("last_attempt") and (now - datetime.fromisoformat(item["last_attempt"])).total_seconds() < 12 * 3600:
            continue
        if processed >= limit:
            break
        processed += 1
        item.update(attempts=attempts+1, last_attempt=now.isoformat())
        try:
            item.update(fetcher(url))
        except Exception as exc:
            # Do not log response/request URLs (Telegram tokens can occur elsewhere).
            item.update(status="needs_review" if attempts+1 >= 3 else "retry",
                        reason="PDF extraction unavailable; document review required.")
            print("PDF extraction failed:", type(exc).__name__)
        if item.get("announced") and item.get("status") == "ready":
            # Follow-up is queued by the monitor after successful delayed extraction.
            item["followup_due"] = True
    return processed
