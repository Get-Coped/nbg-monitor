# NBG bond publication monitor

Tracks actual bond publications on [NBG's public securities page](https://nbg.gov.ge/supervision/public-companies), with Telegram alerts and a daily bond digest. Ordinary page code, rotating link suffixes, issuer website links, name formatting and status-date edits do not produce alerts.

## What gets reported

- New bond prospectus/document, including preliminary documents without an ISIN.
- New bond entry with no new document.
- Prospectus link replacement or an additional document on an existing bond entry.
- A newly assigned or corrected ISIN.
- A bond entry or document absent on **two consecutive successful checks**. Removal means removed from this NBG page; it does not establish redemption, delisting or cancellation.
- A follow-up with extracted terms if PDF extraction could not finish when the initial alert was sent.

Shares are excluded from alerts and the bond totals. The digest explicitly shows the number of share entries excluded. Issuers are counted by Georgian identification code, not spelling variations of their name. Bond entries without an ISIN remain included. These are entries on this page, **not a complete inventory of outstanding bonds in Georgia**.

## Schedule and cost

The page is fetched at **08:15, 12:15, 16:15 and 20:15 Tbilisi time**, every day. That is four page requests per day rather than 96. Detection happens at the next successful check: typically up to four hours during the day, with a twelve-hour overnight gap. GitHub can delay scheduled jobs.

The **21:00 Tbilisi digest** reads saved data and makes no NBG request. It shows bond entries, change from the preceding digest, issuer count, assigned/pending ISINs, and publications/removals since the last digest. Its reporting window continues from the last successfully delivered digest, so an event after 21:00 is not lost at midnight. A repeated digest run on the same day does not send it twice.

A code/configuration deployment also performs one check. There is no browser, automatic WAF bypass, paid AI API, paid OCR service or external database. Standard GitHub-hosted runners on public repositories are currently free under [GitHub's billing rules](https://docs.github.com/en/actions/concepts/billing-and-usage).

## Prospectus terms

For **new document URLs only**, the monitor downloads the PDF and extracts text locally. At most **two new PDF attempts per check**, up to **20 MB** and the **first 60 physical PDF pages** per document. Successful results are cached in `seen.json`; unchanged documents are not downloaded again. A failed download/extraction gets at most three attempts, at least twelve hours apart. Reaching a limit never suppresses the publication alert.

The alert includes, when supported by recognised text:

- Placement agent (recognises common TBC Capital / Galt & Taggart forms; other names retain source text for review).
- Currency and total offer amount, preserving an explicit “up to”. Multiple offer amounts are flagged for review.
- Coupon rate/range and explicit fixed/floating classification, including a recognised benchmark and spread.
- Tenor and coupon payment frequency.
- Selected investment/transfer/instrument restrictions with PDF page references. Common clearly stated restrictions get concise English labels; other detected clauses retain Georgian source wording.

**This is conservative template-based extraction, not a complete document review.** Missing values are marked “not reliably extracted”. Scanned PDFs need manual review; OCR is not enabled. Complex, unusual and multi-tranche terms need source review. Restriction detection is not exhaustive, and clauses outside the page limit may be missed. The issuer name remains the official name from NBG.

The monitor detects changes to the **listed document URLs**. A PDF silently overwritten at an identical URL is not detected; detecting that would require periodic re-downloads or server version checks. It does not repeatedly download every historical PDF just to check for that possibility.

## Why the old false alerts happened

The old parser stripped the entire HTML page to text and treated any nine-digit number as the start of an issuer record. Page code and script payloads became securities, and adjacent issuers' names, ISINs, dates and PDFs were mixed together. It also sent website-link/date edits as “minor updates”.

The replacement reads NBG's embedded `__NEXT_DATA__` securities records, uses NBG's stable record IDs and verifies every record against the displayed row's columns. NBG's actual fields are `licenseOrOrderNumber` (instrument type) and `licenseType` (ISIN); similarly named fields are unused. The random `?v=` PDF suffix is ignored, while meaningful URL parameters are retained. A uniquely matched issuer+ISIN/document can survive an NBG record-ID replacement.

An empty/unreadable response or a mismatch between displayed rows and source records retains the previous snapshot. A sudden drop of more than 20% first needs a matching subsequent snapshot, then ordinary removal confirmation. Failures interrupt consecutive removal confirmation. The digest marks stale/unverified counts rather than announcing mass removals.

## Upgrade and state

`seen.json` is versioned state. On the **first successful v6 run**, the old v5 data is replaced with a corrected baseline. No historical item is announced as new, the old noisy event log is excluded, and historical PDFs are not batch-downloaded. Activity before that baseline cannot be reconstructed reliably from the corrupted v5 snapshot.

Events are queued and state is written before Telegram delivery. Each successfully delivered chunk is recorded. Delivery errors keep unsent messages queued; the workflow commits state even if checking or delivery fails. Telegram messages are escaped and split at whole-line boundaries. Because Telegram has no idempotency key, a hard crash between Telegram accepting a message and the repository persisting its receipt can still produce a duplicate on retry; this is at-least-once delivery, not an exactly-once guarantee.

The digest reporting cursor is also persisted only after delivery. Corrupt JSON stops the run rather than silently starting over. The daily digest surfaces monitoring freshness; detailed technical errors stay in GitHub Actions logs.

Existing repository secrets are used: `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`. Never add credentials to code, fixtures or `seen.json`.

## Test or preview safely

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest==9.1.1
.venv/bin/python -m pytest -q
.venv/bin/python monitor.py --dry-run --html tests/fixtures/nbg-2026-09-14.html
```

`--dry-run` sends no Telegram messages, writes no state and downloads no PDFs. Without `--html`, it performs one live page request. `--digest --dry-run` previews a digest from an existing v6 state without any website request. `--state PATH` uses a separate state file for experiments.

The fixture is a reduced capture of the real NBG rows on 14 September 2026, with unrelated page content removed. Tests cover mapping errors, cache suffixes, blank ISIN assignment, shares, additional/replaced/removed PDFs, transient disappearance, major source shrinkage, delivery retry, digest boundaries and local term extraction.

## Useful next additions

These are possible extensions, not enabled features:

- Weekly issuance pipeline: new preliminary prospectuses, ISIN assignment and final-document links.
- Searchable prospectus history with each version's indicative/final terms side by side.
- Upcoming maturities and coupon dates after a reviewed historical data backfill.
- Currency/placement-agent breakdowns based on successfully extracted and verified documents.

Counts and these saved-data summaries do not inherently need an AI call. Reliable free-form summaries across unfamiliar prospectus layouts would be a separate, optional enhancement.
