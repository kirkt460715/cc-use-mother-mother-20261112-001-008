from collections import defaultdict
from encounters.schema import STAGES
from encounters.values import integer, ratio, days, day


def stage_funnel(dataset, report, stages=None):
    stages = list(STAGES) if stages is None else stages
    if not isinstance(stages, list) or not stages or len(stages) != len(set(stages)) or set(stages) - set(STAGES):
        raise ValueError('ordered distinct stages required')
    cohorts = defaultdict(list)
    for chain in report['chains']['chains']:
        cohorts[chain['person']].append(chain)
    reached = [set() for stage in stages]
    examples = defaultdict(list)
    for person, chains in cohorts.items():
        for chain in chains:
            position = 0
            for stage in chain['stages']:
                if stage == stages[position]:
                    reached[position].add(person)
                    if position == len(stages) - 1:
                        examples[person].append(chain['id'])
                        break
                    position += 1
    rows = []
    first_count = len(reached[0])
    previous = first_count
    for index, stage in enumerate(stages):
        count = len(reached[index])
        if index and not reached[index] <= reached[index - 1]:
            raise ValueError('funnel reach sets do not nest')
        rows.append({'stage': stage, 'people': count,
                     'from_previous': ratio(count, previous), 'from_first': ratio(count, first_count)})
        previous = count
    return {'stages': stages, 'rows': rows, 'completed_chains_by_person': dict(sorted(examples.items())),
            'definition': 'ordered_subsequence_within_one_explicitly_linked_chain',
            'access': 'restricted_people_linkage'}


def monthly_requests(dataset, view):
    groups = defaultdict(lambda: {'requested': 0, 'accepted': 0, 'pending': 0, 'declined': 0,
                                  'cancelled': 0, 'people': set(), 'wait_sum': 0})
    for referral in view['referrals'].values():
        key = referral['requested'][:7], referral['destination_stage']
        counts = groups[key]
        counts['requested'] += 1
        counts[referral['status']] += 1
        counts['people'].add(referral['person'])
        if referral['status'] == 'accepted':
            counts['wait_sum'] += days(referral['requested'], view['visits'][referral['target']]['admitted'])
    rows = []
    for key, counts in sorted(groups.items()):
        people = counts.pop('people')
        rows.append({'month': key[0], 'destination_stage': key[1], 'people': len(people), **counts,
                     'completed_only_mean': ratio(counts['wait_sum'], counts['accepted'])})
    return {'rows': rows, 'cohort': 'month_of_request_not_month_of_admission',
            'warning': 'recent_cohorts_have_less_followup_do_not_compare_raw_acceptance_rates'}


def terminal_outcomes(dataset, view):
    groups = defaultdict(lambda: {'completed': 0, 'transfer': 0, 'death': 0, 'left': 0, 'open': 0, 'people': set()})
    for key, visit in view['visits'].items():
        stage = dataset.stage(key)
        group = groups[stage]
        group[visit['outcome'] or 'open'] += 1
        group['people'].add(visit['person'])
    return {'rows': [{'stage': stage, 'people': len(counts['people']),
                      **{key: value for key, value in counts.items() if key != 'people'}}
                     for stage, counts in sorted(groups.items())],
            'note': 'visit_level_outcomes_no_competing_risk_or_risk_adjustment'}


def orphan_visits(dataset, view):
    linked_targets = {row['target'] for row in view['referrals'].values() if row['target']}
    by_person = defaultdict(list)
    for key, visit in view['visits'].items():
        by_person[visit['person']].append(visit)
    rows = []
    for key, visit in sorted(view['visits'].items()):
        if key in linked_targets or dataset.stage(key) == 'acute':
            continue
        prior = [row for row in by_person[visit['person']] if row['id'] != key
                 and row['discharged'] is not None and row['discharged'] <= visit['admitted']]
        prior.sort(key=lambda row: (row['discharged'], row['id']), reverse=True)
        rows.append({'visit': key, 'person': visit['person'], 'stage': dataset.stage(key),
                     'possible_prior_visits': [row['id'] for row in prior[:5]],
                     'automatic_link_created': False})
    return {'rows': rows, 'reason': 'requires_explicit_referral_or_manual_source_correction'}
