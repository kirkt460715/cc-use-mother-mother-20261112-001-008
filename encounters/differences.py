from collections import defaultdict
from encounters.values import digest, fields, integer, Invalid
from encounters.schema import SCHEMAS


def compare(before, after):
    changes = []
    people = set()
    facilities = set()
    for table in SCHEMAS:
        left, right = getattr(before, table), getattr(after, table)
        for key in sorted(left.keys() | right.keys()):
            first, last = left.get(key), right.get(key)
            if first == last:
                continue
            fields_changed = [field for field in sorted(set(first or {}) | set(last or {}))
                              if (first or {}).get(field) != (last or {}).get(field)]
            changes.append({'table': table, 'id': key, 'action': 'added' if first is None else 'removed' if last is None else 'changed',
                            'fields': fields_changed, 'before_digest': digest(first) if first else None,
                            'after_digest': digest(last) if last else None})
            for dataset, row in ((before, first), (after, last)):
                if row is None:
                    continue
                if table == 'people':
                    people.add(key)
                elif table == 'facilities':
                    facilities.add(key)
                    people.update(visit['person'] for visit in dataset.visits.values() if visit['facility'] == key)
                else:
                    people.add(row['person'])
                    if table == 'visits':
                        facilities.add(row['facility'])
    visits = set()
    referrals = set()
    for dataset in (before, after):
        visits.update(row['id'] for row in dataset.visits.values() if row['person'] in people)
        referrals.update(row['id'] for row in dataset.referrals.values() if row['person'] in people)
    return {'before_digest': before.fingerprint, 'after_digest': after.fingerprint,
            'changes': changes, 'affected_people': sorted(people), 'affected_facilities': sorted(facilities),
            'affected_visits': sorted(visits), 'affected_referrals': sorted(referrals),
            'recompute_all_cohort_statistics': bool(changes)}


def patch(before, after):
    difference = compare(before, after)
    operations = []
    for row in difference['changes']:
        table, key = row['table'], row['id']
        operations.append({'table': table, 'id': key, 'before_digest': row['before_digest'],
                           'after': getattr(after, table).get(key)})
    return {'operations': operations, 'before_digest': before.fingerprint, 'after_digest': after.fingerprint}


def changed_chains(before_report, after_report):
    first = {chain['id']: chain for chain in before_report['chains']['chains']}
    last = {chain['id']: chain for chain in after_report['chains']['chains']}
    rows = []
    for key in sorted(first.keys() | last.keys()):
        left, right = first.get(key), last.get(key)
        if left == right:
            continue
        rows.append({'root': key, 'before_visits': left['visits'] if left else [],
                     'after_visits': right['visits'] if right else [],
                     'before_digest': left['chain_digest'] if left else None,
                     'after_digest': right['chain_digest'] if right else None,
                     'care_day_delta': (right['care_days'] if right else 0) - (left['care_days'] if left else 0),
                     'gap_day_delta': (right['gap_days'] if right else 0) - (left['gap_days'] if left else 0)})
    return {'rows': rows, 'before_report': before_report['report_digest'], 'after_report': after_report['report_digest']}


def explain_entity(dataset, table, key):
    if table not in SCHEMAS:
        raise Invalid('unknown entity table')
    rows = getattr(dataset, table)
    if key not in rows:
        raise KeyError(key)
    row = rows[key]
    people = set()
    facilities = set()
    visits = set()
    referrals = set()
    if table == 'people':
        people.add(key)
    elif table == 'facilities':
        facilities.add(key)
    elif table == 'visits':
        visits.add(key)
        people.add(row['person'])
        facilities.add(row['facility'])
    else:
        referrals.add(key)
        people.add(row['person'])
        visits.add(row['source'])
        if row['target']:
            visits.add(row['target'])
    for visit in dataset.visits.values():
        if visit['person'] in people or visit['facility'] in facilities:
            visits.add(visit['id'])
    for referral in dataset.referrals.values():
        if referral['source'] in visits or referral['target'] in visits:
            referrals.add(referral['id'])
    return {'table': table, 'id': key, 'record': row, 'record_digest': digest(row),
            'related_visits': sorted(visits), 'related_referrals': sorted(referrals),
            'note': 'reference_lineage_not_probabilistic_identity_matching'}
