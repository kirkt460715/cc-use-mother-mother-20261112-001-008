from collections import defaultdict
from encounters.values import days, day, integer, ratio


def referral_waits(dataset, view, source_stage=None, destination_stage=None):
    from encounters.schema import STAGES
    for value in (source_stage, destination_stage):
        if value is not None and value not in STAGES:
            raise ValueError('unsupported stage filter')
    included = []
    excluded = []
    for key, row in sorted(view['referrals'].items()):
        stage = dataset.stage(row['source'])
        if source_stage is not None and stage != source_stage:
            continue
        if destination_stage is not None and row['destination_stage'] != destination_stage:
            continue
        if row['status'] in ('declined', 'cancelled'):
            excluded.append({'id': key, 'reason': row['status']})
            continue
        event = row['status'] == 'accepted'
        end = view['visits'][row['target']]['admitted'] if event else view['as_of']
        included.append({'id': key, 'duration': days(row['requested'], end), 'event': event,
                         'person': row['person'], 'source': row['source'], 'source_stage': stage,
                         'destination_stage': row['destination_stage'], 'requested': row['requested'],
                         'end': end, 'county': dataset.people[row['person']]['county'],
                         'age_band': dataset.people[row['person']]['age_band'],
                         'rural': dataset.people[row['person']]['rural']})
    return {'rows': included, 'excluded': excluded,
            'denominator': 'accepted_or_pending_referral_requests',
            'event': 'target_admission', 'censoring_date': view['as_of']}


def care_spells(dataset, view, stage=None):
    output = []
    for key, visit in sorted(view['visits'].items()):
        actual_stage = dataset.stage(key)
        if stage is not None and actual_stage != stage:
            continue
        end = visit['discharged'] or view['as_of']
        output.append({'id': key, 'duration': days(visit['admitted'], end),
                       'event': visit['discharged'] is not None,
                       'stage': actual_stage, 'person': visit['person'],
                       'outcome': visit['outcome'], 'end': end})
    return {'rows': output, 'event': 'any_discharge',
            'note': 'death_is_a_discharge_here_not_a_recovery_event'}


def summary(values):
    if not values:
        return {'count': 0, 'min': None, 'max': None, 'mean': None, 'median': None}
    ordered = sorted(values)
    size = len(ordered)
    middle = ordered[size // 2] if size % 2 else (ordered[size // 2 - 1] + ordered[size // 2]) / 2
    return {'count': size, 'min': ordered[0], 'max': ordered[-1],
            'mean': ratio(sum(ordered), size), 'median': middle}


def completion_rates(dataset, view, horizon=14):
    integer(horizon, 'horizon', 0, 365)
    groups = defaultdict(lambda: {'requests': 0, 'mature': 0, 'completed_within': 0,
                                  'pending': 0, 'declined': 0, 'cancelled': 0})
    for row in view['referrals'].values():
        key = dataset.stage(row['source']), row['destination_stage']
        group = groups[key]
        group['requests'] += 1
        group[row['status']] = group.get(row['status'], 0) + 1
        mature = days(row['requested'], view['as_of']) >= horizon
        if mature:
            group['mature'] += 1
            if row['status'] == 'accepted':
                target = view['visits'][row['target']]
                if days(row['requested'], target['admitted']) <= horizon:
                    group['completed_within'] += 1
    return {'horizon_days': horizon, 'rows': [
        {'from': key[0], 'to': key[1], **counts,
         'rate': ratio(counts['completed_within'], counts['mature'])}
        for key, counts in sorted(groups.items())],
        'denominator': 'all_requests_with_full_horizon_including_declined_and_cancelled'}


def person_level(rows):
    selected = {}
    excluded = []
    for row in sorted(rows, key=lambda item: (item['requested'], item['id'])):
        key = row['person']
        if key in selected:
            excluded.append(row['id'])
        else:
            selected[key] = row
    return {'rows': sorted(selected.values(), key=lambda item: item['id']),
            'excluded_repeat_referrals': sorted(excluded),
            'selection': 'first_eligible_request_per_person'}


def gaps(chains, threshold=7):
    integer(threshold, 'threshold', 0, 365)
    rows = []
    for chain in chains['chains']:
        for edge in chain['edges']:
            rows.append({'chain': chain['id'], 'person': chain['person'], **edge,
                         'exceeds_threshold': edge['handoff_gap_days'] > threshold})
    return {'threshold_days': threshold, 'rows': rows,
            'summary': summary([row['handoff_gap_days'] for row in rows]),
            'exceeding': sum(row['exceeds_threshold'] for row in rows)}
