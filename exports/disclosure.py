from collections import defaultdict
import hashlib
import hmac
from encounters.values import canonical, digest, integer, fields, Invalid
from pathways.observations import referral_waits


def suppress_cells(cells, minimum_people):
    integer(minimum_people, 'minimum_people', 2, 1000)
    if not isinstance(cells, list):
        raise Invalid('cell array required')
    result = []
    for cell in cells:
        fields(cell, ('key', 'people', 'requests', 'completed', 'pending'))
        if not isinstance(cell['people'], set):
            raise Invalid('distinct contributor sets required')
        count = len(cell['people'])
        private = 0 < count < minimum_people
        result.append({'key': cell['key'], 'people': None if private else count,
                       'requests': None if private else cell['requests'],
                       'completed': None if private else cell['completed'],
                       'pending': None if private else cell['pending'],
                       'suppressed': private})
    hidden = [index for index, cell in enumerate(result) if cell['suppressed']]
    if len(hidden) == 1:
        candidates = [(cell['people'], str(cell['key']), index) for index, cell in enumerate(result)
                      if not cell['suppressed'] and cell['people'] > 0]
        if candidates:
            _, _, index = min(candidates)
            result[index].update(people=None, requests=None, completed=None, pending=None, suppressed=True)
    return result


def public_report(dataset, report, minimum_people=5):
    integer(minimum_people, 'minimum_people', 2, 1000)
    buckets = defaultdict(lambda: {'people': set(), 'requests': 0, 'completed': 0, 'pending': 0})
    for row in report['wait_observations']['rows']:
        key = row['destination_stage']
        bucket = buckets[key]
        bucket['people'].add(row['person'])
        bucket['requests'] += 1
        bucket['completed' if row['event'] else 'pending'] += 1
    cells = [{'key': key, **counts} for key, counts in sorted(buckets.items())]
    result = suppress_cells(cells, minimum_people)
    hidden = any(cell['suppressed'] for cell in result)
    total_people = len({person for cell in cells for person in cell['people']})
    body = {'version': 1, 'minimum_people': minimum_people,
            'as_of': report['options']['as_of'], 'known_at': report['options']['known_at'],
            'dimension': 'destination_stage', 'cells': result,
            'totals': None if hidden else {'people': total_people, 'requests': sum(cell['requests'] for cell in cells)},
            'policy': 'primary_and_single_cell_complementary_suppression_no_cross_tabs',
            'contains_direct_identifiers': False,
            'limitations': ['not_differential_privacy', 'do_not_combine_releases_to_infer_small_cells']}
    return {**body, 'publication_digest': digest(body)}


def token(secret, namespace, value):
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise Invalid('at least 32 secret bytes required')
    message = canonical([namespace, value]).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def restricted_paths(dataset, report, secret, namespace, bin_days=7):
    integer(bin_days, 'bin_days', 1, 90)
    if not isinstance(namespace, str) or not namespace or len(namespace) > 100:
        raise Invalid('release namespace required')
    rows = []
    for chain in report['chains']['chains']:
        rows.append({'person_token': token(secret, namespace, chain['person']),
                     'chain_token': token(secret, namespace, chain['id']),
                     'stages': chain['stages'], 'closed': chain['closed'],
                     'elapsed_bin': chain['elapsed_days'] // bin_days,
                     'gap_bin': chain['gap_days'] // bin_days})
    rows.sort(key=lambda row: row['chain_token'])
    return {'version': 1, 'namespace': namespace, 'bin_days': bin_days, 'rows': rows,
            'access': 'restricted_pseudonymous_not_anonymous',
            'warning': 'stage_sequences_and_durations_can_enable_reidentification',
            'source_report_digest': report['report_digest']}


def verify_public(document):
    expected = digest({key: value for key, value in document.items() if key != 'publication_digest'})
    if document.get('publication_digest') != expected:
        raise Invalid('public report checksum mismatch')
    forbidden = {'person', 'id', 'referral', 'facility', 'chain_token', 'person_token'}
    def visit(value):
        if isinstance(value, dict):
            if value.keys() & forbidden:
                raise Invalid('identifier in public report')
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(document)
    for cell in document['cells']:
        values = [cell[key] for key in ('people', 'requests', 'completed', 'pending')]
        if cell['suppressed'] and any(value is not None for value in values):
            raise Invalid('suppressed cell leaks counts')
        if not cell['suppressed'] and cell['people'] and cell['people'] < document['minimum_people']:
            raise Invalid('small count unsuppressed')
    if any(cell['suppressed'] for cell in document['cells']) and document['totals'] is not None:
        raise Invalid('suppressed cells with public total')
    return {'valid': True, 'cells': len(document['cells'])}
