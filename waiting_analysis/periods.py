from collections import defaultdict
from datetime import date, timedelta
from encounters.values import day, days, integer, ratio, digest, Invalid
from waiting_analysis.cohorts import analyze_rows
from pathways.observations import person_level


def boundaries(start, end, width=30):
    integer(width, 'width', 1, 365)
    first, last = day(start), day(end)
    if last < first or (last - first).days > 3660:
        raise Invalid('invalid period range')
    result = []
    cursor = first
    while cursor < last:
        stop = min(last, cursor + timedelta(days=width))
        result.append((cursor.isoformat(), stop.isoformat()))
        cursor = stop
    return result


def request_periods(rows, start, end, width=30, horizon=14):
    periods = boundaries(start, end, width)
    output = []
    for first, last in periods:
        selected = [row for row in rows if first <= row['requested'] < last]
        analysis = analyze_rows(selected, horizon, thresholds=[horizon], unit='person')
        output.append({'start': first, 'end_exclusive': last, 'people': analysis['people'],
                       'requests': analysis['requests'], 'events': analysis['curve']['events'],
                       'censored': analysis['curve']['censored'],
                       'completion': analysis['thresholds'][0],
                       'restricted_mean': analysis['restricted_mean']})
    return {'rows': output, 'request_window': [start, end], 'width_days': width,
            'horizon_days': horizon, 'cohort_assignment': 'request_date',
            'selection': 'first_request_per_person_within_each_period'}


def mature_service_levels(rows, as_of, start, end, width=30, horizon=14):
    integer(horizon, 'horizon', 0, 365)
    cutoff = day(as_of)
    output = []
    for first, last in boundaries(start, end, width):
        selected = [row for row in rows if first <= row['requested'] < last
                    and (cutoff - day(row['requested'])).days >= horizon]
        chosen = person_level(selected)['rows']
        completed = sum(row['event'] and row['duration'] <= horizon for row in chosen)
        output.append({'start': first, 'end_exclusive': last,
                       'eligible_people': len(chosen), 'completed_in_horizon': completed,
                       'completion': ratio(completed, len(chosen)),
                       'period_fully_mature': (cutoff - (day(last) - timedelta(days=1))).days >= horizon})
    return {'rows': output, 'as_of': as_of, 'horizon': horizon,
            'scope': 'accepted_and_pending_requests_only_not_all_referral_dispositions'}


def alarm_rules(period_report, minimum_people=10, maximum_waiting_fraction=50):
    integer(minimum_people, 'minimum_people', 2, 10000)
    integer(maximum_waiting_fraction, 'maximum_waiting_fraction', 0, 100)
    rows = []
    for period in period_report['rows']:
        denominator = period['eligible_people']
        numerator = period['completed_in_horizon']
        enough = denominator >= minimum_people and period['period_fully_mature']
        breach = enough and (denominator - numerator) * 100 > denominator * maximum_waiting_fraction
        rows.append({'start': period['start'], 'end_exclusive': period['end_exclusive'],
                     'eligible_people': denominator, 'evaluated': enough, 'breach': breach,
                     'reason': 'insufficient_mature_population' if not enough else 'waiting_fraction_exceeds_limit' if breach else 'within_limit'})
    return {'rows': rows, 'minimum_people': minimum_people,
            'maximum_waiting_fraction_pct': maximum_waiting_fraction,
            'action': 'review_data_and_capacity_no_automatic_patient_decision'}


def disposition_table(dataset, view, start, end, width=30):
    output = []
    for first, last in boundaries(start, end, width):
        counts = {'pending': 0, 'accepted': 0, 'declined': 0, 'cancelled': 0}
        people = set()
        for row in view['referrals'].values():
            if first <= row['requested'] < last:
                counts[row['status']] += 1
                people.add(row['person'])
        total = sum(counts.values())
        output.append({'start': first, 'end_exclusive': last, **counts,
                       'requests': total, 'people': len(people),
                       'declined_fraction': ratio(counts['declined'], total),
                       'cancelled_fraction': ratio(counts['cancelled'], total)})
    return {'rows': output, 'cutoff': view['as_of'], 'denominator': 'all_request_dispositions'}


def event_periods(dataset, view, start, end, width=30):
    output = []
    for first, last in boundaries(start, end, width):
        admissions = [row for row in view['visits'].values() if first <= row['admitted'] < last]
        discharges = [row for row in view['visits'].values() if row['discharged'] and first <= row['discharged'] < last]
        requests = [row for row in view['referrals'].values() if first <= row['requested'] < last]
        resolutions = [row for row in view['referrals'].values() if row['resolved'] and first <= row['resolved'] < last]
        output.append({'start': first, 'end_exclusive': last, 'admissions': len(admissions),
                       'discharges': len(discharges), 'requests': len(requests), 'resolutions': len(resolutions),
                       'net_admissions': len(admissions) - len(discharges)})
    return {'rows': output, 'assignment': 'each_event_own_date_not_request_cohort'}


def audit_periods(report):
    previous = None
    for row in report['rows']:
        if day(row['end_exclusive']) <= day(row['start']):
            raise Invalid('nonpositive period')
        if previous is not None and row['start'] != previous:
            raise Invalid('period partition discontinuity')
        previous = row['end_exclusive']
        if 'requests' in row and all(key in row for key in ('pending', 'accepted', 'declined', 'cancelled')):
            if sum(row[key] for key in ('pending', 'accepted', 'declined', 'cancelled')) != row['requests']:
                raise Invalid('disposition counts do not conserve requests')
    return {'valid': True, 'periods': len(report['rows'])}
