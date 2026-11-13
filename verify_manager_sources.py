"""Read real public SEC filings; validate manager reporting in disposable local Postgres."""
from collections import Counter
from dataclasses import asdict
import json,os
from pathlib import Path
from urllib.parse import urlsplit
from ingest import SECClient,submissions,filing
from manager_reporting import manager_report

def main():
    url=os.environ['TEST_DATABASE_URL'];target=urlsplit(url)
    if target.hostname not in ('localhost','127.0.0.1','postgres') or target.path!='/fig_test':raise RuntimeError('Disposable fig_test database required')
    os.environ['DATABASE_URL']=url
    import storage
    from app import app
    from fastapi.testclient import TestClient
    client=SECClient();cik='0001269119'
    snapshots=[filing(cik,e,client) for e in submissions(cik,client)]
    expected=manager_report(snapshots)
    latest=max(snapshots,key=lambda s:(s.period,s.filing_date))
    names={str(int(m['sequence'])):m['name'] for m in latest.managers}
    raw=Counter();unassigned=0
    for row in latest.rows:
        if row['put_call']:continue
        ref=str(row['other_manager']).strip()
        if not ref:unassigned+=row['value_usd'];continue
        # An independent reconciliation for this real, single-manager-reference filing.
        if ref.isdigit():raw[names[str(int(ref))]]+=row['value_usd']
        else:raise AssertionError('Real fixture now contains multi-manager references; review independently')
    observed={m['name']:m['history'][-1]['reported_assets_usd'] for m in expected['managers'] if m['history'][-1]['status']=='listed'}
    assert all(observed[n]==v for n,v in raw.items())
    assert sum(raw.values())+unassigned==latest.expected_total
    with storage.connect() as c:
        storage.setup(c)
        # Remove only prior synthetic fixtures in the guarded disposable CI database.
        c.execute('TRUNCATE fig_filings_v2,fig_import_runs,fig_import_events,fig_release_events RESTART IDENTITY')
    job=storage.begin('ci-manager-validation')
    assert job is not None
    try:
        for snap in snapshots:storage.save(job,snap)
        job.finish('success',[])
    finally:job.close()
    with storage.connect() as c:reloaded=storage.latest(c,cik)
    assert manager_report(reloaded)==expected
    os.environ['FIG_API_TOKEN']='synthetic-ci-manager-token'
    with TestClient(app) as api:
        response=api.get('/api/managers/'+cik,headers={'X-FIG-Token':'synthetic-ci-manager-token'})
        assert response.status_code==200
        result=response.json();assert result['period_summaries']==expected['period_summaries']
        assert result['version']=='0.3.0' and result['method_version']=='manager-attribution-1'
        assert api.get('/api/managers/'+cik).status_code==403
    evidence={'filings':len(snapshots),'periods':expected['periods'],'manager_count_across_history':len(expected['managers']),
      'latest_reconciliation':expected['period_summaries'][-1],'latest_managers':observed,
      'independent_reconciliation':True,'independent_committed_readback':True,'api_test':True,
      'latest_pairs':[p for p in expected['possible_pairs'] if p['period']==latest.period]}
    Path('/tmp/manager-live-evidence.json').write_text(json.dumps(evidence,indent=2))
    Path('/tmp/manager-live-report.json').write_text(json.dumps(result))
    print(json.dumps({k:v for k,v in evidence.items() if k not in ('latest_managers','latest_pairs')},indent=2))
if __name__=='__main__':main()
