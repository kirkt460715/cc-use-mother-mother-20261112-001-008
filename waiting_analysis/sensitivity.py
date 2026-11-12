from collections import defaultdict
from fractions import Fraction
import random
from math import sqrt
from encounters.values import integer, ratio
from pathways.observations import person_level
from waiting_analysis.cohorts import analyze_rows
from waiting_analysis.survival import kaplan_meier, survival_at


def censoring_bounds(rows, horizon=14):
    integer(horizon, 'horizon', 0, 3650)
    selected = person_level(rows)['rows']
    completed = 0
    failed = 0
    unknown = 0
    for row in selected:
        if row['event']:
            if row['duration'] <= horizon:
                completed += 1
            else:
                failed += 1
        elif row['duration'] >= horizon:
            failed += 1
        else:
            unknown += 1
    total = len(selected)
    return {'horizon': horizon, 'people': total, 'known_completed': completed,
            'known_not_completed': failed, 'unknown': unknown,
            'lower_completion': ratio(completed, total),
            'upper_completion': ratio(completed + unknown, total),
            'interpretation': 'logical_bounds_without_independent_censoring_assumption'}


def bootstrap_people(rows, horizon=14, replicates=200, seed=0):
    integer(horizon, 'horizon', 0, 3650)
    integer(replicates, 'replicates', 20, 2000)
    integer(seed, 'seed', 0, 2 ** 32 - 1)
    selected = person_level(rows)['rows']
    if not selected:
        return {'replicates': replicates, 'seed': seed, 'interval': None, 'population': 0,
                'valid_replicates': 0}
    ordered = sorted(selected, key=lambda row: row['person'])
    generator = random.Random(seed)
    values = []
    for index in range(replicates):
        sampled = []
        for draw in range(len(ordered)):
            original = generator.choice(ordered)
            sampled.append({'id': str(index) + ':' + str(draw), 'duration': original['duration'], 'event': original['event']})
        curve = kaplan_meier(sampled)
        last = curve['rows'][-1]['time']
        if horizon > last and survival_at(curve, last) != 0:
            continue
        values.append(float(1 - survival_at(curve, horizon)))
    values.sort()
    if values:
        lower = values[int((len(values) - 1) * .025)]
        upper = values[int((len(values) - 1) * .975)]
        interval = [lower, upper]
    else:
        interval = None
    return {'replicates': replicates, 'seed': seed, 'interval': interval,
            'valid_replicates': len(values), 'population': len(selected),
            'method': 'percentile_resampling_first_request_per_person',
            'warning': 'small_samples_and_informative_censoring_can_invalidate_interval'}


def leave_one_facility(dataset, rows, horizon=14):
    facilities = sorted({dataset.visits[row['source']]['facility'] for row in rows})
    base = analyze_rows(rows, horizon, thresholds=[horizon], unit='person')
    results = []
    for facility in facilities:
        remaining = [row for row in rows if dataset.visits[row['source']]['facility'] != facility]
        result = analyze_rows(remaining, horizon, thresholds=[horizon], unit='person')
        results.append({'excluded_facility': facility, 'remaining_people': result['people'],
                        'completion': result['thresholds'][0]['completion'],
                        'restricted_mean': result['restricted_mean']})
    return {'baseline': base['thresholds'][0], 'rows': results,
            'note': 'sensitivity_to_source_facility_composition_not_facility_performance_ranking'}


def window_grid(rows, horizons):
    if not isinstance(horizons, list) or len(horizons) > 50:
        raise ValueError('bounded horizons required')
    output = []
    for horizon in horizons:
        integer(horizon, 'horizon', 0, 3650)
        result = analyze_rows(rows, horizon, thresholds=[horizon], unit='person')
        output.append({'horizon': horizon, 'completion': result['thresholds'][0],
                       'restricted_mean': result['restricted_mean'],
                       'logical_bounds': censoring_bounds(rows, horizon)})
    return {'rows': output, 'unit': 'person'}


def recording_cutoffs(dataset, as_of, cutoffs, horizon=14):
    from encounters.values import day
    from pathways.observations import referral_waits
    if not isinstance(cutoffs, list) or not cutoffs or len(cutoffs) > 50:
        raise ValueError('bounded knowledge cutoffs required')
    if cutoffs != sorted(set(cutoffs)):
        raise ValueError('cutoffs must be unique and ascending')
    rows = []
    previous = None
    for cutoff in cutoffs:
        view = dataset.slice(as_of, cutoff)
        waits = referral_waits(dataset, view)['rows']
        current = analyze_rows(waits, horizon, thresholds=[horizon], unit='person')
        rows.append({'known_at': cutoff, 'visits': len(view['visits']), 'referrals': len(waits),
                     'people': current['people'], 'completion': current['thresholds'][0],
                     'newly_observed_referrals': len(waits) - previous if previous is not None else None})
        previous = len(waits)
    return {'as_of': as_of, 'rows': rows, 'interpretation': 'reporting_lag_sensitivity'}
