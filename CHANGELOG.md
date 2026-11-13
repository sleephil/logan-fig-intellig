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
