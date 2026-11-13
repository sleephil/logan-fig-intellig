"""SEC-wide 13F identity directory, separate from expensive holdings histories.

Read official compressed quarterly master indexes, never extract ZIP paths.
CIK is the identity. Directory discovery is not a claim that holdings are loaded.
Historical indexes are cached; the current and prior quarter are checked daily.
"""
from __future__ import annotations
import io
import os
import re
import stat
import zipfile
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from ingest import SECClient, PLATFORMS, day
import storage

FORMS = frozenset({'13F-HR', '13F-HR/A', '13F-NT', '13F-NT/A', '13F', '13F/A', '13F-E', '13F-E/A'})
START_YEAR = 1994
MAX_INDEX_ZIP = 64_000_000
MAX_INDEX_TEXT = 256_000_000


def cik(value: str) -> str:
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,10}', value) or int(value) == 0:
        raise ValueError('INVALID_FILER_CIK')
    return value.zfill(10)


def index_sources(today=None):
    today = today or datetime.now(timezone.utc).date()
    return [(y, q, f'https://www.sec.gov/Archives/edgar/full-index/{y}/QTR{q}/master.zip')
            for y in range(today.year, START_YEAR - 1, -1)
            for q in range(4, 0, -1) if date(y, 3*q-2, 1) <= today]


def parse_index(stream, year, quarter):
    """Return one record per filer/form in the complete bounded official index."""
    with zipfile.ZipFile(stream) as archive:
        members = archive.infolist()
        if not 1 <= len(members) <= 4:
            raise ValueError('INDEX_ZIP_MEMBERS')
        for member in members:
            path = PurePosixPath(member.filename)
            if (len(path.parts) != 1 or path.is_absolute() or '..' in path.parts or '\\' in member.filename
                    or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1
                    or member.file_size > MAX_INDEX_TEXT):
                raise ValueError('UNSAFE_INDEX_ZIP')
        files = [m for m in members if m.filename.lower() == 'master.idx']
        if len(files) != 1:
            raise ValueError('INDEX_FILE_MISSING')
        seen_header = False
        records = {}
        with archive.open(files[0]) as raw:
            for n, line in enumerate(io.TextIOWrapper(raw, encoding='latin-1')):
                if n > 3_000_000 or len(line) > 8192:
                    raise ValueError('INDEX_SIZE_LIMIT')
                line = line.strip()
                if not seen_header:
                    if line == 'CIK|Company Name|Form Type|Date Filed|Filename':
                        seen_header = True
                    continue
                if not line or set(line) == {'-'}:
                    continue
                try:
                    c, rest = line.split('|', 1)
                    name, form, filed, path = rest.rsplit('|', 3)
                except ValueError as exc:
                    raise ValueError('MALFORMED_INDEX_ROW') from exc
                if form not in FORMS:
                    continue
                c = cik(c)
                dt = day(filed)
                if (dt.year != year or (dt.month-1)//3+1 != quarter or not name
                        or len(name) > 500 or not re.fullmatch(r'edgar/data/[0-9]+/[0-9-]+\.txt', path)
                        or int(path.split('/')[2]) != int(c)):
                    raise ValueError('INVALID_INDEX_FILING')
                key = (c, form)
                row = records.setdefault(key, {'cik': c, 'name': name, 'first': filed, 'last': filed, 'form': form, 'names': set()})
                row['names'].add(name)
                row['first'] = min(row['first'], filed)
                if filed >= row['last']:
                    row.update(name=name, last=filed)
        if not seen_header:
            raise ValueError('INDEX_HEADER_MISSING')
        return list(records.values())


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS fig_filer_directory(
        cik TEXT PRIMARY KEY, name TEXT NOT NULL, names TEXT[] NOT NULL DEFAULT '{}',
        first_filing DATE, last_filing DATE, forms TEXT[] NOT NULL DEFAULT '{}',
        source_url TEXT, source_sha256 TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        tracked BOOLEAN NOT NULL DEFAULT FALSE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_directory_sources(
        url TEXT PRIMARY KEY, sha256 TEXT NOT NULL, year INTEGER NOT NULL, quarter INTEGER NOT NULL,
        filer_form_count INTEGER NOT NULL, checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_directory_checks(
        id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
        status TEXT NOT NULL, errors JSONB NOT NULL DEFAULT '[]', expected_sources INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS fig_filer_requests(
        cik TEXT PRIMARY KEY REFERENCES fig_filer_directory(cik), status TEXT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
        run_id BIGINT, error TEXT)''')
    for name, code in PLATFORMS.items():
        c.execute('''INSERT INTO fig_filer_directory(cik,name,names,tracked) VALUES(%s,%s,%s,TRUE)
                     ON CONFLICT(cik) DO NOTHING''', (code, name, [name]))


def save_index(c, records, url, digest, year, quarter):
    # Atomically publish complete source membership and receipt, not partial rows.
    with c.transaction():
        with c.cursor() as cur:
            cur.executemany('''INSERT INTO fig_filer_directory(cik,name,names,first_filing,last_filing,forms,source_url,source_sha256)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(cik) DO UPDATE SET
                name=CASE WHEN fig_filer_directory.last_filing IS NULL OR EXCLUDED.last_filing>=fig_filer_directory.last_filing
                    THEN EXCLUDED.name ELSE fig_filer_directory.name END,
                names=ARRAY(SELECT DISTINCT unnest(fig_filer_directory.names || EXCLUDED.names)),
                forms=ARRAY(SELECT DISTINCT unnest(fig_filer_directory.forms || EXCLUDED.forms)),
                first_filing=LEAST(fig_filer_directory.first_filing,EXCLUDED.first_filing),
                last_filing=GREATEST(fig_filer_directory.last_filing,EXCLUDED.last_filing),
                source_url=CASE WHEN fig_filer_directory.last_filing IS NULL OR EXCLUDED.last_filing>=fig_filer_directory.last_filing
                    THEN EXCLUDED.source_url ELSE fig_filer_directory.source_url END,
                source_sha256=CASE WHEN fig_filer_directory.last_filing IS NULL OR EXCLUDED.last_filing>=fig_filer_directory.last_filing
                    THEN EXCLUDED.source_sha256 ELSE fig_filer_directory.source_sha256 END, updated_at=NOW()''',
                [(r['cik'], r['name'], sorted(r['names']), r['first'], r['last'], [r['form']], url, digest) for r in records])
        c.execute('''INSERT INTO fig_directory_sources(url,sha256,year,quarter,filer_form_count)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(url) DO UPDATE SET sha256=EXCLUDED.sha256,
            filer_form_count=EXCLUDED.filer_form_count,checked_at=NOW()''', (url, digest, year, quarter, len(records)))


def refresh(job, client=None, sources=None):
    setup(job.c)
    client = client or SECClient(progress=job.progress)
    sources = sources if sources is not None else index_sources()
    check = job.c.execute("INSERT INTO fig_directory_checks(status,expected_sources) VALUES('running',%s) RETURNING id", (len(sources),)).fetchone()[0]
    errors = []
    try:
        for i, (year, quarter, url) in enumerate(sources):
            # Sealed historical indexes are cached; never suppress failed sources.
            cached = job.c.execute('SELECT checked_at FROM fig_directory_sources WHERE url=%s', (url,)).fetchone()
            if cached and i >= 2:
                continue
            job.progress(stage='indexing_sec_filers', source_url=url, directory_index=i+1, directory_total=len(sources))
            try:
                stream, digest = client.download(url, limit=MAX_INDEX_ZIP)
                with stream:
                    records = parse_index(stream, year, quarter)
                save_index(job.c, records, url, digest, year, quarter)
                storage.event(job.c, job.id, 'FIG_DIRECTORY_INDEX_VERIFIED', source_url=url, sha256=digest, filer_form_count=len(records))
            except Exception as exc:
                from app import error_code
                errors.append({'source_url': url, 'code': error_code(exc)})
                storage.event(job.c, job.id, 'FIG_DIRECTORY_INDEX_FAILED', **errors[-1])
        state = 'partial' if errors else 'success'
        job.c.execute('UPDATE fig_directory_checks SET status=%s,finished_at=NOW(),errors=%s WHERE id=%s', (state, storage.jsonb(errors), check))
        storage.event(job.c, job.id, 'FIG_DIRECTORY_FINISHED', **status(job.c))
        return errors
    except Exception:
        job.c.execute("UPDATE fig_directory_checks SET status='failed',finished_at=NOW() WHERE id=%s", (check,))
        raise


def status(c):
    sources = index_sources()
    total = c.execute('SELECT COUNT(*) FROM fig_filer_directory WHERE last_filing IS NOT NULL').fetchone()[0]
    done = c.execute('SELECT COUNT(*) FROM fig_directory_sources WHERE url=ANY(%s)', ([u for _, _, u in sources],)).fetchone()[0]
    last = c.execute('SELECT status,finished_at,errors FROM fig_directory_checks ORDER BY id DESC LIMIT 1').fetchone()
    checked = last[1].isoformat() if last and last[1] else None
    stale = not last or not last[1] or (datetime.now(timezone.utc)-last[1]).total_seconds() > 36*3600
    return {'filers': total, 'start_year': START_YEAR, 'indexed_quarters': done, 'expected_quarters': len(sources),
            'complete': done == len(sources) and bool(last and last[0] == 'success'), 'stale': stale,
            'status': last[0] if last else 'not_started', 'checked_at': checked, 'errors': last[2] if last else [],
            'holdings_history_start': os.getenv('HISTORY_START', '2023-09-30'),
            'scope': 'SEC indexed 13F reporting entities since 1994, including historical and notice-only filers; not all histories are loaded.'}


def get(c, code):
    code = cik(code)
    from psycopg.rows import dict_row
    with c.cursor(row_factory=dict_row) as cur:
        row = cur.execute('''SELECT d.*,r.status AS load_status,r.error AS load_error,r.finished_at AS load_finished_at,r.run_id
            FROM fig_filer_directory d LEFT JOIN fig_filer_requests r USING(cik) WHERE d.cik=%s''', (code,)).fetchone()
    return row


def listing(c):
    from psycopg.rows import dict_row
    with c.cursor(row_factory=dict_row) as cur:
        rows = cur.execute('''SELECT d.name,d.cik,d.names,d.forms,d.last_filing,r.status AS load_status,
            COALESCE(f.filing_count,0) AS filing_count,f.first_period,f.last_period
            FROM fig_filer_directory d LEFT JOIN fig_filer_requests r USING(cik) LEFT JOIN
            (SELECT cik,COUNT(DISTINCT accession) AS filing_count,MIN(period) AS first_period,MAX(period) AS last_period
             FROM fig_filings_v2 WHERE parser_version=%s GROUP BY cik) f USING(cik)
             ORDER BY LOWER(d.name),d.cik''', (storage.PARSER_VERSION,)).fetchall()
    return [{k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in r.items()} for r in rows]


def request_load(c, code):
    code = cik(code)
    if not get(c, code):
        raise ValueError('UNKNOWN_FILER')
    # A repeated click does not duplicate or reset an active import.
    c.execute('''INSERT INTO fig_filer_requests(cik,status) VALUES(%s,'queued') ON CONFLICT(cik) DO UPDATE SET
        status='queued',requested_at=NOW(),finished_at=NULL,error=NULL
        WHERE fig_filer_requests.status NOT IN ('queued','running')''', (code,))
    c.execute('UPDATE fig_filer_directory SET tracked=TRUE WHERE cik=%s', (code,))
    return get(c, code)


def claim(c, run_id):
    row = c.execute('''UPDATE fig_filer_requests SET status='running',run_id=%s,error=NULL WHERE cik=
        (SELECT cik FROM fig_filer_requests WHERE status='queued' ORDER BY requested_at,cik LIMIT 1)
        RETURNING cik''', (run_id,)).fetchone()
    return row[0] if row else None


def finish(c, code, state, error=None):
    c.execute('''UPDATE fig_filer_requests SET status=%s,finished_at=NOW(),error=%s WHERE cik=%s''', (state, error, code))
    storage.event(c, None, 'FIG_FILER_LOAD_FINISHED', cik=code, load_status=state, error=error)
