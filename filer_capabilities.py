"""Read-only disclosure signals for the firm picker, never a manager-use census."""
from __future__ import annotations


def summarize(summaries):
    """Describe the latest *loaded* quarter; do not substitute historical detail."""
    periods = sorted(summaries or [], key=lambda p: p['period'])
    if not periods:
        return dict(manager_reporting_status='not_loaded', manager_count=None,
                    manager_reporting_period=None, manager_history_available=False,
                    manager_last_detail_period=None, manager_attributed_usd=None,
                    manager_comparison_quality='unknown')
    latest = periods[-1]
    detail = [p for p in periods if p.get('listed_managers', 0) > 0
              and p.get('attributed_unique_usd', 0) > 0]
    count = int(latest.get('listed_managers', 0))
    value = int(latest.get('attributed_unique_usd', 0))
    status = ('manager_reporting' if count and value > 0 else
              'manager_list_only' if count else 'holdings_only')
    return dict(manager_reporting_status=status, manager_count=count,
                manager_reporting_period=latest['period'],
                manager_history_available=bool(detail),
                manager_last_detail_period=detail[-1]['period'] if detail else None,
                manager_attributed_usd=value,
                manager_comparison_quality=latest.get('comparison_quality', 'unknown'))


def platforms(c):
    """Enrich the complete identity directory; no SEC requests or writes here."""
    import filer_directory as directory
    import market_store
    import storage
    from manager_reporting import manager_report
    rows = directory.listing(c)
    published = market_store.directory_rows(c)
    for row in rows:
        saved = published.get(row['cik'])
        if saved is not None:
            row.update(saved, manager_summary_ready=True)
        elif row['filing_count']:
            report = manager_report(storage.latest(c, row['cik']))
            row.update(summarize(report['period_summaries']), manager_summary_ready=True)
        else:
            row.update(manager_reporting_status='not_loaded', manager_history_available=False, manager_summary_ready=False)
    return rows


def emit_coverage():
    """Non-secret production acceptance receipt; no source or queue mutations."""
    import json
    import storage
    try:
        with storage.connect() as c:
            rows = platforms(c)
        managers = [r for r in rows if r['manager_reporting_status'] == 'manager_reporting']
        storage.emit('FIG_FILER_PICKER_READY', filers=len(rows), manager_reporting_firms=len(managers),
            history_with_manager_detail=sum(bool(r.get('manager_history_available')) for r in rows),
            serialized_bytes=len(json.dumps(rows).encode()),
            examples=[dict(cik=r['cik'], name=r['name'], managers=r['manager_count'], period=r['manager_reporting_period'])
                      for r in sorted(managers, key=lambda r: (-r['manager_count'], r['cik']))[:12]])
    except Exception as exc:
        storage.emit('FIG_FILER_PICKER_CHECK_FAILED', code=type(exc).__name__.upper())
