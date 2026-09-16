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

## On-demand Telegram assistant

The request handler and deployment workflow are included in the repository. **Merging the code does not activate Telegram replies.** Complete the one-time setup below and run the deployment workflow; it verifies the handler, connects the existing bot and sends a welcome menu.

| Ask or tap | Result | NBG traffic |
| --- | --- | --- |
| Overview / `/overview` / “how many bonds?” | Bond entries, issuers, assigned/pending ISINs and last check time | None |
| Recent changes / `/changes 7` | Publications, removals and ISIN changes in the recorded period | None |
| Find issuer / `/issuer Nikora` | Issuer selection and its bond entries | None |
| Bond terms / `/terms GE2700605613` | Selected prospectus links and cached terms, with page references | None |
| Digest now / `/digest` | Saved bond overview and changes in the preceding 24 hours | None |
| Check NBG now / `/refresh` | A bounded fresh-check job; reuses a check made in the preceding 15 minutes | At most one page request |
| Extract terms now | A bounded job for the selected record's uncached PDFs | At most two PDF attempts |

Names can be searched in Georgian, by issuer identification code, or by ISIN. Common Latin aliases include Nikora, RICO, Tegeta, TBC Leasing, ALMA, MBC and Bank of Georgia. Ambiguous issuer matches get a selection menu. This is a command/search assistant with a few common English question forms; it does not call a paid language model or claim to answer arbitrary questions.

Ordinary questions read the public repository's saved snapshot. GitHub/CDN caching may delay visibility of a just-completed job, and every answer shows its check time. “Digest now” does not advance the scheduled daily digest's reporting cursor. Reliable change history starts at the corrected v6 baseline. The available log is bounded by the monitor's retention policy.

The small Cloudflare Worker receives Telegram webhooks; it does not keep a server polling in GitHub Actions. Your configured chat and user ID remain the owner. Friends join by a one-use invitation and use their own private chat. In an owner group, only the configured positive user ID can operate the bot. Only token-salted hashes, selected NBG record IDs and a private-routing flag go into public workflow inputs; recipient IDs, names, invitation tokens and private query text are kept out of the repository and logs.

Heavy work runs in the manual `NBG on-demand request` workflow and shares the monitor's state-writing concurrency group. Requests while another writer is running are declined with a retry message. GitHub can take a minute or longer to start; if no completion arrives, inspect the workflow and try again after it finishes. Public state holds opaque job receipts, and retries reuse cached terms. Private delivery receipts and subscribers live in a SQLite-backed Cloudflare Durable Object. An accepted relay message is durably queued; alarms send it and retry transient failures separately for each recipient. A Telegram acceptance followed by a process crash can still duplicate the last message because Telegram has no sendMessage idempotency key. The 15-minute refresh cooldown and PDF retry limits persist across requests. A shared ten-minute job reservation prevents simultaneous friend dispatches while GitHub starts up. At most 12 on-demand jobs may be submitted per Tbilisi calendar day across everyone; scheduled checks and saved lookups do not consume this allowance.

Existing historical PDFs remain untouched until you explicitly select **Extract terms now**. The same 20 MB, 60-page, two-attempt-per-job and three-attempts-per-document limits apply. Failed attempts are at least fifteen minutes apart for preliminary entries and twelve hours apart for assigned-ISIN entries. Scheduled checks can retry a requested PDF after a temporary failure. Unreadable/scanned text and unrecognised terms still need document review.

### Sharing with friends

1. Send **/invite** to your bot and share the returned link with one friend.
2. They open it and tap **Start**. They receive the same bond menu, publication alerts, daily digest, cached queries, requested checks and prospectus extraction.
3. Generate a separate invitation for each friend. Invitations expire after seven days and can be claimed once. **/cancelinvites** cancels unused links.
4. **/friends** shows members and buttons to remove their access. Only the owner can invite or remove people.
5. Anyone can use **/stop** to pause automatic alerts and digests while retaining query access, then **/start** to resume. Blocking the bot pauses their subscription after Telegram rejects a delivery.

The default limit is 50 invited friends. New members receive future notifications, not a replay of earlier alerts. Replies to requested jobs are routed to the original requester; monitor events discovered during those jobs still notify all active subscribers. Revoking membership also suppresses queued deliveries and private results.

The private Durable Object is created by the Worker deployment's SQLite migration and uses the Workers Free plan. No extra API key, paid AI service, polling loop, KV namespace or subscriber list in GitHub is required. A pending alarm runs only when notifications need delivery. Private request and delivery receipts expire after 30 days; invite links after seven days. Keep observability payload logging disabled.

The monitor and on-demand workflows set `TELEGRAM_WORKER_URL` to the deployed Worker origin and authenticate relay calls with a purpose-specific hash of the existing bot token. During first rollout only, a missing (404) broadcast relay falls back to owner delivery. Private results never fall back to the owner or broadcast. Other relay errors retain the public outbox for retry. A successfully queued notification advances the monitor cursor; per-recipient delivery then proceeds privately in Cloudflare. If moving or forking the deployment, update the Worker origin in both workflows.

### One-time activation

No bot token needs to be pasted into chat or copied into source files. The deployment workflow reuses the existing repository secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

1. In your Cloudflare account, enable Workers and select a `workers.dev` subdomain. Use the Workers Free plan for this small personal handler. Review [Cloudflare's current limits](https://developers.cloudflare.com/workers/platform/limits/) if usage grows.
2. Create a Cloudflare API token using **Edit Cloudflare Workers**, scoped to that account. Add it as the repository Actions secret **CLOUDFLARE_API_TOKEN**. Add the account ID as the Actions **variable CLOUDFLARE_ACCOUNT_ID**.
3. Create a fine-grained GitHub token for **Get-Coped/nbg-monitor only**, with repository **Actions: read and write** (metadata read is implicit). Save it as Actions secret **ON_DEMAND_GITHUB_TOKEN**. Choose an expiry you will remember to renew; expiration prevents requested jobs but does not stop the scheduled monitor. This token lets the Worker dispatch only the repository workflows it is authorized to run.
4. If the bot uses a group chat, add Actions **variable TELEGRAM_USER_ID** with your positive personal Telegram user ID. A private chat needs no extra variable.
5. Open [Deploy Telegram request handler](https://github.com/Get-Coped/nbg-monitor/actions/workflows/deploy-telegram.yml), select **Run workflow** on **main**, and wait for it to succeed. The job deploys the Worker, installs its encrypted secrets, verifies authentication and then connects the webhook/menu. It sends a welcome menu on first activation.
6. In Telegram, try `/overview`, `/issuer Nikora`, and **Extract terms now** for a selected bond. Check a requested job in [NBG on-demand request](https://github.com/Get-Coped/nbg-monitor/actions/workflows/on-demand.yml) if its reply is delayed.

Settings: [Actions secrets](https://github.com/Get-Coped/nbg-monitor/settings/secrets/actions) · [Actions variables](https://github.com/Get-Coped/nbg-monitor/settings/variables/actions).

After handler code changes or credential rotation, rerun the deployment workflow. No additional scheduled page checks are created. Do not enable paid plans solely to activate these features. For setup details see [Cloudflare's GitHub Actions documentation](https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/) and [GitHub's fine-grained token instructions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Run the request-handler tests without a bot or Cloudflare account:

```sh
python -m pytest -q
npm test --prefix telegram-worker
```

## Useful next additions

These are possible extensions, not enabled features:

- Weekly issuance pipeline: new preliminary prospectuses, ISIN assignment and final-document links.
- Searchable prospectus history with each version's indicative/final terms side by side.
- Upcoming maturities and coupon dates after a reviewed historical data backfill.
- Currency/placement-agent breakdowns based on successfully extracted and verified documents.

Counts and these saved-data summaries do not inherently need an AI call. Reliable free-form summaries across unfamiliar prospectus layouts would be a separate, optional enhancement.


## Preliminary publication comes first

Use **Preliminary terms** in the Telegram menu, **/preliminary**, or **/preliminary RICO**. These select bond entries without an ISIN. No ISIN is required for extraction. Preliminary entries also appear first in issuer results and the general bond selector.

The monitor treats a new public entry/document before ISIN assignment as a preliminary publication, whose indicative terms can change after bookbuilding. Prospectus approval (including programme approval), publication of offering terms, and final tranche terms are separate milestones. An ISIN assignment alert asks the reader to check the final terms; it does not invent an approval date or imply that the monitor can observe private NBG discussions. A programme ceiling is not reported as a tranche amount, and programme-wide coupon/tenor options are not presented as final tranche terms.

Extracted results retain coupon ranges (including 6.5–7.0% with a single percent sign), fixed/floating status and reference-rate spreads, placement agent, issue amount/currency, tenor, coupon frequency, issue date where explicitly stated, and cited restrictions. Missing or ambiguous values remain unresolved. Combined preliminary-offering-terms/prospectus PDFs are supported within the existing 60-page limit.

A preliminary download failure may be retried after 15 minutes; documents with an ISIN retain the 12-hour interval. Both keep the three-attempt-per-document limit, two PDF attempts per job and shared daily request limit. Pending preliminary extractions run before other pending PDFs. The bot explains failure reasons and the next retry time, and avoids dispatching jobs when all documents are cached, cooling down or have exhausted their attempts. No additional scheduled NBG checks are introduced.
