"""Private FIG API: common verified importer for manual and daily checks."""
from __future__ import annotations
import hmac
import os
import threading
from contextlib import asynccontextmanager
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from ingest import PLATFORMS, SECClient, submissions, filing, bulk_archives, parse_bulk, MAX_ZIP
import storage
import filer_directory as directory
from version import release

def error_code(exc):
    message=str(exc)
    if message and len(message)<100 and all(c.isupper() or c.isdigit() or c=='_' for c in message):return message
    return type(exc).__name__.upper()

def import_filer(job, code, client):
    entity=directory.get(job.c,code); name=entity['name']; errors=[]; imported=0
    job.progress(stage='discovering_filings',current_platform=name,current_accession='',checked_filings=0,new_filings=0)
    try:
        entries=submissions(code,client)
        job.progress(total_filings=len(entries))
        if not entries:
            state='notice_only' if not any(f.startswith('13F-HR') for f in entity['forms']) and any(f.startswith('13F-NT') for f in entity['forms']) else 'no_holdings_in_window'
            directory.finish(job.c,code,state)
            return []
        for n,entry in enumerate(entries):
            acc=entry['accessionNumber']
            job.progress(stage='checking_filing',current_accession=acc,bytes_downloaded=0)
            try:
                if not storage.verified(job.c,code,acc):
                    snap=filing(code,entry,client)
                    job.progress(stage='validating_and_saving')
                    imported+=int(storage.save(job,snap))
            except Exception as exc:
                failure={'platform':name,'cik':code,'accession':acc,'code':error_code(exc)}
                errors.append(failure);storage.event(job.c,job.id,'FIG_FILING_FAILED',**failure)
            finally:job.progress(checked_filings=n+1,new_filings=imported)
        count=job.c.execute('SELECT COUNT(*) FROM fig_filings_v2 WHERE cik=%s AND parser_version=%s',(code,storage.PARSER_VERSION)).fetchone()[0]
        directory.finish(job.c,code,('partial' if count else 'failed') if errors else 'ready',errors[0]['code'] if errors else None)
    except Exception as exc:
        errors.append({'platform':name,'cik':code,'code':error_code(exc)})
        directory.finish(job.c,code,'failed',error_code(exc))
    return errors


def execute(job, *, bulk=False, directory_refresh=True):
    errors=[]
    try:
        client=SECClient(progress=job.progress)
        # The import advisory lock makes this recovery safe across API/cron replicas.
        job.c.execute("UPDATE fig_filer_requests SET status='queued' WHERE status='running'")
        if directory_refresh:
            errors.extend(directory.refresh(job,client))
            for (code,) in job.c.execute('SELECT cik FROM fig_filer_directory WHERE tracked=TRUE').fetchall():
                directory.request_load(job.c,code)
        if bulk:
            codes=[r[0] for r in job.c.execute('SELECT cik FROM fig_filer_directory WHERE tracked=TRUE').fetchall()]
            for url in bulk_archives(client):
                archive,digest=client.download(url,limit=MAX_ZIP)
                with archive:snaps=parse_bulk(archive,url,digest,codes,os.getenv('HISTORY_START','2023-09-30'),job.progress)
                for snap in snaps:storage.save(job,snap)
        while True:
            code=directory.claim(job.c,job.id)
            if code is None:break
            errors.extend(import_filer(job,code,client))
        result='success' if not errors else 'partial' if storage.counts(job.c)['imported_filings'] else 'failed'
        job.finish(result,errors);return result
    except Exception as exc:
        errors.append({'code':error_code(exc)})
        try:job.finish('failed',errors)
        except Exception:storage.emit('FIG_IMPORT_STATUS_WRITE_FAILED',run_id=job.id,code=error_code(exc))
        return 'failed'
    finally:job.close()


def sync(*,bulk=False,trigger='scheduled',directory_refresh=True):
    job=storage.begin(trigger)
    if job is None:
        storage.emit('FIG_IMPORT_ALREADY_RUNNING');return 'already_running'
    return execute(job,bulk=bulk,directory_refresh=directory_refresh)


def queued_worker(stop):
    # Durable queue survives restarts; global import lock prevents duplicate workers.
    while not stop.wait(15):
        try:
            with storage.connect() as c:
                pending=c.execute("SELECT 1 FROM fig_filer_requests WHERE status IN ('queued','running') LIMIT 1").fetchone()
            if pending:sync(trigger='queued-filer',directory_refresh=False)
        except Exception as exc:storage.emit('FIG_QUEUE_CHECK_FAILED',code=error_code(exc))

@asynccontextmanager
async def lifespan(app):
    if os.getenv('DATABASE_URL'):
        with storage.connect() as c: storage.setup(c);storage.record_release(c)
        storage.emit('FIG_RELEASE_READY')
        if os.getenv('RAILWAY_SERVICE_ID'):
            from filer_capabilities import emit_coverage
            threading.Thread(target=emit_coverage,daemon=True).start()
        if os.getenv('FIG_SYNC_ON_STARTUP','').lower()=='true':
            threading.Thread(target=sync,kwargs={'trigger':'startup'},daemon=True).start()
    stop=threading.Event()
    if os.getenv('DATABASE_URL') and os.getenv('FIG_REQUEST_WORKER','true').lower()=='true':
        threading.Thread(target=queued_worker,args=(stop,),daemon=True).start()
    try:yield
    finally:stop.set()

app=FastAPI(title='Logan FIG Intelligence',version=release()['version'],lifespan=lifespan)

def authorize(req):
    secret=os.getenv('FIG_API_TOKEN','');supplied=req.headers.get('X-FIG-Token','')
    if not secret or not hmac.compare_digest(secret.encode(),supplied.encode()):raise HTTPException(403,'Toolkit authorization required')

@app.get('/health')
def health():return {'status':'ok',**release()}
@app.get('/',response_class=HTMLResponse)
def home():return '<h1>Logan FIG Intelligence</h1><p>Private service. Open FIG Intelligence through the authenticated Logan Toolkit.</p>'

@app.get('/api/status')
def status(req:Request):
    authorize(req)
    with storage.connect() as c:
        result=storage.status(c);result['directory']=directory.status(c)
        import market_store
        result['market']=market_store.coverage(c)
        return result

@app.get('/api/version')
def version(req:Request):
    authorize(req)
    from pathlib import Path
    import json
    return {**release(),'changes':json.loads(Path(__file__).with_name('CHANGELOG.json').read_text())}

@app.get('/api/platforms')
def platforms(req:Request):
    authorize(req)
    with storage.connect() as c:
        from filer_capabilities import platforms as firm_options
        return firm_options(c)


def require_filer(code):
    try:code=directory.cik(code)
    except ValueError as exc:raise HTTPException(400,'Invalid filer') from exc
    with storage.connect() as c:entity=directory.get(c,code)
    if not entity:raise HTTPException(404,'Unknown SEC filer')
    return entity

@app.get('/api/filer/{cik}')
def filer_status(cik:str,req:Request):
    authorize(req)
    entity=require_filer(cik)
    with storage.connect() as c:
        row=c.execute('SELECT COUNT(DISTINCT accession),MIN(period),MAX(period) FROM fig_filings_v2 WHERE cik=%s AND parser_version=%s',
            (entity['cik'],storage.PARSER_VERSION)).fetchone()
    import market_store
    with storage.connect() as c:cached=market_store.forward(c,entity['cik'])
    if cached and cached['periods']:
        row=(sum(len(p['sources']) for p in cached['period_summaries']),cached['periods'][0],cached['periods'][-1])
    return {'manager_summary_ready':bool(cached),'name':entity['name'],'cik':entity['cik'],'forms':entity['forms'],
            'last_filing':entity['last_filing'],'load_status':entity['load_status'] or ('ready' if row[0] else 'not_loaded'),
            'load_error':entity['load_error'],'load_finished_at':entity['load_finished_at'],'run_id':entity['run_id'],
            'filing_count':row[0],'first_period':row[1],'last_period':row[2],
            'history_start':os.getenv('HISTORY_START','2023-09-30')}

@app.get('/api/filings/{cik}')
def reports(cik:str,req:Request):
    authorize(req)
    entity=require_filer(cik);cik=entity['cik']
    with storage.connect() as c:
        import market_store
        snaps=market_store.latest_summaries(c,cik) or storage.latest(c,cik)
    return [{'period':s.period,'accession':s.accession,'positions':s.expected_count,'reported_value_usd':s.expected_total,
             'form':s.form,'filing_date':s.filing_date,'amendment_type':s.amendment_type,'report_type':s.report_type,
             'reconciled':True,'snapshot_sha256':s.checksum,'sources':s.sources} for s in sorted(snaps,key=lambda s:(s.period,s.filing_date,s.accession),reverse=True)]

def effective_periods(snaps):
    """Restatements replace a period; new-holdings amendments ADD to a base.
    Keep the raw filing rows intact; same-CUSIP rows can represent shared managers.
    """
    groups=defaultdict(list);result={};issues=[]
    for snap in snaps:groups[snap.period].append(snap)
    for period,versions in groups.items():
        rows=None;sources=[]
        for s in sorted(versions,key=lambda s:(s.filing_date,s.amendment_no,s.accession)):
            if s.form=='13F-HR' or s.amendment_type=='RESTATEMENT':rows=list(s.rows);sources=[s.accession]
            elif s.amendment_type=='NEW HOLDINGS' and rows is not None:rows.extend(s.rows);sources.append(s.accession)
            else:issues.append({'period':period,'accession':s.accession,'code':'AMENDMENT_BASE_MISSING'})
        if rows is not None:result[period]={'rows':rows,'accessions':sources}
    return result,issues

@app.get('/api/analysis/{cik}')
def analysis(cik:str,req:Request):
    authorize(req)
    entity=require_filer(cik);cik=entity['cik']
    from analysis import analyze
    with storage.connect() as c:snaps=storage.latest(c,cik)
    effective,issues=effective_periods(snaps)
    rows=[(p,r['issuer'],r['cusip'],r['title'],r['value_usd'],r['put_call'],r['share_type'])
          for p,details in effective.items() for r in details['rows']]
    result=analyze(rows)
    result.update({'cik':cik,'reporting_entity':entity['name'],
        'amendment_issues':issues,'period_sources':{p:d['accessions'] for p,d in effective.items()},
        'reported_other_managers':[{'period':s.period,'accession':s.accession,'managers':s.managers} for s in snaps],
        **release()})
    return result

@app.get('/api/managers/{cik}')
def managers(cik:str, req:Request):
    authorize(req)
    entity=require_filer(cik);cik=entity['cik']
    from manager_reporting import manager_report
    with storage.connect() as c:
        import market_store
        cached=market_store.forward(c,cik)
        if cached is not None:
            cached.update(release());return cached
        snaps=storage.latest(c,cik)
    result=manager_report(snaps)
    result.update({'cik':cik,'reporting_entity':entity['name'],**release()})
    return result

@app.post('/api/sync')
def manual(req:Request):
    authorize(req)
    try:
        SECClient()  # Reject configuration errors before acknowledging a queued job.
        job=storage.begin('manual')
    except Exception as exc:raise HTTPException(503,error_code(exc)) from exc
    if job is None:return {'queued':False,'reason':'An import is already running'}
    try:
        threading.Thread(target=execute,args=(job,),daemon=True).start()
    except Exception as exc:
        try: job.finish('failed',[{'code':'WORKER_START_FAILED'}])
        finally: job.close()
        raise HTTPException(503,'WORKER_START_FAILED') from exc
    return {'queued':True,'run_id':job.id,'status':'running'}

@app.post('/api/load/{cik}')
def load_filer(cik:str,req:Request):
    authorize(req)
    entity=require_filer(cik)
    try:
        SECClient()
        with storage.connect() as c:state=directory.request_load(c,entity['cik'])
        threading.Thread(target=sync,kwargs={'trigger':'selected-filer','directory_refresh':False},daemon=True).start()
    except Exception as exc:raise HTTPException(503,error_code(exc)) from exc
    return {'queued':True,'cik':entity['cik'],'status':state['load_status'],
            'reason':'Selected-filer request saved; progress is available in the selected-firm status.'}


@app.get('/api/market/status')
def market_status(req:Request):
    authorize(req)
    import market_store
    with storage.connect() as c:return market_store.coverage(c)

@app.get('/api/market/managers')
def market_managers(req:Request,q:str=''):
    authorize(req)
    import market_store
    with storage.connect() as c:return market_store.manager_options(c,q)

@app.get('/api/market/reverse')
def market_reverse(req:Request,manager_id:str):
    authorize(req)
    if len(manager_id)>160:raise HTTPException(400,'Invalid manager identity')
    import market_store
    with storage.connect() as c:return market_store.reverse(c,manager_id)
