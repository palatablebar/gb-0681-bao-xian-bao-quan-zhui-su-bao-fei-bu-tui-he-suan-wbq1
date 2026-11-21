import csv, io
from app.modules.m7_archive.export_out import checksum
from app.workflow import load, run

REVERIFY = {'adjudication': {'default': 'reverify'}}
VOID = {'adjudication': {'default': 'void'}}


def test_succession_sample_starts_pending():
    state = run(load('succession.csv'), {})
    assert state['adjudication']['status'] == 'PENDING'
    assert state['adjudication']['pending_ids'] == ['00004']
    assert state['adjudication']['complete'] is False
    policy = state['policies'][0]
    assert policy['changes'] == []
    assert policy['pending'] == ['00004'] and policy['blocked'] is True


def test_successor_has_explicit_status_after_resolution():
    for parameters, expected in [(REVERIFY, 'REVERIFIED'), (VOID, 'VOID')]:
        state = run(load('succession.csv'), parameters)
        statuses = {a['id']: a['status'] for p in state['policies'] for a in p['amendments']}
        assert statuses['00002'] == 'CANCELLED'
        assert statuses['00004'] == expected
        assert state['adjudication']['status'] == 'RESOLVED'
        assert all(a['status'] != 'PENDING' for p in state['policies'] for a in p['amendments'])


def test_void_keeps_record_and_cancel_link():
    state = run(load('succession.csv'), VOID)
    policy = state['policies'][0]
    voided = [a for a in policy['amendments'] if a['status'] == 'VOID'][0]
    assert voided['upstream'] == ['00002']
    assert voided['id'] == '00004' and voided['line'] == 5
    assert policy['cancellations'][0]['target'] == '00002'


def test_two_choices_differ_in_amount_and_range():
    reverified = run(load('succession.csv'), REVERIFY)
    voided = run(load('succession.csv'), VOID)
    calc_re = {c['id']: c for c in reverified['calculations']}
    calc_void = {c['id']: c for c in voided['calculations']}
    assert calc_re['P101']['expected'] == '4110.00'
    assert calc_void['P101']['expected'] == '3650.00'
    seg_re = [s for s in reverified['segments'] if s['id'] == 'P101']
    seg_void = [s for s in voided['segments'] if s['id'] == 'P101']
    assert len(seg_re) != len(seg_void)


def test_pending_state_cannot_publish(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    r = client.post('/api/runs/' + s['run_id'] + '/publish', json={'version': 0})
    assert r.status_code == 409 and r.get_json()['code'] == 'ADJUDICATION_PENDING'


def test_publish_then_download_one_chain(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    assert client.get('/api/archive/' + rid + '/export.csv').status_code == 409
    p = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY})
    assert p.status_code == 200
    body = p.get_json()
    assert body['published_version'] == 1
    export = client.get('/api/archive/' + rid + '/export.csv')
    assert export.status_code == 200
    text = export.data.decode('utf-8-sig')
    types = [row[0] for row in csv.reader(io.StringIO(text))]
    assert '批改链' in types and '撤销链' in types and '裁决' in types
    assert '00004' in text and 'REVERIFIED' in text and 'CANCELLED' in text


def test_publish_is_idempotent(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    first = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY}).get_json()
    second = client.post('/api/runs/' + rid + '/publish', json={'version': first['published_version'], **REVERIFY}).get_json()
    assert second['published_version'] == first['published_version'] == 1
    assert client.get('/api/runs').get_json()[0]['publish_version'] == 1


def test_publish_failure_keeps_previous_chain(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    first = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY}).get_json()
    bad = client.post('/api/runs/' + rid + '/publish', json={'version': first['published_version'], 'adjudication': {'00004': 'nonsense'}})
    assert bad.status_code == 422
    chain = client.get('/api/runs/' + rid + '/chain').get_json()
    assert chain['published_version'] == 1
    assert {a['status'] for p in chain['policies'] for a in p['amendments']} == {'CANCELLED', 'REVERIFIED'}


def test_stale_publish_rejected(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    first = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY}).get_json()
    client.post('/api/runs/' + rid + '/publish', json={'version': first['published_version'], **VOID})
    stale = client.post('/api/runs/' + rid + '/publish', json={'version': first['published_version'], **REVERIFY})
    assert stale.status_code == 409 and stale.get_json()['code'] == 'STALE'


def test_switch_decision_on_republish_changes_chain_and_amounts(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    first = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY}).get_json()
    assert first['calculations'][0]['expected'] == '4110.00'
    second = client.post('/api/runs/' + rid + '/publish', json={'version': first['published_version'], **VOID}).get_json()
    assert second['published_version'] == 2
    assert second['calculations'][0]['expected'] == '3650.00'
    page = client.get('/m3_versions').data.decode()
    assert '随上游失效' in page
    kpi = client.get('/api/m3_versions').get_json()
    assert kpi['published_version'] == 2 and kpi['calculations'][0]['expected'] == '3650.00'


def test_export_checksum_matches_published(client):
    s = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    rid = s['run_id']
    body = client.post('/api/runs/' + rid + '/publish', json={'version': 0, **REVERIFY}).get_json()
    published = client.get('/api/runs/' + rid + '/chain').get_json()
    assert published['archive']['sha256'] == checksum(published)
    assert body['archive']['sha256'] == published['archive']['sha256']


def test_same_source_rerun_is_idempotent(client):
    before = len(client.get('/api/runs').get_json())
    a = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    b = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    assert a['run_id'] == b['run_id'] and len(client.get('/api/runs').get_json()) == before + 1
    client.post('/api/runs/' + a['run_id'] + '/publish', json={'version': 0, **REVERIFY})
    c = client.post('/api/runs', json={'sample': 'succession.csv'}).get_json()
    assert c['run_id'] == a['run_id'] and len(client.get('/api/runs').get_json()) == before + 1
    assert client.get('/api/runs/' + a['run_id'] + '/chain').get_json()['published_version'] == 1


def test_same_source_rerun_adds_no_money(client):
    a = run(load('succession.csv'), REVERIFY)
    b = run(load('succession.csv'), REVERIFY)
    assert a['business'] == b['business']
    assert [c['difference'] for c in a['calculations']] == [c['difference'] for c in b['calculations']]


def test_successor_chain_is_traceable_after_void():
    state = run(load('succession.csv'), VOID)
    policy = state['policies'][0]
    assert policy['successors'] == {'00002': ['00004']}
    assert policy['cancellations'][0]['id'] == '00003'
    assert policy['cancellations'][0]['status'] == 'CANCELLED'


def test_cascade_void_does_not_delete_upstream_record():
    state = run(load('succession.csv'), VOID)
    ids = {a['id'] for p in state['policies'] for a in p['amendments']}
    assert {'00002', '00004'} <= ids
    assert '00003' in {c['id'] for p in state['policies'] for c in p['cancellations']}


def _chain_rows():
    """P1: issue, 7月加保, 撤销加保, 10月加保, 11月加保 — two hops downstream."""
    rows = [
        {'id':'01','policy_id':'P1','version':'1','kind':'issue','effective':'2026-01-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'3650','paid':'3650','ref':'','cover':'基础险','note':'','_line':2,'_raw':{}},
        {'id':'02','policy_id':'P1','version':'2','kind':'change','effective':'2026-07-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'7300','paid':'0','ref':'','cover':'基础险','note':'','_line':3,'_raw':{}},
        {'id':'03','policy_id':'P1','version':'3','kind':'cancel','effective':'2026-08-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'0','paid':'0','ref':'02','cover':'基础险','note':'','_line':4,'_raw':{}},
        {'id':'04','policy_id':'P1','version':'4','kind':'change','effective':'2026-10-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'5475','paid':'0','ref':'','cover':'基础险','note':'','_line':5,'_raw':{}},
        {'id':'05','policy_id':'P1','version':'5','kind':'change','effective':'2026-11-01','start':'2026-01-01','end':'2027-01-01','annual_premium':'6205','paid':'0','ref':'','cover':'基础险','note':'','_line':6,'_raw':{}},
    ]
    state = run(load('succession.csv'), {}, 2)
    state['rows'] = rows
    state['parameters'] = {}
    from app.modules.m3_versions.rules import apply as apply_versions
    return apply_versions(state)


def test_every_successor_is_pending_including_second_hop():
    p = _chain_rows()['policies'][0]
    assert p['pending'] == ['04', '05']
    assert p['successors'] == {'02': ['04', '05']}


def test_voiding_every_successor_leaves_only_cancelled_evidence():
    state = _chain_rows(); state['parameters'] = {'adjudication': {'default': 'void'}}
    from app.modules.m3_versions.rules import apply as apply_versions
    p = apply_versions(state)['policies'][0]
    assert p['changes'] == [] and p['voided'] == ['04', '05']
    assert p['blocked'] is False and p['publishable'] is True


def test_per_id_decisions_mix():
    state = _chain_rows(); state['parameters'] = {'adjudication': {'04': 'reverify', '05': 'void'}}
    from app.modules.m3_versions.rules import apply as apply_versions
    p = apply_versions(state)['policies'][0]
    assert [c['id'] for c in p['changes']] == ['04'] and p['voided'] == ['05']


def test_voided_successor_cascades_to_later_same_cover():
    state = _chain_rows()
    state['parameters'] = {'adjudication': {'04': 'void', '05': 'reverify'}}
    from app.modules.m3_versions.rules import apply as apply_versions
    p = apply_versions(state)['policies'][0]
    assert p['voided'] == ['04', '05'] and p['changes'] == []
    assert p['amendments'][1]['upstream'] == ['02']
    assert p['amendments'][2]['upstream'] == ['02', '04']
