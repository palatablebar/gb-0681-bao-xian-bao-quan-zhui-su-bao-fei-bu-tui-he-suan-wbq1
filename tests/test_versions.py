from copy import deepcopy
import pytest
from app.workflow import run,load
from app.modules.m3_versions.rules import apply,adjudication_input
from app.errors import DomainError


def _states(rows):
    state=run(load('dirty.csv'),{},2);state['rows']=deepcopy(rows);return state


def test_real_policy_count(raw):assert len(run(raw,{},3)['policies'])==12
def test_real_cancel_reference(raw):
    p=run(raw,{},3)['policies'][0];assert p['cancelled']==['00002'] and p['changes']==[]
def test_real_change_order(raw):
    p=run(raw,{},3)['policies'][1];assert [r['effective'] for r in p['changes']]==['2026-07-01','2026-10-01']
def test_order_independent(raw):
    s=run(raw,{},2);a=apply(deepcopy(s));s['rows'].reverse();b=apply(s)
    assert {p['id']:[x['id'] for x in p['changes']] for p in a['policies']}=={p['id']:[x['id'] for x in p['changes']] for p in b['policies']}
def test_bad_cancel_isolated(raw):
    s=run(raw,{},2);s['rows'][2]['ref']='absent';r=apply(s);assert any(x['code']=='REFERENCE' for x in r['quarantine'])
def test_missing_issue_isolated(raw):
    s=run(raw,{},2);s['rows']=[r for r in s['rows'] if r['kind']!='issue'];r=apply(s);assert r['policies']==[]
def test_effective_outside_rejected(raw):
    s=run(raw,{},2);s['rows'][1]['effective']='2027-01-01';r=apply(s);assert any(q['code']=='RANGE' for q in r['quarantine'])


def _dependent_rows():
    """P001 issue + 7月加保 + 撤销加保 + 10月继续加保 (same cover)."""
    return [
        {'id':'01','policy_id':'P1','version':'1','kind':'issue','effective':'2026-01-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'3650','paid':'3650','ref':'','cover':'基础险','note':'','_line':2,'_raw':{}},
        {'id':'02','policy_id':'P1','version':'2','kind':'change','effective':'2026-07-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'7300','paid':'0','ref':'','cover':'基础险','note':'','_line':3,'_raw':{}},
        {'id':'03','policy_id':'P1','version':'3','kind':'cancel','effective':'2026-08-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'0','paid':'0','ref':'02','cover':'基础险','note':'','_line':4,'_raw':{}},
        {'id':'04','policy_id':'P1','version':'4','kind':'change','effective':'2026-10-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'5475','paid':'0','ref':'','cover':'基础险','note':'','_line':5,'_raw':{}},
    ]


def test_successor_is_pending_by_default():
    state=_states(_dependent_rows());p=apply(state)['policies'][0]
    assert p['pending']==['04'] and p['changes']==[] and p['blocked'] is True
    assert [a['status'] for a in p['amendments']]==['CANCELLED','PENDING']
    assert p['amendments'][1]['upstream']==['02']


def test_reverify_keeps_successor_in_chain():
    state=_states(_dependent_rows());state['parameters']={'adjudication':{'04':'reverify'}}
    p=apply(state)['policies'][0]
    assert p['pending']==[] and [c['id'] for c in p['changes']]==['04']
    assert p['reverified']==['04'] and p['publishable'] is True


def test_void_removes_successor_from_chain_but_keeps_record():
    state=_states(_dependent_rows());state['parameters']={'adjudication':{'04':'void'}}
    p=apply(state)['policies'][0]
    assert p['changes']==[] and p['voided']==['04'] and p['publishable'] is True
    assert any(a['id']=='04' and a['status']=='VOID' for a in p['amendments'])


def test_two_choices_differ_in_range_and_amount():
    from app.modules.m4_segments.rules import cut
    from app.modules.m5_calculate.rules import apply as calc

    def amounts(parameters):
        s=run(load('dirty.csv'),{},2);s['rows']=_dependent_rows();s['parameters']=parameters
        s=apply(s);s['segments']=[x for p in s['policies'] for x in cut(p)]
        s=calc(s);return [x['days'] for x in s['segments']],[c['expected'] for c in s['calculations']]

    days_re,exp_re=amounts({'adjudication':{'04':'reverify'}})
    days_void,exp_void=amounts({'adjudication':{'04':'void'}})
    assert days_re!=days_void and exp_re!=exp_void
    assert exp_re==['4110.00'] and exp_void==['3650.00']


def test_dependent_successor_with_explicit_ref_is_linked_even_if_earlier():
    rows=_dependent_rows();rows[3]['effective']='2026-06-01';rows[3]['ref']='02'
    state=_states(rows);p=apply(state)['policies'][0]
    assert p['pending']==['04']


def test_different_cover_is_not_dependent():
    rows=_dependent_rows();rows[3]['cover']='附加险'
    state=_states(rows);p=apply(state)['policies'][0]
    assert p['pending']==[] and [c['id'] for c in p['changes']]==['04']


def test_superseded_kept_as_evidence():
    rows=_dependent_rows();rows[2]={'id':'03','policy_id':'P1','version':'9','kind':'change','effective':'2026-07-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'8100','paid':'0','ref':'','cover':'基础险','note':'','_line':4,'_raw':{}}
    state=_states(rows);p=apply(state)['policies'][0]
    statuses={a['id']:a['status'] for a in p['amendments']}
    assert statuses['02']=='SUPERSEDED' and statuses['03']=='ACTIVE'
    assert [c['id'] for c in p['changes']]==['03', '04']


def test_adjudication_input_shapes():
    assert adjudication_input({'adjudication':{'default':'void'}})==({},'VOID')
    assert adjudication_input({'adjudication':{'decisions':{'04':'reverify'},'default':'void'}})==({'04':'REVERIFY'},'VOID')
    assert adjudication_input({'adjudication':{'04':'void'}})==({'04':'VOID'},None)
    with pytest.raises(DomainError) as e:adjudication_input({'adjudication':{'04':'maybe'}})
    assert e.value.code=='ADJUDICATION'
