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
from version import release

def error_code(exc):
    message=str(exc)
    if message and len(message)<100 and all(c.isupper() or c.isdigit() or c=='_' for c in message):return message
    return type(exc).__name__.upper()

def execute(job, *, bulk=False):
    errors=[]; checked=0; imported=0
    try:
        client=SECClient(progress=job.progress)
        if bulk:
            for url in bulk_archives(client):
                job.progress(stage='downloading_zip',source_url=url)
                archive,digest=client.download(url,limit=MAX_ZIP)
                with archive:
                    snapshots=parse_bulk(archive,url,digest,PLATFORMS.values(),os.getenv('HISTORY_START','2023-09-30'),job.progress)
                for snap in snapshots:
                    imported+=int(storage.save(job,snap));checked+=1
                    job.progress(checked_filings=checked,new_filings=imported)
        for name,cik in PLATFORMS.items():
            job.progress(stage='discovering_filings',current_platform=name,current_accession='')
            try: entries=submissions(cik,client)
            except Exception as exc:
                errors.append({'platform':name,'code':error_code(exc)});continue
            if not entries:
                errors.append({'platform':name,'code':'NO_13F_FILINGS_FOUND'});continue
            job.progress(total_filings=len(entries))
            for entry in entries:
                acc=entry['accessionNumber']
                job.progress(stage='checking_filing',current_accession=acc,bytes_downloaded=0)
                try:
                    if not storage.verified(job.c,cik,acc):
                        snap=filing(cik,entry,client)
                        job.progress(stage='validating_and_saving')
                        imported+=int(storage.save(job,snap))
                except Exception as exc:
                    failure={'platform':name,'accession':acc,'code':error_code(exc)}
                    errors.append(failure);storage.event(job.c,job.id,'FIG_FILING_FAILED',**failure)
                finally:
                    checked+=1;job.progress(checked_filings=checked,new_filings=imported)
        result='success' if not errors else 'partial' if storage.counts(job.c)['imported_filings'] else 'failed'
        job.finish(result,errors)
        return result
    except Exception as exc:
        errors.append({'code':error_code(exc)})
        try:job.finish('failed',errors)
        except Exception:storage.emit('FIG_IMPORT_STATUS_WRITE_FAILED',run_id=job.id,code=error_code(exc))
        return 'failed'
    finally:job.close()

def sync(*,bulk=False,trigger='scheduled'):
    job=storage.begin(trigger)
    if job is None:
        storage.emit('FIG_IMPORT_ALREADY_RUNNING');return 'already_running'
    return execute(job,bulk=bulk)

@asynccontextmanager
async def lifespan(app):
    if os.getenv('DATABASE_URL'):
        with storage.connect() as c: storage.setup(c);storage.record_release(c)
        storage.emit('FIG_RELEASE_READY')
        if os.getenv('FIG_SYNC_ON_STARTUP','').lower()=='true':
            threading.Thread(target=sync,kwargs={'trigger':'startup'},daemon=True).start()
    yield

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
    with storage.connect() as c: return storage.status(c)

@app.get('/api/version')
def version(req:Request):
    authorize(req)
    from pathlib import Path
    import json
    return {**release(),'changes':json.loads(Path(__file__).with_name('CHANGELOG.json').read_text())}

@app.get('/api/platforms')
def platforms(req:Request):
    authorize(req)
    with storage.connect() as c:snapshots=storage.latest(c)
    return [{'name':name,'cik':cik,'filing_count':sum(s.cik==cik for s in snapshots),
             'first_period':min((s.period for s in snapshots if s.cik==cik),default=None),
             'last_period':max((s.period for s in snapshots if s.cik==cik),default=None)} for name,cik in PLATFORMS.items()]

@app.get('/api/filings/{cik}')
def reports(cik:str,req:Request):
    authorize(req)
    if cik not in PLATFORMS.values():raise HTTPException(404,'Unknown filer')
    with storage.connect() as c:snaps=storage.latest(c,cik)
    return [{'period':s.period,'accession':s.accession,'positions':len(s.rows),'reported_value_usd':s.expected_total,
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
    if cik not in PLATFORMS.values():raise HTTPException(404,'Unknown filer')
    from analysis import analyze
    with storage.connect() as c:snaps=storage.latest(c,cik)
    effective,issues=effective_periods(snaps)
    rows=[(p,r['issuer'],r['cusip'],r['title'],r['value_usd'],r['put_call'],r['share_type'])
          for p,details in effective.items() for r in details['rows']]
    result=analyze(rows)
    result.update({'cik':cik,'reporting_entity':next(name for name,v in PLATFORMS.items() if v==cik),
        'amendment_issues':issues,'period_sources':{p:d['accessions'] for p,d in effective.items()},
        'reported_other_managers':[{'period':s.period,'accession':s.accession,'managers':s.managers} for s in snaps],
        **release()})
    return result

@app.get('/api/managers/{cik}')
def managers(cik:str, req:Request):
    authorize(req)
    if cik not in PLATFORMS.values():raise HTTPException(404,'Unknown filer')
    from manager_reporting import manager_report
    with storage.connect() as c:snaps=storage.latest(c,cik)
    result=manager_report(snaps)
    result.update({'cik':cik,'reporting_entity':next(name for name,v in PLATFORMS.items() if v==cik),**release()})
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
