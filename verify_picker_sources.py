"""Disclosure picker acceptance against real SEC data in local fig_test only."""
import json,os
from pathlib import Path
from urllib.parse import urlsplit
import storage,market_store as store
from filer_capabilities import platforms

def main():
    target=os.environ['TEST_DATABASE_URL'];url=urlsplit(target)
    assert url.hostname in ('localhost','127.0.0.1','postgres') and url.path=='/fig_test'
    os.environ['DATABASE_URL']=target
    with storage.connect() as c:
        storage.setup(c)
        c.execute('TRUNCATE fig_filings_v2,fig_filer_requests,fig_filer_directory RESTART IDENTITY')
    from verify_market_sources import main as verify_market
    verify_market()
    # These identities come from the actual verified archive, not a handpicked roster.
    with storage.connect() as c:
        snapshots=store.latest_summaries(c)
        latest={}
        for s in sorted(snapshots,key=lambda s:(s.filing_date,s.accession)):latest[s.cik]=s
        with c.cursor() as cur:
            cur.executemany('''INSERT INTO fig_filer_directory(cik,name,names,forms,last_filing)
              VALUES(%s,%s,%s,%s,%s) ON CONFLICT(cik) DO UPDATE SET name=EXCLUDED.name,names=EXCLUDED.names,forms=EXCLUDED.forms,last_filing=EXCLUDED.last_filing''',
                [(s.cik,s.manager,[s.manager],[s.form],s.filing_date) for s in latest.values()])
    with storage.connect() as c:
        before=c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]
        rows=platforms(c)
        detail=[r for r in rows if r['manager_reporting_status']=='manager_reporting']
        assert len(rows)>100 and len(detail)>5
        opp=next(r for r in detail if r['cik']=='0001269119')
        assert opp['manager_count']>10
        assert any(r['manager_reporting_status']=='holdings_only' for r in rows)
        assert before==c.execute('SELECT COUNT(*) FROM fig_market_summaries').fetchone()[0]
    from fastapi.testclient import TestClient
    from app import app
    os.environ['FIG_API_TOKEN']='synthetic-picker-acceptance'
    client=TestClient(app)
    assert client.get('/api/platforms').status_code==403
    response=client.get('/api/platforms',headers={'X-FIG-Token':'synthetic-picker-acceptance'})
    assert response.status_code==200 and response.json()==rows
    evidence=dict(scope='One official archive in guarded disposable database; not production coverage',
        directory_firms=len(rows),manager_reporting_firms=len(detail),response_bytes=len(response.content),
        examples=[dict(name=r['name'],cik=r['cik'],managers=r['manager_count'],period=r['manager_reporting_period'])
                  for r in sorted(detail,key=lambda r:-r['manager_count'])[:15]])
    Path('/tmp/fig-picker-real-sources.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence))

if __name__=='__main__':main()
