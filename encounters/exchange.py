import csv
import io
import json
from pathlib import Path
import os
import shutil
import tempfile
from encounters.schema import Dataset, SCHEMAS
from encounters.values import canonical, load, integer, Invalid


NULLABLE = {'discharged', 'outcome', 'target', 'resolved'}


def encode_table(dataset, table):
    if table not in SCHEMAS:
        raise Invalid('unknown exchange table')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(SCHEMAS[table])
    for row in dataset.document[table]:
        writer.writerow(['' if row[key] is None else 'true' if row[key] is True
                         else 'false' if row[key] is False else row[key] for key in SCHEMAS[table]])
    return output.getvalue()


def decode_table(table, content):
    if table not in SCHEMAS or not isinstance(content, str) or len(content.encode()) > 50000000:
        raise Invalid('unsupported table or oversized CSV')
    reader = csv.reader(io.StringIO(content), strict=True)
    header = next(reader, None)
    if header is None or len(header) != len(set(header)) or set(header) != set(SCHEMAS[table]):
        raise Invalid('CSV header mismatch')
    rows = []
    try:
        for cells in reader:
            if len(cells) != len(header):
                raise Invalid('CSV record width mismatch')
            row = dict(zip(header, cells))
            for key, value in list(row.items()):
                if key in NULLABLE and value == '':
                    row[key] = None
                elif key == 'rural':
                    if value not in ('true', 'false'):
                        raise Invalid('CSV boolean must be true or false')
                    row[key] = value == 'true'
            from encounters.schema import VALIDATORS
            VALIDATORS[table](row)
            rows.append(row)
            if len(rows) > 100000:
                raise Invalid('CSV row limit exceeded')
    except csv.Error as error:
        raise Invalid('malformed CSV') from error
    return rows


def import_tables(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise Invalid('regular exchange directory required')
    expected = {table + '.csv' for table in SCHEMAS}
    if {path.name for path in root.iterdir()} != expected:
        raise Invalid('CSV file set mismatch')
    document = {'version': 1}
    for table in SCHEMAS:
        path = root / (table + '.csv')
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 50000000:
            raise Invalid('bounded regular CSV file required')
        document[table] = decode_table(table, path.read_text(encoding='utf-8-sig'))
    return Dataset(document)


def export_tables(dataset, directory):
    target = Path(directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.encounters-', dir=target.parent))
    try:
        for table in SCHEMAS:
            with (stage / (table + '.csv')).open('x', encoding='utf-8', newline='') as output:
                output.write(encode_table(dataset, table))
                output.flush()
                os.fsync(output.fileno())
        check = import_tables(stage)
        if check.fingerprint != dataset.fingerprint:
            raise Invalid('exchange roundtrip failed')
        os.rename(stage, target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return {'files': sorted(path.name for path in target.iterdir()), 'input_digest': dataset.fingerprint}


def operations_from_ndjson(path, maximum=100000):
    integer(maximum, 'maximum', 1, 100000)
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 50000000:
        raise Invalid('bounded NDJSON file required')
    rows = []
    for line_number, line in enumerate(source.read_text().splitlines(), 1):
        if not line.strip():
            raise Invalid('empty NDJSON record at ' + str(line_number))
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise Invalid('invalid NDJSON at ' + str(line_number)) from error
        from encounters.values import fields
        fields(row, ('table', 'id', 'before_digest', 'after'))
        rows.append(row)
        if len(rows) > maximum:
            raise Invalid('operation count exceeds bound')
    return rows


def write_json(value, destination):
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as output:
        output.write(canonical(value) + '\n')
    return {'path': str(target), 'bytes': target.stat().st_size}
