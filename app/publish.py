import hashlib, json
from app import store
from app.errors import DomainError
from app.modules.m3_versions.rules import adjudication_input
from app.workflow import run
from app.modules.m7_archive.export_out import checksum

FINAL_STATUSES = {'ACTIVE', 'CANCELLED', 'SUPERSEDED', 'REVERIFIED', 'VOID'}


def _canonical(state):
    chain = []
    for policy in state.get('policies', []):
        for a in policy.get('amendments', []):
            chain.append([policy['id'], a['id'], a['status'], a.get('upstream', [])])
        for c in policy.get('cancellations', []):
            chain.append([policy['id'], c['id'], 'CANCEL:' + c['target']])
    calculations = [[c['id'], c['expected'], c['paid'], c['difference'],
                     [[s['start'], s['end'], s['premium']] for s in c['segments']]]
                    for c in state.get('calculations', [])]
    payload = {'chain': sorted(chain), 'calculations': calculations,
               'business': state.get('business', {})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _merged_parameters(base, decisions, default):
    merged = dict(base or {})
    existing = merged.get('adjudication')
    existing = existing if isinstance(existing, dict) else {}
    block = {k: v for k, v in existing.items() if k not in ('decisions', 'resolutions', 'default', 'resolution')}
    block['decisions'] = dict(decisions)
    if default:
        block['default'] = default
    merged['adjudication'] = block
    return merged


def _validate(state):
    adjudication = state.get('adjudication') or {}
    if adjudication.get('status') != 'RESOLVED' or adjudication.get('pending'):
        pending = adjudication.get('pending_ids', [])
        raise DomainError('ADJUDICATION_PENDING',
                          '存在待裁决的后继批改，不能发布：' + (', '.join(pending) or '未知'), 409)
    for policy in state.get('policies', []):
        for amendment in policy.get('amendments', []):
            if amendment['status'] not in FINAL_STATUSES:
                raise DomainError('ADJUDICATION_PENDING',
                                  '批改 %s 状态未定，不能发布' % amendment['id'], 409)
        for successor in policy.get('successors', {}).values():
            for amendment_id in successor:
                if amendment_id not in {a['id'] for a in policy['amendments']}:
                    raise DomainError('CHAIN', '撤销链缺少后继批改 %s' % amendment_id, 409)
    if not state.get('reconcile', {}).get('balanced'):
        raise DomainError('UNBALANCED', '对账未平，不能发布', 409)
    if not state.get('archive', {}).get('ready'):
        raise DomainError('ARCHIVE', '结案校验未通过，不能发布', 409)


def publish(run_id, expected_version, body):
    stored = store.get(run_id)
    existing = store.get_published(run_id)
    if expected_version is not None:
        current = existing['published_version'] if existing else 0
        allowed = {current} if current else {0, 1}
        if expected_version not in allowed:
            raise DomainError('STALE', '保障链版本已更新，请刷新', 409)
    run_params = dict(stored.get('manifest', {}).get('parameters') or {})
    existing_params = dict((existing or {}).get('manifest', {}).get('parameters') or {})

    if any(key in body for key in ('adjudication', 'succession', 'decisions', 'default', 'resolution')):
        # An explicit adjudication payload replaces the decisions of the prior
        # publication; ids left out fall back to the submitted default.
        base_params = {k: v for k, v in run_params.items() if k not in ('adjudication', 'succession', 'decisions', 'default', 'resolution')}
        decisions, default = adjudication_input(body)
        merged = _merged_parameters(base_params, decisions, default)
    else:
        # A bare republish inherits the last published adjudication so it does
        # not silently reopen successors that were already resolved.
        merged = existing_params or run_params

    import base64
    source = base64.b64decode(stored['source_b64'])

    def build(version):
        state = run(source, merged, 7)
        _validate(state)
        state['run_id'] = run_id
        state['manifest']['parameters'] = merged
        state['manifest']['published_version'] = version
        state['published_version'] = version
        state['published'] = True
        state['publishable'] = True
        state['archive']['sha256'] = checksum(state)
        state['fingerprint'] = _canonical(state)
        return state

    if existing:
        candidate = build(existing['published_version'])
        if candidate['fingerprint'] == existing.get('fingerprint'):
            return existing
    return store.publish(run_id, expected_version, build)
