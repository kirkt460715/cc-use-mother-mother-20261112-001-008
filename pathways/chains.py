from collections import defaultdict
from encounters.values import day, days, digest, integer


def build(dataset, view):
    visits = view['visits']
    accepted = [row for row in view['referrals'].values() if row['status'] == 'accepted']
    successor = {row['source']: row['target'] for row in accepted}
    predecessor = {row['target']: row['source'] for row in accepted}
    links = {(row['source'], row['target']): row for row in accepted}
    roots = sorted(visits.keys() - predecessor.keys())
    chains = []
    covered = set()
    for root in roots:
        nodes = []
        edges = []
        current = root
        while current is not None:
            if current in covered:
                raise ValueError('path traversal repeats visit')
            covered.add(current)
            nodes.append(current)
            target = successor.get(current)
            if target is not None:
                referral = links[current, target]
                edges.append({'referral': referral['id'], 'source': current, 'target': target,
                              'request_wait_days': days(referral['requested'], visits[target]['admitted']),
                              'handoff_gap_days': days(visits[current]['discharged'], visits[target]['admitted'])})
            current = target
        first, last = visits[nodes[0]], visits[nodes[-1]]
        stages = [dataset.stage(key) for key in nodes]
        end = last['discharged'] or view['as_of']
        length = sum(days(visits[key]['admitted'], visits[key]['discharged'] or view['as_of']) for key in nodes)
        gap = sum(edge['handoff_gap_days'] for edge in edges)
        elapsed = days(first['admitted'], end)
        if length + gap != elapsed:
            raise ValueError('chain interval conservation failed')
        body = {'id': root, 'person': first['person'], 'visits': nodes,
                'stages': stages, 'edges': edges, 'start': first['admitted'], 'end': end,
                'closed': last['discharged'] is not None, 'outcome': last['outcome'],
                'care_days': length, 'gap_days': gap, 'elapsed_days': elapsed,
                'backward_transitions': 0,
                'stage_signature': '>'.join(stages)}
        order = {'acute': 0, 'rehab': 1, 'primary': 2, 'home': 3}
        body['backward_transitions'] = sum(order[b] < order[a] for a, b in zip(stages, stages[1:]))
        chains.append({**body, 'chain_digest': digest(body)})
    if covered != set(visits):
        raise ValueError('unrooted or cyclic visits in selected view')
    return {'chains': chains, 'visits': len(covered), 'accepted_links': len(accepted)}


def transitions(dataset, view):
    grouped = defaultdict(lambda: {'accepted': 0, 'pending': 0, 'declined': 0, 'cancelled': 0,
                                    'request_wait_days': [], 'gap_days': []})
    for referral in view['referrals'].values():
        source = view['visits'][referral['source']]
        key = dataset.stage(source['id']), referral['destination_stage']
        group = grouped[key]
        group[referral['status']] += 1
        if referral['status'] == 'accepted':
            target = view['visits'][referral['target']]
            group['request_wait_days'].append(days(referral['requested'], target['admitted']))
            group['gap_days'].append(days(source['discharged'], target['admitted']))
    return [{'from': key[0], 'to': key[1], **value} for key, value in sorted(grouped.items())]


def readmissions(dataset, view, window=30):
    integer(window, 'window', 1, 365)
    by_person = defaultdict(list)
    linked = {row['target'] for row in view['referrals'].values() if row['target'] is not None}
    for visit in view['visits'].values():
        by_person[visit['person']].append(visit)
    rows = []
    for person, visits in sorted(by_person.items()):
        visits.sort(key=lambda row: (row['admitted'], row['id']))
        for index, visit in enumerate(visits):
            if visit['discharged'] is None or visit['outcome'] != 'completed':
                continue
            later = [row for row in visits[index + 1:] if row['id'] not in linked
                     and day(row['admitted']) >= day(visit['discharged'])]
            following = min(later, key=lambda row: (row['admitted'], row['id']), default=None)
            followup = days(visit['discharged'], view['as_of'])
            difference = days(visit['discharged'], following['admitted']) if following else None
            rows.append({'person': person, 'index_visit': visit['id'], 'followup_days': min(followup, window),
                         'readmitted': difference is not None and difference <= window,
                         'next_visit': following['id'] if difference is not None and difference <= window else None,
                         'gap_days': difference if difference is not None and difference <= window else None,
                         'fully_observed': followup >= window})
    return {'window_days': window, 'rows': rows,
            'denominator': sum(row['fully_observed'] for row in rows),
            'numerator': sum(row['fully_observed'] and row['readmitted'] for row in rows),
            'note': 'descriptive_all_cause_not_risk_adjusted'}


def overlaps(dataset, view):
    by_person = defaultdict(list)
    for visit in view['visits'].values():
        by_person[visit['person']].append(visit)
    output = []
    for person, visits in sorted(by_person.items()):
        visits.sort(key=lambda row: (row['admitted'], row['id']))
        active = []
        for visit in visits:
            start = day(visit['admitted'])
            active = [row for row in active if day(row['discharged'] or view['as_of']) > start]
            for prior in active:
                end = min(day(prior['discharged'] or view['as_of']), day(visit['discharged'] or view['as_of']))
                if end > start:
                    output.append({'person': person, 'visits': [prior['id'], visit['id']],
                                   'days': (end - start).days, 'code': 'overlapping_unlinked_care'})
            active.append(visit)
    return output
