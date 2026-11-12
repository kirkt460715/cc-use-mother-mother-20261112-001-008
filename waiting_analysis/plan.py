from encounters.values import fields, identifier, integer, records, digest, Conflict, Invalid
from waiting_analysis.cohorts import select, analyze_rows, grouped, compare, standardize
from waiting_analysis.sensitivity import censoring_bounds, bootstrap_people, leave_one_facility, window_grid, recording_cutoffs
from pathways.funnels import stage_funnel, monthly_requests, terminal_outcomes, orphan_visits
from pathways.timeline import person_timeline, stage_days, bed_turnover, cross_county
from pathways.provenance import waiting_lineage, chain_lineage, evidence_index


KINDS = {'cohort', 'group', 'compare', 'standardize', 'censoring-bounds', 'bootstrap',
         'leave-facility', 'windows', 'recording-cutoffs', 'funnel', 'monthly', 'outcomes',
         'orphans', 'person-timeline', 'stage-days', 'turnover', 'cross-county',
         'waiting-lineage', 'chain-lineage', 'evidence-index', 'period-requests', 'period-mature', 'period-dispositions', 'period-events', 'period-alarms', 'source-visibility', 'person-coverage', 'facility-coverage', 'destination-coverage', 'partition-audit'}


def execute_one(dataset, report, kind, options):
    rows = report['wait_observations']['rows']
    view = dataset.slice(report['options']['as_of'], report['options']['known_at'])
    if kind in ('source-visibility', 'person-coverage', 'facility-coverage', 'destination-coverage', 'partition-audit'):
        from pathways import coverage
        if kind == 'source-visibility':
            fields(options, ())
            return coverage.source_visibility(dataset, view['as_of'], view['known_at'])
        if kind == 'person-coverage':
            fields(options, ())
            return coverage.person_coverage(dataset, view)
        if kind == 'facility-coverage':
            fields(options, ('start', 'end'))
            return coverage.facility_coverage(dataset, view, **options)
        if kind == 'destination-coverage':
            fields(options, ())
            return coverage.destination_coverage(dataset, view)
        if kind == 'partition-audit':
            fields(options, ())
            return coverage.audit_observation_partition(dataset, report)
    if kind.startswith('period-'):
        from waiting_analysis import periods
        if kind == 'period-requests':
            fields(options, ('start', 'end'), ('width', 'horizon'))
            return periods.request_periods(rows, **options)
        if kind == 'period-mature':
            fields(options, ('start', 'end'), ('width', 'horizon'))
            return periods.mature_service_levels(rows, view['as_of'], **options)
        if kind == 'period-dispositions':
            fields(options, ('start', 'end'), ('width',))
            return periods.disposition_table(dataset, view, **options)
        if kind == 'period-events':
            fields(options, ('start', 'end'), ('width',))
            return periods.event_periods(dataset, view, **options)
        if kind == 'period-alarms':
            fields(options, ('start', 'end'), ('width', 'horizon', 'minimum_people', 'maximum_waiting_fraction'))
            temporal = {key: value for key, value in options.items() if key in ('start', 'end', 'width', 'horizon')}
            mature = periods.mature_service_levels(rows, view['as_of'], **temporal)
            return periods.alarm_rules(mature, options.get('minimum_people', 10), options.get('maximum_waiting_fraction', 50))
    if kind == 'cohort':
        fields(options, (), ('filters', 'horizon', 'unit'))
        return analyze_rows(select(rows, options.get('filters')), options.get('horizon', 14), unit=options.get('unit', 'person'))
    if kind == 'group':
        fields(options, ('dimension',), ('horizon', 'unit'))
        return grouped(rows, **options)
    if kind == 'compare':
        fields(options, ('left', 'right'), ('horizon', 'unit'))
        return compare(rows, **options)
    if kind == 'standardize':
        fields(options, ('dimension', 'weights'), ('horizon',))
        return standardize(rows, **options)
    if kind == 'censoring-bounds':
        fields(options, (), ('horizon',))
        return censoring_bounds(rows, **options)
    if kind == 'bootstrap':
        fields(options, (), ('horizon', 'replicates', 'seed'))
        return bootstrap_people(rows, **options)
    if kind == 'leave-facility':
        fields(options, (), ('horizon',))
        return leave_one_facility(dataset, rows, **options)
    if kind == 'windows':
        fields(options, ('horizons',))
        return window_grid(rows, **options)
    if kind == 'recording-cutoffs':
        fields(options, ('cutoffs',), ('horizon',))
        return recording_cutoffs(dataset, report['options']['as_of'], **options)
    if kind == 'funnel':
        fields(options, (), ('stages',))
        return stage_funnel(dataset, report, **options)
    if kind in ('monthly', 'outcomes', 'orphans', 'cross-county'):
        fields(options, ())
        function = {'monthly': monthly_requests, 'outcomes': terminal_outcomes,
                    'orphans': orphan_visits, 'cross-county': cross_county}[kind]
        return function(dataset, view)
    if kind == 'person-timeline':
        fields(options, ('person', 'start', 'end'))
        return person_timeline(dataset, view, **options)
    if kind in ('stage-days', 'turnover'):
        fields(options, ('start', 'end'))
        return (stage_days if kind == 'stage-days' else bed_turnover)(dataset, view, **options)
    if kind == 'waiting-lineage':
        fields(options, ('referral_id',))
        return waiting_lineage(dataset, report, **options)
    if kind == 'chain-lineage':
        fields(options, ('root',))
        return chain_lineage(dataset, report, **options)
    if kind == 'evidence-index':
        fields(options, ())
        return evidence_index(dataset, report)
    raise Invalid('unknown analysis operation')


def run(dataset, report, definition):
    fields(definition, ('version', 'operations'))
    if definition['version'] != 1 or type(definition['version']) is not int:
        raise Invalid('unsupported plan version')
    records(definition['operations'], 'operations', 100)
    keys = set()
    checked = []
    for operation in definition['operations']:
        fields(operation, ('id', 'kind', 'options'))
        identifier(operation['id'], 'operation.id')
        if operation['id'] in keys:
            raise Invalid('duplicate analysis operation id')
        keys.add(operation['id'])
        if operation['kind'] not in KINDS:
            raise Invalid('unsupported analysis operation')
        if not isinstance(operation['options'], dict):
            raise Invalid('operation options must be object')
        checked.append(operation)
    output = []
    for operation in checked:
        result = execute_one(dataset, report, operation['kind'], operation['options'])
        output.append({'id': operation['id'], 'kind': operation['kind'], 'result': result,
                       'result_digest': digest(result)})
    body = {'version': 1, 'input_digest': dataset.fingerprint,
            'report_digest': report['report_digest'], 'plan_digest': digest(definition),
            'results': output, 'definition': definition,
            'all_operations_completed': True}
    return {**body, 'execution_digest': digest(body)}


def verify(dataset, report, execution, replay=True):
    body = {key: value for key, value in execution.items() if key != 'execution_digest'}
    if digest(body) != execution['execution_digest']:
        raise Invalid('analysis execution checksum mismatch')
    if execution['input_digest'] != dataset.fingerprint or execution['report_digest'] != report['report_digest']:
        raise Invalid('analysis execution source binding mismatch')
    for row in execution['results']:
        if digest(row['result']) != row['result_digest']:
            raise Invalid('operation result checksum mismatch')
    if replay and run(dataset, report, execution['definition']) != execution:
        raise Invalid('analysis execution replay mismatch')
    return {'valid': True, 'operations': len(execution['results']), 'replayed': replay}
