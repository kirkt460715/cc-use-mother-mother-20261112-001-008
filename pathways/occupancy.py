from collections import defaultdict
from datetime import timedelta
from encounters.values import day, days, integer, ratio


def daily(dataset, view, start, end):
    first, last = day(start), day(end)
    span = (last - first).days
    if span < 0 or span > 3660 or last > day(view['as_of']):
        raise ValueError('invalid bounded occupancy window')
    deltas = defaultdict(lambda: defaultdict(int))
    admissions = defaultdict(lambda: defaultdict(int))
    discharges = defaultdict(lambda: defaultdict(int))
    stages = set()
    for key, visit in view['visits'].items():
        stage = dataset.stage(key)
        stages.add(stage)
        admitted = day(visit['admitted'])
        discharged = day(visit['discharged']) if visit['discharged'] else day(view['as_of'])
        if first <= admitted < last:
            admissions[stage][admitted] += 1
        if visit['discharged'] and first <= discharged < last:
            discharges[stage][discharged] += 1
        begin, stop = max(first, admitted), min(last, discharged)
        if stop > begin:
            deltas[stage][begin] += 1
            deltas[stage][stop] -= 1
    output = []
    for stage in sorted(stages):
        active = 0
        for offset in range(span):
            date = first + timedelta(days=offset)
            active += deltas[stage][date]
            if active < 0:
                raise ValueError('occupancy sweep negative')
            output.append({'date': date.isoformat(), 'stage': stage, 'occupied': active,
                           'admissions': admissions[stage][date], 'discharges': discharges[stage][date]})
    return {'start': start, 'end_exclusive': end, 'rows': output,
            'occupied_day_sum': sum(row['occupied'] for row in output),
            'convention': 'half_open_inpatient_intervals_zero_day_visits_use_no_overnight_capacity'}


def continuity_coverage(dataset, view):
    eligible = defaultdict(set)
    completed = defaultdict(set)
    for key, visit in view['visits'].items():
        if visit['outcome'] == 'transfer':
            eligible[dataset.stage(key)].add(key)
    for referral in view['referrals'].values():
        if referral['status'] == 'accepted':
            completed[dataset.stage(referral['source'])].add(referral['source'])
    rows = []
    for stage, sources in sorted(eligible.items()):
        accepted = completed[stage] & sources
        rows.append({'stage': stage, 'transfer_discharges': len(sources),
                     'linked_continuations': len(accepted), 'coverage': ratio(len(accepted), len(sources)),
                     'unlinked': sorted(sources - accepted)})
    return {'rows': rows, 'denominator': 'discharges_recorded_as_transfer',
            'warning': 'unlinked_does_not_establish_absence_of_care'}


def pathways_by_signature(report, minimum=1):
    integer(minimum, 'minimum', 1)
    grouped = defaultdict(lambda: {'chains': 0, 'people': set(), 'elapsed': 0, 'gaps': 0, 'closed': 0})
    for chain in report['chains']['chains']:
        bucket = grouped[chain['stage_signature']]
        bucket['chains'] += 1
        bucket['people'].add(chain['person'])
        bucket['elapsed'] += chain['elapsed_days']
        bucket['gaps'] += chain['gap_days']
        bucket['closed'] += chain['closed']
    return {'rows': [{'signature': signature, 'chains': values['chains'], 'people': len(values['people']),
                      'closed': values['closed'], 'elapsed_sum': values['elapsed'], 'gap_sum': values['gaps'],
                      'mean_elapsed': ratio(values['elapsed'], values['chains'])}
                     for signature, values in sorted(grouped.items()) if values['chains'] >= minimum],
            'access': 'restricted_sequences_not_for_public_release'}


def bottlenecks(dataset, view, threshold=7):
    integer(threshold, 'threshold', 0, 365)
    rows = []
    for referral in view['referrals'].values():
        if referral['status'] != 'pending':
            continue
        age = days(referral['requested'], view['as_of'])
        if age < threshold:
            continue
        source = view['visits'][referral['source']]
        rows.append({'referral': referral['id'], 'person': referral['person'],
                     'source_facility': source['facility'], 'source_stage': dataset.stage(source['id']),
                     'destination_stage': referral['destination_stage'], 'waiting_days': age,
                     'source_still_open': source['discharged'] is None,
                     'terminal_source': source['outcome'] in ('death', 'left')})
    rows.sort(key=lambda row: (-row['waiting_days'], row['referral']))
    return {'threshold': threshold, 'rows': rows, 'action': 'data_review_not_automated_clinical_triage'}
