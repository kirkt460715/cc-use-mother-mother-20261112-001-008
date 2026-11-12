from collections import defaultdict
from datetime import timedelta
from encounters.values import day, days, integer, ratio


def person_timeline(dataset, view, person, start, end):
    if person not in dataset.people:
        raise KeyError(person)
    first, last = day(start), day(end)
    if last < first or (last - first).days > 3660 or last > day(view['as_of']):
        raise ValueError('invalid bounded timeline interval')
    visits = [visit for visit in view['visits'].values() if visit['person'] == person]
    output = []
    events = defaultdict(lambda: {'start': [], 'end': []})
    for visit in visits:
        begin = max(first, day(visit['admitted']))
        stop = min(last, day(visit['discharged'] or view['as_of']))
        if begin < stop:
            events[begin]['start'].append(visit['id'])
            events[stop]['end'].append(visit['id'])
    points = sorted(set(events) | {first, last})
    active = set()
    for index, point in enumerate(points[:-1]):
        active.difference_update(events[point]['end'])
        active.update(events[point]['start'])
        stop = points[index + 1]
        stages = sorted({dataset.stage(key) for key in active})
        output.append({'start': point.isoformat(), 'end': stop.isoformat(),
                       'days': (stop - point).days, 'visits': sorted(active), 'stages': stages,
                       'state': 'unobserved' if not active else 'overlap' if len(active) > 1 else 'care'})
    if sum(row['days'] for row in output) != (last - first).days:
        raise ValueError('timeline partition failed')
    return {'person': person, 'start': start, 'end_exclusive': end, 'segments': output,
            'unobserved_is_not_proof_of_no_care': True}


def stage_days(dataset, view, start, end):
    first, last = day(start), day(end)
    if last < first or (last - first).days > 3660 or last > day(view['as_of']):
        raise ValueError('invalid stage day window')
    people = sorted({row['person'] for row in view['visits'].values()})
    groups = defaultdict(lambda: {'people': set(), 'person_days': 0, 'overlap_days': 0})
    unobserved = 0
    for person in people:
        timeline = person_timeline(dataset, view, person, start, end)
        for segment in timeline['segments']:
            if not segment['stages']:
                unobserved += segment['days']
            for stage in segment['stages']:
                group = groups[stage]
                group['people'].add(person)
                group['person_days'] += segment['days']
                if segment['state'] == 'overlap':
                    group['overlap_days'] += segment['days']
    return {'start': start, 'end_exclusive': end, 'people': len(people),
            'rows': [{'stage': stage, 'people': len(counts['people']),
                      'person_days': counts['person_days'], 'overlap_days': counts['overlap_days']}
                     for stage, counts in sorted(groups.items())],
            'unobserved_person_days': unobserved,
            'sum_across_stages_may_double_count_overlap': True}


def bed_turnover(dataset, view, start, end):
    first, last = day(start), day(end)
    span = (last - first).days
    if span <= 0 or span > 3660 or last > day(view['as_of']):
        raise ValueError('turnover requires positive bounded window')
    groups = defaultdict(lambda: {'admissions': 0, 'discharges': 0, 'same_day': 0,
                                  'occupied_days': 0, 'people': set()})
    for visit in view['visits'].values():
        group = groups[visit['facility']]
        admitted = day(visit['admitted'])
        discharged = day(visit['discharged'] or view['as_of'])
        if first <= admitted < last:
            group['admissions'] += 1
            if visit['discharged'] == visit['admitted']:
                group['same_day'] += 1
        if visit['discharged'] and first <= discharged < last:
            group['discharges'] += 1
        overlap = max(0, (min(last, discharged) - max(first, admitted)).days)
        group['occupied_days'] += overlap
        if overlap or first <= admitted < last:
            group['people'].add(visit['person'])
    return {'rows': [{'facility': key, 'stage': dataset.facilities[key]['stage'],
                      'people': len(counts['people']), 'admissions': counts['admissions'],
                      'discharges': counts['discharges'], 'same_day': counts['same_day'],
                      'occupied_days': counts['occupied_days'], 'mean_daily_occupied': ratio(counts['occupied_days'], span)}
                     for key, counts in sorted(groups.items())],
            'note': 'no_capacity_denominator_no_occupancy_percentage_claimed'}


def cross_county(dataset, view):
    groups = defaultdict(lambda: {'visits': 0, 'people': set(), 'referrals': 0})
    for visit in view['visits'].values():
        residence = dataset.people[visit['person']]['county']
        service = dataset.facilities[visit['facility']]['county']
        group = groups[residence, service]
        group['visits'] += 1
        group['people'].add(visit['person'])
    for referral in view['referrals'].values():
        if referral['status'] == 'accepted':
            person = dataset.people[referral['person']]
            target = view['visits'][referral['target']]
            county = dataset.facilities[target['facility']]['county']
            groups[person['county'], county]['referrals'] += 1
    return {'rows': [{'residence_county': key[0], 'service_county': key[1],
                      'visits': counts['visits'], 'people': len(counts['people']),
                      'accepted_referrals': counts['referrals'], 'cross_county': key[0] != key[1]}
                     for key, counts in sorted(groups.items())],
            'access': 'restricted_small_geographic_cells'}
