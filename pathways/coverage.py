from collections import defaultdict
from encounters.values import day, days, ratio, integer, Invalid
from encounters.schema import STAGES


def source_visibility(dataset, as_of, known_at):
    business, knowledge = day(as_of), day(known_at)
    view = dataset.slice(as_of, known_at)
    rows = []
    for table, date_field in (('visits', 'admitted'), ('referrals', 'requested')):
        stored = getattr(dataset, table)
        future_business = []
        future_knowledge = []
        visible = set(view[table])
        for key, row in stored.items():
            if day(row[date_field]) > business:
                future_business.append(key)
            elif day(row['recorded']) > knowledge:
                future_knowledge.append(key)
        rows.append({'table': table, 'stored': len(stored), 'visible': len(visible),
                     'future_business': sorted(future_business),
                     'future_knowledge': sorted(future_knowledge),
                     'not_visible_due_to_source_reference': sorted(set(stored) - visible - set(future_business) - set(future_knowledge))})
    return {'as_of': as_of, 'known_at': known_at, 'rows': rows,
            'hidden_future_visit_closures': view['hidden_future_closures'],
            'scope': 'relative_to_stored_dataset_not_population_coverage'}


def person_coverage(dataset, view):
    counts = defaultdict(lambda: {'visits': 0, 'referrals': 0, 'accepted': 0, 'pending': 0})
    for visit in view['visits'].values():
        counts[visit['person']]['visits'] += 1
    for referral in view['referrals'].values():
        group = counts[referral['person']]
        group['referrals'] += 1
        if referral['status'] in ('accepted', 'pending'):
            group[referral['status']] += 1
    rows = []
    for key, person in sorted(dataset.people.items()):
        row = counts[key]
        rows.append({'person': key, 'county': person['county'], **row,
                     'observed': row['visits'] > 0,
                     'multiple_requests': row['referrals'] > 1})
    return {'rows': rows, 'registered_people': len(rows),
            'observed_people': sum(row['observed'] for row in rows),
            'request_people': sum(row['referrals'] > 0 for row in rows),
            'unobserved_registered_people': sum(not row['observed'] for row in rows)}


def facility_coverage(dataset, view, start, end):
    first, last = day(start), day(end)
    if last <= first or last > day(view['as_of']) or (last - first).days > 3660:
        raise Invalid('invalid facility coverage window')
    counts = defaultdict(lambda: {'admissions': 0, 'discharges': 0, 'people': set(), 'recorded_dates': set()})
    for visit in view['visits'].values():
        group = counts[visit['facility']]
        if start <= visit['admitted'] < end:
            group['admissions'] += 1
            group['people'].add(visit['person'])
        if visit['discharged'] and start <= visit['discharged'] < end:
            group['discharges'] += 1
        if start <= visit['recorded'] < end:
            group['recorded_dates'].add(visit['recorded'])
    output = []
    for key, facility in sorted(dataset.facilities.items()):
        group = counts[key]
        output.append({'facility': key, 'stage': facility['stage'], 'county': facility['county'],
                       'admissions': group['admissions'], 'discharges': group['discharges'],
                       'people': len(group['people']), 'recording_days': len(group['recorded_dates']),
                       'no_admissions_observed': group['admissions'] == 0})
    return {'start': start, 'end_exclusive': end, 'rows': output,
            'zero_is_not_proof_of_complete_zero_activity_reporting': True}


def destination_coverage(dataset, view):
    facilities = defaultdict(set)
    people = defaultdict(set)
    requests = defaultdict(int)
    targets = defaultdict(set)
    for key, facility in dataset.facilities.items():
        facilities[facility['stage']].add(key)
    for referral in view['referrals'].values():
        stage = referral['destination_stage']
        requests[stage] += 1
        people[stage].add(referral['person'])
        if referral['status'] == 'accepted':
            target = view['visits'][referral['target']]
            targets[stage].add(target['facility'])
    output = []
    for stage in STAGES:
        output.append({'stage': stage, 'registered_facilities': len(facilities[stage]),
                       'target_facilities_observed': len(targets[stage]),
                       'requests': requests[stage], 'people': len(people[stage]),
                       'registered_facility_coverage': ratio(len(targets[stage]), len(facilities[stage]))})
    return {'rows': output, 'note': 'network_observation_not_capacity_or_access_measure'}


def audit_observation_partition(dataset, report):
    view = dataset.slice(report['options']['as_of'], report['options']['known_at'])
    included = report['wait_observations']['rows']
    excluded = report['wait_observations']['excluded']
    first = [row['id'] for row in included]
    second = [row['id'] for row in excluded]
    if len(first) != len(set(first)) or len(second) != len(set(second)) or set(first) & set(second):
        raise Invalid('waiting observation partition overlaps')
    if set(first) | set(second) != set(view['referrals']):
        raise Invalid('waiting observation partition incomplete')
    for row in included:
        source = view['referrals'][row['id']]
        expected_event = source['status'] == 'accepted'
        end = view['visits'][source['target']]['admitted'] if expected_event else view['as_of']
        if row['event'] != expected_event or row['duration'] != days(source['requested'], end):
            raise Invalid('waiting observation differs from source')
        if row['person'] != source['person']:
            raise Invalid('observation person differs')
    for row in excluded:
        if row['reason'] != view['referrals'][row['id']]['status'] or row['reason'] not in ('declined', 'cancelled'):
            raise Invalid('exclusion reason differs from source')
    return {'valid': True, 'included': len(first), 'excluded': len(second)}
