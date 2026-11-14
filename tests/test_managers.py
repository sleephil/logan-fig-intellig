"""Manager attribution and trend safeguards with explicitly synthetic inputs."""
from copy import deepcopy
from dataclasses import replace
import pytest
from ingest import Snapshot
from manager_reporting import manager_report, references

def manager(seq, name, cik='', file=''):
    return {'sequence': str(seq), 'name': name, 'cik': cik, 'form13f_file_number': file}

def snapshot(period='2026-06-30', values=None, managers=None, serial=1, **changes):
    values = values if values is not None else [('1',100,''),('2',200,'')]
    rows = [{'issuer':'TEST', 'cusip':str(i+1).zfill(9), 'title':'COM', 'value_usd':v,
             'other_manager':ref, 'put_call':opt, 'share_type':'SH'} for i,(ref,v,opt) in enumerate(values)]
    s=Snapshot('0001269119',f'0001085146-26-{serial:06d}',period,'2026-11-01','13F-HR',
      'Synthetic Filer','13F HOLDINGS REPORT','',0,len(rows),sum(r['value_usd'] for r in rows),rows,
      managers if managers is not None else [manager(1,'Alpha'),manager(2,'Beta')],[])
    return replace(s,**changes)

def item(result,name):return next(m for m in result['managers'] if m['name']==name)

def test_single_linked_reconciles_and_does_not_mutate():
    s=snapshot();before=deepcopy(s)
    r=manager_report([s]);assert s==before
    assert item(r,'Alpha')['history'][0]['reported_assets_usd']==100
    t=r['period_summaries'][0];assert t['reported_total_usd']==t['attributed_unique_usd']==300
    assert t['reconciled'] and not r['issues']

def test_single_shared_unassigned_unknown_and_option_buckets():
    r=manager_report([snapshot(values=[('1',100,''),('1,2',200,''),('',30,''),('9',40,''),('1',50,'Call')])])
    t=r['period_summaries'][0]
    assert [t[k] for k in ['single_linked_usd','jointly_linked_usd','unassigned_usd','unresolved_usd','options_reference_usd']]==[100,200,30,40,50]
    assert t['reported_total_usd']==420 and t['attributed_unique_usd']==300
    assert item(r,'Alpha')['history'][0]['reported_assets_usd']==300
    assert item(r,'Beta')['history'][0]['reported_assets_usd']==200
    assert not item(r,'Alpha')['history'][0]['comparisons_allowed']

@pytest.mark.parametrize('value,expected,bad',[('01, 2;2',['1','2'],False),('',[''],False),('N/A',[],False),('1 2',['1','2'],False),('1x2',[],True),('0',[],True)])
def test_sequence_parser(value,expected,bad):
    if value=='':expected=[]
    assert references(value)==(expected,bad)

def test_duplicate_refs_and_identity_aliases_count_one_row_once():
    r=manager_report([snapshot(values=[('1,2,1',100,'')],managers=[manager(1,'Alpha',cik='9'),manager(2,'Alpha LLC',cik='9')])])
    assert len(r['managers'])==1 and r['period_summaries'][0]['single_linked_usd']==100
    assert r['managers'][0]['history'][0]['reported_assets_usd']==100

def test_quarter_changes_do_not_join_by_reused_sequence():
    a=snapshot();b=snapshot('2026-09-30',values=[('1',180,''),('2',130,'')],managers=[manager(1,'Beta'),manager(2,'Alpha')],serial=2)
    r=manager_report([a,b]);assert item(r,'Alpha')['history'][-1]['change_usd']==30
    assert item(r,'Beta')['history'][-1]['change_usd']==-20
    assert item(r,'Alpha')['history'][-1]['change_pct']==30
    assert r['possible_pairs'][0]['decrease_usd']==20

def test_stable_id_joins_renamed_manager_and_later_cik():
    a=snapshot(values=[('1',100,'')],managers=[manager(1,'Old Name',file='028-12')])
    b=snapshot('2026-09-30',values=[('2',120,'')],managers=[manager(2,'New Name',cik='99',file='028-00012')],serial=2)
    r=manager_report([a,b]);assert len(r['managers'])==1
    assert r['managers'][0]['id']=='cik:0000000099' and r['managers'][0]['history'][-1]['change_usd']==20

def test_conflicting_ids_not_merged_or_attributed():
    r=manager_report([snapshot(managers=[manager(1,'A',cik='1',file='028-2'),manager(2,'B',cik='3',file='028-2')])])
    assert not r['managers'] and r['period_summaries'][0]['unresolved_usd']==300
    assert r['issues']

def test_ambiguous_same_name_without_id_not_assigned():
    s=snapshot(values=[('1',100,'')],managers=[manager(1,'Same'),manager(2,'Same',cik='1'),manager(3,'Same',cik='2')])
    r=manager_report([s]);assert r['period_summaries'][0]['unresolved_usd']==100


def test_missing_manager_is_not_zero_or_fake_outflow():
    a=snapshot();b=snapshot('2026-09-30',values=[('2',400,'')],managers=[manager(2,'Beta')],serial=2)
    r=manager_report([a,b]);h=item(r,'Alpha')['history'][-1]
    assert h['reported_assets_usd'] is None and h['change_usd'] is None and h['status']=='not_reported'
    assert not r['possible_pairs']

def test_gap_not_labelled_quarter_change():
    r=manager_report([snapshot('2026-03-31'),snapshot('2026-09-30',serial=2)])
    assert all(m['history'][-1]['change_usd'] is None for m in r['managers'])
    assert not r['possible_pairs']

def test_year_change_uses_calendar_year_not_four_array_entries():
    r=manager_report([snapshot('2025-09-30'),snapshot('2026-09-30',values=[('1',120,''),('2',190,'')],serial=2)])
    h=item(r,'Alpha')['history'][-1]
    assert h['year_change_usd']==20 and h['year_change_pct']==20 and h['change_usd'] is None

def test_zero_base_percent_is_not_infinity():
    r=manager_report([snapshot(values=[('1',0,'')]),snapshot('2026-09-30',values=[('1',100,'')],serial=2)])
    h=item(r,'Alpha')['history'][-1];assert h['change_usd']==100 and h['change_pct'] is None

def test_new_holdings_uses_amendment_local_manager_mapping():
    base=snapshot();amend=snapshot(values=[('1',50,'')],managers=[manager(1,'Beta')],serial=2,form='13F-HR/A',amendment_type='NEW HOLDINGS',amendment_no=1,filing_date='2026-11-02')
    r=manager_report([base,amend]);assert item(r,'Alpha')['history'][0]['reported_assets_usd']==100
    assert item(r,'Beta')['history'][0]['reported_assets_usd']==250
    assert r['period_summaries'][0]['reported_total_usd']==350

def test_restatement_replaces_rather_than_adds():
    base=snapshot();amend=snapshot(values=[('1',50,'')],serial=2,form='13F-HR/A',amendment_type='RESTATEMENT',amendment_no=1,filing_date='2026-11-02')
    r=manager_report([base,amend]);assert r['period_summaries'][0]['reported_total_usd']==50

def test_orphan_amendment_does_not_create_complete_report():
    r=manager_report([snapshot(form='13F-HR/A',amendment_type='NEW HOLDINGS',amendment_no=1)])
    assert not r['periods'] and r['issues'][0]['code']=='AMENDMENT_BASE_MISSING'

def test_shared_positions_excluded_from_pairs():
    a=snapshot(values=[('1,2',100,''),('3',100,'')],managers=[manager(1,'A'),manager(2,'B'),manager(3,'C')])
    b=snapshot('2026-09-30',values=[('1,2',80,''),('3',120,'')],managers=a.managers,serial=2)
    assert not manager_report([a,b])['possible_pairs']

def test_pairs_are_same_quarter_similar_opposite_changes():
    a=snapshot();b=snapshot('2026-09-30',values=[('1',50,''),('2',249,'')],serial=2)
    r=manager_report([a,b]);p=r['possible_pairs'][0]
    assert p['declining']=='Alpha' and p['increasing']=='Beta' and p['magnitude_similarity']==.98
    assert p['previous_period']=='2026-06-30' and p['period']=='2026-09-30'
    assert 'not verified' in p['interpretation']

def test_duplicate_accession_not_counted_twice():
    s=snapshot();assert manager_report([s,s])['period_summaries'][0]['reported_total_usd']==300

def test_mixed_filer_fails_closed():
    with pytest.raises(ValueError,match='MIXED_FILER'):manager_report([snapshot(),snapshot(serial=2,cik='0000000001')])

def test_conflicting_same_accession_revision_fails_closed():
    with pytest.raises(ValueError,match='CONFLICTING_ACCESSION'):manager_report([snapshot(),snapshot(values=[('1',90,'')])])

def test_partial_row_link_never_misrepresented_as_single():
    r=manager_report([snapshot(values=[('1,99',100,'')])]);assert r['period_summaries'][0]['unresolved_usd']==100
    assert item(r,'Alpha')['history'][0]['reported_assets_usd']==0

def test_duplicate_sequence_is_ambiguous():
    r=manager_report([snapshot(managers=[manager(1,'A'),manager(1,'B')])]);assert r['period_summaries'][0]['unresolved_usd']==300

def test_empty_report_is_valid_empty_not_fake_data():
    r=manager_report([]);assert not r['managers'] and not r['periods'] and not r['possible_pairs']

def test_manager_api_permission_and_schema(monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from app import app
    import storage
    monkeypatch.setenv('FIG_API_TOKEN','synthetic-test-token')
    monkeypatch.setattr(storage,'connect',lambda:nullcontext(object()))
    monkeypatch.setattr(storage,'latest',lambda c,cik:[snapshot()])
    import market_store
    monkeypatch.setattr(market_store,'forward',lambda c,cik:None)
    import filer_directory
    monkeypatch.setattr(filer_directory,'get',lambda c,code:{'cik':code,'name':'Synthetic reporting entity'})
    c=TestClient(app)
    assert c.get('/api/managers/0001269119').status_code==403
    h={'X-FIG-Token':'synthetic-test-token'}
    assert c.get('/api/managers/invalid',headers=h).status_code==400
    r=c.get('/api/managers/0001269119',headers=h)
    assert r.status_code==200 and len(r.json()['managers'])==2 and 'positions' not in r.json()
    assert r.json()['version']=='0.5.1'
