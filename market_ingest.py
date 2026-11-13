"""Bounded SEC-wide import of compact manager-attribution summaries.

Read each security once, retain only accession-local manager buckets and provenance.
These summaries are never exposed as a security-level portfolio.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from collections import defaultdict
from pathlib import PurePosixPath
import csv, hashlib, io, json, re, sqlite3, stat, tempfile, zipfile
from ingest import (Snapshot, SECClient, day, number, multiplier, position, safe_url,
                    ACC, ET, local, text, child, parse_xml)

SUMMARY_VERSION = 'market-summary-1'
MAX_BULK_BYTES = 512_000_000
MAX_BULK_EXPANDED = 6_000_000_000

@dataclass
class SummarySnapshot(Snapshot):
    summary_version: str = SUMMARY_VERSION

    def validate(self):
        if self.summary_version != SUMMARY_VERSION: raise ValueError('SUMMARY_VERSION')
        if not ACC.fullmatch(self.accession): raise ValueError('INVALID_ACCESSION')
        if not re.fullmatch(r'[0-9]{10}', self.cik) or int(self.cik) == 0: raise ValueError('INVALID_CIK')
        if self.form not in ('13F-HR', '13F-HR/A'): raise ValueError('UNSUPPORTED_FORM')
        if not self.manager or day(self.period) > day(self.filing_date): raise ValueError('INVALID_FILING_METADATA')
        if self.form.endswith('/A') and self.amendment_type not in ('RESTATEMENT', 'NEW HOLDINGS'):
            raise ValueError('UNSUPPORTED_AMENDMENT_TYPE')
        if not 0 <= self.expected_count <= 1_000_000: raise ValueError('ROW_COUNT_LIMIT')
        if sum(number(r['row_count'], integral=True) for r in self.rows) != self.expected_count:
            raise ValueError('TABLE_ENTRY_COUNT_MISMATCH')
        if sum(number(r['value_usd'], integral=True) for r in self.rows) != self.expected_total:
            raise ValueError('TABLE_VALUE_TOTAL_MISMATCH')
        if any(r['put_call'] not in ('', 'Put', 'Call') for r in self.rows): raise ValueError('INVALID_OPTION')
        return self

    def canonical(self):
        return {k: v for k, v in asdict(self).items() if k != 'sources'}


def compact(s: Snapshot) -> SummarySnapshot:
    s.validate(); groups = {}
    for row in s.rows:
        k = (row.get('other_manager', ''), row['put_call'])
        b = groups.setdefault(k, dict(other_manager=k[0], put_call=k[1], value_usd=0, row_count=0))
        b['value_usd'] += row['value_usd']; b['row_count'] += 1
    data = asdict(s); data['rows'] = [groups[k] for k in sorted(groups)]
    return SummarySnapshot(**data).validate()


def bulk_summaries(stream, url, digest, start, progress=None):
    """Return reconciled summaries and individual rejected accessions; no extraction."""
    safe_url(url); start = day(start); progress = progress or (lambda **kw: None)
    if not re.fullmatch(r'[a-f0-9]{64}', digest): raise ValueError('INVALID_SOURCE_HASH')
    with zipfile.ZipFile(stream) as z, tempfile.TemporaryDirectory(prefix='fig-row-check-') as temp:
        members = {}; expanded = 0
        if len(z.infolist()) > 64: raise ValueError('ZIP_TOO_MANY_MEMBERS')
        for item in z.infolist():
            p = PurePosixPath(item.filename); expanded += item.file_size
            if (len(p.parts) != 1 or p.is_absolute() or '..' in p.parts or '\\' in item.filename
                or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1): raise ValueError('UNSAFE_ZIP_MEMBER')
            if expanded > MAX_BULK_EXPANDED or item.file_size / max(item.compress_size, 1) > 500:
                raise ValueError('ZIP_EXPANSION_LIMIT')
            key = item.filename.upper()
            if key in members: raise ValueError('DUPLICATE_ZIP_MEMBER')
            members[key] = item.filename
        required = {'SUBMISSION.TSV','COVERPAGE.TSV','SUMMARYPAGE.TSV','INFOTABLE.TSV'}
        if not required <= set(members): raise ValueError('ZIP_TABLES_MISSING')
        def records(table, fields):
            with z.open(members[table]) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding='utf-8-sig'), delimiter='\t')
                if not set(fields) <= set(reader.fieldnames or []): raise ValueError('BULK_FIELDS_MISSING')
                for r in reader:
                    if None in r: raise ValueError('MALFORMED_TSV_ROW')
                    yield r
        selected = {}; bad = {}; covers = {}; summaries = {}; managers = defaultdict(list)
        for r in records('SUBMISSION.TSV', ['ACCESSION_NUMBER','CIK','SUBMISSIONTYPE','FILING_DATE','PERIODOFREPORT']):
            if r['SUBMISSIONTYPE'] not in ('13F-HR','13F-HR/A'): continue
            if day(r['PERIODOFREPORT']) < start: continue
            acc = r['ACCESSION_NUMBER']
            if acc in selected or not ACC.fullmatch(acc): raise ValueError('DUPLICATE_OR_INVALID_ACCESSION')
            selected[acc] = r
            if len(selected) > 100_000: raise ValueError('BULK_FILING_LIMIT')
        for table, dest in [('COVERPAGE.TSV',covers), ('SUMMARYPAGE.TSV',summaries)]:
            for r in records(table, ['ACCESSION_NUMBER']):
                a = r['ACCESSION_NUMBER']
                if a not in selected: continue
                if a in dest: bad[a] = 'DUPLICATE_BULK_METADATA'
                dest[a] = r
        if 'OTHERMANAGER2.TSV' in members:
            for r in records('OTHERMANAGER2.TSV', ['ACCESSION_NUMBER','SEQUENCENUMBER','NAME']):
                a = r['ACCESSION_NUMBER']
                if a in selected:
                    managers[a].append(dict(sequence=r['SEQUENCENUMBER'], name=r['NAME'], cik=r.get('CIK',''),
                                            form13f_file_number=r.get('FORM13FFILENUMBER','')))
        # Disk-backed duplicate-ID validation bounds memory even for market-wide archives.
        db = sqlite3.connect(temp+'/ids.sqlite'); db.execute('PRAGMA journal_mode=OFF'); db.execute('PRAGMA synchronous=OFF')
        db.execute('CREATE TABLE ids(acc TEXT, sk TEXT, PRIMARY KEY(acc,sk)) WITHOUT ROWID')
        groups = defaultdict(dict); counts = defaultdict(int); factors = {a:multiplier(s['FILING_DATE']) for a,s in selected.items()}
        try:
            for i,r in enumerate(records('INFOTABLE.TSV', ['ACCESSION_NUMBER','INFOTABLE_SK','NAMEOFISSUER','CUSIP','VALUE','SSHPRNAMT','SSHPRNAMTTYPE'])):
                if i % 100_000 == 0: progress(stage='reading_market_zip', source_url=url, scanned_rows=i)
                a = r['ACCESSION_NUMBER']
                if a not in selected or a in bad: continue
                try:
                    db.execute('INSERT INTO ids VALUES(?,?)',(a,r['INFOTABLE_SK']))
                    row = position({**{k.lower():v for k,v in r.items()},
                                    **{k:r.get('VOTING_AUTH_'+k.upper(),'0') for k in ('sole','shared','none')}}, factors[a])
                    if not row['issuer'] or not re.fullmatch(r'[A-Z0-9*@#]{9}', row['cusip']): raise ValueError('INVALID_SECURITY_IDENTIFIER')
                    if row['share_type'] not in ('SH','PRN') or row['put_call'] not in ('','Put','Call'): raise ValueError('INVALID_SECURITY_TYPE')
                    k = (row['other_manager'],row['put_call'])
                    b = groups[a].setdefault(k, dict(other_manager=k[0], put_call=k[1], value_usd=0, row_count=0))
                    b['value_usd'] += row['value_usd']; b['row_count'] += 1; counts[a] += 1
                    if counts[a] > 1_000_000: raise ValueError('FILING_ROW_LIMIT')
                except (ValueError, sqlite3.IntegrityError) as exc:
                    bad[a] = 'DUPLICATE_BULK_ROW_ID' if isinstance(exc,sqlite3.IntegrityError) else str(exc)[:100]
            result = []
            for a,s in selected.items():
                if a in bad: continue
                try:
                    cp = covers[a]; sm = summaries[a]; period = day(s['PERIODOFREPORT']).isoformat()
                    if period != day(cp['REPORTCALENDARORQUARTER']).isoformat(): raise ValueError('BULK_PERIOD_MISMATCH')
                    ss = SummarySnapshot(str(int(s['CIK'])).zfill(10), a, period, day(s['FILING_DATE']).isoformat(),
                        s['SUBMISSIONTYPE'], cp['FILINGMANAGER_NAME'], cp.get('REPORTTYPE',''), cp.get('AMENDMENTTYPE','').upper(),
                        number(cp.get('AMENDMENTNO') or 0,integral=True), number(sm['TABLEENTRYTOTAL'],integral=True),
                        number(sm['TABLEVALUETOTAL'],integral=True)*factors[a],
                        [groups[a][k] for k in sorted(groups[a])], managers[a],
                        [dict(url=url, sha256=digest, format='zip-manager-summary', accession=a)]).validate()
                    result.append(ss)
                except (ValueError, KeyError) as exc: bad[a] = str(exc)[:100]
        finally: db.close()
        rejected = [dict(accession=a,cik=str(int(selected[a]['CIK'])).zfill(10),
                        period=day(selected[a]['PERIODOFREPORT']).isoformat(),code=code) for a,code in bad.items()]
        return result,rejected


def index_filings(stream, year, quarter, after):
    """All holdings accessions after the bulk watermark, not a per-filer whitelist."""
    import filer_directory as directory
    stream.seek(0); directory.parse_index(stream,year,quarter)  # Reuse all archive/path/header checks.
    stream.seek(0); result=[]
    with zipfile.ZipFile(stream) as z:
        name=next(n for n in z.namelist() if n.lower()=='master.idx')
        with z.open(name) as raw:
            header=False
            for line in io.TextIOWrapper(raw,encoding='latin-1'):
                line=line.strip()
                if line=='CIK|Company Name|Form Type|Date Filed|Filename':header=True;continue
                if not header or not line or set(line)=={'-'}:continue
                c,rest=line.split('|',1);name,form,filed,path=rest.rsplit('|',3)
                if form not in ('13F-HR','13F-HR/A') or day(filed)<=day(after):continue
                acc=PurePosixPath(path).stem
                if not ACC.fullmatch(acc):raise ValueError('INVALID_ACCESSION')
                result.append(dict(cik=directory.cik(c), name=name, accession=acc, form=form, filing_date=filed))
    return result


def indexed_filing(entry, client):
    """Load a newly indexed accession without a redundant submissions lookup."""
    cik=entry['cik'];acc=entry['accession'];safe_url('https://www.sec.gov/')
    if not re.fullmatch(r'[0-9]{10}',cik) or not ACC.fullmatch(acc):raise ValueError('INVALID_ACCESSION')
    base=f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace("-", "")}/'
    data=client.json(base+'index.json');cover=None;tables=[];sources=[];primary=''
    for name in sorted({r['name'] for r in data['directory']['item'] if r['name'].lower().endswith('.xml')}):
        if '/' in name or '\\' in name or name in ('.','..'):raise ValueError('UNSAFE_DOCUMENT_NAME')
        body,digest=client.get(base+name);root=ET.fromstring(body)
        kind=local(root.tag)
        if kind=='edgarsubmission':
            if cover is not None:raise ValueError('DUPLICATE_COVER')
            cover=body;primary=name;period=day(text(root,'reportCalendarOrQuarter')).isoformat()
        elif kind=='informationtable':tables.append(body)
        else:continue
        sources.append(dict(url=base+name,sha256=digest,format='xml'))
    if cover is None or not tables:raise ValueError('MISSING_INFORMATION_TABLE')
    e=dict(accessionNumber=acc,form=entry['form'],reportDate=period,filingDate=entry['filing_date'],primaryDocument=primary)
    return parse_xml(cik,e,cover,tables,sources)
