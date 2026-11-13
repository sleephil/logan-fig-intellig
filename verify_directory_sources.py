"""Real SEC directory and cross-filer validation. Disposable local DB ONLY."""
import os,json
from pathlib import Path
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
import storage
import filer_directory as directory
from ingest import SECClient,submissions,filing

def main():
    url=os.environ['TEST_DATABASE_URL'];target=urlsplit(url)
    if target.hostname not in ('localhost','127.0.0.1','postgres') or target.path!='/fig_test':
        raise RuntimeError('Disposable local fig_test database required')
    os.environ['DATABASE_URL']=url;os.environ['FIG_REQUEST_WORKER']='false';os.environ['FIG_SYNC_ON_STARTUP']='false'
    with storage.connect() as c:
        storage.setup(c)
        c.execute('TRUNCATE fig_filer_requests,fig_filer_directory,fig_directory_sources,fig_directory_checks,fig_filings_v2,fig_import_runs,fig_import_events,fig_release_events RESTART IDENTITY')
        directory.setup(c)
    job=storage.begin('ci-sec-universe');client=SECClient(progress=job.progress)
    try:
        sources=directory.index_sources()
        full=os.getenv('FIG_VERIFY_FULL_DIRECTORY')=='true'
        errors=directory.refresh(job,client,sources if full else [sources[0],sources[1],sources[-1]])
        assert not errors,errors
        all_firms=directory.listing(job.c)
        assert len(all_firms)>1000, len(all_firms)
        assert len({r['cik'] for r in all_firms})==len(all_firms)
        assert any('13F-NT' in r['forms'] for r in all_firms)
        assert any('LOGAN CAPITAL' in r['name'].upper() for r in all_firms)
        samples=['0001269119','0001067983']
        receipts=[]
        for code in samples:
            entity=directory.get(job.c,code);assert entity
            entries=submissions(code,client);assert entries
            snap=filing(code,entries[0],client)
            assert storage.save(job,snap) and not storage.save(job,snap)
            receipts.append({'cik':code,'name':entity['name'],'period':snap.period,'rows':len(snap.rows),'usd':snap.expected_total,'source_sha256':snap.checksum})
        from app import app
        os.environ['FIG_API_TOKEN']='synthetic-ci-token'
        with TestClient(app) as api:
            h={'X-FIG-Token':'synthetic-ci-token'}
            rows=api.get('/api/platforms',headers=h).json();assert len(rows)==len(all_firms)
            for rec in receipts:
                result=api.get('/api/managers/'+rec['cik'],headers=h)
                assert result.status_code==200
                body=result.json();assert body['cik']==rec['cik']
                assert body['period_summaries'][-1]['reported_total_usd']==rec['usd']
            assert api.get('/api/platforms').status_code==403
        report={'directory':directory.status(job.c),'sample_filers':receipts,'listed_filers':len(all_firms),
                'full_index_validation':full,'catalogue_response_bytes':len(json.dumps(rows).encode()),'holdings_preloaded_for_all':False}
        if full:assert report['directory']['complete']
        Path('/tmp/fig-universe-live.json').write_text(json.dumps(report,indent=2,default=str))
        Path('/tmp/fig-universe-catalogue.json').write_text(json.dumps(all_firms,default=str))
        print(json.dumps(report,indent=2,default=str))
        job.finish('success',[])
    finally:job.close()
if __name__=='__main__':main()
