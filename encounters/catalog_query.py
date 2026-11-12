from collections import defaultdict
from encounters.values import integer, identifier, Invalid
from encounters.schema import SCHEMAS
from encounters.differences import compare, explain_entity


def inventory(dataset):
    by_county = defaultdict(lambda: {'people': 0, 'facilities': 0, 'visits': 0, 'referrals': 0})
    stages = defaultdict(int)
    for person in dataset.people.values():
        by_county[person['county']]['people'] += 1
    for facility in dataset.facilities.values():
        by_county[facility['county']]['facilities'] += 1
        stages[facility['stage']] += 1
    for visit in dataset.visits.values():
        county = dataset.people[visit['person']]['county']
        by_county[county]['visits'] += 1
    for referral in dataset.referrals.values():
        county = dataset.people[referral['person']]['county']
        by_county[county]['referrals'] += 1
    return {'input_digest': dataset.fingerprint,
            'tables': {table: len(getattr(dataset, table)) for table in SCHEMAS},
            'by_county': [{'county': key, **counts} for key, counts in sorted(by_county.items())],
            'facility_stages': dict(sorted(stages.items())), 'access': 'restricted'}


def table_page(dataset, table, after=None, limit=100):
    if table not in SCHEMAS:
        raise Invalid('unknown catalog table')
    integer(limit, 'limit', 1, 1000)
    if after is not None:
        identifier(after, 'after')
    keys = sorted(key for key in getattr(dataset, table) if after is None or key > after)
    selected = keys[:limit]
    return {'rows': [getattr(dataset, table)[key] for key in selected],
            'next_after': selected[-1] if len(keys) > limit else None,
            'input_digest': dataset.fingerprint, 'table': table,
            'paging_requires_same_input_digest': True}


def journal_summary(journal):
    dataset = journal.dataset()
    analyses = []
    for row in journal.connection.execute('SELECT id,revision FROM analyses ORDER BY id'):
        analysis = journal.analysis(row['id'])
        report = analysis['result']
        analyses.append({'id': row['id'], 'revision': row['revision'], 'options': report['options'],
                         'report_digest': report['report_digest'], 'population': report['population']})
    releases = []
    for row in journal.connection.execute('SELECT label FROM releases ORDER BY label'):
        publication = journal.publication(row['label'])
        releases.append({'label': row['label'], 'analysis': publication['analysis'],
                         'publication_digest': publication['report']['publication_digest']})
    return {'revision': journal.revision, 'inventory': inventory(dataset),
            'analyses': analyses, 'releases': releases}


def revision_impact(journal, before_revision, after_revision):
    integer(before_revision, 'before_revision')
    integer(after_revision, 'after_revision')
    if before_revision > after_revision:
        raise Invalid('revision order reversed')
    before, after = journal.dataset(before_revision), journal.dataset(after_revision)
    difference = compare(before, after)
    affected = []
    for row in journal.connection.execute('SELECT id,revision FROM analyses ORDER BY id'):
        if row['revision'] <= before_revision:
            analysis = journal.analysis(row['id'])
            affected.append({'id': row['id'], 'frozen_revision': row['revision'],
                             'original_report_digest': analysis['result']['report_digest'],
                             'changed_source_since_snapshot': analysis['result']['input_digest'] != after.fingerprint,
                             'snapshot_remains_immutable': True})
    return {'before_revision': before_revision, 'after_revision': after_revision,
            'difference': difference, 'prior_analyses': affected,
            'published_releases_not_rewritten': True}


def entity_history(journal, table, key):
    changes = journal.history(table, key)
    current = getattr(journal.dataset(), table).get(key)
    return {'table': table, 'id': key, 'current': current, 'changes': changes,
            'exists': current is not None, 'deleted': bool(changes) and current is None,
            'lineage': explain_entity(journal.dataset(), table, key) if current else None}
