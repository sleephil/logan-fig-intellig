"""Resumable SEC market preload. Runs outside browser report requests."""
from __future__ import annotations
import os, time, hashlib
from datetime import datetime, timezone
from dataclasses import asdict
import storage, market_store as store
import filer_directory as directory
from ingest import SECClient, bulk_archives, archive_range, day
from market_ingest import bulk_summaries, index_filings, indexed_filing, compact, MAX_BULK_BYTES

LOCK=8726320

def stamp():return datetime.now(timezone.utc).isoformat()
def code(exc):
    value=str(exc)
    return value if value and len(value)<120 and all(c.isupper() or c.isdigit() or c=='_' for c in value) else type(exc).__name__.upper()


def run_preload(*,max_seconds=21600,client=None):
    """Checkpoint each complete ZIP and accession. Re-entry safely resumes remaining work."""
    started=time.monotonic();c=storage.connect();errors=[]
    try:
        storage.setup(c);store.setup(c)
        if not c.execute('SELECT pg_try_advisory_lock(%s)',(LOCK,)).fetchone()[0]:return 'already_running'
        def progress(**values):
            values.update(heartbeat=stamp(),state='running')
            store.state(c,'progress',values)
            storage.emit('FIG_MARKET_PROGRESS',**values)
        progress(stage='starting_market_preload')
        start=os.getenv('HISTORY_START','2023-09-30');store.state(c,'history',dict(start=start))
        store.state(c,'last_check',dict(status='running',started_at=stamp()))
        c.execute("UPDATE fig_market_queue SET status='queued' WHERE status='running'")
        client=client or SECClient(progress=progress)
        store.seed_raw(c)
        urls=bulk_archives(client,history_start=start)
        store.state(c,'archives',dict(urls=urls,discovered_at=stamp()))
        for index,url in enumerate(urls):
            previous=c.execute('SELECT checked_at FROM fig_market_sources WHERE url=%s',(url,)).fetchone()
            if previous and (index>=2 or (datetime.now(timezone.utc)-previous[0]).total_seconds()<20*3600):continue
            if time.monotonic()-started>max_seconds:break
            progress(stage='downloading_market_archive',source_url=url,archive=index+1,archive_count=len(urls))
            try:
                stream,digest=client.download(url,limit=MAX_BULK_BYTES)
                with stream:snaps,rejected=bulk_summaries(stream,url,digest,start,progress)
                with c.transaction():
                    store.save_summaries(c,snaps,'bulk')
                    with c.cursor() as cur:
                        cur.executemany('''INSERT INTO fig_market_rejections(accession,cik,period,code,source_url) VALUES(%s,%s,%s,%s,%s)
                         ON CONFLICT(accession) DO UPDATE SET code=EXCLUDED.code,source_url=EXCLUDED.source_url,recorded_at=NOW()''',
                         [(r['accession'],r['cik'],r['period'],r['code'],url) for r in rejected])
                        # A bulk-table mismatch gets an independent original-XML retry, not an invented correction.
                        cur.executemany('''INSERT INTO fig_market_queue(accession,cik,filing_date,payload)
                          VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                          [(r['accession'],r['cik'],r['filing_date'],storage.jsonb(r)) for r in rejected])
                        cur.execute('''DELETE FROM fig_market_rejections r WHERE EXISTS
                          (SELECT 1 FROM fig_market_summaries s WHERE s.accession=r.accession AND s.cik=r.cik AND s.source_kind='xml')''')
                    c.execute('''INSERT INTO fig_market_sources(url,sha256,end_date,status,filings,rejected) VALUES(%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(url) DO UPDATE SET sha256=EXCLUDED.sha256,status=EXCLUDED.status,filings=EXCLUDED.filings,
                      rejected=EXCLUDED.rejected,checked_at=NOW()''',
                      (url,digest,archive_range(url)[1],'partial' if rejected else 'ready',len(snaps),len(rejected)))
                storage.event(c,None,'FIG_MARKET_ARCHIVE_VERIFIED',source_url=url,sha256=digest,filings=len(snaps),rejected=len(rejected))
                # Publish useful verified coverage between archives; never label it complete prematurely.
                store.rebuild(c,progress)
            except Exception as exc:
                errors.append(dict(source_url=url,code=code(exc)))
                storage.event(c,None,'FIG_MARKET_SOURCE_FAILED',**errors[-1])
        # Recent filings not yet in a released ZIP are discovered once, market-wide.
        loaded=c.execute('SELECT MAX(end_date) FROM fig_market_sources').fetchone()[0]
        if loaded:
            for year,quarter,url in directory.index_sources():
                from calendar import monthrange
                end=f'{year:04d}-{quarter*3:02d}-{monthrange(year,quarter*3)[1]:02d}'
                if day(end)<=loaded:continue
                try:
                    stream,digest=client.download(url,limit=directory.MAX_INDEX_ZIP)
                    with stream:entries=index_filings(stream,year,quarter,loaded.isoformat())
                    with c.cursor() as cur:cur.executemany('''INSERT INTO fig_market_queue(accession,cik,filing_date,payload)
                      VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',[(e['accession'],e['cik'],e['filing_date'],storage.jsonb(e)) for e in entries])
                    storage.event(c,None,'FIG_MARKET_DELTA_INDEX_VERIFIED',source_url=url,sha256=digest,accessions=len(entries))
                except Exception as exc:errors.append(dict(source_url=url,code=code(exc)))
        c.execute('''UPDATE fig_market_queue q SET status='ready',error=NULL,updated_at=NOW()
                     WHERE EXISTS(SELECT 1 FROM fig_market_summaries s WHERE s.accession=q.accession AND s.cik=q.cik AND
                       (s.source_kind='xml' OR NOT EXISTS(SELECT 1 FROM fig_market_rejections r WHERE r.accession=q.accession)))''')
        # Explicit per-accession retry limit; subsequent daily runs can retry older failures.
        c.execute("UPDATE fig_market_queue SET status='queued',attempts=0 WHERE status='failed' AND updated_at<NOW()-INTERVAL '20 hours'")
        processed=0
        while time.monotonic()-started<=max_seconds:
            row=c.execute("""UPDATE fig_market_queue SET status='running',attempts=attempts+1,updated_at=NOW()
              WHERE accession=(SELECT accession FROM fig_market_queue WHERE status='queued' ORDER BY filing_date DESC,accession LIMIT 1)
              RETURNING payload,attempts""").fetchone()
            if not row:break
            entry,attempt=row;acc=entry['accession']
            progress(stage='loading_recent_market_filing',cik=entry['cik'],accession=acc,processed=processed)
            try:
                snap=indexed_filing(entry,client)
                if day(snap.period)>=day(start):store.save_summaries(c,[compact(snap)],'xml')
                c.execute("UPDATE fig_market_queue SET status=%s,error=NULL,updated_at=NOW() WHERE accession=%s",
                    ('ready' if day(snap.period)>=day(start) else 'outside_window',acc))
            except Exception as exc:
                failure=code(exc)
                c.execute('UPDATE fig_market_queue SET status=%s,error=%s,updated_at=NOW() WHERE accession=%s',
                    ('queued' if attempt<2 else 'failed',failure,acc))
                storage.event(c,None,'FIG_MARKET_FILING_FAILED',accession=acc,cik=entry['cik'],code=failure)
            processed+=1
            if processed%500==0:store.rebuild(c,progress)
        pending=c.execute("SELECT COUNT(*) FROM fig_market_queue WHERE status IN ('queued','running')").fetchone()[0]
        failed=c.execute("SELECT COUNT(*) FROM fig_market_queue WHERE status='failed'").fetchone()[0]
        rejected=c.execute('SELECT COUNT(*) FROM fig_market_rejections').fetchone()[0]
        loaded_count=c.execute('SELECT COUNT(*) FROM fig_market_sources WHERE url=ANY(%s)',(urls,)).fetchone()[0]
        status='success' if not errors and not pending and not failed and not rejected and loaded_count==len(urls) else 'partial'
        store.state(c,'last_check',dict(status=status,checked_at=stamp(),errors=errors,pending=pending,failed=failed))
        store.rebuild(c,progress)
        store.state(c,'progress',dict(state=status,stage='completed' if status=='success' else 'partial_preload',heartbeat=stamp(),pending=pending,failed=failed))
        storage.event(c,None,'FIG_MARKET_PRELOAD_FINISHED',**store.coverage(c))
        return status
    except Exception as exc:
        try:
            store.state(c,'progress',dict(state='failed',stage='failed',error=code(exc),heartbeat=stamp()))
            store.state(c,'last_check',dict(status='failed',checked_at=stamp(),errors=[dict(code=code(exc))]))
            storage.event(c,None,'FIG_MARKET_PRELOAD_FAILED',code=code(exc))
        except Exception:storage.emit('FIG_MARKET_STATUS_WRITE_FAILED')
        return 'failed'
    finally:
        try:c.execute('SELECT pg_advisory_unlock(%s)',(LOCK,))
        finally:c.close()

if __name__=='__main__':
    import logging
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(0 if run_preload() in ('success','partial','already_running') else 1)
