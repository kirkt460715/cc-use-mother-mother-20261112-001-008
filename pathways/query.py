import base64
import csv
import io
import json
from encounters.values import digest, canonical, fields, integer, Invalid
from pathways.observations import referral_waits, care_spells
from pathways.occupancy import daily, continuity_coverage, pathways_by_signature, bottlenecks
from waiting_analysis.cohorts import select, analyze_rows, grouped, compare, standardize
from exports.disclosure import public_report


PROJECTIONS = {
    'chains': {'id', 'person', 'visits', 'stages', 'start', 'end', 'closed', 'outcome', 'care_days', 'gap_days', 'elapsed_days', 'stage_signature', 'chain_digest'},
    'waits': {'id', 'person', 'duration', 'event', 'source_stage', 'destination_stage', 'county', 'age_band', 'rural', 'requested', 'end'},
    'issues': {'kind', 'entity', 'person', 'days', 'severity'},
}
FILTERS = {'chains': {'person', 'closed', 'outcome', 'stage_signature'},
           'waits': {'person', 'event', 'source_stage', 'destination_stage', 'county', 'age_band', 'rural'},
           'issues': {'kind', 'person', 'severity'}}


def cursor_encode(context, offset):
    return base64.urlsafe_b64encode(canonical({'context': context, 'offset': offset}).encode()).decode().rstrip('=')


def cursor_decode(value):
    if not isinstance(value, str) or len(value) > 5000:
        raise Invalid('invalid cursor')
    try:
        body = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
    except (ValueError, UnicodeError) as error:
        raise Invalid('malformed cursor') from error
    fields(body, ('context', 'offset'))
    return body


def page(report, table, filters=None, columns=None, limit=100, cursor=None):
    if table not in PROJECTIONS:
        raise Invalid('unsupported query table')
    filters = {} if filters is None else filters
    columns = sorted(PROJECTIONS[table]) if columns is None else columns
    if not isinstance(filters, dict) or filters.keys() - FILTERS[table]:
        raise Invalid('unsupported query filters')
    if not isinstance(columns, list) or not columns or len(columns) != len(set(columns)):
        raise Invalid('projection requires unique nonempty columns')
    if set(columns) - PROJECTIONS[table]:
        raise Invalid('unknown projection column')
    integer(limit, 'limit', 1, 1000)
    for key, values in filters.items():
        if not isinstance(values, list) or len(values) > 100:
            raise Invalid('filter values must be bounded arrays')
        for value in values:
            if key in ('closed', 'event', 'rural'):
                if type(value) is not bool:
                    raise Invalid('boolean filter required')
            elif value is not None and not isinstance(value, str):
                raise Invalid('categorical filter required')
    source = {'chains': report['chains']['chains'], 'waits': report['wait_observations']['rows'],
              'issues': report['quality']['issues']}[table]
    selected = [row for row in source if all(row.get(key) in values for key, values in filters.items())]
    selected.sort(key=lambda row: canonical(row))
    context = digest({'report': report['report_digest'], 'table': table, 'filters': filters, 'columns': columns})
    offset = 0
    if cursor is not None:
        decoded = cursor_decode(cursor)
        if decoded['context'] != context:
            raise Invalid('cursor belongs to another query')
        offset = integer(decoded['offset'], 'offset', 0, len(selected))
    result = [{key: row.get(key) for key in columns} for row in selected[offset:offset + limit]]
    end = offset + len(result)
    return {'rows': result, 'total': len(selected), 'next_cursor': cursor_encode(context, end) if end < len(selected) else None}


def person_detail(dataset, report, key):
    if key not in dataset.people:
        raise KeyError(key)
    view = dataset.slice(report['options']['as_of'], report['options']['known_at'])
    chains = [row for row in report['chains']['chains'] if row['person'] == key]
    visits = [row for row in view['visits'].values() if row['person'] == key]
    referrals = [row for row in view['referrals'].values() if row['person'] == key]
    return {'person': dataset.people[key], 'chains': chains,
            'visits': sorted(visits, key=lambda row: (row['admitted'], row['id'])),
            'referrals': sorted(referrals, key=lambda row: (row['requested'], row['id'])),
            'issues': [row for row in report['quality']['issues'] if row['person'] == key],
            'access': 'restricted_identifiable'}


def dispatch(dataset, report, kind, options=None):
    options = {} if options is None else options
    if kind == 'analysis-plan':
        from waiting_analysis.plan import run
        return run(dataset, report, options)
    if kind in PROJECTIONS:
        fields(options, (), ('filters', 'columns', 'limit', 'cursor'))
        return page(report, kind, **options)
    if kind == 'person':
        fields(options, ('id',))
        return person_detail(dataset, report, options['id'])
    if kind == 'public':
        fields(options, (), ('minimum_people',))
        return public_report(dataset, report, **options)
    if kind == 'cohort':
        fields(options, (), ('filters', 'horizon', 'unit'))
        rows = select(report['wait_observations']['rows'], options.get('filters'))
        return analyze_rows(rows, options.get('horizon', 14), unit=options.get('unit', 'person'))
    if kind == 'group':
        fields(options, ('dimension',), ('horizon', 'unit'))
        return grouped(report['wait_observations']['rows'], **options)
    if kind == 'compare':
        fields(options, ('left', 'right'), ('horizon', 'unit'))
        return compare(report['wait_observations']['rows'], **options)
    if kind == 'standardize':
        fields(options, ('dimension', 'weights'), ('horizon',))
        return standardize(report['wait_observations']['rows'], **options)
    if kind == 'occupancy':
        fields(options, ('start', 'end'))
        view = dataset.slice(report['options']['as_of'], report['options']['known_at'])
        return daily(dataset, view, **options)
    if kind == 'coverage':
        fields(options, ())
        return continuity_coverage(dataset, dataset.slice(report['options']['as_of'], report['options']['known_at']))
    if kind == 'bottlenecks':
        fields(options, (), ('threshold',))
        return bottlenecks(dataset, dataset.slice(report['options']['as_of'], report['options']['known_at']), **options)
    if kind == 'signatures':
        fields(options, (), ('minimum',))
        return pathways_by_signature(report, **options)
    simple = {'summary': 'population', 'waiting': 'waiting', 'transitions': 'transitions',
              'quality': 'quality', 'readmissions': 'readmissions', 'gaps': 'handoff_gaps',
              'rates': 'completion_rates', 'care-spells': 'care_spells', 'lag': 'recording_lag'}
    if kind in simple:
        fields(options, ())
        return report[simple[kind]]
    raise Invalid('unknown query kind')


def csv_view(report, table):
    if table not in PROJECTIONS:
        raise Invalid('unsupported CSV view')
    columns = sorted(PROJECTIONS[table])
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    cursor = None
    while True:
        result = page(report, table, columns=columns, limit=1000, cursor=cursor)
        for row in result['rows']:
            writer.writerow([canonical(row[key]) if isinstance(row[key], (dict, list)) else row[key] for key in columns])
        cursor = result['next_cursor']
        if cursor is None:
            break
    return output.getvalue()
