from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from encounters.values import canonical, digest, identifier, integer, fields, records, Conflict, Invalid
from encounters.schema import Dataset, SCHEMAS, VALIDATORS


class Journal:
    def __init__(self, path):
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS batches(
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            base_revision INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS changes(
            revision INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            table_name TEXT NOT NULL,
            entity TEXT NOT NULL,
            before_digest TEXT,
            after_body TEXT,
            PRIMARY KEY(revision,ordinal)
        );
        CREATE TABLE IF NOT EXISTS heads(
            table_name TEXT NOT NULL,
            entity TEXT NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY(table_name,entity)
        );
        CREATE TABLE IF NOT EXISTS versions(
            revision INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            document TEXT NOT NULL,
            batch_id TEXT UNIQUE REFERENCES batches(id)
        );
        CREATE TABLE IF NOT EXISTS analyses(
            id TEXT PRIMARY KEY,
            request TEXT NOT NULL,
            revision INTEGER NOT NULL REFERENCES versions(revision),
            body TEXT NOT NULL,
            fingerprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
            label TEXT PRIMARY KEY,
            analysis_id TEXT NOT NULL REFERENCES analyses(id),
            body TEXT NOT NULL,
            fingerprint TEXT NOT NULL
        );
        ''')
        empty = Dataset({'version': 1, **{key: [] for key in SCHEMAS}})
        self.connection.execute('INSERT OR IGNORE INTO versions VALUES(0,?,?,NULL)',
                                (empty.fingerprint, canonical(empty.document)))
        self.serial = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        nested = self.connection.in_transaction
        self.serial += 1
        point = 'j' + str(self.serial)
        self.connection.execute('SAVEPOINT ' + point if nested else 'BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                self.connection.execute('ROLLBACK TO ' + point)
                self.connection.execute('RELEASE ' + point)
            else:
                self.connection.execute('ROLLBACK')
            raise
        else:
            self.connection.execute('RELEASE ' + point if nested else 'COMMIT')

    @property
    def revision(self):
        return self.connection.execute('SELECT MAX(revision) FROM versions').fetchone()[0]

    def dataset(self, revision=None):
        revision = self.revision if revision is None else integer(revision, 'revision')
        row = self.connection.execute('SELECT * FROM versions WHERE revision=?', (revision,)).fetchone()
        if row is None:
            raise KeyError(revision)
        dataset = Dataset(json.loads(row['document']))
        if dataset.fingerprint != row['fingerprint']:
            raise Invalid('snapshot digest mismatch')
        return dataset

    def ingest(self, batch_id, base_revision, operations):
        identifier(batch_id, 'batch')
        integer(base_revision, 'base_revision')
        records(operations, 'operations', 100000)
        request = {'base_revision': base_revision, 'operations': operations}
        signature = digest(request)
        with self.transaction():
            old = self.connection.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if old:
                if old['fingerprint'] != signature:
                    raise Conflict('batch id reused with different content')
                return {'batch': batch_id, 'revision': old['revision'], 'duplicate': True,
                        'input_digest': self.dataset(old['revision']).fingerprint}
            if self.revision != base_revision:
                raise Conflict('stale base revision')
            original = self.dataset(base_revision)
            tables = {table: {row['id']: row for row in original.document[table]} for table in SCHEMAS}
            changes = []
            touched = set()
            for index, operation in enumerate(operations):
                fields(operation, ('table', 'id', 'before_digest', 'after'))
                table, key = operation['table'], operation['id']
                identifier(key)
                if table not in SCHEMAS or (table, key) in touched:
                    raise Invalid('unknown table or repeated operation target')
                touched.add((table, key))
                previous = tables[table].get(key)
                prior_digest = digest(previous) if previous is not None else None
                if prior_digest != operation['before_digest']:
                    raise Conflict('entity digest changed')
                after = operation['after']
                if after is None:
                    if previous is None:
                        raise Invalid('cannot remove missing entity')
                    del tables[table][key]
                else:
                    VALIDATORS[table](after)
                    if after['id'] != key:
                        raise Invalid('operation identity mismatch')
                    tables[table][key] = after
                changes.append((base_revision + 1, index, table, key, prior_digest,
                                canonical(after) if after is not None else None))
            dataset = Dataset({'version': 1, **{table: list(rows.values()) for table, rows in tables.items()}})
            revision = base_revision + 1
            self.connection.execute('INSERT INTO batches VALUES(?,?,?,?,?)',
                                    (batch_id, signature, base_revision, revision, canonical(request)))
            self.connection.execute('INSERT INTO versions VALUES(?,?,?,?)',
                                    (revision, dataset.fingerprint, canonical(dataset.document), batch_id))
            self.connection.executemany('INSERT INTO changes VALUES(?,?,?,?,?,?)', changes)
            for operation in operations:
                table, key, after = operation['table'], operation['id'], operation['after']
                if after is None:
                    self.connection.execute('DELETE FROM heads WHERE table_name=? AND entity=?', (table, key))
                else:
                    self.connection.execute('INSERT INTO heads VALUES(?,?,?) ON CONFLICT(table_name,entity) DO UPDATE SET body=excluded.body',
                                            (table, key, canonical(after)))
            return {'batch': batch_id, 'revision': revision, 'duplicate': False, 'input_digest': dataset.fingerprint}

    def replace(self, batch_id, base_revision, document):
        dataset = Dataset(document)
        previous = self.dataset(base_revision)
        operations = []
        for table in SCHEMAS:
            left, right = getattr(previous, table), getattr(dataset, table)
            for key in sorted(left.keys() | right.keys()):
                if left.get(key) != right.get(key):
                    operations.append({'table': table, 'id': key,
                                       'before_digest': digest(left[key]) if key in left else None,
                                       'after': right.get(key)})
        return self.ingest(batch_id, base_revision, operations)

    def analyze(self, analysis_id, revision, options):
        from pathways.report import analyze, verify
        identifier(analysis_id, 'analysis')
        integer(revision, 'revision')
        request = {'revision': revision, 'options': options}
        row = self.connection.execute('SELECT request FROM analyses WHERE id=?', (analysis_id,)).fetchone()
        if row:
            if json.loads(row[0]) != request:
                raise Conflict('analysis id binds another request')
            return self.analysis(analysis_id)
        dataset = self.dataset(revision)
        result = analyze(dataset, **options)
        verify(dataset, result, replay=True)
        with self.transaction():
            row = self.connection.execute('SELECT request FROM analyses WHERE id=?', (analysis_id,)).fetchone()
            if row and json.loads(row[0]) != request:
                raise Conflict('concurrent analysis identity conflict')
            self.connection.execute('INSERT OR IGNORE INTO analyses VALUES(?,?,?,?,?)',
                                    (analysis_id, canonical(request), revision, canonical(result), digest(result)))
        return self.analysis(analysis_id)

    def analysis(self, key):
        from pathways.report import verify
        row = self.connection.execute('SELECT * FROM analyses WHERE id=?', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        request, body = json.loads(row['request']), json.loads(row['body'])
        if digest(body) != row['fingerprint'] or request['revision'] != row['revision']:
            raise Invalid('stored analysis integrity failure')
        verify(self.dataset(row['revision']), body)
        return {'id': key, 'revision': row['revision'], 'request': request, 'result': body}

    def release(self, label, analysis_id, minimum_people=5):
        from exports.disclosure import public_report
        identifier(label, 'label')
        with self.transaction():
            analysis = self.analysis(analysis_id)
            document = public_report(self.dataset(analysis['revision']), analysis['result'], minimum_people)
            row = self.connection.execute('SELECT * FROM releases WHERE label=?', (label,)).fetchone()
            if row:
                if row['analysis_id'] != analysis_id or json.loads(row['body']) != document:
                    raise Conflict('release label immutable')
            else:
                self.connection.execute('INSERT INTO releases VALUES(?,?,?,?)',
                                        (label, analysis_id, canonical(document), digest(document)))
        return self.publication(label)

    def publication(self, label):
        row = self.connection.execute('SELECT * FROM releases WHERE label=?', (label,)).fetchone()
        if row is None:
            raise KeyError(label)
        body = json.loads(row['body'])
        if digest(body) != row['fingerprint']:
            raise Invalid('publication checksum mismatch')
        from exports.disclosure import public_report
        analysis = self.analysis(row['analysis_id'])
        expected = public_report(self.dataset(analysis['revision']), analysis['result'], body['minimum_people'])
        if expected != body:
            raise Invalid('publication semantic replay failed')
        return {'label': label, 'analysis': row['analysis_id'], 'report': body}

    def history(self, table, key):
        if table not in SCHEMAS:
            raise Invalid('unknown history table')
        identifier(key)
        rows = self.connection.execute('SELECT * FROM changes WHERE table_name=? AND entity=? ORDER BY revision', (table, key))
        return [{'revision': row['revision'], 'before_digest': row['before_digest'],
                 'after': json.loads(row['after_body']) if row['after_body'] else None} for row in rows]

    def audit(self, replay=True):
        if self.connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise Invalid('SQLite integrity failure')
        if self.connection.execute('PRAGMA foreign_key_check').fetchall():
            raise Invalid('SQLite reference failure')
        versions = list(self.connection.execute('SELECT revision,batch_id FROM versions ORDER BY revision'))
        if [item['revision'] for item in versions] != list(range(self.revision + 1)):
            raise Invalid('snapshot revision sequence has gaps')
        if versions[0]['batch_id'] is not None:
            raise Invalid('initial version cannot bind a batch')
        for version in versions[1:]:
            batch = self.connection.execute('SELECT revision FROM batches WHERE id=?', (version['batch_id'],)).fetchone()
            if batch is None or batch['revision'] != version['revision']:
                raise Invalid('snapshot batch reference mismatch')
        prior = 0
        for row in self.connection.execute('SELECT * FROM batches ORDER BY revision'):
            body = json.loads(row['body'])
            if digest(body) != row['fingerprint'] or row['base_revision'] != prior or row['revision'] != prior + 1:
                raise Invalid('batch history discontinuity')
            if body['base_revision'] != prior:
                raise Invalid('batch base binding mismatch')
            before = self.dataset(prior)
            tables = {table: dict(getattr(before, table)) for table in SCHEMAS}
            changes = list(self.connection.execute('SELECT * FROM changes WHERE revision=? ORDER BY ordinal', (row['revision'],)))
            if len(changes) != len(body['operations']):
                raise Invalid('change journal size mismatch')
            for index, (change, operation) in enumerate(zip(changes, body['operations'])):
                table, key = operation['table'], operation['id']
                current = tables[table].get(key)
                signature = digest(current) if current is not None else None
                after = json.loads(change['after_body']) if change['after_body'] else None
                if (change['ordinal'] != index or change['table_name'] != table or change['entity'] != key
                        or signature != operation['before_digest'] or signature != change['before_digest']
                        or after != operation['after']):
                    raise Invalid('change journal does not match batch')
                if after is None:
                    tables[table].pop(key)
                else:
                    tables[table][key] = after
            actual = Dataset({'version': 1, **{table: list(values.values()) for table, values in tables.items()}})
            if actual.fingerprint != self.dataset(row['revision']).fingerprint:
                raise Invalid('version does not match journal replay')
            prior = row['revision']
        head = {'version': 1, **{table: [] for table in SCHEMAS}}
        for row in self.connection.execute('SELECT * FROM heads ORDER BY table_name,entity'):
            head[row['table_name']].append(json.loads(row['body']))
        if Dataset(head).fingerprint != self.dataset().fingerprint:
            raise Invalid('materialized heads disagree with version')
        for row in self.connection.execute('SELECT id FROM analyses'):
            record = self.analysis(row[0])
            if replay:
                from pathways.report import verify
                verify(self.dataset(record['revision']), record['result'], True)
        for row in self.connection.execute('SELECT label FROM releases'):
            self.publication(row[0])
        count = lambda table: self.connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        return {'valid': True, 'revision': self.revision, 'batches': count('batches'),
                'analyses': count('analyses'), 'releases': count('releases'), 'replayed': replay}
