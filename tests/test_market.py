"""Market preload and reverse lookup checks; database tests use disposable fig_test only."""
import io,json,os,zipfile
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import pytest
from market_ingest import compact, bulk_summaries, index_filings, SummarySnapshot
from manager_reporting import manager_report,IdentityIndex
from test_ingest import snapshot,make_zip,URL
from test_postgres import database
import market_store as store


def test_compact_preserves_manager_assets_and_counts():
    s=snapshot();s.managers=[dict(sequence='1',name='Polen Example',cik='123',form13f_file_number='028-00123')]
    for row in s.rows:row['other_manager']='1'
    small=compact(s)
    assert len(small.rows)==1 and small.expected_count==2 and small.expected_total==300
    full=manager_report([s]);reduced=manager_report([small])
    assert full['managers']==reduced['managers']
    assert full['period_summaries'][0]['reported_total_usd']==reduced['period_summaries'][0]['reported_total_usd']
    assert 'cusip' not in small.rows[0]


def test_compact_counts_not_bucket_counts():
    s=compact(snapshot());assert s.expected_count==2 and len(s.rows)==1
    s.expected_count=1
    with pytest.raises(ValueError,match='COUNT'):s.validate()


def test_market_zip_has_no_filer_whitelist():
    summaries,bad=bulk_summaries(make_zip(),URL,'a'*64,'2023-09-30')
    assert not bad and len(summaries)==1 and summaries[0].expected_total==300
    assert len(summaries[0].rows)==1


def replace_member(buf, member, old,new):
    out=io.BytesIO()
    with zipfile.ZipFile(buf) as src,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            b=src.read(name)
            if name==member:b=b.decode().replace(old,new).encode()
            dst.writestr(name,b)
    out.seek(0);return out


def test_bad_filing_rejected_not_silently_accepted():
    b=replace_member(make_zip(),'SUMMARYPAGE.tsv','\t300','\t301')
    rows,bad=bulk_summaries(b,URL,'a'*64,'2023-09-30')
    assert not rows and len(bad)==1 and bad[0]['code']=='TABLE_VALUE_TOTAL_MISMATCH'


@pytest.mark.parametrize('kwargs',[{'extra':('../evil','x')},{'extra':('/abs','x')},{'missing':'SUMMARYPAGE.tsv'},{'duplicate':True}])
def test_zip_security(kwargs):
    with pytest.raises(ValueError):bulk_summaries(make_zip(**kwargs),URL,'a'*64,'2023-09-30')


def test_duplicate_raw_keys_are_rejected():
    b=replace_member(make_zip(),'INFOTABLE.tsv','\t1\tBETA','\t0\tBETA')
    rows,bad=bulk_summaries(b,URL,'a'*64,'2023-09-30')
    assert not rows and bad[0]['code']=='DUPLICATE_BULK_ROW_ID'


def test_recent_index_filters_exact_forms_and_watermark():
    from test_directory import packed
    stream=packed('123|Firm|13F-HR|2026-11-01|edgar/data/123/0000000123-26-000001.txt\n'
                  '123|Firm|13F-NT|2026-11-02|edgar/data/123/0000000123-26-000002.txt\n'
                  '456|Other|13F-HR/A|2026-11-03|edgar/data/456/0000000456-26-000001.txt\n')
    rows=index_filings(stream,2026,4,'2026-11-01')
    assert len(rows)==1 and rows[0]['cik']=='0000000456' and rows[0]['form']=='13F-HR/A'


def test_global_identity_resolves_identifier_and_exact_alias():
    a=snapshot();a.managers=[dict(sequence='1',name='Polen Example',cik='123',form13f_file_number='028-00123')]
    b=deepcopy(a);b.cik='0000000456';b.accession='0000000456-26-000001';b.managers[0]['cik']=''
    for s in (a,b):
        for row in s.rows:row['other_manager']='1'
    index=IdentityIndex([a,b])
    assert manager_report([a],identity_index=index)['managers'][0]['id']==manager_report([b],identity_index=index)['managers'][0]['id']


@pytest.fixture
def market_db(request,monkeypatch):
    if not os.getenv('TEST_DATABASE_URL'):pytest.skip('Disposable PostgreSQL not configured')
    db=request.getfixturevalue('database')
    with db.connect() as c:
        store.setup(c)
        c.execute('TRUNCATE fig_market_sources,fig_market_summaries,fig_market_rejections,fig_market_queue,fig_market_generations,fig_market_reports,fig_market_relations,fig_market_state RESTART IDENTITY')
    return db


def examples():
    out=[]
    for cik,values in [('0001269119',[300,350]),('0000000456',[200,150])]:
        for i,value in enumerate(values):
            s=snapshot();s.cik=cik;s.accession=f'{cik}-26-{i+1:06d}'
            s.period=['2026-06-30','2026-09-30'][i];s.filing_date=['2026-08-01','2026-11-01'][i]
            s.rows[0]['value_usd']=value-100;s.rows[1]['value_usd']=100;s.expected_total=value
            s.managers=[dict(sequence='1',name='Polen Example',cik='123',form13f_file_number='028-00123')]
            for row in s.rows:row['other_manager']='1'
            out.append(compact(s))
    return out


def test_index_reverse_and_atomic_publication(market_db):
    with market_db.connect() as c:
        store.save_summaries(c,examples(),'bulk');store.save_summaries(c,examples(),'bulk')
        assert c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]==4
        gen=store.rebuild(c);r=store.reverse(c,'cik:0000000123')
        assert len(r['firms'])==2 and r['totals'][-1]['gross_reported_assets_usd']==500
        assert r['totals'][-1]['comparable_change_usd']==0
        assert r['totals'][-1]['comparable_firms']==2
        assert store.manager_options(c,'polen')['total']==1
        assert store.manager_options(c,"%' OR 1=1 --")['total']==0
        assert store.forward(c,'0001269119')['managers'][0]['history'][-1]['reported_assets_usd']==350
        c.execute("INSERT INTO fig_market_generations(status,method) VALUES('building',%s)",(store.METHOD,))
        assert store.active(c)==gen
    with market_db.connect() as c:
        assert store.reverse(c,'cik:0000000123')['generation']==gen


def test_incomplete_sources_never_claim_all(market_db):
    with market_db.connect() as c:
        store.save_summaries(c,examples(),'bulk');store.rebuild(c)
        assert store.coverage(c)['complete'] is False
        store.state(c,'archives',dict(urls=[URL]));assert store.coverage(c)['missing_archives']==[URL]
        assert store.reverse(c,'not-known')['manager'] is None


def test_reverse_routes_read_only_and_protected(market_db,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('FIG_API_TOKEN','synthetic')
    with market_db.connect() as c:store.save_summaries(c,examples(),'bulk');store.rebuild(c)
    client=TestClient(app);h={'X-FIG-Token':'synthetic'}
    for path in ['/api/market/status','/api/market/managers','/api/market/reverse?manager_id=cik:0000000123']:
        assert client.get(path).status_code==403
        assert client.get(path,headers=h).status_code==200
    assert client.get('/api/market/reverse?manager_id=cik:0000000123',headers=h).json()['totals'][-1]['reporting_firms']==2


def test_summary_zero_entries_not_missing_assets():
    s=compact(snapshot());s.rows=[];s.expected_count=0;s.expected_total=0
    assert s.validate().expected_total==0
    assert manager_report([s])['managers']==[]


def test_reverse_comparable_total_excludes_prior_joint_values(market_db):
    samples=examples()
    first=samples[0]
    first.managers.append(dict(sequence='2',name='Other Manager',cik='789',form13f_file_number=''))
    first.rows[0]['other_manager']='1,2'
    with market_db.connect() as c:
        store.save_summaries(c,samples,'bulk');store.rebuild(c)
        result=store.reverse(c,'cik:0000000123')
        assert result['totals'][-1]['comparable_firms']==1
        assert result['totals'][-1]['comparable_change_usd']==-50


def test_manual_refresh_not_hidden_by_old_published_cache(market_db):
    with market_db.connect() as c:
        store.save_summaries(c,examples(),'bulk');store.rebuild(c)
        assert store.forward(c,'0001269119') is not None
    job=market_db.begin('synthetic-refresh')
    try:market_db.save(job,snapshot())
    finally:job.close()
    with market_db.connect() as c:
        assert store.forward(c,'0001269119') is None


def test_pending_new_filing_suppresses_comparison_not_assets(market_db):
    with market_db.connect() as c:
        store.save_summaries(c,examples(),'bulk')
        c.execute("INSERT INTO fig_market_queue(accession,cik,filing_date,payload) VALUES(%s,%s,%s,%s)",
            ('0001269119-26-999999','0001269119','2026-11-02',market_db.jsonb({})))
        store.rebuild(c);result=store.reverse(c,'cik:0000000123')
        firm=next(f for f in result['firms'] if f['cik']=='0001269119')
        assert firm['history'][-1]['reported_assets_usd']==350
        assert firm['history'][-1]['change_usd'] is None
        assert result['coverage']['complete'] is False


def test_bulk_rejections_retain_independent_retry_metadata():
    b=replace_member(make_zip(),'SUMMARYPAGE.tsv','\t300','\t301')
    _,bad=bulk_summaries(b,URL,'a'*64,'2023-09-30')
    assert bad[0]['filing_date']=='2026-10-27' and bad[0]['form']=='13F-HR'
    assert bad[0]['name'] and bad[0]['accession']


def test_compact_summary_remains_idempotent():
    s=compact(snapshot());assert compact(s).checksum==s.checksum


def test_xml_retry_requires_verified_zero_count_and_value():
    from market_ingest import indexed_filing
    from test_ingest import COVER,TABLE
    empty=COVER.replace('<tableEntryTotal>2</tableEntryTotal>','<tableEntryTotal>0</tableEntryTotal>').replace('<tableValueTotal>300</tableValueTotal>','<tableValueTotal>0</tableValueTotal>')
    class Client:
        table=False
        cover=empty
        def json(self,url):return {'directory':{'item':[{'name':'cover.xml'}]+([{'name':'table.xml'}] if self.table else [])}}
        def get(self,url):return (TABLE if url.endswith('table.xml') else self.cover).encode(),'b'*64
    e=dict(cik='0001269119',accession='0001269119-26-000001',form='13F-HR',filing_date='2026-10-27')
    c=Client();result=indexed_filing(e,c);assert result.expected_count==result.expected_total==0
    c.cover=empty.replace('<tableValueTotal>0','<tableValueTotal>1')
    with pytest.raises(ValueError,match='TABLE_VALUE_TOTAL_MISMATCH'):indexed_filing(e,c)
    c.cover=empty;c.table=True
    with pytest.raises(ValueError,match='TABLE_ENTRY_COUNT_MISMATCH'):indexed_filing(e,c)


def test_original_xml_kept_even_when_canonical_totals_equal_bulk(market_db):
    s=compact(snapshot())
    with market_db.connect() as c:
        store.save_summaries(c,[s],'bulk');store.save_summaries(c,[s],'xml')
        assert c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]==2
        assert c.execute("SELECT COUNT(*) FROM fig_market_summaries WHERE source_kind='xml'").fetchone()[0]==1
        assert len(store.latest_summaries(c))==1


def test_market_worker_recovers_bulk_rejection_from_original_xml(market_db,monkeypatch):
    import market_worker as worker
    from test_ingest import COVER,TABLE,ENTRY
    bad_archive=replace_member(make_zip(),'SUMMARYPAGE.tsv','\t300','\t301').getvalue()
    class Client:
        def download(self,url,**kw):return io.BytesIO(bad_archive),'a'*64
        def json(self,url):return {'directory':{'item':[{'name':'cover.xml'},{'name':'table.xml'}]}}
        def get(self,url):return (TABLE if url.endswith('table.xml') else COVER).encode(),'b'*64
    monkeypatch.setattr(worker,'bulk_archives',lambda *a,**kw:[URL])
    monkeypatch.setattr(worker.directory,'index_sources',lambda:[])
    assert worker.run_preload(client=Client())=='success'
    with market_db.connect() as c:
        assert store.coverage(c)['complete'] is True
        assert c.execute('SELECT COUNT(*) FROM fig_market_rejections').fetchone()[0]==0
        assert c.execute("SELECT status FROM fig_market_queue WHERE accession=%s",(ENTRY['accessionNumber'],)).fetchone()[0]=='ready'
        assert store.latest_summaries(c)[0].expected_total==300
    # A repeated bulk rejection cannot erase a previously verified original.
    with market_db.connect() as c:c.execute("UPDATE fig_market_sources SET checked_at=NOW()-INTERVAL '21 hours'")
    assert worker.run_preload(client=Client())=='success'
