from collections import defaultdict
from decimal import Decimal
from app.errors import DomainError

STATUS_LABELS = {
    'ACTIVE': '生效',
    'CANCELLED': '已撤销',
    'SUPERSEDED': '版本被替代',
    'PENDING': '待重新核验',
    'REVERIFIED': '已重新核验',
    'VOID': '随上游失效',
}

REVERIFY_VALUES = {'reverify', 'reverified', '重新核验', '重新告知', '保留', 'renew'}
VOID_VALUES = {'void', 'cascade', '失效', '作废', '随上游失效', 'cascade-void'}
RESERVED_KEYS = {'decisions', 'resolutions', 'default', 'resolution'}


def _decision(value):
    text = str(value).strip().lower()
    if text in REVERIFY_VALUES:
        return 'REVERIFY'
    if text in VOID_VALUES:
        return 'VOID'
    raise DomainError('ADJUDICATION', '裁决值无效：%s' % value)


def adjudication_input(parameters):
    """Normalise the adjudication block from run parameters.

    Accepted shapes:
      {"adjudication": {"default": "reverify", "decisions": {"00004": "void"}}}
      {"adjudication": {"00004": "reverify"}}
      {"decisions": {"00004": "reverify"}}
    """
    parameters = parameters or {}
    raw = parameters.get('adjudication')
    if raw is None:
        raw = parameters.get('succession')
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise DomainError('ADJUDICATION', '裁决参数必须是对象')

    default = raw.get('default', raw.get('resolution'))
    if default is None:
        default = parameters.get('default', parameters.get('resolution'))
    default = _decision(default) if default is not None else None

    decisions = {}

    def merge(source):
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            if key in RESERVED_KEYS:
                continue
            if value is not None:
                decisions[str(key)] = _decision(value)

    nested = raw.get('decisions', raw.get('resolutions'))
    merge(nested)
    # A flat adjudication map is also accepted.
    flat = {k: v for k, v in raw.items() if k not in RESERVED_KEYS}
    merge(flat)
    merge(parameters.get('decisions'))
    return decisions, default


def _upstream(record, cancelled_targets, cancel_target):
    """Cancelled amendments that removed this record's basis.

    A later amendment on the same policy and cover is dependent on an earlier
    cancelled amendment.  An explicit ref (to the cancelled change or to its
    cancel record) also marks dependency even when the effective date is not
    later.  Different covers are independent liability lines.
    """
    upstream = set()
    record_id = record['id']
    ref = str(record.get('ref') or '').strip()
    for target_id, target in cancelled_targets.items():
        if record_id == target_id:
            continue
        if record.get('cover') != target.get('cover'):
            continue
        if ref and (ref == target_id or cancel_target.get(ref) == target_id):
            upstream.add(target_id)
        elif record['effective'] > target['effective']:
            upstream.add(target_id)
    return sorted(upstream)


def _amendment(policy_id, record, status, upstream, reason, cancel_ids=None):
    return {
        'id': record['id'],
        'policy_id': policy_id,
        'version': str(record['version']),
        'effective': record['effective'],
        'start': record['start'],
        'end': record['end'],
        'cover': record['cover'],
        'annual_premium': str(record['annual_premium']),
        'paid': str(record['paid']),
        'line': record['_line'],
        'ref': record.get('ref', ''),
        'status': status,
        'status_label': STATUS_LABELS[status],
        'upstream': upstream,
        'reason': reason,
        'cancel_ids': cancel_ids or [],
    }


def apply(state):
    decisions, default = adjudication_input(state.get('parameters'))
    groups = defaultdict(list)
    bad = []
    for row in state['rows']:
        if (row['kind'] not in ('issue', 'change', 'cancel')
                or row['start'] >= row['end']
                or not row['start'] <= row['effective'] < row['end']
                or Decimal(str(row['annual_premium'])) < 0
                or Decimal(str(row['paid'])) < 0):
            bad.append((row, 'RANGE'))
            continue
        groups[row['policy_id']].append(row)

    policies = []
    pending_all = []
    resolved_all = []
    counts = {status: 0 for status in STATUS_LABELS}
    for policy_id, events in groups.items():
        issues = [r for r in events if r['kind'] == 'issue']
        if len(issues) != 1:
            bad.extend((r, 'REFERENCE') for r in events)
            continue
        base = issues[0]
        changes = [r for r in events if r['kind'] == 'change']
        targets = {r['id']: r for r in changes}
        cancelled_set = set()
        cancellations = []
        cancel_target = {}
        cancel_ids_for_target = defaultdict(list)
        for cancel in (r for r in events if r['kind'] == 'cancel'):
            target = targets.get(cancel['ref'])
            if target is None or cancel['effective'] < target['effective']:
                bad.append((cancel, 'REFERENCE'))
                continue
            cancelled_set.add(cancel['ref'])
            cancel_target[cancel['id']] = cancel['ref']
            cancel_ids_for_target[cancel['ref']].append(cancel['id'])
            cancellations.append({
                'id': cancel['id'],
                'target': cancel['ref'],
                'policy_id': policy_id,
                'effective': cancel['effective'],
                'line': cancel['_line'],
                'status': 'CANCELLED',
            })
        cancelled_targets = {tid: targets[tid] for tid in cancelled_set}

        # Pick one effective record per (effective date, cover).  The version
        # rule is unchanged; losers stay in the ledger as SUPERSEDED evidence.
        chosen = {}
        superseded = []
        for record in sorted(changes, key=lambda r: (r['effective'], Decimal(str(r['version'])), r['id'])):
            if record['id'] in cancelled_set:
                continue
            key = (record['effective'], record['cover'])
            current = chosen.get(key)
            if current is None:
                chosen[key] = record
                continue
            if record['version'] == current['version'] and record['annual_premium'] != current['annual_premium']:
                raise DomainError('VERSION', '同一生效日存在矛盾批单')
            if Decimal(str(record['version'])) > Decimal(str(current['version'])):
                superseded.append(current)
                chosen[key] = record
            else:
                superseded.append(record)

        amendments = []
        for record in changes:
            if record['id'] in cancelled_set:
                status = 'CANCELLED'
                amendments.append(_amendment(
                    policy_id, record, status, [],
                    'direct-cancel', cancel_ids_for_target[record['id']]))
                record['status'] = status
                counts[status] += 1
        for record in superseded:
            amendments.append(_amendment(
                policy_id, record, 'SUPERSEDED', [], 'lower-version',
                cancel_ids_for_target.get(record['id'], [])))
            record['status'] = 'SUPERSEDED'
            counts['SUPERSEDED'] += 1

        included = []
        pending = []
        voided = []
        reverified = []
        successors = defaultdict(list)
        candidates = sorted(chosen.values(), key=lambda r: (r['effective'], Decimal(str(r['version'])), r['id']))
        upstreams = {r['id']: list(_upstream(r, cancelled_targets, cancel_target)) for r in candidates}
        statuses = {}
        for record in candidates:
            upstream = upstreams[record['id']]
            # A voided successor cannot support anything further downstream:
            # add earlier same-cover successors that were voided.
            for earlier in candidates:
                if earlier['effective'] >= record['effective'] or earlier.get('cover') != record.get('cover'):
                    continue
                if statuses.get(earlier['id']) == 'VOID' and earlier['id'] not in upstream:
                    upstream.append(earlier['id'])
            upstreams[record['id']] = sorted(upstream)
            if not upstream:
                statuses[record['id']] = 'ACTIVE'
                continue
            decision = decisions.get(record['id'], default)
            if decision == 'VOID' or any(statuses.get(u) == 'VOID' for u in upstream):
                statuses[record['id']] = 'VOID'
            elif decision == 'REVERIFY':
                statuses[record['id']] = 'REVERIFIED'
            else:
                statuses[record['id']] = 'PENDING'

        for record in candidates:
            status = statuses[record['id']]
            upstream = upstreams[record['id']]
            reason = {'ACTIVE': 'in-effect', 'VOID': 'upstream-voided',
                      'REVERIFIED': 'reverified', 'PENDING': 'awaiting-adjudication'}[status]
            if status == 'PENDING':
                pending.append(record['id'])
                pending_all.append({
                    'policy_id': policy_id, 'id': record['id'],
                    'effective': record['effective'], 'line': record['_line'],
                    'upstream': upstream,
                })
            elif status == 'VOID':
                voided.append(record['id'])
                resolved_all.append({
                    'policy_id': policy_id, 'id': record['id'],
                    'decision': 'VOID', 'status': status,
                    'upstream': upstream,
                })
            elif status == 'REVERIFIED':
                reverified.append(record['id'])
                included.append(record)
                resolved_all.append({
                    'policy_id': policy_id, 'id': record['id'],
                    'decision': 'REVERIFY', 'status': status,
                    'upstream': upstream,
                })
            else:
                included.append(record)
            for target_id in upstream:
                successors[target_id].append(record['id'])
            amendments.append(_amendment(policy_id, record, status, upstream, reason))
            record['status'] = status
            counts[status] += 1

        amendments.sort(key=lambda a: (a['effective'], Decimal(a['version']), a['id']))
        included.sort(key=lambda r: (r['effective'], Decimal(str(r['version'])), r['id']))
        policies.append({
            'id': policy_id,
            'base': base,
            'changes': included,
            'cancelled': sorted(cancelled_set),
            'cancellations': sorted(cancellations, key=lambda c: (c['effective'], c['id'])),
            'amendments': amendments,
            'pending': sorted(pending),
            'voided': sorted(voided),
            'reverified': sorted(reverified),
            'successors': {k: sorted(v) for k, v in sorted(successors.items())},
            'blocked': bool(pending),
            'publishable': not pending,
        })

    bad_lines = {r['_line'] for r, _ in bad}
    for row, code in bad:
        state['quarantine'].append({'line': row['_line'], 'id': row['id'], 'code': code, 'raw': row['_raw']})
    state['rows'] = [r for r in state['rows'] if r['_line'] not in bad_lines]
    state['policies'] = policies

    summary = {
        'status': 'PENDING' if pending_all else 'RESOLVED',
        'complete': not pending_all,
        'publishable': not pending_all,
        'pending': pending_all,
        'pending_ids': [p['id'] for p in pending_all],
        'resolved': resolved_all,
        'decisions': decisions,
        'default': default,
        'counts': counts,
    }
    state['adjudication'] = summary
    # Explicit alias for callers that use the business term "succession".
    state['succession'] = summary
    return state
