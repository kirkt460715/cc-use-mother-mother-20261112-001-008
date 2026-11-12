from collections import defaultdict
from fractions import Fraction
from encounters.values import integer, fields, ratio, digest
from pathways.observations import referral_waits, person_level, summary
from waiting_analysis.survival import kaplan_meier, restricted_mean, completion_thresholds, logrank


DIMENSIONS = {'county', 'age_band', 'rural', 'source_stage', 'destination_stage'}


def select(rows, filters=None):
    filters = {} if filters is None else filters
    if not isinstance(filters, dict) or filters.keys() - DIMENSIONS:
        raise ValueError('unsupported cohort filter')
    for key, values in filters.items():
        if not isinstance(values, list) or not values or len(values) > 100:
            raise ValueError('filter must be bounded nonempty array')
        for value in values:
            if key == 'rural' and type(value) is not bool:
                raise ValueError('rural filter requires boolean')
            if key != 'rural' and not isinstance(value, str):
                raise ValueError('categorical filter requires text')
    return [row for row in rows if all(row[key] in values for key, values in filters.items())]


def analyze_rows(rows, horizon=14, thresholds=None, unit='request'):
    integer(horizon, 'horizon', 0, 3650)
    if unit not in ('request', 'person'):
        raise ValueError('unsupported analysis unit')
    selected = person_level(rows) if unit == 'person' else {'rows': rows, 'excluded_repeat_referrals': []}
    rows = selected['rows']
    observations = [{key: row[key] for key in ('id', 'duration', 'event')} for row in rows]
    curve = kaplan_meier(observations)
    completed = [row['duration'] for row in rows if row['event']]
    return {'unit': unit, 'people': len({row['person'] for row in rows}),
            'requests': len(rows), 'curve': curve,
            'restricted_mean': restricted_mean(curve, horizon),
            'thresholds': completion_thresholds(curve, [7, 14, 30] if thresholds is None else thresholds),
            'completed_wait_summary': summary(completed),
            'completed_only_mean_is_biased_under_censoring': True,
            'excluded_repeat_referrals': selected['excluded_repeat_referrals'],
            'repeated_people_present': len({row['person'] for row in rows}) < len(rows)}


def grouped(rows, dimension, horizon=14, unit='request'):
    if dimension not in DIMENSIONS:
        raise ValueError('unsupported grouping dimension')
    buckets = defaultdict(list)
    for row in rows:
        buckets[row[dimension]].append(row)
    return {'dimension': dimension, 'rows': [
        {'group': value, 'analysis': analyze_rows(items, horizon, unit=unit)}
        for value, items in sorted(buckets.items(), key=lambda item: str(item[0]))]}


def compare(rows, left, right, horizon=14, unit='person'):
    first, second = select(rows, left), select(rows, right)
    first_people = {row['person'] for row in first}
    second_people = {row['person'] for row in second}
    overlap = first_people & second_people
    if overlap:
        raise ValueError('comparison cohorts overlap by person')
    if unit == 'person':
        first, second = person_level(first)['rows'], person_level(second)['rows']
    elif unit != 'request':
        raise ValueError('invalid comparison unit')
    left_result = analyze_rows(first, horizon, unit=unit)
    right_result = analyze_rows(second, horizon, unit=unit)
    projection = lambda values: [{key: row[key] for key in ('id', 'duration', 'event')} for row in values]
    left_mean = left_result['restricted_mean']['mean_days']
    right_mean = right_result['restricted_mean']['mean_days']
    difference = None
    if left_mean is not None and right_mean is not None:
        value = Fraction(**left_mean) - Fraction(**right_mean)
        difference = {'numerator': value.numerator, 'denominator': value.denominator}
    return {'left': left_result, 'right': right_result, 'restricted_mean_difference': difference,
            'logrank': logrank(projection(first), projection(second)),
            'independence_warning': unit == 'request' and any(result['repeated_people_present'] for result in (left_result, right_result)),
            'causal_interpretation': False}


def standardize(rows, dimension, weights, horizon=14):
    if dimension not in DIMENSIONS:
        raise ValueError('unsupported standardization dimension')
    if not isinstance(weights, list) or not weights:
        raise ValueError('reference weights required')
    checked = {}
    total = 0
    for record in weights:
        fields(record, ('group', 'weight'))
        key = record['group']
        if key in checked:
            raise ValueError('duplicate standardization stratum')
        integer(record['weight'], 'weight', 1)
        checked[key] = record['weight']
        total += record['weight']
    buckets = defaultdict(list)
    for row in rows:
        buckets[row[dimension]].append(row)
    contributions = []
    missing = []
    estimate = Fraction(0)
    for key, weight in checked.items():
        analysis = analyze_rows(buckets[key], horizon, thresholds=[horizon], unit='person')
        value = analysis['thresholds'][0]['completion']
        if value is None:
            missing.append(key)
        else:
            estimate += Fraction(weight, total) * Fraction(**value)
        contributions.append({'group': key, 'weight': weight, 'completion': value,
                              'population': analysis['requests']})
    return {'dimension': dimension, 'rows': contributions, 'missing_strata': missing,
            'horizon_days': horizon, 'completion': None if missing else ratio(estimate.numerator, estimate.denominator),
            'renormalized_missing_strata': False}
