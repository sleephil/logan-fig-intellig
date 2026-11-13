# FIG import operations and evidence

The configured CIK 0001269119 is Oppenheimer Asset Management Inc. It is not a
complete Oppenheimer platform census. The daily job and manual button run the
same importer, with a single PostgreSQL session advisory lock.

## Sources

- Filing discovery: https://data.sec.gov/submissions/CIK0001269119.json
- Raw primary XML and information-table XML: the official EDGAR accession directory.
  XSL display prefixes in discovery metadata are removed only to retrieve raw XML.
- Bulk discovery: https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
- ZIPs contain SUBMISSION, COVERPAGE, SUMMARYPAGE and INFOTABLE TSVs joined by
  ACCESSION_NUMBER. Other-manager sequence mappings are retained. Files are streamed
  from the archive, never extracted using archive-supplied filesystem paths.
- Source units: https://www.sec.gov/filergroup/announcements-old/edgar-release-2241
- Fair access: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

Incremental XML is more efficient for one configured filer than downloading every
quarter's full-market ZIP. `python worker.py --bulk` is the explicit historical
backfill path; it discovers archive links, filters by HISTORY_START, reads selected
CIKs, and finishes with XML updates newer than the latest bulk release. The existing
schedule runs `python worker.py`, not a full bulk reload every morning.

## Configuration and acceptance

DATABASE_URL and SEC_USER_AGENT (firm plus contact email) are required. FIG_API_TOKEN
protects all private endpoints. Do not put any secret in browser JavaScript.
HISTORY_START is a reporting-period cutoff (default 2023-09-30); no claim of history
before that cutoff is made. FIG_SYNC_ON_STARTUP=true performs an initial/recovery
incremental check after deployment, using the same locking and audit flow.

A successful deployment is not a successful import. Look for FIG_FILING_READBACK_VERIFIED
and FIG_IMPORT_FINISHED with success, the actual accession, period, row count, USD
value and snapshot/source hashes. `/api/status` reports checks separately from data
period and records heartbeat/stage. Missing configuration returns HTTP 503 before
claiming an import was queued. A process without heartbeat is marked stalled;
on the next import, old running jobs without the lock become interrupted.

## Records and rollback

`fig_filings_v2` contains append-only, content-addressed validated filing snapshots,
including raw source fields, original dollar value/multiplier and URLs/hashes.
`fig_import_runs` contains operational lifecycle state. `fig_import_events` and
`fig_release_events` append evidence of work and release identity. Legacy `filings`,
`holdings` and `ingest_runs` tables remain untouched. Previously stored prototype
rows are not silently represented as reconciled. Repeated imports do not duplicate
an accession's same snapshot. A new parser or changed normalized filing makes a
new revision, retaining the prior one. No destructive migration is needed to roll
back code; do not drop evidence tables on rollback.

The Toolkit release tracker maps this change to the page and API paths. The FIG
page displays its frontend release and the live backend version/commit. Neither
repository history nor application logs alone imply regulatory certification,
independent backup/restore validation, tamper-proof storage or WORM compliance.

## Manager report v0.3.0

`/api/managers/{cik}` aggregates the SEC other-included-manager list and the
information table's Other Manager references from already verified snapshots.
Sequence numbers are resolved within each accession, including amendments.
Stable CIK/13F identifiers are preferred; exact normalized names are used where
identifiers are absent. Conflicting identifiers are unresolved, never merged.
The report reconciles single-linked, jointly linked (once), unassigned, unresolved,
and options-reference values to the effective filing total. Manager columns that
include shared attribution are not additive. No arbitrary split is invented.
Absent managers are not reported, not zero; missing quarters do not get quarter
change calculations. Pairs use adjacent, complete, non-shared manager records,
opposite changes and magnitude ratio >=0.5, ranked by ratio then smaller amount.
At most 20 candidates per interval; similarity is not a transfer probability.
Public definitions: https://www.sec.gov/rules-regulations/staff-guidance/frequently-asked-questions-about-form-13f (questions 37 and 48).
This endpoint reports covered holdings, not comprehensive platform assets, true
flows, return-adjusted flows or independently verified external relationships.

## SEC-wide directory (0.4.0)
The selector includes all 13F filing identities discovered in SEC quarterly master indexes since 1994, including notice-only and historical filers. It is NOT a promise that all holdings or outside-manager histories are preloaded. Quarterly index ZIPs are streamed and decompressed in memory with bounded size, path validation, CIK/form/date validation, atomic publication and SHA-256 receipts. Coverage reports successful versus expected index quarters, failure sources, freshness and date range. Current/prior indexes refresh on scheduled checks; sealed historical indexes are cached.

Selecting an unloaded entity and choosing Load/refresh this firm saves a durable deduplicated queue request. A single advisory-locked worker downloads that CIK's individual filings using the existing HISTORY_START (default 2023-09-30), validates/saves them using the existing importer, then updates the firm's status. Requested firms join subsequent scheduled checks. The directory alone never starts thousands of full-history imports. Queue requests survive process restart, and an API worker resumes them. Notifications and statuses distinguish queued/running/ready/partial/failed, notice-only, and no holdings in the configured window. Notice filings are not holdings and are not zero assets. Other-manager disclosures vary across filers; absence is not evidence that no external mandates exist. Existing manager amounts, attribution, style uncertainty and pair caveats remain unchanged.

Additive schema: fig_filer_directory, fig_directory_sources, fig_directory_checks, fig_filer_requests. No existing rows or permissions are removed. All APIs retain the same private token boundary; Toolkit keeps its existing Entra and origin checks. No extra credentials or subscriptions are required.


## Market-wide preload and reverse reporting (0.5.0)
The scheduled FIG worker preloads the existing `HISTORY_START` window (default
2023-09-30) for all reporting entities. `FIG_MARKET_PRELOAD=false` disables this
stage without changing the original importer. No new infrastructure is required.
Bulk archives are downloaded once, stream-read and reduced to manager-reference
buckets. A disk-backed duplicate row-ID check bounds memory. Complete raw counts
and USD totals must reconcile before a filing is accepted. Bad filings remain in
an explicit rejection ledger, never silently assigned zero assets.

The recent EDGAR index fills the gap after the newest published archive. Each
accession is checkpointed, retried at most twice per daily attempt, and reattempted
on a later day after failure. Restart resumes rather than starts over. Index
coverage and holdings coverage are separate; the current filing season can still
be incomplete even after a successful availability check.

Report requests perform only reads of the indexed firm-manager-quarter table and
published report generation. No browser request builds the market data. A
complete generation is published atomically; partial validated source coverage
is explicitly marked. Old derived generations may be retired after a grace
period; source summaries, raw snapshots and audit events are retained.

Reverse totals are gross attribution across legal reporting entities, potentially
with overlapping holdings. Do not call them unique assets, total AUM, confirmed
customer relationships or measured flows. Quarter-change totals use only the
same comparable reporting firms. Exact names without stable IDs carry an explicit
identity limitation; fuzzy matches are never auto-merged.
