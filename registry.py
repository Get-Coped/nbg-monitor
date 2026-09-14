"""Read the securities records NBG actually renders; never scrape page-wide text."""

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

URL = "https://nbg.gov.ge/supervision/public-companies"
TBILISI = timezone(timedelta(hours=4))
KINDS = {"ობლიგაცია": "bond", "ჩვ. აქცია": "share", "პრ. აქცია": "share"}


class SourceError(ValueError):
    """Incomplete or unexpected source: retain the last good snapshot."""


def clean(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def document_url(value):
    """NBG adds a random ?v= value to the same PDF on each page render."""
    value = clean(value)
    if value.startswith("cm/"):
        value = "/fm/" + value
    parts = urlsplit(urljoin(URL, value))
    path = unicodedata.normalize("NFC", unquote(parts.path))
    if parts.hostname != "nbg.gov.ge" or parts.scheme != "https" or not path.lower().endswith(".pdf"):
        raise SourceError("Unrecognised approved-document URL")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in {"v", "fbclid"} and not k.lower().startswith("utm_")]
    return urlunsplit(("https", "nbg.gov.ge", path, urlencode(sorted(query)), ""))


def normal_isin(value):
    value = re.sub(r"\s+", "", clean(value)).upper()
    if value and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", value):
        raise SourceError("Unexpected ISIN field")
    return value


def parse_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    try:
        records = json.loads(script.string)["props"]["initialProps"]["pageProps"]["organizations"]
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        raise SourceError("NBG securities dataset is missing or unreadable") from exc
    if not isinstance(records, list) or not records:
        raise SourceError("NBG securities dataset is empty")
    rendered = soup.select(".tb-row")
    if len(rendered) != len(records):
        raise SourceError("Rendered rows and source dataset disagree")
    rows = {}
    for record, element in zip(records, rendered):
        try:
            source_id = str(record["id"])
            issuer_id = clean(record["identificationCode"])
            issuer = clean(record["title"])
            # These are NBG's actual mapped columns (the tempting fields
            # publicSecuritiesType/securitiesIdentificationNumber are unused).
            security_type = clean(record["licenseOrOrderNumber"])
            isin = normal_isin(record["licenseType"])
            docs = record["approvedDocuments"]
            if not isinstance(docs, list):
                raise SourceError("Approved documents is not a list")
            documents = sorted({document_url(d) for d in docs})
            columns = element.select_one(".dynamic-columns").find_all(recursive=False)
            visible_name = clean(element.find_all(recursive=False)[0].get_text(" ", strip=True))
            visible_docs = sorted({document_url(a["href"]) for a in columns[3].select("a[href]")})
            if (len(columns) != 6 or visible_name != issuer
                    or clean(columns[0].get_text()) != issuer_id
                    or clean(columns[1].get_text()) != security_type
                    or normal_isin(columns[2].get_text()) != isin
                    or visible_docs != documents):
                raise SourceError("Rendered securities columns and dataset disagree")
            if not source_id.isdigit() or not re.fullmatch(r"\d{9}", issuer_id) or not issuer:
                raise SourceError("Invalid issuer or record identity")
            if security_type not in KINDS:
                raise SourceError("Unrecognised instrument type; classification needs review")
            date = ""
            if record.get("releaseDate"):
                date = datetime.fromisoformat(record["releaseDate"].replace("Z", "+00:00")).astimezone(TBILISI).date().isoformat()
            row = {"source_id": source_id, "id": issuer_id, "issuer": issuer,
                   "type": security_type, "kind": KINDS[security_type], "isin": isin,
                   "documents": documents, "date": date}
        except (KeyError, IndexError, AttributeError, TypeError, ValueError) as exc:
            raise SourceError(f"Invalid securities record: {exc}") from exc
        key = "nbg:" + source_id
        if key in rows:
            raise SourceError("Duplicate NBG record ID")
        rows[key] = row
    isins = [r["isin"] for r in rows.values() if r["isin"]]
    if len(isins) != len(set(isins)):
        raise SourceError("Duplicate ISIN: cannot establish reliable bond count")
    return rows


def bond_counts(rows):
    bonds = [r for r in rows.values() if r["kind"] == "bond"]
    return {"bonds": len(bonds), "issuers": len({r["id"] for r in bonds}),
            "assigned": sum(bool(r["isin"]) for r in bonds),
            "awaiting_isin": sum(not r["isin"] for r in bonds),
            "shares": sum(r["kind"] == "share" for r in rows.values())}
