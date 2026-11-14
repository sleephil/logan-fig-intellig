"""Picker evidence classification and real PostgreSQL/API readback."""
from copy import deepcopy
import os
import pytest
from filer_capabilities import summarize
from test_market import market_db, examples
from test_postgres import database
import market_store as store


def period(**kw):
    return dict(period='2026-09-30', listed_managers=2, attributed_unique_usd=150,
                comparison_quality='complete', **kw)


def test_current_detail_not_a_claim_about_affiliations():
    d=summarize([period()])
    assert d['manager_reporting_status']=='manager_reporting'
    assert d['manager_count']==2 and d['manager_history_available']


def test_list_without_linked_assets_is_distinct():
    p=period();p['attributed_unique_usd']=0
    assert summarize([p])['manager_reporting_status']=='manager_list_only'


def test_historical_links_do_not_make_latest_manager_reporting():
    old=period();old['period']='2026-06-30'
    now=period();now.update(listed_managers=0,attributed_unique_usd=0)
    d=summarize([now,old])
    assert d['manager_reporting_status']=='holdings_only'
    assert d['manager_history_available'] and d['manager_last_detail_period']=='2026-06-30'
    assert d['manager_reporting_period']=='2026-09-30'


def test_missing_is_not_holdings_only_or_zero():
    d=summarize([])
    assert d['manager_reporting_status']=='not_loaded'
    assert d['manager_count'] is None and d['manager_attributed_usd'] is None


def test_partial_retains_quality_warning():
    p=period();p['comparison_quality']='review'
    assert summarize([p])['manager_comparison_quality']=='review'


def test_complete_directory_readonly_with_current_disclosure(market_db,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    import filer_directory as directory
    samples=examples()
    samples[-1].managers=[]
    for row in samples[-1].rows:row['other_manager']=''
    with market_db.connect() as c:
        directory.setup(c)
        for code,name in [('0000000456','Holdings Only'),('0000000789','Not loaded')]:
            c.execute('INSERT INTO fig_filer_directory(cik,name,names,forms) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                      (code,name,[name],['13F-HR']))
        store.save_summaries(c,samples,'bulk');store.rebuild(c)
        before=c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]
    monkeypatch.setenv('FIG_API_TOKEN','synthetic')
    client=TestClient(app)
    assert client.get('/api/platforms').status_code==403
    rows=client.get('/api/platforms',headers={'X-FIG-Token':'synthetic'}).json()
    d={r['cik']:r for r in rows}
    assert d['0001269119']['manager_reporting_status']=='manager_reporting'
    assert d['0000000456']['manager_reporting_status']=='holdings_only'
    assert d['0000000456']['manager_history_available'] is True
    assert d['0000000789']['manager_reporting_status']=='not_loaded'
    with market_db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]==before
        assert store.reverse(c,'cik:0000000123')['firms']


def test_newer_raw_metadata_wins_over_published(market_db):
    from test_ingest import snapshot
    from filer_capabilities import platforms
    with market_db.connect() as c:
        store.save_summaries(c,examples(),'bulk');store.rebuild(c)
    job=market_db.begin('synthetic-manual')
    try:market_db.save(job,snapshot())
    finally:job.close()
    with market_db.connect() as c:
        row=next(r for r in platforms(c) if r['cik']=='0001269119')
    assert row['manager_reporting_status']=='holdings_only'
