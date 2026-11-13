"""Directory identities must never be mistaken for loaded holdings."""
import io
import os
import zipfile
import pytest
from datetime import date
import filer_directory as directory
from test_postgres import database

HEADER='Description\nCIK|Company Name|Form Type|Date Filed|Filename\n-------\n'

def packed(rows,filename='master.idx'):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:z.writestr(filename,HEADER+rows)
    out.seek(0);return out

def records():
    return directory.parse_index(packed('123|Example Managers|13F-HR|2026-11-01|edgar/data/123/0000000123-26-000001.txt\n'
        '123|Example New Name|13F-HR|2026-11-02|edgar/data/123/0000000123-26-000002.txt\n'
        '456|Notice Only|13F-NT|2026-11-03|edgar/data/456/0000000456-26-000001.txt\n'
        '789|Unrelated|10-Q|2026-11-03|edgar/data/789/0000000789-26-000001.txt\n'),2026,4)

def test_complete_index_all_filings_not_just_seed():
    rows=records();assert len(rows)==2
    assert rows[0]['cik']=='0000000123' and rows[0]['name']=='Example New Name'
    assert rows[0]['names']=={'Example Managers','Example New Name'}
    assert rows[1]['form']=='13F-NT'

@pytest.mark.parametrize('code',['','0','-1','1/../2','11111111111','123x','\u0661'])
def test_invalid_cik(code):
    with pytest.raises(ValueError):directory.cik(code)

@pytest.mark.parametrize('row',[
 '123|Name|13F-HR|2025-11-01|edgar/data/123/0000000123-26-000001.txt\n',
 '123|Name|13F-HR|2026-11-01|edgar/data/999/0000000123-26-000001.txt\n',
 '123|Name|13F-HR|2026-11-01|https://evil.example/a\n',
 '123|Name|13F-HR|2026-11-01|edgar/data/123/../foo.txt\n'])
def test_invalid_metadata_rejected(row):
    with pytest.raises(ValueError):directory.parse_index(packed(row),2026,4)

def test_zip_path_and_html_rejected():
    with pytest.raises(ValueError):directory.parse_index(packed('', '../master.idx'),2026,4)
    x=io.BytesIO()
    with zipfile.ZipFile(x,'w') as z:z.writestr('master.idx','<html>blocked</html>')
    x.seek(0)
    with pytest.raises(ValueError,match='HEADER'):directory.parse_index(x,2026,4)

def test_all_years_including_current_quarter_no_future():
    sources=directory.index_sources(date(2026,11,13))
    assert len(sources)==132 and sources[0][:2]==(2026,4) and sources[-1][:2]==(1994,1)

@pytest.fixture
def directory_db(request,monkeypatch):
    if not os.getenv('TEST_DATABASE_URL'):
        pytest.skip('Disposable PostgreSQL not configured')
    database=request.getfixturevalue('database')
    with database.connect() as c:
        c.execute('TRUNCATE fig_filer_requests,fig_filer_directory,fig_directory_sources,fig_directory_checks RESTART IDENTITY')
        directory.setup(c)
    return database

def test_directory_upserts_and_latest_name_preserved(directory_db):
    with directory_db.connect() as c:
        directory.save_index(c,records(),'https://www.sec.gov/2026.zip','a'*64,2026,4)
        old=[{'cik':'0000000123','name':'Old Name','names':{'Old Name'},'first':'2000-01-01','last':'2000-01-01','form':'13F-HR'}]
        directory.save_index(c,old,'https://www.sec.gov/2000.zip','b'*64,2000,1)
        row=directory.get(c,'123')
        assert row['name']=='Example New Name' and 'Old Name' in row['names']
        assert str(row['first_filing'])=='2000-01-01'
        row=next(r for r in directory.listing(c) if r['cik']=='0000000123')
        assert row['filing_count']==0 and row['last_period'] is None
        assert directory.status(c)['complete'] is False

def test_queue_deduplication_claim_and_missing_not_zero(directory_db):
    with directory_db.connect() as c:
        directory.save_index(c,records(),'https://www.sec.gov/test.zip','a'*64,2026,4)
        assert directory.request_load(c,'123')['load_status']=='queued'
        directory.request_load(c,'123');assert directory.claim(c,7)=='0000000123'
        directory.request_load(c,'123');assert directory.claim(c,8) is None
        directory.finish(c,'0000000123','no_holdings_in_window')
        assert directory.get(c,'123')['load_status']=='no_holdings_in_window'
        assert directory.get(c,'123')['tracked'] is True
        with pytest.raises(ValueError,match='UNKNOWN'):directory.request_load(c,'999')

def test_new_filer_api_and_protection(directory_db,monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('FIG_API_TOKEN','synthetic-directory-token')
    client=TestClient(app);h={'X-FIG-Token':'synthetic-directory-token'}
    with directory_db.connect() as c:directory.save_index(c,records(),'https://www.sec.gov/test.zip','a'*64,2026,4)
    for path in ['/api/filer/123','/api/platforms']:
        assert client.get(path).status_code==403
    assert client.post('/api/load/123').status_code==403
    assert client.get('/api/filer/invalid',headers=h).status_code==400
    assert client.get('/api/filer/999',headers=h).status_code==404
    state=client.get('/api/filer/123',headers=h).json()
    assert state['load_status']=='not_loaded' and state['filing_count']==0
    assert client.get('/api/managers/123',headers=h).json()['managers']==[]
    rows=client.get('/api/platforms',headers=h).json()
    assert {'0000000123','0000000456','0001269119'} <= {r['cik'] for r in rows}

def test_notice_only_handling_without_fake_holdings(directory_db,monkeypatch):
    import app
    with directory_db.connect() as c:
        directory.save_index(c,records(),'https://www.sec.gov/test.zip','a'*64,2026,4)
        directory.request_load(c,'456')
    job=directory_db.begin('synthetic-notice')
    monkeypatch.setattr(app,'submissions',lambda *a:[])
    try:
        directory.claim(job.c,job.id)
        assert app.import_filer(job,'0000000456',object())==[]
        assert directory.get(job.c,'456')['load_status']=='notice_only'
        assert directory_db.latest(job.c,'0000000456')==[]
    finally:job.finish('success',[]);job.close()
