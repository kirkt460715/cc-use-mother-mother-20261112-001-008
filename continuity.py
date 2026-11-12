import argparse
from pathlib import Path
import sys
import tempfile
from encounters.values import canonical, load, Conflict, Invalid
from encounters.schema import Dataset
from encounters.journal import Journal
from encounters import exchange, recovery
from pathways.report import analyze, verify, compare_snapshots
from pathways.query import dispatch, csv_view
from exports import archive


def demo(fixture):
    dataset = Dataset(load(fixture))
    options = {'as_of': '2026-11-12', 'known_at': '2026-11-12', 'horizon': 7}
    with tempfile.TemporaryDirectory(prefix='continuity-demo-') as directory:
        root = Path(directory)
        with Journal(root / 'journal.db') as journal:
            imported = journal.replace('initial', 0, dataset.document)
            result = journal.analyze('baseline', imported['revision'], options)
            public = journal.release('public-v1', 'baseline', 2)
            exported = archive.export(dataset, result['result'], root / 'archive', 2)
            tables = exchange.export_tables(dataset, root / 'csv')
            if exchange.import_tables(root / 'csv').fingerprint != dataset.fingerprint:
                raise Invalid('demo exchange failed')
            saved = recovery.backup(journal, root / 'backup.db')
            restored = recovery.restore(root / 'backup.db', root / 'restore.db', saved['sha256'])
            rebuilt = recovery.rebuild(journal, root / 'rebuilt.db')
            return {'source': 'local_synthetic_fixture', 'import': imported,
                    'population': result['result']['population'], 'waiting': result['result']['waiting'],
                    'public': public, 'archive': exported, 'csv': tables,
                    'restored': restored, 'rebuilt': rebuilt, 'audit': journal.audit()}


def parser():
    root = argparse.ArgumentParser(description='County care continuity and censored waiting analysis')
    commands = root.add_subparsers(dest='command', required=True)
    inventory = commands.add_parser('inventory')
    inventory.add_argument('--database', required=True)
    catalog = commands.add_parser('catalog')
    catalog.add_argument('--database', required=True)
    catalog.add_argument('--revision', type=int)
    catalog.add_argument('--table', required=True)
    catalog.add_argument('--limit', type=int, default=100)
    catalog.add_argument('--after')
    impact = commands.add_parser('revision-impact')
    impact.add_argument('--database', required=True)
    impact.add_argument('--before', type=int, required=True)
    impact.add_argument('--after', type=int, required=True)
    lineage = commands.add_parser('entity-lineage')
    lineage.add_argument('--database', required=True)
    lineage.add_argument('--revision', type=int)
    lineage.add_argument('--table', required=True)
    lineage.add_argument('--id', required=True)
    mapped = commands.add_parser('mapped-import')
    mapped.add_argument('--database', required=True)
    mapped.add_argument('--input', required=True)
    mapped.add_argument('--mapping', required=True)
    mapped.add_argument('--batch', required=True)
    mapped.add_argument('--base-revision', type=int, required=True)
    planning = commands.add_parser('analysis-plan')
    planning.add_argument('--database', required=True)
    planning.add_argument('--id', required=True)
    planning.add_argument('--definition', required=True)
    comparison = commands.add_parser('compare-dataset')
    comparison.add_argument('--before', required=True)
    comparison.add_argument('--after', required=True)
    demonstration = commands.add_parser('demo')
    demonstration.add_argument('--fixture', required=True)
    direct = commands.add_parser('analyze')
    direct.add_argument('--input', required=True)
    stored = commands.add_parser('run')
    stored.add_argument('--database', required=True)
    stored.add_argument('--id', required=True)
    stored.add_argument('--revision', required=True, type=int)
    for command in (direct, stored):
        command.add_argument('--as-of', required=True)
        command.add_argument('--known-at', required=True)
        command.add_argument('--horizon', type=int, default=14)
        command.add_argument('--unit', choices=['person', 'request'], default='person')
        command.add_argument('--gap-threshold', type=int, default=7)
        command.add_argument('--readmission-window', type=int, default=30)
    for name in ('replace', 'ingest'):
        command = commands.add_parser(name)
        command.add_argument('--database', required=True)
        command.add_argument('--batch', required=True)
        command.add_argument('--base-revision', required=True, type=int)
        command.add_argument('--input', required=True)
    query = commands.add_parser('query')
    query.add_argument('--database', required=True)
    query.add_argument('--id', required=True)
    query.add_argument('--kind', required=True)
    query.add_argument('--options')
    query.add_argument('--csv', action='store_true')
    release = commands.add_parser('release')
    release.add_argument('--database', required=True)
    release.add_argument('--id', required=True)
    release.add_argument('--label', required=True)
    release.add_argument('--minimum-people', type=int, default=5)
    export = commands.add_parser('archive')
    export.add_argument('--database', required=True)
    export.add_argument('--id', required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--minimum-people', type=int, default=5)
    inspect = commands.add_parser('verify-archive')
    inspect.add_argument('--input', required=True)
    for name in ('csv-in', 'csv-out'):
        command = commands.add_parser(name)
        command.add_argument('--input', required=True)
        command.add_argument('--output', required=True)
    history = commands.add_parser('history')
    history.add_argument('--database', required=True)
    history.add_argument('--table', required=True)
    history.add_argument('--id', required=True)
    for name in ('audit', 'backup', 'rebuild'):
        command = commands.add_parser(name)
        command.add_argument('--database', required=True)
        if name != 'audit':
            command.add_argument('--output', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--input', required=True)
    restore.add_argument('--output', required=True)
    restore.add_argument('--sha256', required=True)
    comparison = commands.add_parser('compare')
    comparison.add_argument('--before', required=True)
    comparison.add_argument('--after', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--database', required=True)
    serve.add_argument('--secret-file', required=True)
    serve.add_argument('--port', type=int, default=8080)
    return root


def execute(args):
    command = args.command
    if command == 'compare-dataset':
        from encounters.differences import compare, patch
        before = Dataset(load(args.before))
        after = Dataset(load(args.after))
        return {'difference': compare(before, after), 'patch': patch(before, after)}
    if command == 'demo':
        return demo(args.fixture)
    if command == 'analyze':
        dataset = Dataset(load(args.input))
        result = analyze(dataset, **analysis_options(args))
        verify(dataset, result, True)
        return result
    if command == 'verify-archive':
        return archive.inspect(args.input)
    if command == 'restore':
        return recovery.restore(args.input, args.output, args.sha256)
    if command == 'csv-out':
        return exchange.export_tables(Dataset(load(args.input)), args.output)
    if command == 'csv-in':
        dataset = exchange.import_tables(args.input)
        return exchange.write_json(dataset.document, args.output)
    if command == 'compare':
        return compare_snapshots(load(args.before), load(args.after))
    if command == 'serve':
        from encounters.service import serve
        secret = Path(args.secret_file)
        if secret.is_symlink() or not secret.is_file() or secret.stat().st_mode & 0o077:
            raise Invalid('secret must be regular owner-only file')
        return serve(args.database, secret.read_text().strip(), args.port)
    with Journal(args.database) as journal:
        if command == 'inventory':
            from encounters.catalog_query import journal_summary
            return journal_summary(journal)
        if command == 'catalog':
            from encounters.catalog_query import table_page
            return table_page(journal.dataset(args.revision), args.table, args.after, args.limit)
        if command == 'revision-impact':
            from encounters.catalog_query import revision_impact
            return revision_impact(journal, args.before, args.after)
        if command == 'entity-lineage':
            from encounters.differences import explain_entity
            return explain_entity(journal.dataset(args.revision), args.table, args.id)
        if command == 'mapped-import':
            from encounters.mapping import ingest
            source = Path(args.input)
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 50000000:
                raise Invalid('bounded CSV input required')
            return ingest(journal, args.batch, args.base_revision, source.read_text(), load(args.mapping))
        if command == 'analysis-plan':
            from waiting_analysis.plan import run, verify
            record = journal.analysis(args.id)
            dataset = journal.dataset(record['revision'])
            execution = run(dataset, record['result'], load(args.definition))
            verify(dataset, record['result'], execution)
            return execution
        if command == 'replace':
            return journal.replace(args.batch, args.base_revision, load(args.input))
        if command == 'ingest':
            return journal.ingest(args.batch, args.base_revision, exchange.operations_from_ndjson(args.input))
        if command == 'run':
            return journal.analyze(args.id, args.revision, analysis_options(args))
        if command == 'query':
            record = journal.analysis(args.id)
            if args.csv:
                return csv_view(record['result'], args.kind)
            return dispatch(journal.dataset(record['revision']), record['result'], args.kind,
                            load(args.options) if args.options else None)
        if command == 'release':
            return journal.release(args.label, args.id, args.minimum_people)
        if command == 'archive':
            record = journal.analysis(args.id)
            return archive.export(journal.dataset(record['revision']), record['result'], args.output, args.minimum_people)
        if command == 'history':
            return journal.history(args.table, args.id)
        if command == 'audit':
            return journal.audit()
        if command == 'backup':
            return recovery.backup(journal, args.output)
        if command == 'rebuild':
            return recovery.rebuild(journal, args.output)
    raise Invalid('unsupported command')


def analysis_options(args):
    return {'as_of': args.as_of, 'known_at': args.known_at, 'horizon': args.horizon,
            'unit': args.unit, 'gap_threshold': args.gap_threshold, 'readmission_window': args.readmission_window}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (ValueError, KeyError, OSError) as error:
        print(canonical({'error': str(error)}), file=sys.stderr)
        return 2
    print(result if isinstance(result, str) else canonical(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
