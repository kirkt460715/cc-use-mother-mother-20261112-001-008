import copy
from fractions import Fraction
import json
from pathlib import Path
import unittest
from encounters.schema import Dataset
from encounters.values import digest
from pathways.chains import build, transitions, readmissions, overlaps
from pathways.observations import referral_waits, care_spells, completion_rates, person_level, gaps
from pathways.report import analyze, verify, compare_snapshots
from pathways.quality import assess, by_facility, recording_lag
from pathways.occupancy import daily, continuity_coverage, pathways_by_signature, bottlenecks
from pathways.timeline import person_timeline, stage_days, bed_turnover, cross_county
from pathways.funnels import stage_funnel, monthly_requests, terminal_outcomes, orphan_visits
from pathways.coverage import source_visibility, person_coverage, facility_coverage, destination_coverage, audit_observation_partition
from pathways.provenance import waiting_lineage, chain_lineage, evidence_index, verify_index
from waiting_analysis.survival import kaplan_meier, survival_at, restricted_mean, completion_thresholds, logrank, verify as verify_curve
from waiting_analysis.cohorts import analyze_rows, select, grouped, compare, standardize
from waiting_analysis.sensitivity import censoring_bounds, bootstrap_people, leave_one_facility, window_grid, recording_cutoffs
from waiting_analysis.periods import boundaries, request_periods, mature_service_levels, alarm_rules, disposition_table, event_periods, audit_periods
from waiting_analysis.plan import run as run_plan, verify as verify_plan


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/care.json').read_text())
        self.dataset = Dataset(self.data)
        self.view = self.dataset.slice('2026-11-12', '2026-11-12')
        self.report = analyze(self.dataset, '2026-11-12', '2026-11-12', horizon=7)
        self.rows = self.report['wait_observations']['rows']

    def test_chain_coverage_and_interval_conservation(self):
        chains = self.report['chains']
        self.assertEqual(chains['visits'], 8)
        self.assertEqual(chains['accepted_links'], 2)
        first = next(row for row in chains['chains'] if row['id'] == 'v1')
        self.assertEqual(first['visits'], ['v1', 'v2', 'v3'])
        self.assertEqual(first['stages'], ['acute', 'rehab', 'home'])
        self.assertEqual(first['care_days'], 8)
        self.assertEqual(first['gap_days'], 3)
        self.assertEqual(first['elapsed_days'], 11)
        self.assertFalse(first['closed'])
        self.assertEqual(first['backward_transitions'], 0)
        self.assertTrue(verify(self.dataset, self.report, True)['valid'])

    def test_pending_waits_censored_and_declined_excluded(self):
        waits = referral_waits(self.dataset, self.view)
        self.assertEqual([row['id'] for row in waits['rows']], ['r1', 'r2', 'r3'])
        self.assertEqual([row['duration'] for row in waits['rows']], [2, 1, 8])
        self.assertEqual([row['event'] for row in waits['rows']], [True, True, False])
        self.assertEqual({row['reason'] for row in waits['excluded']}, {'declined', 'cancelled'})
        selected = person_level(waits['rows'])
        self.assertEqual([row['id'] for row in selected['rows']], ['r1', 'r3'])
        self.assertEqual(selected['excluded_repeat_referrals'], ['r2'])
        self.assertEqual(self.report['waiting']['requests'], 2)
        self.assertEqual(self.report['waiting']['people'], 2)
        self.assertEqual(self.report['waiting']['curve']['censored'], 1)

    def test_kaplan_meier_ties_use_full_risk_set(self):
        observations = [{'id': 'a', 'duration': 2, 'event': True},
                        {'id': 'b', 'duration': 2, 'event': False},
                        {'id': 'c', 'duration': 4, 'event': True}]
        curve = kaplan_meier(observations)
        self.assertEqual(curve['rows'][0]['at_risk'], 3)
        self.assertEqual(Fraction(**curve['rows'][0]['survival']), Fraction(2, 3))
        self.assertEqual(curve['rows'][1]['at_risk'], 1)
        self.assertEqual(Fraction(**curve['rows'][1]['survival']), 0)
        self.assertEqual(curve['median_days'], 4)
        self.assertEqual(curve['events'], 2)
        self.assertEqual(curve['censored'], 1)
        self.assertTrue(verify_curve(curve)['valid'])
        self.assertEqual(survival_at(curve, 1), 1)
        self.assertEqual(survival_at(curve, 3), Fraction(2, 3))

    def test_zero_day_event_and_all_censored(self):
        zero = kaplan_meier([{'id': 'a', 'duration': 0, 'event': True}])
        self.assertEqual(zero['median_days'], 0)
        self.assertEqual(survival_at(zero, 0), 0)
        self.assertEqual(restricted_mean(zero, 30)['mean_days'], {'numerator': 0, 'denominator': 1})
        censored = kaplan_meier([{'id': 'a', 'duration': 3, 'event': False}])
        self.assertIsNone(censored['median_days'])
        self.assertEqual(survival_at(censored, 3), 1)
        self.assertFalse(restricted_mean(censored, 4)['supported'])
        self.assertIsNone(restricted_mean(censored, 4)['mean_days'])
        self.assertEqual(restricted_mean(censored, 3)['mean_days'], {'numerator': 3, 'denominator': 1})
        self.assertEqual(completion_thresholds(censored, [3, 4])[0]['completion']['numerator'], 0)
        self.assertIsNone(completion_thresholds(censored, [4])[0]['completion'])

    def test_empty_curve_and_invalid_observations(self):
        curve = kaplan_meier([])
        self.assertEqual(curve['population'], 0)
        self.assertEqual(curve['rows'], [])
        self.assertFalse(restricted_mean(curve, 7)['supported'])
        self.assertIsNone(completion_thresholds(curve, [0])[0]['completion'])
        for observations in ([{'id': 'a', 'duration': -1, 'event': True}],
                             [{'id': 'a', 'duration': 1, 'event': 1}],
                             [{'id': 'a', 'duration': 1, 'event': True}] * 2):
            with self.subTest(observations=observations):
                with self.assertRaises(ValueError):
                    kaplan_meier(observations)
        self.assertTrue(verify_curve(curve)['valid'])

    def test_restricted_mean_matches_piecewise_integral(self):
        observations = [{'id': 'a', 'duration': 2, 'event': True},
                        {'id': 'b', 'duration': 4, 'event': True}]
        curve = kaplan_meier(observations)
        self.assertEqual(Fraction(**restricted_mean(curve, 1)['mean_days']), 1)
        self.assertEqual(Fraction(**restricted_mean(curve, 3)['mean_days']), Fraction(5, 2))
        self.assertEqual(Fraction(**restricted_mean(curve, 4)['mean_days']), 3)
        self.assertEqual(Fraction(**restricted_mean(curve, 30)['mean_days']), 3)
        self.assertEqual(curve['rows'][-1]['confidence_95'], [0.0, 0.0])
        for row in curve['rows']:
            self.assertLessEqual(row['confidence_95'][0], row['confidence_95'][1])
        forged = copy.deepcopy(curve)
        forged['rows'][0]['survival']['numerator'] = 9
        with self.assertRaises(ValueError):
            verify_curve(forged)

    def test_logrank_symmetry_and_no_event_degeneracy(self):
        left = [{'id': 'a', 'duration': 1, 'event': True}, {'id': 'b', 'duration': 4, 'event': True}]
        right = [{'id': 'c', 'duration': 3, 'event': True}, {'id': 'd', 'duration': 4, 'event': False}]
        first, second = logrank(left, right), logrank(right, left)
        self.assertEqual(first['chi_square'], second['chi_square'])
        self.assertTrue(first['interpretable'])
        no_events = logrank([{'id': 'a', 'duration': 3, 'event': False}], [])
        self.assertFalse(no_events['interpretable'])
        self.assertIsNone(no_events['chi_square'])
        self.assertEqual(no_events['variance']['numerator'], 0)
        same = logrank(left, left)
        self.assertEqual(same['chi_square']['numerator'], 0)

    def test_group_comparison_requires_disjoint_people(self):
        result = compare(self.rows, {'age_band': ['older']}, {'age_band': ['adult']}, horizon=7)
        self.assertEqual(result['left']['people'], 1)
        self.assertEqual(result['right']['people'], 1)
        self.assertFalse(result['causal_interpretation'])
        with self.assertRaisesRegex(ValueError, 'overlap'):
            compare(self.rows, {}, {})
        groups = grouped(self.rows, 'age_band', horizon=7)
        self.assertEqual(len(groups['rows']), 2)
        self.assertEqual(select(self.rows, {'rural': [True]}), [])
        with self.assertRaises(ValueError):
            select(self.rows, {'rural': [1]})
        with self.assertRaises(ValueError):
            grouped(self.rows, 'facility')

    def test_standardization_does_not_renormalize_missing_strata(self):
        weights = [{'group': 'older', 'weight': 1}, {'group': 'adult', 'weight': 1}]
        result = standardize(self.rows, 'age_band', weights, horizon=7)
        self.assertEqual(Fraction(**result['completion']), Fraction(1, 2))
        self.assertEqual(result['missing_strata'], [])
        missing = standardize(self.rows, 'age_band', weights + [{'group': 'child', 'weight': 1}], horizon=7)
        self.assertEqual(missing['missing_strata'], ['child'])
        self.assertIsNone(missing['completion'])
        self.assertFalse(missing['renormalized_missing_strata'])
        with self.assertRaises(ValueError):
            standardize(self.rows, 'age_band', weights + weights)
        self.assertEqual(len(result['rows']), 2)

    def test_logical_bounds_and_reproducible_bootstrap(self):
        bounds = censoring_bounds(self.rows, 7)
        self.assertEqual(bounds['people'], 2)
        self.assertEqual(Fraction(**bounds['lower_completion']), Fraction(1, 2))
        self.assertEqual(bounds['lower_completion'], bounds['upper_completion'])
        short = copy.deepcopy(self.rows)
        short[-1]['duration'] = 1
        bounds = censoring_bounds(short, 7)
        self.assertEqual(bounds['unknown'], 1)
        self.assertEqual(Fraction(**bounds['upper_completion']), 1)
        result = bootstrap_people(self.rows, 7, 40, 12)
        self.assertEqual(result, bootstrap_people(self.rows, 7, 40, 12))
        self.assertEqual(result['valid_replicates'], 40)
        self.assertEqual(result['population'], 2)
        self.assertEqual(bootstrap_people([], 7, 20)['interval'], None)

    def test_knowledge_cutoff_and_facility_sensitivity(self):
        series = recording_cutoffs(self.dataset, '2026-11-04', ['2026-11-04', '2026-11-12'], horizon=1)
        self.assertEqual(len(series['rows']), 2)
        self.assertGreater(series['rows'][1]['referrals'], series['rows'][0]['referrals'])
        with self.assertRaises(ValueError):
            recording_cutoffs(self.dataset, '2026-11-04', ['2026-11-12', '2026-11-04'])
        facility = leave_one_facility(self.dataset, self.rows, horizon=7)
        self.assertGreaterEqual(len(facility['rows']), 1)
        grid = window_grid(self.rows, [1, 7, 30])
        self.assertEqual([row['horizon'] for row in grid['rows']], [1, 7, 30])
        self.assertIsNone(grid['rows'][-1]['completion']['completion'])
        self.assertEqual(grid['unit'], 'person')

    def test_mature_rates_include_cancelled_requests(self):
        rates = completion_rates(self.dataset, self.view, 7)
        rehab = next(row for row in rates['rows'] if row['to'] == 'rehab')
        self.assertEqual(rehab['requests'], 3)
        self.assertEqual(rehab['mature'], 3)
        self.assertEqual(rehab['completed_within'], 1)
        self.assertEqual(Fraction(**rehab['rate']), Fraction(1, 3))
        self.assertEqual(rehab['cancelled'], 1)
        spells = care_spells(self.dataset, self.view)
        self.assertEqual(len(spells['rows']), 8)
        death = next(row for row in spells['rows'] if row['id'] == 'v7')
        self.assertTrue(death['event'])
        self.assertIn('not_a_recovery', spells['note'])

    def test_readmission_denominator_requires_full_window(self):
        result = readmissions(self.dataset, self.view, window=7)
        index = next(row for row in result['rows'] if row['index_visit'] == 'v5')
        self.assertTrue(index['readmitted'])
        self.assertEqual(index['next_visit'], 'v6')
        self.assertEqual(index['gap_days'], 6)
        self.assertTrue(index['fully_observed'])
        late = next(row for row in result['rows'] if row['index_visit'] == 'v6')
        self.assertFalse(late['fully_observed'])
        self.assertEqual(result['denominator'], 1)
        self.assertEqual(result['numerator'], 1)
        self.assertEqual(overlaps(self.dataset, self.view), [])

    def test_occupancy_sweep_equals_direct_interval_sum(self):
        result = daily(self.dataset, self.view, '2026-11-01', '2026-11-12')
        from encounters.values import days
        expected = sum(days(row['admitted'], row['discharged'] or '2026-11-12') for row in self.view['visits'].values())
        self.assertEqual(result['occupied_day_sum'], expected)
        rehab = [row for row in result['rows'] if row['stage'] == 'rehab']
        self.assertEqual(len(rehab), 11)
        self.assertEqual(next(row for row in rehab if row['date'] == '2026-11-05')['occupied'], 1)
        self.assertEqual(next(row for row in rehab if row['date'] == '2026-11-08')['occupied'], 1)
        with self.assertRaises(ValueError):
            daily(self.dataset, self.view, '2026-11-01', '2026-11-13')
        self.assertTrue(all(row['occupied'] >= 0 for row in result['rows']))

    def test_timeline_conserves_care_gap_and_overlap_days(self):
        timeline = person_timeline(self.dataset, self.view, 'p1', '2026-11-01', '2026-11-12')
        self.assertEqual(sum(row['days'] for row in timeline['segments']), 11)
        self.assertEqual(sum(row['days'] for row in timeline['segments'] if row['state'] == 'unobserved'), 3)
        self.assertTrue(timeline['unobserved_is_not_proof_of_no_care'])
        total = stage_days(self.dataset, self.view, '2026-11-01', '2026-11-12')
        self.assertEqual(total['people'], 5)
        self.assertTrue(total['sum_across_stages_may_double_count_overlap'])
        turnover = bed_turnover(self.dataset, self.view, '2026-11-01', '2026-11-12')
        self.assertEqual(sum(row['admissions'] for row in turnover['rows']), 8)
        self.assertEqual(sum(row['discharges'] for row in turnover['rows']), 6)
        self.assertEqual(sum(row['visits'] for row in cross_county(self.dataset, self.view)['rows']), 8)

    def test_funnel_coverage_and_unlinked_path_reporting(self):
        funnel = stage_funnel(self.dataset, self.report, ['acute', 'rehab', 'home'])
        self.assertEqual([row['people'] for row in funnel['rows']], [4, 1, 1])
        self.assertEqual(funnel['completed_chains_by_person'], {'p1': ['v1']})
        coverage = continuity_coverage(self.dataset, self.view)
        acute = next(row for row in coverage['rows'] if row['stage'] == 'acute')
        self.assertEqual(acute['transfer_discharges'], 2)
        self.assertEqual(acute['linked_continuations'], 1)
        self.assertEqual(acute['unlinked'], ['v4'])
        orphans = orphan_visits(self.dataset, self.view)
        self.assertIn('v6', [row['visit'] for row in orphans['rows']])
        self.assertFalse(orphans['rows'][0]['automatic_link_created'])
        self.assertEqual(bottlenecks(self.dataset, self.view, 7)['rows'][0]['referral'], 'r3')

    def test_data_quality_and_recording_lag_are_observable(self):
        quality = assess(self.dataset, self.view)
        self.assertIn('transfer_without_accepted_continuation', quality['counts'])
        self.assertFalse(quality['automatic_exclusion'])
        grouped_quality = by_facility(self.dataset, self.view, quality)
        self.assertEqual(sum(row['visits'] for row in grouped_quality), 8)
        lag = recording_lag(self.dataset, '2026-11-12', '2026-11-12')
        self.assertEqual(lag['rows'][0]['records'], 8)
        for row in lag['rows']:
            self.assertEqual(row['same_day'] + row['one_to_seven'] + row['over_seven'], row['records'])
        monthly = monthly_requests(self.dataset, self.view)
        self.assertEqual(sum(row['requested'] for row in monthly['rows']), 5)
        outcomes = terminal_outcomes(self.dataset, self.view)
        self.assertEqual(sum(row['death'] for row in outcomes['rows']), 1)

    def test_provenance_replays_exact_input_terms(self):
        wait = waiting_lineage(self.dataset, self.report, 'r1')
        self.assertEqual(wait['formula'], 'target_admission_minus_request')
        self.assertEqual(wait['observation']['duration'], 2)
        self.assertFalse(wait['excluded_as_repeated_person'])
        repeat = waiting_lineage(self.dataset, self.report, 'r2')
        self.assertTrue(repeat['excluded_as_repeated_person'])
        chain = chain_lineage(self.dataset, self.report, 'v1')
        self.assertEqual(sum(row['days'] for row in chain['care_day_terms']), 8)
        self.assertEqual(sum(row['days'] for row in chain['gap_day_terms']), 3)
        index = evidence_index(self.dataset, self.report)
        self.assertTrue(verify_index(self.dataset, self.report, index)['valid'])
        index['waiting_evidence'].pop()
        with self.assertRaises(ValueError):
            verify_index(self.dataset, self.report, index)

    def test_coverage_partition_and_visible_source_counts(self):
        audit = audit_observation_partition(self.dataset, self.report)
        self.assertEqual(audit, {'valid': True, 'included': 3, 'excluded': 2})
        visibility = source_visibility(self.dataset, '2026-11-04', '2026-11-04')
        self.assertEqual(visibility['rows'][0]['visible'], 5)
        self.assertIn('r1', visibility['rows'][1]['future_knowledge'])
        people = person_coverage(self.dataset, self.view)
        self.assertEqual(people['observed_people'], 5)
        facilities = facility_coverage(self.dataset, self.view, '2026-11-01', '2026-11-12')
        self.assertEqual(sum(row['admissions'] for row in facilities['rows']), 8)
        destinations = destination_coverage(self.dataset, self.view)
        self.assertEqual(sum(row['requests'] for row in destinations['rows']), 5)
        self.assertEqual(len(destinations['rows']), 4)

    def test_period_partition_maturity_and_alarm_thresholds(self):
        periods = boundaries('2026-11-01', '2026-11-12', 5)
        self.assertEqual(len(periods), 3)
        self.assertEqual(periods[-1], ('2026-11-11', '2026-11-12'))
        rates = mature_service_levels(self.rows, '2026-11-12', '2026-11-01', '2026-11-12', 5, 7)
        self.assertEqual(rates['rows'][0]['eligible_people'], 2)
        self.assertEqual(rates['rows'][0]['completed_in_horizon'], 1)
        alarms = alarm_rules(rates, 2, 40)
        self.assertTrue(alarms['rows'][0]['breach'])
        self.assertFalse(alarms['rows'][1]['evaluated'])
        dispositions = disposition_table(self.dataset, self.view, '2026-11-01', '2026-11-12', 5)
        self.assertTrue(audit_periods(dispositions)['valid'])
        self.assertEqual(sum(row['requests'] for row in dispositions['rows']), 5)
        events = event_periods(self.dataset, self.view, '2026-11-01', '2026-11-12', 5)
        self.assertEqual(sum(row['admissions'] for row in events['rows']), 8)

    def test_analysis_plan_atomic_validation_and_replay(self):
        definition = {'version': 1, 'operations': [
            {'id': 'bounds', 'kind': 'censoring-bounds', 'options': {'horizon': 7}},
            {'id': 'funnel', 'kind': 'funnel', 'options': {'stages': ['acute', 'rehab', 'home']}},
            {'id': 'periods', 'kind': 'period-dispositions', 'options': {'start': '2026-11-01', 'end': '2026-11-12'}}]}
        execution = run_plan(self.dataset, self.report, definition)
        self.assertTrue(execution['all_operations_completed'])
        self.assertEqual(len(execution['results']), 3)
        self.assertTrue(verify_plan(self.dataset, self.report, execution)['valid'])
        changed = copy.deepcopy(execution)
        changed['results'][0]['result']['people'] = 900
        with self.assertRaises(ValueError):
            verify_plan(self.dataset, self.report, changed)
        definition['operations'].append(definition['operations'][0])
        with self.assertRaises(ValueError):
            run_plan(self.dataset, self.report, definition)

    def test_report_checksum_and_restatement_semantics(self):
        early = analyze(self.dataset, '2026-11-04', '2026-11-04', horizon=1)
        later = analyze(self.dataset, '2026-11-04', '2026-11-12', horizon=1)
        restated = compare_snapshots(early, later)
        self.assertEqual(restated['population_delta']['referrals'], 1)
        self.assertEqual(restated['changed_observations'][0]['referral'], 'r1')
        with self.assertRaises(ValueError):
            compare_snapshots(later, early)
        with self.assertRaises(ValueError):
            compare_snapshots(early, self.report)
        tampered = copy.deepcopy(self.report)
        tampered['population']['visits'] = 9
        with self.assertRaises(ValueError):
            verify(self.dataset, tampered)
        self.assertTrue(verify(self.dataset, self.report, True)['replayed'])
