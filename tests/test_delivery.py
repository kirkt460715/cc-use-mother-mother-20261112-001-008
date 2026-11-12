import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from encounters.schema import Dataset
from encounters.values import digest, canonical, Conflict
from encounters.journal import Journal
from encounters import recovery
from encounters.service import route, handler
from encounters.catalog_query import journal_summary, revision_impact, entity_history
from exports import archive
from exports.disclosure import suppress_cells, public_report, verify_public, token, restricted_paths
from pathways.query import page, person_detail, dispatch, csv_view
from pathways.report import analyze
from waiting_analysis.plan import run, verify
import continuity


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = json.loads(Path('fixtures/care.json').read_text())
        self.dataset = Dataset(self.data)
        self.journal = Journal(self.root / 'care.db')
        self.journal.replace('first', 0, self.data)
        self.options = {'as_of': '2026-11-12', 'known_at': '2026-11-12', 'horizon': 7}
        self.record = self.journal.analyze('baseline', 1, self.options)
        self.report = self.record['result']

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_analysis_identity_is_immutable_and_idempotent(self):
        repeated = self.journal.analyze('baseline', 1, self.options)
        self.assertEqual(repeated, self.record)
        with self.assertRaises(Conflict):
            self.journal.analyze('baseline', 1, {**self.options, 'horizon': 14})
        with self.assertRaises(KeyError):
            self.journal.analysis('missing')
        self.assertEqual(self.journal.audit()['analyses'], 1)
        self.assertEqual(self.record['revision'], 1)
        self.assertEqual(self.record['result']['input_digest'], self.dataset.fingerprint)
        changed = copy.deepcopy(self.data)
        changed['people'][0]['county'] = 'west'
        self.journal.replace('revise', 1, changed)
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.assertNotEqual(self.journal.dataset().fingerprint, self.dataset.fingerprint)

    def test_release_primary_and_complementary_suppression(self):
        report = public_report(self.dataset, self.report, 2)
        self.assertTrue(verify_public(report)['valid'])
        self.assertEqual(len(report['cells']), 2)
        self.assertTrue(all(row['suppressed'] for row in report['cells']))
        self.assertIsNone(report['totals'])
        self.assertFalse(report['contains_direct_identifiers'])
        self.assertNotIn('p1', canonical(report))
        self.assertNotIn('r1', canonical(report))
        release = self.journal.release('public', 'baseline', 2)
        self.assertEqual(self.journal.release('public', 'baseline', 2), release)
        self.assertEqual(self.journal.publication('public')['report'], report)
        with self.assertRaises(Conflict):
            self.journal.release('public', 'baseline', 3)
        self.assertEqual(self.journal.audit()['releases'], 1)

    def test_disclosure_counts_contributors_not_repeated_requests(self):
        cells = [{'key': 'a', 'people': {'x'}, 'requests': 40, 'completed': 20, 'pending': 20},
                 {'key': 'b', 'people': {'y', 'z'}, 'requests': 2, 'completed': 1, 'pending': 1}]
        checked = suppress_cells(cells, 2)
        self.assertTrue(checked[0]['suppressed'])
        self.assertTrue(checked[1]['suppressed'])
        self.assertIsNone(checked[0]['requests'])
        self.assertIsNone(checked[1]['people'])
        self.assertEqual(cells[0]['requests'], 40)
        with self.assertRaises(ValueError):
            suppress_cells(cells, 1)
        empty = suppress_cells([{'key': 'zero', 'people': set(), 'requests': 0, 'completed': 0, 'pending': 0}], 2)
        self.assertFalse(empty[0]['suppressed'])
        self.assertEqual(empty[0]['people'], 0)

    def test_public_integrity_rejects_leaked_suppressed_count(self):
        document = public_report(self.dataset, self.report, 2)
        document['cells'][0]['people'] = 1
        document['publication_digest'] = digest({key: value for key, value in document.items() if key != 'publication_digest'})
        with self.assertRaisesRegex(ValueError, 'leaks'):
            verify_public(document)
        document = public_report(self.dataset, self.report, 2)
        document['person'] = 'p1'
        document['publication_digest'] = digest({key: value for key, value in document.items() if key != 'publication_digest'})
        with self.assertRaisesRegex(ValueError, 'identifier'):
            verify_public(document)
        document = public_report(self.dataset, self.report, 2)
        document['minimum_people'] = 9
        with self.assertRaises(ValueError):
            verify_public(document)
        self.assertEqual(self.journal.audit()['releases'], 0)

    def test_pseudonym_tokens_are_domain_separated(self):
        secret = b'x' * 32
        first = token(secret, 'release-a', 'p1')
        self.assertEqual(first, token(secret, 'release-a', 'p1'))
        self.assertNotEqual(first, token(secret, 'release-b', 'p1'))
        self.assertNotEqual(first, token(b'y' * 32, 'release-a', 'p1'))
        with self.assertRaises(ValueError):
            token(b'short', 'release-a', 'p1')
        result = restricted_paths(self.dataset, self.report, secret, 'release-a')
        self.assertEqual(len(result['rows']), 6)
        self.assertEqual(result['access'], 'restricted_pseudonymous_not_anonymous')
        self.assertNotIn('p1', canonical(result['rows']))
        self.assertIn('reidentification', result['warning'])
        self.assertEqual(result, restricted_paths(self.dataset, self.report, secret, 'release-a'))

    def test_archive_checksums_and_semantic_replay(self):
        exported = archive.export(self.dataset, self.report, self.root / 'archive', 2)
        self.assertTrue(exported['valid'])
        self.assertEqual(exported['files'], 5)
        self.assertEqual(archive.inspect(self.root / 'archive'), exported)
        manifest = json.loads((self.root / 'archive/manifest.json').read_text())
        self.assertEqual(manifest['input_digest'], self.dataset.fingerprint)
        self.assertIn('restricted', manifest['access'])
        with self.assertRaises(FileExistsError):
            archive.export(self.dataset, self.report, self.root / 'archive', 2)
        extra = self.root / 'archive/extra'
        extra.write_text('extra')
        with self.assertRaises(ValueError):
            archive.inspect(self.root / 'archive')
        extra.unlink()
        (self.root / 'archive/public.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            archive.inspect(self.root / 'archive')

    def test_archive_rejects_semantic_forgery_even_with_new_checksum(self):
        archive.export(self.dataset, self.report, self.root / 'archive', 2)
        target = self.root / 'archive/public.json'
        target.write_text('{"forged":true}')
        manifest_path = self.root / 'archive/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['public.json'] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, 'semantic'):
            archive.inspect(self.root / 'archive')
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.assertEqual(self.journal.audit()['revision'], 1)
        self.assertEqual(target.read_text(), '{"forged":true}')
        self.assertEqual(manifest['version'], 1)

    def test_backup_restore_and_rebuild_preserve_versions_and_release(self):
        self.journal.release('public', 'baseline', 2)
        changed = copy.deepcopy(self.data)
        changed['people'][0]['county'] = 'west'
        self.journal.replace('correction', 1, changed)
        saved = recovery.backup(self.journal, self.root / 'backup.db')
        restored = recovery.restore(self.root / 'backup.db', self.root / 'restored.db', saved['sha256'])
        self.assertEqual(restored['audit'], saved['audit'])
        with Journal(self.root / 'restored.db') as journal:
            self.assertEqual(journal.revision, 2)
            self.assertEqual(journal.dataset(1).fingerprint, self.dataset.fingerprint)
            self.assertEqual(journal.analysis('baseline'), self.record)
            self.assertEqual(journal.publication('public'), self.journal.publication('public'))
        rebuilt = recovery.rebuild(self.journal, self.root / 'rebuilt.db')
        self.assertTrue(rebuilt['replayed'])
        self.assertEqual(rebuilt['audit'], saved['audit'])
        self.assertEqual(rebuilt['source_digest'], self.journal.dataset().fingerprint)

    def test_restore_refuses_wrong_checksum_and_existing_target(self):
        saved = recovery.backup(self.journal, self.root / 'backup.db')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            recovery.restore(self.root / 'backup.db', self.root / 'invalid.db', '0' * 64)
        self.assertFalse((self.root / 'invalid.db').exists())
        with self.assertRaises(FileExistsError):
            recovery.backup(self.journal, self.root / 'backup.db')
        with self.assertRaises(FileExistsError):
            recovery.restore(self.root / 'backup.db', self.root / 'care.db', saved['sha256'])
        with self.assertRaises(FileExistsError):
            recovery.rebuild(self.journal, self.root / 'care.db')
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.assertTrue(self.journal.audit()['valid'])

    def test_journal_detects_head_and_event_corruption(self):
        self.journal.connection.execute("UPDATE heads SET body='{}' WHERE table_name='people' AND entity='p1'")
        with self.assertRaises(ValueError):
            self.journal.audit()
        self.assertEqual(self.journal.dataset().fingerprint, self.dataset.fingerprint)
        self.journal.connection.execute("UPDATE heads SET body=? WHERE table_name='people' AND entity='p1'", (canonical(self.dataset.people['p1']),))
        self.assertTrue(self.journal.audit()['valid'])
        self.journal.connection.execute("UPDATE changes SET before_digest='forged' WHERE revision=1 AND ordinal=0")
        with self.assertRaisesRegex(ValueError, 'journal'):
            self.journal.audit()
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.assertEqual(self.journal.revision, 1)

    def test_analysis_and_publication_corruption_is_detected(self):
        self.journal.release('public', 'baseline', 2)
        self.journal.connection.execute("UPDATE releases SET fingerprint='forged' WHERE label='public'")
        with self.assertRaises(ValueError):
            self.journal.publication('public')
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.journal.connection.execute("UPDATE analyses SET fingerprint='forged' WHERE id='baseline'")
        with self.assertRaises(ValueError):
            self.journal.analysis('baseline')
        with self.assertRaises(ValueError):
            self.journal.audit()
        self.assertEqual(self.journal.dataset().fingerprint, self.dataset.fingerprint)
        self.assertEqual(self.journal.revision, 1)

    def test_query_pagination_context_and_projection(self):
        first = page(self.report, 'waits', columns=['id', 'duration'], limit=1)
        second = page(self.report, 'waits', columns=['id', 'duration'], limit=1, cursor=first['next_cursor'])
        self.assertEqual(first['total'], 3)
        self.assertNotEqual(first['rows'][0]['id'], second['rows'][0]['id'])
        with self.assertRaises(ValueError):
            page(self.report, 'waits', columns=['id'], cursor=first['next_cursor'])
        with self.assertRaises(ValueError):
            page(self.report, 'chains', columns=['person'], cursor=first['next_cursor'])
        self.assertEqual(page(self.report, 'waits', filters={'event': [False]})['total'], 1)
        with self.assertRaises(ValueError):
            page(self.report, 'waits', filters={'event': [1]})
        with self.assertRaises(ValueError):
            page(self.report, 'waits', columns=['id', 'id'])
        with self.assertRaises(ValueError):
            page(self.report, 'waits', cursor='?')

    def test_person_detail_and_csv_have_expected_business_data(self):
        detail = person_detail(self.dataset, self.report, 'p1')
        self.assertEqual([row['id'] for row in detail['visits']], ['v1', 'v2', 'v3'])
        self.assertEqual([row['id'] for row in detail['referrals']], ['r1', 'r2'])
        self.assertEqual(len(detail['chains']), 1)
        self.assertEqual(detail['access'], 'restricted_identifiable')
        with self.assertRaises(KeyError):
            person_detail(self.dataset, self.report, 'absent')
        text = csv_view(self.report, 'waits')
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn('r3', text)
        with self.assertRaises(ValueError):
            csv_view(self.report, 'unknown')
        self.assertEqual(dispatch(self.dataset, self.report, 'summary'), self.report['population'])
        with self.assertRaises(ValueError):
            dispatch(self.dataset, self.report, 'summary', {'extra': True})

    def test_service_routes_enforce_request_schema_and_conflict(self):
        self.assertEqual(route(self.journal, 'GET', '/status', None)['revision'], 1)
        stored = route(self.journal, 'GET', '/analyses/baseline', None)
        self.assertEqual(stored, self.record)
        result = route(self.journal, 'POST', '/query', {'id': 'baseline', 'kind': 'summary'})
        self.assertEqual(result, self.report['population'])
        with self.assertRaises(ValueError):
            route(self.journal, 'POST', '/batches', {'id': 'bad'})
        with self.assertRaises(Conflict):
            route(self.journal, 'POST', '/batches', {'id': 'bad', 'base_revision': 0, 'operations': []})
        with self.assertRaises(KeyError):
            route(self.journal, 'GET', '/unknown', None)
        with self.assertRaises(ValueError):
            handler(self.root / 'care.db', 'too-short')
        self.assertEqual(route(self.journal, 'POST', '/audit', {})['analyses'], 1)

    def test_revision_impact_retains_frozen_analysis(self):
        altered = copy.deepcopy(self.data)
        altered['people'][0]['county'] = 'west'
        self.journal.replace('update', 1, altered)
        result = revision_impact(self.journal, 1, 2)
        self.assertEqual(result['difference']['affected_people'], ['p1'])
        self.assertTrue(result['prior_analyses'][0]['snapshot_remains_immutable'])
        self.assertTrue(result['prior_analyses'][0]['changed_source_since_snapshot'])
        history = entity_history(self.journal, 'people', 'p1')
        self.assertEqual(len(history['changes']), 2)
        self.assertEqual(history['current']['county'], 'west')
        summary = journal_summary(self.journal)
        self.assertEqual(summary['revision'], 2)
        self.assertEqual(summary['analyses'][0]['id'], 'baseline')
        self.assertEqual(self.journal.analysis('baseline'), self.record)

    def test_all_plan_dispatch_families_execute_real_analytics(self):
        cases = [('group', {'dimension': 'county'}), ('windows', {'horizons': [1, 7]}),
                 ('monthly', {}), ('outcomes', {}), ('orphans', {}), ('cross-county', {}),
                 ('person-timeline', {'person': 'p1', 'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('stage-days', {'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('turnover', {'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('waiting-lineage', {'referral_id': 'r1'}), ('chain-lineage', {'root': 'v1'}),
                 ('evidence-index', {}), ('source-visibility', {}), ('person-coverage', {}),
                 ('destination-coverage', {}), ('partition-audit', {}),
                 ('facility-coverage', {'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('period-events', {'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('period-mature', {'start': '2026-11-01', 'end': '2026-11-12'}),
                 ('period-alarms', {'start': '2026-11-01', 'end': '2026-11-12'})]
        definition = {'version': 1, 'operations': [{'id': 'op' + str(index), 'kind': kind, 'options': options}
                                                  for index, (kind, options) in enumerate(cases)]}
        result = run(self.dataset, self.report, definition)
        self.assertEqual(len(result['results']), len(cases))
        self.assertTrue(verify(self.dataset, self.report, result)['valid'])
        by_kind = {row['kind']: row['result'] for row in result['results']}
        self.assertEqual(by_kind['partition-audit']['included'], 3)
        self.assertEqual(by_kind['person-coverage']['observed_people'], 5)
        self.assertEqual(sum(row['admissions'] for row in by_kind['turnover']['rows']), 8)
        self.assertEqual(by_kind['waiting-lineage']['observation']['duration'], 2)
        self.assertEqual(by_kind['chain-lineage']['elapsed_days'], 11)
        self.assertEqual(sum(row['requested'] for row in by_kind['monthly']['rows']), 5)

    def test_cli_success_error_and_demo_are_observable(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = continuity.main(['analyze', '--input', str(self.root / 'missing'),
                                    '--as-of', '2026-11-12', '--known-at', '2026-11-12'])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('error', json.loads(errors.getvalue()))
        output = io.StringIO()
        with redirect_stdout(output):
            code = continuity.main(['query', '--database', str(self.root / 'care.db'), '--id', 'baseline', '--kind', 'summary'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), self.report['population'])
        result = continuity.demo('fixtures/care.json')
        self.assertEqual(result['population']['visits'], 8)
        self.assertTrue(result['audit']['valid'])
        self.assertTrue(result['restored']['restored'])
        self.assertTrue(result['rebuilt']['replayed'])

    def test_http_authentication_queries_and_conflict_statuses(self):
        from http.server import ThreadingHTTPServer
        from http.client import HTTPConnection
        from threading import Thread
        secret = 's' * 32
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.root / 'care.db', secret))
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', '/status')
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            self.assertEqual(json.loads(response.read())['error'], 'authentication_required')
            connection.close()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'}
            connection.request('POST', '/query', canonical({'id': 'baseline', 'kind': 'summary'}), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), self.report['population'])
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            connection.close()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('POST', '/batches', canonical({'id': 'stale', 'base_revision': 0, 'operations': []}), headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            self.assertIn('stale', json.loads(response.read())['error'])
            connection.close()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('GET', '/analyses/missing', headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            self.assertEqual(json.loads(response.read())['error'], 'not_found')
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.journal.revision, 1)

    def test_service_catalog_and_lineage_routes_match_snapshots(self):
        result = route(self.journal, 'POST', '/catalog', {'table': 'people', 'limit': 2})
        self.assertEqual([row['id'] for row in result['rows']], ['p1', 'p2'])
        self.assertEqual(result['next_after'], 'p2')
        result = route(self.journal, 'GET', '/inventory', None)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['inventory']['tables']['visits'], 8)
        result = route(self.journal, 'POST', '/entity-history', {'table': 'people', 'id': 'p1'})
        self.assertEqual(len(result['changes']), 1)
        self.assertTrue(result['exists'])
        result = route(self.journal, 'POST', '/entity-lineage', {'table': 'people', 'id': 'p1'})
        self.assertEqual(result['related_visits'], ['v1', 'v2', 'v3'])
        result = route(self.journal, 'POST', '/revision-impact', {'before': 0, 'after': 1})
        self.assertEqual(len(result['difference']['affected_people']), 5)
        self.assertTrue(result['published_releases_not_rewritten'])
        self.assertEqual(self.journal.audit()['analyses'], 1)

    def test_snapshot_batch_binding_detects_corruption(self):
        self.journal.connection.execute('PRAGMA foreign_keys=OFF')
        self.journal.connection.execute("UPDATE versions SET batch_id='missing' WHERE revision=1")
        with self.assertRaises(ValueError):
            self.journal.audit()
        self.journal.connection.execute("UPDATE versions SET batch_id='first' WHERE revision=1")
        self.journal.connection.execute('PRAGMA foreign_keys=ON')
        self.assertTrue(self.journal.audit()['valid'])
        self.assertEqual(self.journal.analysis('baseline'), self.record)
        self.assertEqual(self.journal.dataset(0).visits, {})
        self.assertEqual(self.journal.revision, 1)

    def test_empty_dataset_can_be_analyzed_without_fake_rates(self):
        empty = self.journal.dataset(0)
        result = analyze(empty, '2026-11-12', '2026-11-12')
        self.assertEqual(result['population']['people'], 0)
        self.assertEqual(result['chains']['chains'], [])
        self.assertEqual(result['waiting']['curve']['population'], 0)
        self.assertIsNone(result['waiting']['restricted_mean']['mean_days'])
        self.assertEqual(result['completion_rates']['rows'], [])
        self.assertEqual(result['quality']['issues'], [])
        public = public_report(empty, result, 2)
        self.assertEqual(public['cells'], [])
        self.assertEqual(public['totals']['people'], 0)
        self.assertTrue(verify_public(public)['valid'])
        self.assertEqual(result['readmissions']['denominator'], 0)
        self.assertEqual(result['care_spells']['curve']['rows'], [])
