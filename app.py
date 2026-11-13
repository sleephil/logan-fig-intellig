import os,threading,logging,hmac
from datetime import datetime,timezone
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse
from ingest import PLATFORMS,submissions,filing
import psycopg
app=FastAPI(title="Logan FIG Intelligence")
log=logging.getLogger("fig")
state={"last_run":None,"last_error":None,"ingested":0}
def connect():
    url=os.getenv("DATABASE_URL")
    if not url:raise RuntimeError("DATABASE_URL missing")
    return psycopg.connect(url)
def authorize(req):
    # Fail closed until the Toolkit Entra gateway is integrated.
    secret=os.getenv("FIG_API_TOKEN","")
    supplied=req.headers.get("X-FIG-Token","")
    if not secret or not hmac.compare_digest(secret,supplied):
        raise HTTPException(403,"Toolkit authentication integration required")
def setup(c):
    c.execute("CREATE TABLE IF NOT EXISTS filings(cik TEXT,accession TEXT,period DATE,form TEXT,source TEXT,ingested_at TIMESTAMPTZ DEFAULT NOW(),PRIMARY KEY(cik,accession))")
    c.execute("CREATE TABLE IF NOT EXISTS holdings(cik TEXT,accession TEXT,row_id INTEGER,issuer TEXT,cusip TEXT,title TEXT,value_usd BIGINT,shares DOUBLE PRECISION,PRIMARY KEY(cik,accession,row_id))")
    c.execute("CREATE TABLE IF NOT EXISTS ingest_runs(id BIGSERIAL PRIMARY KEY,started_at TIMESTAMPTZ DEFAULT NOW(),finished_at TIMESTAMPTZ,status TEXT NOT NULL DEFAULT 'running',checked_filings INTEGER NOT NULL DEFAULT 0,new_filings INTEGER NOT NULL DEFAULT 0,error TEXT)")
def sync():
    run_id=None
    checked=0
    imported=0
    try:
        with connect() as c:
            setup(c)
            run_id=c.execute("INSERT INTO ingest_runs(status) VALUES('running') RETURNING id").fetchone()[0]
            c.commit()
            for name,cik in PLATFORMS.items():
                entries=submissions(cik)
                for entry in entries:
                    checked+=1
                    acc=entry["accessionNumber"]
                    if c.execute("SELECT 1 FROM filings WHERE cik=%s AND accession=%s",(cik,acc)).fetchone():
                        continue
                    try:
                        rows,source=filing(cik,entry)
                        with c.transaction():
                            c.execute("INSERT INTO filings(cik,accession,period,form,source) VALUES(%s,%s,%s,%s,%s)",(cik,acc,entry["reportDate"],entry["form"],source))
                            with c.cursor() as cur:
                                cur.executemany("INSERT INTO holdings(cik,accession,row_id,issuer,cusip,title,value_usd,shares) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",[(cik,acc,i,*row) for i,row in enumerate(rows)])
                        imported+=1
                    except Exception:
                        log.exception("Ingest failed for %s",acc)
                        raise
                    finally:
                        c.execute("UPDATE ingest_runs SET checked_filings=%s,new_filings=%s WHERE id=%s",(checked,imported,run_id))
                        c.commit()
            c.execute("UPDATE ingest_runs SET finished_at=NOW(),status='success',checked_filings=%s,new_filings=%s WHERE id=%s",(checked,imported,run_id))
            c.commit()
    except Exception as exc:
        log.exception("FIG SEC check failed")
        if run_id is not None:
            try:
                with connect() as c:
                    c.execute("UPDATE ingest_runs SET finished_at=NOW(),status='failed',checked_filings=%s,new_filings=%s,error=%s WHERE id=%s",(checked,imported,str(exc)[:500],run_id))
                    c.commit()
            except Exception:
                log.exception("Could not record FIG failure")
        raise

@app.get("/api/status")
def status(req:Request):
    authorize(req)
    with connect() as c:
        setup(c)
        last=c.execute("SELECT started_at,finished_at,status,checked_filings,new_filings,error FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
        last_success=c.execute("SELECT finished_at FROM ingest_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
        counts=c.execute("SELECT count(*),max(period),max(ingested_at) FROM filings").fetchone()
    def iso(value):return value.isoformat() if value else None
    return {"last_check_started":iso(last[0]) if last else None,"last_check_completed":iso(last[1]) if last else None,
            "last_check_status":last[2] if last else "never","checked_filings":last[3] if last else 0,
            "new_filings":last[4] if last else 0,"last_error":last[5] if last else None,
            "last_successful_check":iso(last_success[0]) if last_success else None,
            "imported_filings":counts[0],"latest_reporting_period":iso(counts[1]),"latest_import":iso(counts[2])}

@app.get("/health")
def health():return {"status":"ok"}
@app.get("/",response_class=HTMLResponse)
def home():return "<h1>Logan FIG Intelligence</h1><p>Private service. Toolkit Entra SSO integration pending; no public reports enabled.</p>"
@app.get("/api/platforms")
def platforms(req:Request):
    authorize(req)
    with connect() as c:
        setup(c); rows=c.execute("SELECT cik,count(*),min(period)::text,max(period)::text FROM filings GROUP BY cik").fetchall()
    return [{"name":name,"cik":cik,"filing_count":next((r[1] for r in rows if r[0]==cik),0),"first_period":next((r[2] for r in rows if r[0]==cik),None),"last_period":next((r[3] for r in rows if r[0]==cik),None)} for name,cik in PLATFORMS.items()]
@app.get("/api/filings/{cik}")
def reports(cik:str,req:Request):
    authorize(req)
    with connect() as c:
        return [{"period":str(r[0]),"accession":r[1],"positions":r[2],"reported_value_usd":r[3]} for r in c.execute("SELECT f.period,f.accession,count(h.row_id),coalesce(sum(h.value_usd),0) FROM filings f LEFT JOIN holdings h USING(cik,accession) WHERE f.cik=%s GROUP BY f.period,f.accession ORDER BY f.period DESC",(cik,)).fetchall()]
@app.post("/api/sync")
def manual(req:Request):
    authorize(req)
    threading.Thread(target=sync,daemon=True).start()
    return {"queued":True}
