import csv
import io
from encounters.schema import SCHEMAS, VALIDATORS
from encounters.values import fields, integer, text, records, Invalid


def validate_mapping(definition):
    fields(definition, ('version', 'table', 'columns', 'defaults', 'values'))
    if type(definition['version']) is not int or definition['version'] != 1:
        raise Invalid('unsupported mapping version')
    table = definition['table']
    if table not in SCHEMAS:
        raise Invalid('unknown mapping table')
    columns, defaults, values = definition['columns'], definition['defaults'], definition['values']
    if not all(isinstance(item, dict) for item in (columns, defaults, values)):
        raise Invalid('mapping dictionaries required')
    if columns.keys() & defaults.keys():
        raise Invalid('mapped and defaulted fields overlap')
    if columns.keys() | defaults.keys() != set(SCHEMAS[table]):
        raise Invalid('mapping must cover every canonical field exactly once')
    if values.keys() - columns.keys():
        raise Invalid('value conversion must name mapped fields')
    for target, source in columns.items():
        text(source, 'source column', 100)
    if len(set(columns.values())) != len(columns):
        raise Invalid('one source column cannot feed multiple target fields')
    for target, conversion in values.items():
        if not isinstance(conversion, dict) or len(conversion) > 100:
            raise Invalid('bounded value translation required')
        for key in conversion:
            if not isinstance(key, str):
                raise Invalid('CSV translation keys must be strings')
    return definition


def translate(content, definition, strict=True):
    validate_mapping(definition)
    if not isinstance(content, str) or len(content.encode()) > 50000000:
        raise Invalid('bounded CSV source required')
    if type(strict) is not bool:
        raise Invalid('strict flag must be boolean')
    reader = csv.reader(io.StringIO(content), strict=True)
    header = next(reader, None)
    expected = set(definition['columns'].values())
    if header is None or len(header) != len(set(header)) or not expected <= set(header):
        raise Invalid('mapped source headers missing or duplicated')
    if strict and set(header) != expected:
        raise Invalid('unmapped source columns in strict mode')
    output = []
    rejected = []
    for number, cells in enumerate(reader, 2):
        if len(cells) != len(header):
            rejected.append({'line': number, 'error': 'record width mismatch'})
            continue
        source = dict(zip(header, cells))
        target = dict(definition['defaults'])
        try:
            for field, column in definition['columns'].items():
                value = source[column]
                conversion = definition['values'].get(field)
                if conversion is not None:
                    if value not in conversion:
                        raise Invalid('unmapped value for ' + field)
                    value = conversion[value]
                target[field] = value
            VALIDATORS[definition['table']](target)
            output.append(target)
        except (ValueError, TypeError) as error:
            rejected.append({'line': number, 'error': str(error)})
        if len(output) + len(rejected) > 100000:
            raise Invalid('mapping row bound exceeded')
    return {'table': definition['table'], 'rows': output, 'rejected': rejected,
            'accepted': len(output), 'complete': not rejected,
            'unmapped_columns': sorted(set(header) - expected),
            'rejected_rows_are_not_imported': True}


def operations(journal, translation):
    if not translation['complete']:
        raise Invalid('partial translation cannot become import batch')
    table = translation['table']
    current = getattr(journal.dataset(), table)
    from encounters.values import digest
    output = []
    seen = set()
    for row in translation['rows']:
        key = row['id']
        if key in seen:
            raise Invalid('duplicate mapped entity identity')
        seen.add(key)
        previous = current.get(key)
        if previous == row:
            continue
        output.append({'table': table, 'id': key,
                       'before_digest': digest(previous) if previous is not None else None,
                       'after': row})
    return output


def ingest(journal, batch_id, base_revision, content, definition):
    translated = translate(content, definition)
    if journal.revision != base_revision:
        from encounters.values import Conflict
        raise Conflict('mapped batch base revision changed')
    planned = operations(journal, translated)
    result = journal.ingest(batch_id, base_revision, planned)
    return {'translation': {'accepted': translated['accepted'], 'table': translated['table']},
            'import': result, 'changed_entities': len(planned)}
