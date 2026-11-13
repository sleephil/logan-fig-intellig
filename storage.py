"""Append-only verified filing revisions and durable import/change receipts.

Legacy filings/holdings are deliberately left untouched and are not displayed as
validated v2 data. Only the isolated FIG database is used.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from ingest import Snapshot
from version import PARSER_VERSION, release
LOCK = 8726319
log = logging.getLogger('fig')

def emit(event, **fields):
    log.warning(json.dumps({'event':event, **release(), **fields}, default=str, separators=(',',':')))

def connect():
    import psycopg
    url = os.getenv('DATABASE_URL', '')
    if not url: raise RuntimeError('DATABASE_URL_MISSING')
    return psycopg.connect(url, autocommit=True, connect_timeout=10)

def setup(c):
    # Additive, isolated FIG schema; no drops, legacy rewrites or role changes.
    c.execute('''CREATE TABLE IF NOT EXISTS fig_filings_v2(
      cik TEXT NOT NULL, accession TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
      period DATE NOT NULL, filing_date DATE NOT NULL, form TEXT NOT NULL,
      parser_version TEXT NOT NULL, row_count INTEGER NOT NULL, value_usd BIGINT NOT NULL,
      payload JSONB NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY(cik,accession,snapshot_sha256))''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_import_runs(
      id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      finished_at TIMESTAMPTZ, heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      status TEXT NOT NULL, trigger TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'starting',
      current_platform TEXT, current_accession TEXT, source_url TEXT,
      bytes_downloaded BIGINT NOT NULL DEFAULT 0, scanned_rows BIGINT NOT NULL DEFAULT 0,
      checked_filings INTEGER NOT NULL DEFAULT 0, new_filings INTEGER NOT NULL DEFAULT 0,
      total_filings INTEGER NOT NULL DEFAULT 0, error TEXT, errors JSONB NOT NULL DEFAULT '[]',
      version TEXT NOT NULL, commit_sha TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_import_events(
      id BIGSERIAL PRIMARY KEY, run_id BIGINT, recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      event TEXT NOT NULL, detail JSONB NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_release_events(
      version TEXT NOT NULL, commit_sha TEXT NOT NULL, changes_sha256 TEXT NOT NULL,
      recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), changes JSONB NOT NULL,
      PRIMARY KEY(version,commit_sha,changes_sha256))''')

def jsonb(value):
    from psycopg.types.json import Jsonb
    return Jsonb(value)

def event(c, run_id, kind, **detail):
    c.execute('INSERT INTO fig_import_events(run_id,event,detail) VALUES(%s,%s,%s)',
              (run_id,kind,jsonb(detail)))
    emit(kind, run_id=run_id, **detail)

def record_release(c):
    changes = json.loads(Path(__file__).with_name('CHANGELOG.json').read_text())
    raw = json.dumps(changes,sort_keys=True,separators=(',',':')).encode()
    info = release()
    c.execute('''INSERT INTO fig_release_events(version,commit_sha,changes_sha256,changes)
                 VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
              (info['version'],info['commit'],hashlib.sha256(raw).hexdigest(),jsonb(changes)))

class Job:
    def __init__(self, c, run_id): self.c=c; self.id=run_id
    def progress(self, **fields):
        allowed = {'stage','current_platform','current_accession','source_url','bytes_downloaded',
                   'scanned_rows','checked_filings','new_filings','total_filings'}
        values = {k:v for k,v in fields.items() if k in allowed}
        if values:
            assignments = ','.join(k+'=%s' for k in values)
            self.c.execute('UPDATE fig_import_runs SET heartbeat=NOW(),'+assignments+' WHERE id=%s',
                           (*values.values(),self.id))
        # Bounded public-source diagnostics, never credentials or client data.
        emit('FIG_IMPORT_PROGRESS', run_id=self.id, **fields)

    def finish(self, status, errors):
        self.c.execute('''UPDATE fig_import_runs SET status=%s,stage=%s,finished_at=NOW(),heartbeat=NOW(),
            error=%s,errors=%s WHERE id=%s''',(status,status,errors[0]['code'] if errors else None,jsonb(errors),self.id))
        event(self.c,self.id,'FIG_IMPORT_FINISHED',status=status,errors=errors,**counts(self.c))

    def close(self):
        try: self.c.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        finally: self.c.close()

def begin(trigger='manual'):
    c=connect()
    try:
        if not c.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:
            c.close(); return None
        setup(c); record_release(c)
        stale=c.execute("UPDATE fig_import_runs SET status='interrupted',stage='interrupted',finished_at=NOW(),error='WORKER_INTERRUPTED' WHERE status='running' RETURNING id").fetchall()
        for (run_id,) in stale: event(c,run_id,'FIG_IMPORT_INTERRUPTED',reason='Previous process no longer holds import lock')
        info=release()
        run_id=c.execute("INSERT INTO fig_import_runs(status,trigger,version,commit_sha) VALUES('running',%s,%s,%s) RETURNING id",
                         (trigger,info['version'],info['commit'])).fetchone()[0]
        event(c,run_id,'FIG_IMPORT_STARTED',trigger=trigger)
        return Job(c,run_id)
    except Exception: c.close(); raise

def verified(c,cik,accession):
    return bool(c.execute('SELECT 1 FROM fig_filings_v2 WHERE cik=%s AND accession=%s AND parser_version=%s LIMIT 1',
                          (cik,accession,PARSER_VERSION)).fetchone())

def save(job,snapshot):
    s=snapshot.validate(); payload=vars(s); digest=s.checksum
    with job.c.transaction():
        inserted=job.c.execute('''INSERT INTO fig_filings_v2(cik,accession,snapshot_sha256,period,filing_date,form,
          parser_version,row_count,value_usd,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT DO NOTHING RETURNING snapshot_sha256''',
          (s.cik,s.accession,digest,s.period,s.filing_date,s.form,PARSER_VERSION,len(s.rows),s.expected_total,jsonb(payload))).fetchone()
        stored=job.c.execute('''SELECT row_count,value_usd,jsonb_array_length(payload->'rows'),
            (SELECT SUM((x->>'value_usd')::bigint) FROM jsonb_array_elements(payload->'rows') x)
            FROM fig_filings_v2 WHERE cik=%s AND accession=%s AND snapshot_sha256=%s''',(s.cik,s.accession,digest)).fetchone()
        if stored != (len(s.rows),s.expected_total,len(s.rows),s.expected_total): raise RuntimeError('DATABASE_RECONCILIATION_FAILED')
    # A separate connection must see the committed source snapshot.
    with connect() as reader:
        check=reader.execute('SELECT payload FROM fig_filings_v2 WHERE cik=%s AND accession=%s AND snapshot_sha256=%s',
                             (s.cik,s.accession,digest)).fetchone()
        if not check: raise RuntimeError('DATABASE_READBACK_MISSING')
        loaded=Snapshot(**check[0]).validate()
        if loaded.checksum != digest: raise RuntimeError('DATABASE_READBACK_HASH_MISMATCH')
    event(job.c,job.id,'FIG_FILING_READBACK_VERIFIED',cik=s.cik,accession=s.accession,period=s.period,
          row_count=len(s.rows),value_usd=s.expected_total,snapshot_sha256=digest,
          sources=s.sources,inserted=bool(inserted),reported_other_managers=len(s.managers))
    return bool(inserted)

def latest(c,cik=None):
    where='WHERE parser_version=%s'; params=[PARSER_VERSION]
    if cik:where+=' AND cik=%s';params.append(cik)
    rows=c.execute('''SELECT DISTINCT ON(cik,accession) payload FROM fig_filings_v2 '''+where+
                   ' ORDER BY cik,accession,recorded_at DESC,snapshot_sha256 DESC',params).fetchall()
    return [Snapshot(**r[0]) for r in rows]

def counts(c):
    row=c.execute('''SELECT count(DISTINCT(cik,accession)),min(period),max(period),max(recorded_at),
                    count(*) FROM fig_filings_v2 WHERE parser_version=%s''',(PARSER_VERSION,)).fetchone()
    return {'imported_filings':row[0],'first_reporting_period':str(row[1]) if row[1] else None,
            'latest_reporting_period':str(row[2]) if row[2] else None,'latest_import':row[3].isoformat() if row[3] else None,
            'stored_revisions':row[4]}

def status(c):
    from psycopg.rows import dict_row
    with c.cursor(row_factory=dict_row) as cur:
        last=cur.execute('SELECT * FROM fig_import_runs ORDER BY id DESC LIMIT 1').fetchone()
        success=cur.execute("SELECT finished_at FROM fig_import_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
    info={'last_check_status':'never','checked_filings':0,'new_filings':0,'total_filings':0,'last_error':None}
    if last:
        status_name=last['status']
        if status_name=='running' and (datetime.now(timezone.utc)-last['heartbeat']).total_seconds()>180:status_name='stalled'
        info.update({'run_id':last['id'],'last_check_status':status_name,'stage':last['stage'],
            'last_check_started':last['started_at'].isoformat(),'last_check_completed':last['finished_at'].isoformat() if last['finished_at'] else None,
            'last_heartbeat':last['heartbeat'].isoformat(),'last_error':last['error'],
            **{k:last[k] for k in ('checked_filings','new_filings','total_filings','current_platform','current_accession','source_url','bytes_downloaded','scanned_rows','errors')}})
    info.update(counts(c));info.update(release())
    info['last_successful_check']=success['finished_at'].isoformat() if success else None
    info['value_unit']='USD';info['legacy_data_displayed']=False
    return info
