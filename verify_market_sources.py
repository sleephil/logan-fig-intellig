"""Live public-source acceptance; writes only a guarded disposable CI database."""
import io,json,os,time
from pathlib import Path
from urllib.parse import urlsplit
from ingest import SECClient,parse_bulk,bulk_archives
from market_ingest import bulk_summaries,MAX_BULK_BYTES
from manager_reporting import manager_report
import storage,market_store as store

def main():
    target=os.environ['TEST_DATABASE_URL'];u=urlsplit(target)
    assert u.hostname in ('localhost','127.0.0.1','postgres') and u.path=='/fig_test'
    os.environ['DATABASE_URL']=target
    client=SECClient();url=bulk_archives(client,history_start='2026-06-01')[0]
    started=time.perf_counter();stream,digest=client.download(url,limit=MAX_BULK_BYTES)
    with stream:
        all_summaries,rejected=bulk_summaries(stream,url,digest,'2023-09-30')
        assert len({s.cik for s in all_summaries})>100
        stream.seek(0);original=parse_bulk(stream,url,digest,['0001269119'],'2023-09-30')
    assert original,'Reference filer absent from chosen archive'
    reduced=[s for s in all_summaries if s.cik=='0001269119']
    def values(report):
        return sorted((m['name'],h['period'],h['reported_assets_usd'],h['single_linked_usd'],h['jointly_linked_usd']) for m in report['managers'] for h in m['history'])
    assert values(manager_report(original))==values(manager_report(reduced))
    with storage.connect() as c:
        storage.setup(c)
        c.execute('TRUNCATE fig_market_sources,fig_market_summaries,fig_market_rejections,fig_market_queue,fig_market_generations,fig_market_reports,fig_market_relations,fig_market_state RESTART IDENTITY')
        store.save_summaries(c,all_summaries,'bulk');generation=store.rebuild(c)
    with storage.connect() as c:
        options=store.manager_options(c,'Polen');assert options['managers'],'Polen manager must be discoverable'
        example=store.reverse(c,options['managers'][0]['id']);assert example['firms']
        readings=[]
        for _ in range(10):
            tick=time.perf_counter();out=store.reverse(c,options['managers'][0]['id']);readings.append(time.perf_counter()-tick)
        assert out['generation']==generation
        from fastapi.testclient import TestClient
        from app import app
        os.environ['FIG_API_TOKEN']='synthetic-market-acceptance'
        response=TestClient(app).get('/api/market/reverse',params={'manager_id':options['managers'][0]['id']},headers={'X-FIG-Token':'synthetic-market-acceptance'})
        assert response.status_code==200 and response.json()['firms']==example['firms']
    evidence=dict(source_url=url,source_sha256=digest,validated_filings=len(all_summaries),validated_filers=len({s.cik for s in all_summaries}),
        raw_positions=sum(s.expected_count for s in all_summaries),summary_buckets=sum(len(s.rows) for s in all_summaries),
        rejected_filings=len(rejected),rejection_examples=rejected[:10],generation=generation,
        polen_matches=options['managers'],example_firms=len(example['firms']),lookup_median_seconds=sorted(readings)[5],
        build_and_validation_seconds=time.perf_counter()-started,scope='One complete official archive in disposable local PostgreSQL; not production coverage or user-network timing.')
    Path('/tmp/fig-market-source-evidence.json').write_text(json.dumps(evidence,indent=2))
    Path('/tmp/fig-market-reverse-example.json').write_text(json.dumps(example,indent=2))
    print(json.dumps(evidence))

if __name__=='__main__':main()
