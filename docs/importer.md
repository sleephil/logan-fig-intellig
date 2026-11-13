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
