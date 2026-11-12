from fractions import Fraction
from math import exp, log, sqrt, isfinite
from encounters.values import fields, integer, ratio, Invalid


def validate(observations):
    if not isinstance(observations, list) or len(observations) > 100000:
        raise Invalid('bounded observations required')
    keys = set()
    for row in observations:
        fields(row, ('id', 'duration', 'event'))
        if not isinstance(row['id'], str) or not row['id'] or row['id'] in keys:
            raise Invalid('duplicate or invalid observation identity')
        keys.add(row['id'])
        integer(row['duration'], 'duration', 0, 36500)
        if type(row['event']) is not bool:
            raise Invalid('event must be boolean')
    return observations


def fraction(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def confidence(survival, greenwood, z=1.959963984540054):
    if survival == 0:
        return [0.0, 0.0]
    if survival == 1 or greenwood == 0:
        return [float(survival), float(survival)]
    value = float(survival)
    center = log(-log(value))
    error = sqrt(float(greenwood)) / abs(log(value))
    low = exp(-exp(min(700, center + z * error)))
    high = exp(-exp(max(-700, center - z * error)))
    return [low, high]


def kaplan_meier(observations):
    validate(observations)
    groups = {}
    for row in observations:
        group = groups.setdefault(row['duration'], {'events': 0, 'censored': 0})
        group['events' if row['event'] else 'censored'] += 1
    at_risk = len(observations)
    survival = Fraction(1)
    greenwood = Fraction(0)
    rows = []
    median = None
    for time, counts in sorted(groups.items()):
        events = counts['events']
        censored = counts['censored']
        if events > at_risk:
            raise Invalid('event count exceeds risk set')
        if events:
            survival *= Fraction(at_risk - events, at_risk)
            if at_risk > events:
                greenwood += Fraction(events, at_risk * (at_risk - events))
            if median is None and survival <= Fraction(1, 2):
                median = time
        rows.append({'time': time, 'at_risk': at_risk, 'events': events, 'censored': censored,
                     'survival': fraction(survival), 'completion': fraction(1 - survival),
                     'greenwood': fraction(greenwood), 'confidence_95': confidence(survival, greenwood)})
        at_risk -= events + censored
    if at_risk != 0:
        raise Invalid('risk sets do not conserve observations')
    return {'population': len(observations), 'events': sum(row['event'] for row in observations),
            'censored': sum(not row['event'] for row in observations), 'median_days': median,
            'rows': rows, 'ties': 'events_before_censoring',
            'assumption': 'independent_right_censoring_not_established_by_data'}


def survival_at(curve, time):
    integer(time, 'time', 0, 36500)
    value = Fraction(1)
    for row in curve['rows']:
        if row['time'] > time:
            break
        value = Fraction(**row['survival'])
    return value


def restricted_mean(curve, horizon):
    integer(horizon, 'horizon', 0, 36500)
    if curve['population'] == 0:
        return {'horizon': horizon, 'mean_days': None, 'supported': False}
    last_time = curve['rows'][-1]['time']
    supported = horizon <= last_time or survival_at(curve, last_time) == 0
    area = Fraction(0)
    previous = 0
    survival = Fraction(1)
    for row in curve['rows']:
        current = min(row['time'], horizon)
        if current >= previous:
            area += (current - previous) * survival
        if row['time'] >= horizon:
            previous = horizon
            break
        previous = current
        survival = Fraction(**row['survival'])
    if previous < horizon:
        area += (horizon - previous) * survival
    return {'horizon': horizon, 'mean_days': fraction(area) if supported else None,
            'supported': supported, 'reason': None if supported else 'followup_ends_before_horizon'}


def completion_thresholds(curve, thresholds):
    if not isinstance(thresholds, list) or len(thresholds) > 100:
        raise Invalid('bounded threshold array required')
    output = []
    last = curve['rows'][-1]['time'] if curve['rows'] else None
    for time in thresholds:
        integer(time, 'threshold', 0, 36500)
        supported = last is not None and (time <= last or survival_at(curve, last) == 0)
        output.append({'days': time, 'completion': fraction(1 - survival_at(curve, time)) if supported else None,
                       'supported': supported})
    return output


def logrank(left, right):
    validate(left)
    validate(right)
    times = sorted({row['duration'] for row in left + right if row['event']})
    observed = Fraction(0)
    expected = Fraction(0)
    variance = Fraction(0)
    for time in times:
        n1 = sum(row['duration'] >= time for row in left)
        n2 = sum(row['duration'] >= time for row in right)
        d1 = sum(row['duration'] == time and row['event'] for row in left)
        d2 = sum(row['duration'] == time and row['event'] for row in right)
        n, d = n1 + n2, d1 + d2
        observed += d1
        if n:
            expected += Fraction(d * n1, n)
        if n > 1:
            variance += Fraction(n1 * n2 * d * (n - d), n * n * (n - 1))
    statistic = (observed - expected) ** 2 / variance if variance else None
    return {'observed_left': fraction(observed), 'expected_left': fraction(expected),
            'variance': fraction(variance), 'chi_square': fraction(statistic) if statistic is not None else None,
            'interpretable': bool(left and right and variance),
            'note': 'unadjusted_descriptive_comparison_no_causal_claim'}


def verify(curve):
    at_risk = curve['population']
    previous_time = -1
    previous_survival = Fraction(1)
    events = censored = 0
    for row in curve['rows']:
        if row['time'] <= previous_time or row['at_risk'] != at_risk:
            raise Invalid('curve ordering or risk set invalid')
        value = Fraction(**row['survival'])
        if not 0 <= value <= previous_survival:
            raise Invalid('survival not monotonic')
        expected = previous_survival * Fraction(at_risk - row['events'], at_risk)
        if value != expected:
            raise Invalid('survival arithmetic mismatch')
        low, high = row['confidence_95']
        if not all(isfinite(item) for item in (low, high)) or not 0 <= low <= high <= 1:
            raise Invalid('confidence interval invalid')
        at_risk -= row['events'] + row['censored']
        events += row['events']
        censored += row['censored']
        previous_time = row['time']
        previous_survival = value
    if at_risk or events != curve['events'] or censored != curve['censored']:
        raise Invalid('curve count mismatch')
    return {'valid': True, 'population': events + censored}
