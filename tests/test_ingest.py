import csv,io,json,zipfile
from datetime import date
from pathlib import Path
import pytest
import ingest
from ingest import Snapshot,parse_xml,parse_bulk,SECClient

ENTRY={'accessionNumber':'0001085146-26-000917','form':'13F-HR','reportDate':'2026-09-30','filingDate':'2026-10-27','primaryDocument':'xslForm13F_X02/primary_doc.xml'}
COVER='''<edgarSubmission><headerData><submissionType>13F-HR</submissionType><filerInfo><cik>0001269119</cik></filerInfo></headerData><formData><coverPage><reportCalendarOrQuarter>09-30-2026</reportCalendarOrQuarter><filingManager><name>Test Manager</name></filingManager><reportType>13F HOLDINGS REPORT</reportType></coverPage><summaryPage><tableEntryTotal>2</tableEntryTotal><tableValueTotal>300</tableValueTotal></summaryPage></formData></edgarSubmission>'''
TABLE='''<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable"><infoTable><nameOfIssuer>ALPHA</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>000000001</cusip><value>100</value><shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable><infoTable><nameOfIssuer>BETA</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>000000002</cusip><value>200</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><votingAuthority><Sole>20</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>'''
def snapshot():return parse_xml('1269119',ENTRY,COVER.encode(),[TABLE.encode()],[])

def test_usd_exact_and_raw_retained():
    s=snapshot();assert s.expected_total==300 and len(s.rows)==2
    assert s.rows[0]['raw_value']==100 and s.rows[0]['value_multiplier']==1

def test_legacy_units_follow_filing_date_not_period():
    e={**ENTRY,'filingDate':'2022-11-01','reportDate':'2022-09-30'}
    s=parse_xml('1269119',e,COVER.replace('09-30-2026','09-30-2022').encode(),[TABLE.encode()],[])
    assert s.expected_total==300000
    e={**ENTRY,'filingDate':'2023-01-03','reportDate':'2022-09-30'}
    assert parse_xml('1269119',e,COVER.replace('09-30-2026','09-30-2022').encode(),[TABLE.encode()],[]).expected_total==300

@pytest.mark.parametrize('old,new,code',[
    ('<tableEntryTotal>2','<tableEntryTotal>3','TABLE_ENTRY_COUNT_MISMATCH'),
    ('<tableValueTotal>300','<tableValueTotal>301','TABLE_VALUE_TOTAL_MISMATCH'),
    ('0001269119','0001269120','FILER_CIK_MISMATCH'),
    ('09-30-2026','06-30-2026','FILING_PERIOD_MISMATCH'),
    ('13F-HR','13F-HR/A','FILING_FORM_MISMATCH')])
def test_reconciliation_blocks_bad_cover(old,new,code):
    with pytest.raises(ValueError,match=code):parse_xml('1269119',ENTRY,COVER.replace(old,new).encode(),[TABLE.encode()],[])

@pytest.mark.parametrize('old,new', [('000000001',''),('<value>100','<value>-1'),('<sshPrnamt>10','<sshPrnamt>NaN'),('<sshPrnamtType>SH','<sshPrnamtType>BAD')])
def test_invalid_rows_block_whole_filing(old,new):
    with pytest.raises(ValueError):parse_xml('1269119',ENTRY,COVER.encode(),[TABLE.replace(old,new).encode()],[])

def test_xml_entities_forbidden():
    with pytest.raises(Exception):parse_xml('1269119',ENTRY,b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><edgarSubmission>&e;</edgarSubmission>',[],[])

@pytest.mark.parametrize('url',['http://www.sec.gov/x','https://evil.example/x','https://data.sec.gov@evil.example/x','https://www.sec.gov:123/x','https://www.sec.gov/../secret'])
def test_only_official_urls(url):
    with pytest.raises(ValueError):ingest.safe_url(url)

def make_zip(extra=None,missing=None,duplicate=False):
    a=ENTRY['accessionNumber']
    data={
      'SUBMISSION.tsv':[{'ACCESSION_NUMBER':a,'FILING_DATE':'27-OCT-2026','SUBMISSIONTYPE':'13F-HR','CIK':'0001269119','PERIODOFREPORT':'30-SEP-2026'}],
      'COVERPAGE.tsv':[{'ACCESSION_NUMBER':a,'REPORTCALENDARORQUARTER':'30-SEP-2026','FILINGMANAGER_NAME':'Test Manager','REPORTTYPE':'13F HOLDINGS REPORT','AMENDMENTTYPE':'','AMENDMENTNO':''}],
      'SUMMARYPAGE.tsv':[{'ACCESSION_NUMBER':a,'TABLEENTRYTOTAL':'2','TABLEVALUETOTAL':'300'}],
      'INFOTABLE.tsv':[{'ACCESSION_NUMBER':a,'INFOTABLE_SK':str(i),'NAMEOFISSUER':name,'TITLEOFCLASS':'COM','CUSIP':c,'VALUE':str(v),'SSHPRNAMT':str(v//10),'SSHPRNAMTTYPE':'SH','VOTING_AUTH_SOLE':str(v//10)} for i,(name,c,v) in enumerate([('ALPHA','000000001',100),('BETA','000000002',200)])]}
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for n,rows in data.items():
            if n==missing:continue
            s=io.StringIO();w=csv.DictWriter(s,fieldnames=rows[0].keys(),delimiter='\t');w.writeheader();w.writerows(rows)
            z.writestr(n,s.getvalue())
        if extra:z.writestr(*extra)
        if duplicate:z.writestr('submission.TSV','x')
    b.seek(0);return b
URL='https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip'
def test_bulk_join_and_units():
    ss=parse_bulk(make_zip(),URL,'a'*64,['1269119'],'2023-09-30')
    assert len(ss)==1 and ss[0].expected_total==300 and len(ss[0].rows)==2
    assert ss[0].rows[0]['cusip']=='000000001'

def test_bulk_filters_unknown_filers():assert parse_bulk(make_zip(),URL,'a'*64,['999999'],'2023-09-30')==[]

@pytest.mark.parametrize('kwargs',[{'extra':('../escape','x')},{'extra':('/abs','x')},{'extra':('nested/path','x')},{'missing':'SUMMARYPAGE.tsv'},{'duplicate':True}])
def test_unsafe_incomplete_zip_rejected(kwargs):
    with pytest.raises(ValueError):parse_bulk(make_zip(**kwargs),URL,'a'*64,['1269119'],'2023-09-30')

def test_compression_bomb_rejected():
    with pytest.raises(ValueError,match='ZIP_EXPANSION_LIMIT'):parse_bulk(make_zip(extra=('bomb','0'*10000000)),URL,'a'*64,['1269119'],'2023-09-30')

@pytest.mark.parametrize('name,start,end',[
 ('2023q4_form13f.zip','2023-10-01','2023-12-31'),
 ('01jun2026-31aug2026_form13f.zip','2026-06-01','2026-08-31')])
def test_archive_dates(name,start,end):assert ingest.archive_range('https://www.sec.gov/'+name)==(date.fromisoformat(start),date.fromisoformat(end))

class Response:
    def __init__(self,status,data=b'{}'):self.status_code=status;self.headers={};self.data=data
    def close(self):pass
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError('HTTP_ERROR')
    def iter_content(self,size):yield self.data
class Session:
    def __init__(self,statuses):self.statuses=list(statuses);self.calls=[]
    def get(self,url,**kw):self.calls.append(kw);return Response(self.statuses.pop(0))
@pytest.fixture
def contact(monkeypatch):
    monkeypatch.setenv('SEC_USER_AGENT','Synthetic test contact@example.com');monkeypatch.setattr(ingest.time,'sleep',lambda n:None)

def test_request_identification_and_retry(contact):
    sess=Session([429,500,200]);c=SECClient(session=sess);assert c.json('https://data.sec.gov/x')=={}
    assert len(sess.calls)==3 and sess.calls[0]['headers']['User-Agent'].endswith('contact@example.com')
    assert sess.calls[0]['allow_redirects'] is False

def test_does_not_evade_denied_access(contact):
    sess=Session([403,200]);c=SECClient(session=sess)
    with pytest.raises(RuntimeError,match='SEC_ACCESS_DENIED_403'):c.get('https://data.sec.gov/x')
    assert len(sess.calls)==1

def test_no_redirect_to_unapproved_host(contact):
    with pytest.raises(RuntimeError,match='SEC_UNEXPECTED_REDIRECT'):SECClient(session=Session([302])).get('https://data.sec.gov/x')

def test_missing_contact_rejected(monkeypatch):
    monkeypatch.delenv('SEC_USER_AGENT',raising=False)
    with pytest.raises(RuntimeError,match='SEC_USER_AGENT_MISSING'):SECClient()

def test_analysis_has_no_second_thousand_multiplier():
    from analysis import analyze
    r=analyze([('2026-09-30','ALPHA','000000001','COM',100,'','SH'),('2026-09-30','ALPHA','000000001','COM',50,'Call','SH')])
    assert len(r['positions'])==2 and sum(x['latest_value_usd'] for x in r['positions'])==150

def test_amendments_add_or_replace():
    from app import effective_periods
    base=snapshot();a=snapshot();a.form='13F-HR/A';a.amendment_type='NEW HOLDINGS';a.amendment_no=1;a.filing_date='2026-10-28';a.accession='0001085146-26-000918'
    result,issues=effective_periods([base,a]);assert len(result[base.period]['rows'])==4 and not issues
    a.amendment_type='RESTATEMENT';result,issues=effective_periods([base,a]);assert len(result[base.period]['rows'])==2
    a.amendment_type='NEW HOLDINGS';result,issues=effective_periods([a]);assert not result and issues

def test_manual_configuration_error_not_false_queue(monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('FIG_API_TOKEN','synthetic-token');monkeypatch.delenv('SEC_USER_AGENT',raising=False)
    r=TestClient(app).post('/api/sync',headers={'X-FIG-Token':'synthetic-token'})
    assert r.status_code==503 and r.json()['detail']=='SEC_USER_AGENT_MISSING'

def test_version_and_private_apis_stay_protected(monkeypatch):
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv('FIG_API_TOKEN','synthetic-token');c=TestClient(app)
    for url in ['/api/status','/api/version','/api/platforms','/api/filings/0001269119','/api/analysis/0001269119','/api/managers/0001269119']:
        assert c.get(url).status_code==403
    assert c.post('/api/sync').status_code==403
    result=c.get('/api/version',headers={'X-FIG-Token':'synthetic-token'}).json()
    assert result['version']=='0.5.1' and result['changes'][0]['version']==result['version']
    assert '/fig-intelligence' in result['changes'][0]['changed_pages']
