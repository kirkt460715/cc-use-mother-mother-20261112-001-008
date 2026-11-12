from collections import defaultdict
from encounters.values import days, digest, integer
from pathways.chains import overlaps


def assess(dataset, view, late_days=7):
    integer(late_days, 'late_days', 0, 365)
    issues = []
    accepted_sources = {row['source'] for row in view['referrals'].values() if row['status'] == 'accepted'}
    pending_by_source = defaultdict(list)
    for row in view['referrals'].values():
        if row['status'] == 'pending':
            pending_by_source[row['source']].append(row['id'])
        lag = days(row['requested'], row['recorded'])
        if lag > late_days:
            issues.append({'kind': 'late_referral_recording', 'entity': row['id'],
                           'person': row['person'], 'days': lag, 'severity': 'warning'})
        source = view['visits'][row['source']]
        if source['discharged'] and row['requested'] > source['discharged']:
            issues.append({'kind': 'request_after_discharge', 'entity': row['id'],
                           'person': row['person'], 'days': days(source['discharged'], row['requested']),
                           'severity': 'warning'})
    for key, visit in view['visits'].items():
        lag = days(visit['admitted'], visit['recorded'])
        if lag > late_days:
            issues.append({'kind': 'late_visit_recording', 'entity': key,
                           'person': visit['person'], 'days': lag, 'severity': 'warning'})
        if visit['outcome'] == 'transfer' and key not in accepted_sources:
            issues.append({'kind': 'transfer_without_accepted_continuation', 'entity': key,
                           'person': visit['person'], 'pending_requests': pending_by_source[key],
                           'severity': 'warning'})
        if key in pending_by_source and visit['outcome'] in ('death', 'left'):
            issues.append({'kind': 'pending_referral_after_terminal_outcome', 'entity': key,
                           'person': visit['person'], 'pending_requests': pending_by_source[key],
                           'severity': 'review'})
    for row in overlaps(dataset, view):
        issues.append({'kind': row['code'], 'entity': ':'.join(row['visits']),
                       'person': row['person'], 'days': row['days'], 'severity': 'review'})
    for key, ids in pending_by_source.items():
        if len(ids) > 1:
            issues.append({'kind': 'parallel_pending_referrals', 'entity': key,
                           'person': view['visits'][key]['person'], 'requests': sorted(ids),
                           'severity': 'review'})
    issues.sort(key=lambda row: (row['kind'], row['entity']))
    counts = defaultdict(int)
    for row in issues:
        counts[row['kind']] += 1
    return {'issues': issues, 'counts': dict(sorted(counts.items())),
            'issue_count': len(issues), 'automatic_exclusion': False,
            'note': 'warnings_are_data_quality_signals_not_clinical_judgments'}


def by_facility(dataset, view, quality):
    grouped = defaultdict(lambda: {'visits': 0, 'people': set(), 'issues': 0})
    for key, visit in view['visits'].items():
        bucket = grouped[visit['facility']]
        bucket['visits'] += 1
        bucket['people'].add(visit['person'])
    for issue in quality['issues']:
        key = issue['entity']
        if key in view['referrals']:
            key = view['referrals'][key]['source']
        if key in view['visits']:
            grouped[view['visits'][key]['facility']]['issues'] += 1
    return [{'facility': key, 'visits': value['visits'], 'people': len(value['people']),
             'issues': value['issues'], 'stage': dataset.facilities[key]['stage']}
            for key, value in sorted(grouped.items())]


def recording_lag(dataset, as_of, known_at):
    view = dataset.slice(as_of, known_at)
    rows = []
    for table, start_field in (('visits', 'admitted'), ('referrals', 'requested')):
        lags = [days(row[start_field], row['recorded']) for row in view[table].values()]
        buckets = {'same_day': 0, 'one_to_seven': 0, 'over_seven': 0}
        for lag in lags:
            buckets['same_day' if lag == 0 else 'one_to_seven' if lag <= 7 else 'over_seven'] += 1
        rows.append({'table': table, 'records': len(lags), 'lag_day_sum': sum(lags), **buckets})
    return {'rows': rows, 'known_at': known_at, 'as_of': as_of,
            'late_records_can_change_historical_estimates': True}
