"""Opt-in SEC verification against a disposable database. Never uses production."""
import os,json
from pathlib import Path
from ingest import SECClient,submissions,filing,bulk_archives,parse_bulk,MAX_ZIP
import storage

def main():
    if not os.getenv('TEST_DATABASE_URL'):raise RuntimeError('Disposable TEST_DATABASE_URL required')
    from urllib.parse import urlsplit
    target=urlsplit(os.environ['TEST_DATABASE_URL'])
    if target.hostname not in ('localhost','127.0.0.1','postgres') or target.path!='/fig_test':raise RuntimeError('Only disposable local fig_test database permitted')
    os.environ['DATABASE_URL']=os.environ['TEST_DATABASE_URL']
    with storage.connect() as db:
        storage.setup(db)
        db.execute('TRUNCATE fig_filings_v2,fig_import_runs,fig_import_events,fig_release_events RESTART IDENTITY')
    c=SECClient();cik='0001269119';entry=submissions(cik,c)[0]
    xml=filing(cik,entry,c)
    archives=bulk_archives(c);url=archives[0]
    archive,digest=c.download(url,limit=MAX_ZIP)
    with archive:bulk=parse_bulk(archive,url,digest,[cik],'2023-09-30')
    assert bulk,'No Oppenheimer holdings in latest published archive'
    receipts=[];job=storage.begin('ci-live-verification')
    try:
        for s in [xml,*bulk]:
            assert storage.save(job,s)
            assert not storage.save(job,s)
            receipts.append({'period':s.period,'accession':s.accession,'rows':len(s.rows),'value_usd':s.expected_total,'sources':s.sources,'sha256':s.checksum})
        job.finish('success',[])
    finally:job.close()
    from fastapi.testclient import TestClient
    from app import app
    os.environ['FIG_API_TOKEN']='ci-synthetic-token'
    with TestClient(app) as client:
        headers={'X-FIG-Token':'ci-synthetic-token'}
        status=client.get('/api/status',headers=headers);assert status.status_code==200
        history=client.get('/api/filings/'+cik,headers=headers);assert history.status_code==200
        analysis=client.get('/api/analysis/'+cik,headers=headers);assert analysis.status_code==200
        latest=sum(p['latest_value_usd'] for p in analysis.json()['positions'])
        assert latest==xml.expected_total,'Analysis dollar totals differ from latest SEC filing'
        assert len(history.json())==len(receipts)
    report={'verified':receipts,'status':status.json(),'analysis_latest_total_usd':latest,'duplicate_inserts':0}
    Path('/tmp/fig-live-verification.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
