from encounters.values import day, digest, integer, fields
from pathways.chains import build, transitions, readmissions, overlaps
from pathways.observations import referral_waits, care_spells, completion_rates, gaps
from pathways.quality import assess, by_facility, recording_lag
from waiting_analysis.cohorts import analyze_rows, grouped
from waiting_analysis.survival import kaplan_meier, verify as verify_curve


def analyze(dataset, as_of, known_at, horizon=14, readmission_window=30, gap_threshold=7, unit='person'):
    integer(horizon, 'horizon', 0, 3650)
    integer(readmission_window, 'readmission_window', 1, 365)
    integer(gap_threshold, 'gap_threshold', 0, 365)
    view = dataset.slice(as_of, known_at)
    chains = build(dataset, view)
    waiting = referral_waits(dataset, view)
    quality = assess(dataset, view)
    spells = care_spells(dataset, view)
    spell_observations = [{key: row[key] for key in ('id', 'duration', 'event')} for row in spells['rows']]
    result = {
        'version': 1,
        'input_digest': dataset.fingerprint,
        'options': {'as_of': as_of, 'known_at': known_at, 'horizon': horizon,
                    'readmission_window': readmission_window, 'gap_threshold': gap_threshold, 'unit': unit},
        'population': {'people': len({row['person'] for row in view['visits'].values()}),
                       'visits': len(view['visits']), 'referrals': len(view['referrals']),
                       'chains': len(chains['chains'])},
        'chains': chains,
        'transitions': transitions(dataset, view),
        'wait_observations': waiting,
        'waiting': analyze_rows(waiting['rows'], horizon, unit=unit),
        'by_county': grouped(waiting['rows'], 'county', horizon, unit),
        'by_destination': grouped(waiting['rows'], 'destination_stage', horizon, unit),
        'completion_rates': completion_rates(dataset, view, min(horizon, 365)),
        'care_spells': {'curve': kaplan_meier(spell_observations), 'definition': spells['note']},
        'readmissions': readmissions(dataset, view, readmission_window),
        'handoff_gaps': gaps(chains, gap_threshold),
        'quality': quality,
        'facility_quality': by_facility(dataset, view, quality),
        'recording_lag': recording_lag(dataset, as_of, known_at),
        'limits': ['explicit_identity_links_only', 'descriptive_not_clinical_recommendation',
                   'pending_referrals_right_censored', 'declined_and_cancelled_excluded_from_wait_curve',
                   'terminal_outcomes_require_quality_review', 'no_competing_risk_estimation'],
    }
    return {**result, 'report_digest': digest(result)}


def verify(dataset, report, replay=False):
    if report['version'] != 1 or report['input_digest'] != dataset.fingerprint:
        raise ValueError('report source binding mismatch')
    signature = digest({key: value for key, value in report.items() if key != 'report_digest'})
    if signature != report['report_digest']:
        raise ValueError('report checksum mismatch')
    from pathways.coverage import audit_observation_partition
    audit_observation_partition(dataset, report)
    options = report['options']
    if options['unit'] not in ('person', 'request'):
        raise ValueError('invalid report unit')
    integer(options['horizon'], 'horizon', 0, 3650)
    integer(options['readmission_window'], 'readmission_window', 1, 365)
    integer(options['gap_threshold'], 'gap_threshold', 0, 365)
    view = dataset.slice(options['as_of'], options['known_at'])
    expected_people = len({row['person'] for row in view['visits'].values()})
    if report['population']['people'] != expected_people:
        raise ValueError('report people count differs from selected source')
    if report['population']['referrals'] != len(view['referrals']):
        raise ValueError('report request count differs from selected source')
    if report['population']['chains'] != len(report['chains']['chains']):
        raise ValueError('report chain count differs from details')
    verify_curve(report['waiting']['curve'])
    verify_curve(report['care_spells']['curve'])
    for grouping in ('by_county', 'by_destination'):
        for group in report[grouping]['rows']:
            verify_curve(group['analysis']['curve'])
    chains = report['chains']['chains']
    visits = [key for chain in chains for key in chain['visits']]
    if len(visits) != len(set(visits)) or len(visits) != report['population']['visits']:
        raise ValueError('chain visit coverage mismatch')
    if sum(len(chain['edges']) for chain in chains) != report['chains']['accepted_links']:
        raise ValueError('chain edge count mismatch')
    for chain in chains:
        signature = digest({key: value for key, value in chain.items() if key != 'chain_digest'})
        if signature != chain['chain_digest'] or chain['care_days'] + chain['gap_days'] != chain['elapsed_days']:
            raise ValueError('chain evidence arithmetic failure')
    if replay and analyze(dataset, **report['options']) != report:
        raise ValueError('report semantic replay mismatch')
    return {'valid': True, 'replayed': replay, **report['population']}


def compare_snapshots(before, after):
    if before['options']['as_of'] != after['options']['as_of']:
        raise ValueError('historical comparison requires same business cutoff')
    if before['options']['known_at'] > after['options']['known_at']:
        raise ValueError('knowledge cannot move backwards')
    left = {row['id']: row for row in before['wait_observations']['rows']}
    right = {row['id']: row for row in after['wait_observations']['rows']}
    changes = []
    for key in sorted(left.keys() | right.keys()):
        if left.get(key) != right.get(key):
            changes.append({'referral': key, 'before': left.get(key), 'after': right.get(key)})
    return {'before': before['report_digest'], 'after': after['report_digest'],
            'changed_observations': changes,
            'population_delta': {key: after['population'][key] - value for key, value in before['population'].items()},
            'note': 'knowledge_restatement_not_service_performance_change'}
