import hashlib
from pathlib import Path
import sqlite3
from encounters.journal import Journal
from encounters.values import Invalid


def backup(journal, destination):
    audit = journal.audit()
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    try:
        connection = sqlite3.connect(str(target))
        try:
            journal.connection.backup(connection)
        finally:
            connection.close()
        with Journal(target) as restored:
            if restored.audit() != audit:
                raise Invalid('backup audit differs')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'audit': audit}


def restore(source, destination, checksum):
    source = Path(source)
    target = Path(destination)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 500000000:
        raise Invalid('bounded regular database required')
    if hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
        raise Invalid('backup checksum mismatch')
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    try:
        left = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        right = sqlite3.connect(str(target))
        try:
            left.backup(right)
        finally:
            left.close()
            right.close()
        with Journal(target) as journal:
            audit = journal.audit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'restored': True, 'audit': audit}


def rebuild(journal, destination):
    journal.audit()
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    try:
        with Journal(target) as rebuilt:
            for row in journal.connection.execute('SELECT id,body FROM batches ORDER BY revision'):
                import json
                request = json.loads(row['body'])
                rebuilt.ingest(row['id'], request['base_revision'], request['operations'])
            for row in journal.connection.execute('SELECT id FROM analyses ORDER BY id'):
                original = journal.analysis(row['id'])
                result = rebuilt.analyze(row['id'], original['revision'], original['request']['options'])
                if result != original:
                    raise Invalid('analysis rebuild differs')
            for row in journal.connection.execute('SELECT label,analysis_id FROM releases ORDER BY label'):
                original = journal.publication(row['label'])
                result = rebuilt.release(row['label'], row['analysis_id'], original['report']['minimum_people'])
                if result != original:
                    raise Invalid('release rebuild differs')
            audit = rebuilt.audit()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'replayed': True, 'audit': audit, 'source_digest': journal.dataset().fingerprint}
