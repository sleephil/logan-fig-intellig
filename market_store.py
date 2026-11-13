"""Indexed, versioned firm-manager-quarter read models for both report directions."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from types import SimpleNamespace
import hashlib, json
import storage
from market_ingest import SummarySnapshot, SUMMARY_VERSION, compact
from manager_reporting import IdentityIndex, manager_report, adjacent, pct

METHOD = 'market-relations-1'

def setup(c):
    statements = [
    '''CREATE TABLE IF NOT EXISTS fig_market_sources(url TEXT PRIMARY KEY,sha256 TEXT NOT NULL,
      end_date DATE NOT NULL,status TEXT NOT NULL,filings INTEGER NOT NULL,rejected INTEGER NOT NULL,
      checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
    '''CREATE TABLE IF NOT EXISTS fig_market_summaries(cik TEXT NOT NULL,accession TEXT NOT NULL,
      digest TEXT NOT NULL,period DATE NOT NULL,filing_date DATE NOT NULL,source_kind TEXT NOT NULL,
      payload JSONB NOT NULL,recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      PRIMARY KEY(cik,accession,digest,source_kind))''',
    '''CREATE TABLE IF NOT EXISTS fig_market_rejections(accession TEXT PRIMARY KEY,cik TEXT NOT NULL,
      period DATE,code TEXT NOT NULL,source_url TEXT NOT NULL,recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
    '''CREATE TABLE IF NOT EXISTS fig_market_queue(accession TEXT PRIMARY KEY,cik TEXT NOT NULL,
      filing_date DATE NOT NULL,payload JSONB NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
      attempts INTEGER NOT NULL DEFAULT 0,error TEXT,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
    '''CREATE TABLE IF NOT EXISTS fig_market_generations(id BIGSERIAL PRIMARY KEY,status TEXT NOT NULL,
      method TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),published_at TIMESTAMPTZ,
      coverage JSONB NOT NULL DEFAULT '{}')''',
    '''CREATE TABLE IF NOT EXISTS fig_market_reports(generation BIGINT NOT NULL,cik TEXT NOT NULL,
      name TEXT NOT NULL,filing_count INTEGER NOT NULL,first_period DATE,last_period DATE,payload JSONB NOT NULL,
      PRIMARY KEY(generation,cik))''',
    '''CREATE TABLE IF NOT EXISTS fig_market_relations(generation BIGINT NOT NULL,manager_id TEXT NOT NULL,
      cik TEXT NOT NULL,period DATE NOT NULL,manager_name TEXT NOT NULL,filer_name TEXT NOT NULL,
      metadata JSONB NOT NULL,observation JSONB NOT NULL,sources JSONB NOT NULL,
      PRIMARY KEY(generation,manager_id,cik,period))''',
    '''CREATE INDEX IF NOT EXISTS fig_market_reverse_lookup ON fig_market_relations(generation,manager_id,period)''',
    '''CREATE TABLE IF NOT EXISTS fig_market_state(key TEXT PRIMARY KEY,value JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''']
    for sql in statements:c.execute(sql)


def state(c,key,value=None):
    if value is not None:
        c.execute('''INSERT INTO fig_market_state(key,value) VALUES(%s,%s) ON CONFLICT(key)
                     DO UPDATE SET value=EXCLUDED.value,updated_at=NOW()''',(key,storage.jsonb(value)))
    row=c.execute('SELECT value FROM fig_market_state WHERE key=%s',(key,)).fetchone()
    return row[0] if row else {}


def latest_summaries(c,code=None):
    where=' WHERE cik=%s' if code else '';args=(code,) if code else ()
    # XML has precedence when both archive and raw SEC versions exist for an accession.
    rows=c.execute('''SELECT DISTINCT ON(cik,accession) payload FROM fig_market_summaries'''+where+
        " ORDER BY cik,accession,(source_kind='xml') DESC,recorded_at DESC,digest DESC",args).fetchall()
    return [SummarySnapshot(**r[0]).validate() for r in rows]


def save_summaries(c,summaries,kind):
    summaries=list(summaries)
    for s in summaries:s.validate()
    with c.transaction():
        with c.cursor() as cur:
            cur.executemany('''INSERT INTO fig_market_summaries(cik,accession,digest,period,filing_date,source_kind,payload)
              VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
              [(s.cik,s.accession,s.checksum,s.period,s.filing_date,kind,storage.jsonb(asdict(s))) for s in summaries])
            cur.executemany('DELETE FROM fig_market_rejections WHERE accession=%s',[(s.accession,) for s in summaries])
    # The exact summary hash is verified again by the report builder before publication.
    return len(summaries)


def seed_raw(c):
    # A named cursor streams source JSON instead of loading every security into RAM.
    with c.transaction():
        with c.cursor(name='fig_market_seed_raw') as cur:
            cur.execute('''SELECT DISTINCT ON(cik,accession) payload FROM fig_filings_v2
              WHERE parser_version=%s ORDER BY cik,accession,recorded_at DESC''',(storage.PARSER_VERSION,))
            for (payload,) in cur:
                save_summaries(c,[compact(storage.Snapshot(**payload))],'xml')


def coverage(c):
    sources=c.execute('SELECT url,sha256,end_date,status,filings,rejected FROM fig_market_sources ORDER BY end_date').fetchall()
    n=c.execute('SELECT COUNT(DISTINCT cik),COUNT(DISTINCT accession),MIN(period),MAX(period) FROM fig_market_summaries').fetchone()
    queue=dict(c.execute('SELECT status,COUNT(*) FROM fig_market_queue GROUP BY status').fetchall())
    rejects=c.execute('SELECT COUNT(*) FROM fig_market_rejections').fetchone()[0]
    progress=state(c,'progress');last=state(c,'last_check')
    published=c.execute("SELECT id,published_at FROM fig_market_generations WHERE status='published' AND method=%s ORDER BY id DESC LIMIT 1",(METHOD,)).fetchone()
    if progress.get('state')=='running' and progress.get('heartbeat'):
        if (datetime.now(timezone.utc)-datetime.fromisoformat(progress['heartbeat'])).total_seconds()>180:
            progress={**progress,'state':'stalled','stage':'heartbeat_overdue'}
    archive_list=state(c,'archives').get('urls',[])
    completed={s[0] for s in sources};missing=[u for u in archive_list if u not in completed]
    pending=queue.get('queued',0)+queue.get('running',0)
    return dict(schema_version=1,method_version=METHOD,history_start=state(c,'history').get('start','2023-09-30'),
        filers=n[0],filings=n[1],first_period=str(n[2]) if n[2] else None,last_period=str(n[3]) if n[3] else None,
        archives_loaded=len(sources),archives_expected=len(archive_list),missing_archives=missing,
        archive_through=max((str(s[2]) for s in sources),default=None),queue=queue,pending_filings=pending,rejected_filings=rejects,
        complete=bool(archive_list and not missing and not pending and not rejects and not queue.get('failed') and last.get('status')=='success'),
        progress=progress,last_check=last,index_generation=published[0] if published else None,index_published_at=published[1].isoformat() if published else None,
        limitation='All totals cover the loaded, publicly disclosed reporting window. Missing links do not establish that a firm does not use a manager.',
        sources=[dict(url=s[0],sha256=s[1],through=str(s[2]),status=s[3],filings=s[4],rejected=s[5]) for s in sources])


def active(c):
    row=c.execute("SELECT id FROM fig_market_generations WHERE status='published' AND method=%s ORDER BY id DESC LIMIT 1",(METHOD,)).fetchone()
    return row[0] if row else None


def rebuild(c,progress=None):
    """Build once during ingestion; all browser requests are indexed DB reads."""
    progress=progress or (lambda **kw:None)
    snapshots=latest_summaries(c)
    # Global identity graph gives the same canonical ID in the forward and reverse views.
    identities=IdentityIndex(snapshots)
    by_filer=defaultdict(list)
    for s in snapshots:by_filer[s.cik].append(s)
    gen=c.execute("INSERT INTO fig_market_generations(status,method) VALUES('building',%s) RETURNING id",(METHOD,)).fetchone()[0]
    rejections=defaultdict(set)
    for code,period in c.execute('SELECT cik,period FROM fig_market_rejections').fetchall():
        if period:rejections[code].add(str(period))
    failed_filers={r[0] for r in c.execute("SELECT DISTINCT cik FROM fig_market_queue WHERE status IN ('failed','queued','running')").fetchall()}
    try:
        for i,(code,snaps) in enumerate(sorted(by_filer.items())):
            report=manager_report(snaps,identity_index=identities)
            name=max(snaps,key=lambda s:(s.filing_date,s.accession)).manager
            report.update(cik=code,reporting_entity=name,read_model=METHOD,generation=gen)
            if code in failed_filers:
                rejections[code].update(report['periods'])
            if rejections[code]:
                for p in report['period_summaries']:
                    if p['period'] in rejections[code]:p['comparison_quality']='review'
                for m in report['managers']:
                    for h in m['history']:
                        year=str(int(h['period'][:4])-1)+h['period'][4:]
                        bad=any(p==h['period'] or adjacent(p,h['period']) for p in rejections[code])
                        if bad:
                            h.update(comparisons_allowed=False,comparable=False,change_usd=None,change_pct=None,previous_value_usd=None)
                        if year in rejections[code] or h['period'] in rejections[code]:h.update(year_change_usd=None,year_change_pct=None)
                report['possible_pairs']=[p for p in report['possible_pairs'] if p['period'] not in rejections[code] and p['previous_period'] not in rejections[code]]
                report['issues'].append(dict(code='NEWER_OR_ADDITIONAL_FILING_REJECTED',periods=sorted(rejections[code])))
            source_by_period={p['period']:p['sources'] for p in report['period_summaries']}
            relations=[]
            for m in report['managers']:
                meta={k:v for k,v in m.items() if k!='history'}
                for h in m['history']:
                    relations.append((gen,m['id'],code,h['period'],m['name'],name,storage.jsonb(meta),storage.jsonb(h),storage.jsonb(source_by_period.get(h['period'],[]))))
            with c.transaction():
                c.execute('''INSERT INTO fig_market_reports(generation,cik,name,filing_count,first_period,last_period,payload)
                  VALUES(%s,%s,%s,%s,%s,%s,%s)''',(gen,code,name,len(snaps),min(s.period for s in snaps),max(s.period for s in snaps),storage.jsonb(report)))
                if relations:
                    with c.cursor() as cur:cur.executemany('INSERT INTO fig_market_relations VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',relations)
            if i%100==0:progress(stage='building_manager_index',indexed_firms=i+1,total_firms=len(by_filer))
        cov=coverage(c)
        c.execute("UPDATE fig_market_generations SET status='published',published_at=NOW(),coverage=%s WHERE id=%s",(storage.jsonb(cov),gen))
        storage.event(c,None,'FIG_MARKET_INDEX_PUBLISHED',generation=gen,filers=len(by_filer),filings=len(snapshots))
        # Only disposable read models are pruned; source summaries and audit events stay append-only.
        obsolete=[r[0] for r in c.execute('''SELECT id FROM fig_market_generations WHERE id NOT IN
            (SELECT id FROM fig_market_generations WHERE status='published' ORDER BY id DESC LIMIT 3)
            AND created_at<NOW()-INTERVAL '10 minutes' AND status IN ('published','failed')''').fetchall()]
        if obsolete:
            with c.transaction():
                c.execute('DELETE FROM fig_market_relations WHERE generation=ANY(%s)',(obsolete,))
                c.execute('DELETE FROM fig_market_reports WHERE generation=ANY(%s)',(obsolete,))
                c.execute("UPDATE fig_market_generations SET status='retired' WHERE id=ANY(%s)",(obsolete,))
        return gen
    except Exception:
        c.execute("UPDATE fig_market_generations SET status='failed' WHERE id=%s",(gen,));raise


def forward(c,code):
    gen=active(c)
    if gen is None:return None
    # A manual raw refresh must not be hidden behind an older read model.
    changed=c.execute('''SELECT 1 FROM fig_filings_v2 f JOIN fig_market_generations g ON g.id=%s
        WHERE f.cik=%s AND f.parser_version=%s AND f.recorded_at>g.created_at LIMIT 1''',
        (gen,code,storage.PARSER_VERSION)).fetchone()
    if changed:return None
    row=c.execute('SELECT payload FROM fig_market_reports WHERE generation=%s AND cik=%s',(gen,code)).fetchone()
    if not row:return None
    result=row[0];result['market_coverage']=coverage(c)
    return result


def directory_rows(c):
    gen=active(c)
    if gen is None:return {}
    return {r[0]:dict(filing_count=r[1],first_period=str(r[2]),last_period=str(r[3])) for r in c.execute(
        'SELECT cik,filing_count,first_period,last_period FROM fig_market_reports WHERE generation=%s',(gen,)).fetchall()}


def manager_options(c,search='',limit=250):
    gen=active(c)
    if gen is None:return dict(managers=[],total=0,coverage=coverage(c),generation=None)
    # Parameterized search covers the precomputed manager name/identifier/alias registry.
    term=search.strip()[:120];needle='%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    rows=c.execute('''SELECT manager_id,MAX(manager_name),COUNT(DISTINCT cik),MAX(period) FILTER (WHERE observation->>'reported_assets_usd' IS NOT NULL)
        FROM fig_market_relations WHERE generation=%s AND (manager_name ILIKE %s OR manager_id ILIKE %s OR metadata->>'names' ILIKE %s)
        GROUP BY manager_id ORDER BY LOWER(MAX(manager_name)),manager_id LIMIT %s''',(gen,needle,needle,needle,min(max(limit,1),250))).fetchall()
    total=c.execute('''SELECT COUNT(DISTINCT manager_id) FROM fig_market_relations WHERE generation=%s
        AND (manager_name ILIKE %s OR manager_id ILIKE %s OR metadata->>'names' ILIKE %s)''',(gen,needle,needle,needle)).fetchone()[0]
    return dict(managers=[dict(id=r[0],name=r[1],reporting_firms=r[2],last_period=str(r[3])) for r in rows],
                total=total,generation=gen,coverage=coverage(c))


def reverse(c,key):
    gen=active(c)
    if gen is None:return dict(schema_version=1,manager=None,firms=[],periods=[],totals=[],coverage=coverage(c))
    rows=c.execute('''SELECT cik,filer_name,metadata,observation,sources FROM fig_market_relations
                     WHERE generation=%s AND manager_id=%s ORDER BY cik,period''',(gen,key)).fetchall()
    if not rows:return dict(schema_version=1,manager=None,firms=[],periods=[],totals=[],coverage=coverage(c))
    by_firm={};periods=sorted({r[3]['period'] for r in rows});totals=[]
    for code,name,meta,obs,sources in rows:
        firm=by_firm.setdefault(code,dict(cik=code,name=name,history=[]))
        firm['history'].append({**obs,'sources':sources})
    comparable_observations={}
    for firm in by_firm.values():
        before=None
        for h in firm['history']:
            if h['comparable'] and before and adjacent(before['period'],h['period']) and not h['jointly_linked_usd'] and not before['jointly_linked_usd']:
                comparable_observations.setdefault(h['period'],[]).append(h)
            before=h
    for p in periods:
        observations=[r[3] for r in rows if r[3]['period']==p and r[3]['reported_assets_usd'] is not None]
        comparable=comparable_observations.get(p,[])
        totals.append(dict(period=p,gross_reported_assets_usd=sum(h['reported_assets_usd'] for h in observations),
            reporting_firms=len(observations),jointly_linked_usd=sum(h['jointly_linked_usd'] for h in observations),
            comparable_firms=len(comparable),comparable_change_usd=sum(h['change_usd'] for h in comparable) if comparable else None))
    return dict(schema_version=1,method_version=METHOD,generation=gen,manager=rows[-1][2],firms=list(by_firm.values()),periods=periods,totals=totals,coverage=coverage(c),
        limitations=[
        'These are reporting entities that name this manager in their other-included-manager disclosures, not a verified client list.',
        'Aggregate assets are gross sums across filings and may overlap between reporting entities. They are not deduplicated economic assets or manager-wide AUM.',
        'Quarterly comparable change uses only firms with adjacent, comparable observations. A missing relationship or missing filing is not a zero balance or confirmed loss of business.',
        'Current-quarter filings may still be arriving. Missing managers can also reflect disclosure limitations, affiliates, name variations or unresolved identifiers.',
        'Identity uses consistent SEC identifiers, otherwise exact normalized names. Similar names are not automatically merged.'])
