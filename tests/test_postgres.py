"""Integration tests use ONLY a disposable CI database, never production."""
import os
import pytest
pytestmark=pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'),reason='Disposable PostgreSQL not configured')

@pytest.fixture
def database(monkeypatch):
    from urllib.parse import urlsplit
    target=urlsplit(os.environ['TEST_DATABASE_URL'])
    assert target.hostname in ('localhost','127.0.0.1','postgres') and target.path=='/fig_test'
    monkeypatch.setenv('DATABASE_URL',os.environ['TEST_DATABASE_URL'])
    import storage
    with storage.connect() as c:
        storage.setup(c)
        c.execute('TRUNCATE fig_filings_v2,fig_import_runs,fig_import_events,fig_release_events,fig_market_sources,fig_market_summaries,fig_market_rejections,fig_market_queue,fig_market_generations,fig_market_reports,fig_market_relations,fig_market_state RESTART IDENTITY')
    return storage

def test_commit_readback_idempotency_and_events(database):
    from test_ingest import snapshot
    j=database.begin('test');s=snapshot()
    assert database.save(j,s) is True
    assert database.save(j,s) is False
    assert database.counts(j.c)['stored_revisions']==1
    assert len(database.latest(j.c))==1
    j.finish('success',[]);j.close()
    with database.connect() as c:
        assert database.status(c)['last_check_status']=='success'
        assert c.execute('SELECT count(*) FROM fig_release_events').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM fig_import_events WHERE event='FIG_FILING_READBACK_VERIFIED'").fetchone()[0]==2

def test_validation_failure_not_saved(database):
    from test_ingest import snapshot
    j=database.begin('test');s=snapshot();s.expected_total+=1
    try:
        with pytest.raises(ValueError):database.save(j,s)
        assert database.counts(j.c)['imported_filings']==0
    finally:j.close()

def test_concurrency_and_interrupted_job(database):
    j=database.begin('first');assert database.begin('second') is None
    first=j.id;j.close()
    j=database.begin('recovery')
    assert j.c.execute('SELECT status FROM fig_import_runs WHERE id=%s',(first,)).fetchone()[0]=='interrupted'
    j.finish('success',[]);j.close()

def test_failure_recorded_and_not_running(database,monkeypatch):
    import app
    monkeypatch.delenv('SEC_USER_AGENT',raising=False)
    assert app.sync(trigger='test')=='failed'
    with database.connect() as c:
        s=database.status(c);assert s['last_check_status']=='failed' and s['last_error']=='SEC_USER_AGENT_MISSING'

def test_new_revision_preserves_previous(database):
    from test_ingest import snapshot
    j=database.begin('test');s=snapshot();database.save(j,s)
    s.rows[0]['value_usd']=101;s.rows[0]['raw_value']=101;s.expected_total=301;database.save(j,s)
    assert database.counts(j.c)['stored_revisions']==2 and database.counts(j.c)['imported_filings']==1
    assert database.latest(j.c)[0].expected_total==301
    j.finish('success',[]);j.close()
