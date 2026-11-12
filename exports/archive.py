import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from encounters.values import canonical, digest, load, Invalid
from encounters.schema import Dataset
from pathways.report import verify
from exports.disclosure import public_report, verify_public


def contents(dataset, report, minimum_people=5):
    audit = verify(dataset, report, True)
    public = public_report(dataset, report, minimum_people)
    verify_public(public)
    files = {'dataset.json': canonical(dataset.document), 'report.json': canonical(report),
             'public.json': canonical(public), 'audit.json': canonical(audit)}
    manifest = {'version': 1, 'input_digest': dataset.fingerprint,
                'report_digest': report['report_digest'], 'minimum_people': minimum_people,
                'files': {name: hashlib.sha256(body.encode()).hexdigest() for name, body in files.items()},
                'access': 'restricted_contains_person_level_identifiers'}
    return {**files, 'manifest.json': canonical(manifest)}


def export(dataset, report, directory, minimum_people=5):
    bodies = contents(dataset, report, minimum_people)
    target = Path(directory)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.continuity-', dir=target.parent))
    try:
        for name, body in bodies.items():
            with (stage / name).open('x', encoding='utf-8') as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
        inspect(stage)
        os.rename(stage, target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    return inspect(target)


def inspect(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise Invalid('regular archive directory required')
    manifest = load(root / 'manifest.json')
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise Invalid('archive manifest invalid')
    if {path.name for path in root.iterdir()} != set(manifest['files']) | {'manifest.json'}:
        raise Invalid('archive file set mismatch')
    for name, checksum in manifest['files'].items():
        if Path(name).name != name or name in ('.', '..'):
            raise Invalid('archive path traversal')
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000000:
            raise Invalid('archive file invalid')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise Invalid('archive checksum mismatch')
    dataset = Dataset(load(root / 'dataset.json'))
    report = load(root / 'report.json')
    expected = contents(dataset, report, manifest['minimum_people'])
    for name, body in expected.items():
        if (root / name).read_bytes() != body.encode():
            raise Invalid('archive semantic replay mismatch')
    return {'valid': True, 'files': len(expected), 'report_digest': report['report_digest']}
