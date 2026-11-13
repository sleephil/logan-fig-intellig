"""Official SEC 13F XML and bounded ZIP/TSV ingestion.

All values are normalized ONCE to USD using filing date, not holdings period.
Only reconciled filings leave the parser. No SEC credentials or paid feed needed.
"""
from __future__ import annotations
import csv
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit
import requests
from defusedxml import ElementTree as ET
from version import PARSER_VERSION

SEC = 'https://data.sec.gov'
BULK_INDEX = 'https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets'
# This CIK is Oppenheimer Asset Management, NOT every Oppenheimer legal entity.
PLATFORMS = {'Oppenheimer Asset Management': '0001269119'}
MAX_XML = 32_000_000
MAX_ZIP = 256_000_000
MAX_EXPANDED = 2_000_000_000
MAX_ROWS = 300_000
ACC = re.compile(r'^\d{10}-\d{2}-\d{6}$')

def day(value):
    value = str(value).strip()
    for fmt in ('%Y-%m-%d', '%m-%d-%Y', '%d-%b-%Y'):
        try: return datetime.strptime(value, fmt).date()
        except ValueError: pass
    raise ValueError('Invalid source date: ' + value[:40])

def number(value, *, integral=False):
    try: n = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation as exc: raise ValueError('Invalid source number') from exc
    if not n.is_finite() or n < 0 or (integral and n != n.to_integral_value()):
        raise ValueError('Invalid non-negative source number')
    if n > Decimal('9223372036854775807'): raise ValueError('Source number overflow')
    return int(n) if integral else str(n)

def multiplier(filing_date):
    return 1000 if day(filing_date) < date(2023, 1, 3) else 1

def local(tag): return tag.rsplit('}', 1)[-1].lower()
def text(node, name, default=''):
    return next(((x.text or '').strip() for x in node.iter() if local(x.tag) == name.lower()), default)
def child(node, name):
    return next((x for x in node.iter() if local(x.tag) == name.lower()), None)

def safe_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in ('www.sec.gov', 'data.sec.gov', 'sec.gov') or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Only official SEC HTTPS sources are allowed')
    if '..' in PurePosixPath(p.path).parts: raise ValueError('Invalid SEC path')
    return url

class SECClient:
    def __init__(self, progress=None, session=None):
        self.ua = os.getenv('SEC_USER_AGENT', '').strip()
        if '@' not in self.ua or len(self.ua) < 10: raise RuntimeError('SEC_USER_AGENT_MISSING')
        self.session = session or requests.Session()
        self.progress = progress or (lambda **kw: None)
        self.last_request = 0.0
        self.lock = threading.Lock()

    def download(self, url, *, limit=MAX_XML):
        safe_url(url)
        for attempt in range(4):
            with self.lock:
                time.sleep(max(0, .35 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
            self.progress(stage='downloading', source_url=url, attempt=attempt+1, bytes_downloaded=0)
            try:
                response = self.session.get(url, headers={'User-Agent': self.ua, 'Accept-Encoding': 'gzip, deflate'},
                                            timeout=(10, 45), stream=True, allow_redirects=False)
                if response.status_code in (429, 500, 502, 503, 504):
                    retry = response.headers.get('Retry-After', '')
                    response.close()
                    if attempt == 3: raise RuntimeError('SEC_RETRIES_EXHAUSTED')
                    delay = min(60, max(2**attempt, int(retry) if retry.isdigit() else 0))
                    self.progress(stage='retrying', source_url=url, retry_seconds=delay)
                    time.sleep(delay); continue
                if response.status_code == 403:
                    response.close(); raise RuntimeError('SEC_ACCESS_DENIED_403')
                if 300 <= response.status_code < 400:
                    response.close(); raise RuntimeError('SEC_UNEXPECTED_REDIRECT')
                try: response.raise_for_status()
                except Exception:
                    response.close(); raise
                dest = tempfile.SpooledTemporaryFile(max_size=4_000_000, mode='w+b')
                sha = hashlib.sha256(); size = 0; reported = 0
                try:
                    for block in response.iter_content(256_000):
                        if not block: continue
                        size += len(block)
                        if size > limit: raise ValueError('SEC_DOWNLOAD_TOO_LARGE')
                        dest.write(block); sha.update(block)
                        if size - reported >= 1_000_000:
                            self.progress(stage='downloading', source_url=url, bytes_downloaded=size)
                            reported = size
                    dest.seek(0)
                    self.progress(stage='downloaded', source_url=url, bytes_downloaded=size)
                    return dest, sha.hexdigest()
                except Exception:
                    dest.close(); raise
                finally: response.close()
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3: raise RuntimeError('SEC_NETWORK_TIMEOUT')
                time.sleep(2**attempt)
        raise RuntimeError('SEC_DOWNLOAD_FAILED')

    def get(self, url):
        f, digest = self.download(url)
        with f: return f.read(), digest

    def json(self, url):
        data, _ = self.get(url)
        return json.loads(data)

@dataclass
class Snapshot:
    cik: str
    accession: str
    period: str
    filing_date: str
    form: str
    manager: str
    report_type: str
    amendment_type: str
    amendment_no: int
    expected_count: int
    expected_total: int
    rows: list[dict]
    managers: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    def validate(self):
        if not ACC.fullmatch(self.accession): raise ValueError('INVALID_ACCESSION')
        self.cik = f'{int(self.cik):010d}'
        if self.form not in ('13F-HR', '13F-HR/A'): raise ValueError('UNSUPPORTED_FORM')
        if not self.manager: raise ValueError('FILING_MANAGER_MISSING')
        if day(self.period) > day(self.filing_date): raise ValueError('FILING_DATE_BEFORE_PERIOD')
        if self.form.endswith('/A') and self.amendment_type not in ('RESTATEMENT', 'NEW HOLDINGS'):
            raise ValueError('UNSUPPORTED_AMENDMENT_TYPE')
        if not self.rows or len(self.rows) > MAX_ROWS: raise ValueError('INVALID_HOLDINGS_ROW_COUNT')
        if len(self.rows) != self.expected_count: raise ValueError('TABLE_ENTRY_COUNT_MISMATCH')
        if sum(r['value_usd'] for r in self.rows) != self.expected_total: raise ValueError('TABLE_VALUE_TOTAL_MISMATCH')
        for r in self.rows:
            if not r['issuer'] or not re.fullmatch(r'[A-Z0-9*@#]{9}', r['cusip']): raise ValueError('INVALID_SECURITY_IDENTIFIER')
            if r['share_type'] not in ('SH', 'PRN') or r['put_call'] not in ('', 'Put', 'Call'):
                raise ValueError('INVALID_SECURITY_TYPE')
        return self

    def canonical(self):
        d = {k: v for k, v in vars(self).items() if k != 'sources'}
        d['parser_version'] = PARSER_VERSION
        return d

    @property
    def checksum(self):
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

def position(d, factor):
    raw = number(d.get('value', ''), integral=True)
    usd = raw * factor
    if usd > 9223372036854775807: raise ValueError('VALUE_OVERFLOW')
    return {'issuer': d.get('nameofissuer', '').strip(), 'cusip': d.get('cusip', '').upper().strip(),
            'title': d.get('titleofclass', '').strip(), 'figi': d.get('figi', '').strip(),
            'raw_value': raw, 'value_multiplier': factor, 'value_usd': usd,
            'shares': number(d.get('sshprnamt', '')), 'share_type': d.get('sshprnamttype', '').upper(),
            'put_call': d.get('putcall', '').title(), 'discretion': d.get('investmentdiscretion', ''),
            'other_manager': d.get('othermanager', ''),
            'voting_sole': number(d.get('sole', '0') or '0', integral=True),
            'voting_shared': number(d.get('shared', '0') or '0', integral=True),
            'voting_none': number(d.get('none', '0') or '0', integral=True)}

def parse_xml(cik, entry, cover_bytes, tables, sources):
    cover = ET.fromstring(cover_bytes)
    if local(cover.tag) != 'edgarsubmission': raise ValueError('INVALID_COVER_XML')
    header = child(cover, 'headerData'); cp = child(cover, 'coverPage'); summary = child(cover, 'summaryPage')
    if header is None or cp is None or summary is None: raise ValueError('MISSING_FILING_SECTIONS')
    if int(text(header, 'cik')) != int(cik): raise ValueError('FILER_CIK_MISMATCH')
    period = day(text(cp, 'reportCalendarOrQuarter')).isoformat()
    if period != day(entry['reportDate']).isoformat(): raise ValueError('FILING_PERIOD_MISMATCH')
    form = text(header, 'submissionType')
    if form != entry['form']: raise ValueError('FILING_FORM_MISMATCH')
    factor = multiplier(entry['filingDate']); rows = []
    for data in tables:
        root = ET.fromstring(data)
        if local(root.tag) != 'informationtable': raise ValueError('INVALID_INFORMATION_TABLE')
        for node in root:
            if local(node.tag) != 'infotable': continue
            rows.append(position({local(x.tag): (x.text or '').strip() for x in node.iter()}, factor))
            if len(rows) > MAX_ROWS: raise ValueError('TOO_MANY_ROWS')
    managers = []
    for m in summary.iter():
        if local(m.tag) == 'othermanager2':
            managers.append({'sequence': text(m, 'sequenceNumber'), 'name': text(m, 'name'),
                             'cik': text(m, 'cik'), 'form13f_file_number': text(m, 'form13FFileNumber')})
    filing_manager = child(cp, 'filingManager')
    return Snapshot(f'{int(cik):010d}', entry['accessionNumber'], period, day(entry['filingDate']).isoformat(),
                    form, text(filing_manager, 'name') if filing_manager is not None else '', text(cp, 'reportType'),
                    text(cp, 'amendmentType').upper(), number(text(cp, 'amendmentNo', '0') or '0', integral=True),
                    number(text(summary, 'tableEntryTotal'), integral=True),
                    number(text(summary, 'tableValueTotal'), integral=True)*factor, rows, managers, sources).validate()

def submissions(cik, client=None, history_start=None):
    client = client or SECClient()
    start = day(history_start or os.getenv('HISTORY_START', '2023-09-30'))
    data = client.json(f'{SEC}/submissions/CIK{int(cik):010d}.json')
    if int(data.get('cik', -1)) != int(cik): raise ValueError('SUBMISSIONS_CIK_MISMATCH')
    arrays = [data['filings']['recent']]
    for f in data['filings'].get('files', []):
        if day(f['filingTo']) < start: continue
        name = f['name']
        if not re.fullmatch(r'CIK\d{10}-submissions-\d+\.json', name): raise ValueError('INVALID_SUBMISSION_FILE')
        arrays.append(client.json(f'{SEC}/submissions/{name}'))
    records = {}
    for a in arrays:
        keys = ('accessionNumber', 'form', 'reportDate', 'filingDate', 'primaryDocument')
        if not all(k in a for k in keys): raise ValueError('SUBMISSIONS_FIELDS_MISSING')
        if len({len(a[k]) for k in keys}) != 1: raise ValueError('SUBMISSIONS_LENGTH_MISMATCH')
        for values in zip(*(a[k] for k in keys)):
            e = dict(zip(keys, values))
            if e['form'] not in ('13F-HR', '13F-HR/A'): continue
            if day(e['reportDate']) >= start:
                if not ACC.fullmatch(e['accessionNumber']): raise ValueError('INVALID_ACCESSION')
                records[e['accessionNumber']] = e
    return sorted(records.values(), key=lambda e: (e['filingDate'], e['accessionNumber']), reverse=True)

def filing(cik, entry, client=None):
    client = client or SECClient()
    acc = entry['accessionNumber']
    if not ACC.fullmatch(acc): raise ValueError('INVALID_ACCESSION')
    base = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/'
    index = client.json(base+'index.json')
    # The submissions API often supplies an XSL display path. Read raw XML instead.
    primary = PurePosixPath(entry.get('primaryDocument', '')).name
    names = sorted({i['name'] for i in index['directory']['item'] if i['name'].lower().endswith('.xml')})
    if primary not in names: raise ValueError('PRIMARY_DOCUMENT_NOT_FOUND')
    sources = []; cover = None; tables = []
    for name in [primary]+[n for n in names if n != primary]:
        if '/' in name or '\\' in name or name in ('.', '..'): raise ValueError('UNSAFE_DOCUMENT_NAME')
        data, sha = client.get(base+name)
        root = ET.fromstring(data)
        if name == primary:
            cover = data
        elif local(root.tag) == 'informationtable': tables.append(data)
        else: continue
        sources.append({'url': base+name, 'sha256': sha, 'format': 'xml'})
    if cover is None or not tables: raise ValueError('MISSING_INFORMATION_TABLE')
    return parse_xml(cik, entry, cover, tables, sources)

class _Links(HTMLParser):
    def __init__(self): super().__init__(); self.urls = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.lower().endswith('_form13f.zip'): self.urls.append(urljoin(BULK_INDEX, href))

def archive_range(url):
    name = PurePosixPath(urlsplit(url).path).name.lower()
    match = re.fullmatch(r'(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f.zip', name)
    if match: return tuple(datetime.strptime(s, '%d%b%Y').date() for s in match.groups())
    match = re.fullmatch(r'(\d{4})q([1-4])_form13f.zip', name)
    if match:
        from calendar import monthrange
        y, q = map(int, match.groups()); m = 3*q
        return date(y, m-2, 1), date(y, m, monthrange(y, m)[1])
    raise ValueError('UNKNOWN_BULK_ARCHIVE_DATE')

def bulk_archives(client=None, history_start=None):
    client = client or SECClient(); start = day(history_start or os.getenv('HISTORY_START', '2023-09-30'))
    data, _ = client.get(BULK_INDEX); parser = _Links(); parser.feed(data.decode('utf-8'))
    urls = []
    for url in set(parser.urls):
        safe_url(url)
        if '/form-13f-data-sets/' not in url: raise ValueError('INVALID_BULK_SOURCE')
        begin, end = archive_range(url)
        if end >= start: urls.append((end, url))
    if not urls: raise ValueError('NO_BULK_ARCHIVES_DISCOVERED')
    return [url for _, url in sorted(urls, reverse=True)]

def parse_bulk(fileobj, source_url, archive_sha256, ciks, history_start, progress=None):
    """Read TSV members in place: never extract archive paths to the filesystem."""
    safe_url(source_url); wanted = {f'{int(c):010d}' for c in ciks}; start = day(history_start)
    progress = progress or (lambda **kw: None)
    with zipfile.ZipFile(fileobj) as z:
        members = {}; total = 0
        if len(z.infolist()) > 64: raise ValueError('ZIP_TOO_MANY_MEMBERS')
        for i in z.infolist():
            p = PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or len(p.parts) != 1 or stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1:
                raise ValueError('UNSAFE_ZIP_MEMBER')
            total += i.file_size
            if total > MAX_EXPANDED or i.file_size / max(1, i.compress_size) > 500: raise ValueError('ZIP_EXPANSION_LIMIT')
            key = i.filename.upper()
            if key in members: raise ValueError('DUPLICATE_ZIP_MEMBER')
            members[key] = i.filename
        required = {'SUBMISSION.TSV', 'COVERPAGE.TSV', 'SUMMARYPAGE.TSV', 'INFOTABLE.TSV'}
        if not required.issubset(members): raise ValueError('ZIP_TABLES_MISSING')
        def records(name, required_fields):
            with z.open(members[name]) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig'), delimiter='\t')
                if not required_fields.issubset(reader.fieldnames or []): raise ValueError('BULK_FIELDS_MISSING_'+name)
                for r in reader:
                    if None in r: raise ValueError('MALFORMED_TSV_ROW')
                    yield r
        selected = {}
        for r in records('SUBMISSION.TSV', {'ACCESSION_NUMBER', 'CIK', 'SUBMISSIONTYPE', 'FILING_DATE', 'PERIODOFREPORT'}):
            if f"{int(r['CIK']):010d}" in wanted and r['SUBMISSIONTYPE'] in ('13F-HR','13F-HR/A') and day(r['PERIODOFREPORT']) >= start:
                a = r['ACCESSION_NUMBER']
                if a in selected: raise ValueError('DUPLICATE_BULK_ACCESSION')
                selected[a] = r
        covers = {}; summaries = {}; holdings = {a: [] for a in selected}; manager_map = {a: [] for a in selected}
        for table, dest in [('COVERPAGE.TSV', covers), ('SUMMARYPAGE.TSV', summaries)]:
            for r in records(table, {'ACCESSION_NUMBER'}):
                a = r['ACCESSION_NUMBER']
                if a in selected:
                    if a in dest: raise ValueError('DUPLICATE_BULK_METADATA')
                    dest[a] = r
        if 'OTHERMANAGER2.TSV' in members:
            for r in records('OTHERMANAGER2.TSV', {'ACCESSION_NUMBER','SEQUENCENUMBER','NAME'}):
                a = r['ACCESSION_NUMBER']
                if a in selected: manager_map[a].append({'sequence':r['SEQUENCENUMBER'],'name':r['NAME'], 'cik':r.get('CIK',''), 'form13f_file_number':r.get('FORM13FFILENUMBER','')})
        if selected:
            seen = set()
            for index, r in enumerate(records('INFOTABLE.TSV', {'ACCESSION_NUMBER','INFOTABLE_SK','NAMEOFISSUER','CUSIP','VALUE','SSHPRNAMT','SSHPRNAMTTYPE'})):
                if index % 100000 == 0: progress(stage='reading_zip', source_url=source_url, scanned_rows=index)
                a = r['ACCESSION_NUMBER']
                if a not in selected: continue
                key = (a, r['INFOTABLE_SK'])
                if key in seen: raise ValueError('DUPLICATE_BULK_ROW_ID')
                seen.add(key)
                d = {k.lower(): v for k,v in r.items()}
                for k in ('sole','shared','none'): d[k] = r.get('VOTING_AUTH_'+k.upper(), '0')
                holdings[a].append(position(d, multiplier(selected[a]['FILING_DATE'])))
                if len(holdings[a]) > MAX_ROWS: raise ValueError('TOO_MANY_ROWS')
        result = []
        for a, s in selected.items():
            if a not in covers or a not in summaries: raise ValueError('BULK_METADATA_JOIN_INCOMPLETE')
            cp = covers[a]; summary = summaries[a]; factor = multiplier(s['FILING_DATE'])
            period = day(s['PERIODOFREPORT']).isoformat()
            if period != day(cp['REPORTCALENDARORQUARTER']).isoformat(): raise ValueError('BULK_PERIOD_MISMATCH')
            snap = Snapshot(f"{int(s['CIK']):010d}",a,period,day(s['FILING_DATE']).isoformat(),s['SUBMISSIONTYPE'],
                cp['FILINGMANAGER_NAME'],cp.get('REPORTTYPE',''),cp.get('AMENDMENTTYPE','').upper(),
                number(cp.get('AMENDMENTNO') or '0',integral=True),number(summary['TABLEENTRYTOTAL'],integral=True),
                number(summary['TABLEVALUETOTAL'],integral=True)*factor,holdings[a],manager_map[a],
                [{'url':source_url,'sha256':archive_sha256,'format':'zip-tsv','accession':a}]).validate()
            result.append(snap)
        return result
