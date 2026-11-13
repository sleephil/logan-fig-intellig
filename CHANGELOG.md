## FIG 0.4.0 - 2026-11-13 - SEC-wide filer directory
- Replace one-firm whitelist with official SEC quarterly master-index identity directory, 1994 onward.
- Keep directory discovery separate from reconciled holdings; include notices and historical filers without invented balances.
- Use a durable, deduplicated selected-filer queue and existing cross-process import lock; daily checks refresh the directory and previously requested firms.
- Retain source hashes, explicit incomplete/stale coverage, importer safeguards, all original snapshots and independent version records.

## 0.3.0 - 2026-11-13 - Outside-manager reporting
- Added authenticated `/api/managers/{cik}` over existing reconciled snapshots; no importer or storage migration.
- Totals, quarter/year changes, all-period history and paired opposite changes use accession-local manager mappings, stable IDs where present and conservative exact names otherwise.
- Joint attribution, unresolved and unassigned rows, options reference values, missing periods and amendments remain explicit and reconcilable. No asserted money transfers or total-manager AUM.
- Source accessions, source/snapshot hashes and method version retained. Toolkit page changes have a separate frontend release record.

# FIG Intelligence change log

## 0.2.0 - 2026-11-13 - Verified SEC importer

Scope: importer, import-status display, filing-history values, exploratory-trend
units and source/version disclosures. No change to client portfolios, trading,
permissions, other tools or the existing daily SEC schedule.

- Manual and scheduled imports use the same declared SEC identification header.
- Individual raw XML is the normal incremental path. `worker.py --bulk` safely
  reads official historical ZIP/TSV data and then checks newer XML filings.
- Strict identity, period, row-count and dollar-total reconciliation precedes save.
- Values are USD once; filing dates before 2023-01-03 use the original thousand-
  dollar convention. Reporting-period dates are not used to infer units.
- Raw rows, shared-manager identifiers, option/share distinctions, provenance and
  revisions are retained. Restatements and additional-holdings amendments differ.
- Durable jobs and database advisory locking prevent concurrent duplicate imports;
  interrupted/stalled processes are not represented as successful background work.
- Independent committed-snapshot readback provides concrete success evidence.
- Additive `fig_*` tables preserve legacy records; only validated v2 data is shown.
- Version, deployment commit, source checksums, import events and a release record
  are retained for review. This is not a regulatory certification or WORM claim.

Machine-readable page mapping and limitations: `CHANGELOG.json`.
Tests and live verification receipts are attached to the release pull request.
Do not infer successful data ingestion from Railway deployment status alone.

## 0.1.0 - 2026-11-13 - Initial prototype (retrospective baseline)

Baseline: `cd6135c81adb423ab1950f672066fd6a9a6d5fcd`.
Initial Oppenheimer XML import, separate PostgreSQL store and owner-only Toolkit
page. The manual service lacked SEC identification, there was no complete source
reconciliation, and analysis multiplied current dollar values by 1,000. These
limitations are recorded, not erased from the historical release description.
