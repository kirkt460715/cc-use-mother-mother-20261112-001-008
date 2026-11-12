import copy
import json
from pathlib import Path
import tempfile
import unittest
from encounters.values import day, days, shift, integer, identifier, load, digest, Invalid, Conflict
from encounters.schema import Dataset
from encounters.journal import Journal
from encounters.exchange import encode_table, decode_table, import_tables, export_tables, operations_from_ndjson
from encounters.differences import compare, patch, explain_entity
from encounters.mapping import translate, validate_mapping, ingest
from encounters.catalog_query import inventory, table_page, journal_summary, revision_impact, entity_history


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path('fixtures/care.json').read_text())
        self.dataset = Dataset(self.data)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / 'care.db')
        self.imported = self.journal.replace('first', 0, self.data)

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_strict_dates_and_identifiers(self):
        self.assertEqual(day('2024-02-29').day, 29)
        self.assertEqual(days('2026-11-01', '2026-11-12'), 11)
        self.assertEqual(shift('2026-11-01', -1), '2026-10-31')
        self.assertEqual(identifier('county:1'), 'county:1')
        for value in ('2026-2-01', '2026-02-29', '1999-01-01', 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    day(value)
        with self.assertRaises(ValueError):
            days('2026-11-12', '2026-11-01')
        with self.assertRaises(ValueError):
            integer(True, 'integer')
        with self.assertRaises(ValueError):
            identifier('../person')

    def test_input_ownership_and_normalization(self):
        original = self.dataset.fingerprint
        self.data['people'][0]['county'] = 'changed'
        self.assertEqual(self.dataset.people['p1']['county'], 'east')
        exported = self.dataset.export()
        exported['people'][0]['county'] = 'elsewhere'
        self.assertEqual(self.dataset.fingerprint, original)
        reversed_document = self.dataset.export()
        reversed_document['visits'].reverse()
        self.assertEqual(Dataset(reversed_document).fingerprint, original)
        duplicate = self.dataset.export()
        duplicate['visits'].append(copy.deepcopy(duplicate['visits'][0]))
        normalized = Dataset(duplicate)
        self.assertEqual(normalized.duplicates['visits'], 1)
        self.assertEqual(normalized.fingerprint, original)
        duplicate['visits'][-1]['recorded'] = '2026-11-12'
        with self.assertRaises(Conflict):
            Dataset(duplicate)

    def test_schema_rejects_unknown_and_invalid_values(self):
        for table, key, value in [('people', 'rural', 1), ('facilities', 'stage', 'emergency'),
                                  ('visits', 'person', 'missing'), ('visits', 'outcome', 'unknown'),
                                  ('visits', 'discharged', '2026-10-31'), ('referrals', 'source', 'missing')]:
            altered = copy.deepcopy(self.data)
            altered[table][0][key] = value
            with self.subTest(table=table, key=key):
                with self.assertRaises(ValueError):
                    Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['unexpected'] = []
        with self.assertRaises(ValueError):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['version'] = True
        with self.assertRaises(ValueError):
            Dataset(altered)

    def test_referral_cross_person_and_overlapping_link_rejected(self):
        altered = copy.deepcopy(self.data)
        altered['referrals'][0]['person'] = 'p2'
        with self.assertRaisesRegex(ValueError, 'person mismatch'):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['visits'][1]['admitted'] = '2026-11-02'
        altered['referrals'][0]['requested'] = '2026-11-02'
        altered['referrals'][0]['resolved'] = '2026-11-02'
        with self.assertRaisesRegex(ValueError, 'overlap'):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['referrals'][0]['destination_stage'] = 'home'
        with self.assertRaisesRegex(ValueError, 'stage mismatch'):
            Dataset(altered)
        self.assertEqual(self.journal.revision, 1)
        self.assertTrue(self.journal.audit()['valid'])

    def test_accepted_link_requires_discharge_and_known_target(self):
        altered = copy.deepcopy(self.data)
        altered['visits'][0]['discharged'] = None
        altered['visits'][0]['outcome'] = None
        with self.assertRaises(ValueError):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['referrals'][0]['recorded'] = '2026-11-05'
        with self.assertRaisesRegex(ValueError, 'future knowledge'):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['referrals'][0]['target'] = 'v1'
        with self.assertRaises(ValueError):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['referrals'][2]['target'] = 'v6'
        with self.assertRaises(ValueError):
            Dataset(altered)

    def test_duplicate_branch_and_cycle_are_rejected(self):
        altered = copy.deepcopy(self.data)
        duplicate = copy.deepcopy(altered['referrals'][0])
        duplicate['id'] = 'other'
        altered['referrals'].append(duplicate)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            Dataset(altered)
        altered = copy.deepcopy(self.data)
        altered['referrals'][0]['target'] = 'absent'
        with self.assertRaises(ValueError):
            Dataset(altered)
        self.assertEqual(len(self.dataset.referrals), 5)
        self.assertEqual(self.journal.dataset().fingerprint, self.dataset.fingerprint)

    def test_business_and_knowledge_slicing(self):
        early = self.dataset.slice('2026-11-04', '2026-11-12')
        self.assertNotIn('v2', early['visits'])
        self.assertEqual(early['referrals']['r1']['status'], 'pending')
        self.assertIsNone(early['referrals']['r1']['target'])
        self.assertIn('v4', early['visits'])
        late_knowledge = self.dataset.slice('2026-11-04', '2026-11-04')
        self.assertNotIn('r1', late_knowledge['referrals'])
        self.assertEqual(len(late_knowledge['visits']), 5)
        truncated = self.dataset.slice('2026-11-06', '2026-11-12')
        self.assertIsNone(truncated['visits']['v2']['discharged'])
        self.assertIn('v2', truncated['hidden_future_closures'])
        with self.assertRaises(ValueError):
            self.dataset.slice('2026-11-12', '2026-11-11')

    def test_batch_identity_and_revision_rules(self):
        operations = self.journal.connection.execute("SELECT body FROM batches WHERE id='first'").fetchone()[0]
        duplicate = self.journal.ingest('first', 0, json.loads(operations)['operations'])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['revision'], 1)
        with self.assertRaises(Conflict):
            self.journal.ingest('first', 0, [])
        with self.assertRaises(Conflict):
            self.journal.ingest('stale', 0, [])
        self.assertEqual(self.journal.revision, 1)
        self.assertEqual(self.journal.dataset(0).people, {})
        self.assertEqual(self.journal.audit()['batches'], 1)
        self.assertFalse(self.imported['duplicate'])

    def test_optimistic_entity_changes_and_atomic_reference_check(self):
        original = self.dataset.people['p1']
        operation = {'table': 'people', 'id': 'p1', 'before_digest': digest(original),
                     'after': {**original, 'county': 'west'}}
        imported = self.journal.ingest('correction', 1, [operation])
        self.assertEqual(imported['revision'], 2)
        self.assertEqual(self.journal.dataset().people['p1']['county'], 'west')
        with self.assertRaises(Conflict):
            self.journal.ingest('stale-entity', 2, [operation])
        remove = {'table': 'people', 'id': 'p1', 'before_digest': digest(operation['after']), 'after': None}
        with self.assertRaises(ValueError):
            self.journal.ingest('invalid-delete', 2, [remove])
        self.assertEqual(self.journal.revision, 2)
        self.assertEqual(self.journal.audit()['batches'], 2)
        self.assertEqual(len(self.journal.history('people', 'p1')), 2)

    def test_concurrent_writer_cannot_replace_newer_revision(self):
        with Journal(self.root / 'care.db') as second:
            prior = second.revision
            altered = copy.deepcopy(self.data)
            altered['people'][0]['county'] = 'west'
            self.journal.replace('update', prior, altered)
            with self.assertRaises(Conflict):
                second.replace('other', prior, self.data)
            self.assertEqual(second.revision, 2)
            self.assertEqual(second.dataset().people['p1']['county'], 'west')
        self.assertTrue(self.journal.audit()['valid'])
        self.assertEqual(self.journal.dataset(1).fingerprint, self.dataset.fingerprint)

    def test_nested_transactions_preserve_outer_work(self):
        with self.journal.transaction():
            self.journal.ingest('empty', 1, [])
            with self.assertRaises(RuntimeError):
                with self.journal.transaction():
                    self.journal.ingest('inner', 2, [])
                    raise RuntimeError('abort nested')
            self.assertEqual(self.journal.revision, 2)
        self.assertEqual(self.journal.audit()['batches'], 2)
        with self.assertRaises(RuntimeError):
            with self.journal.transaction():
                self.journal.ingest('abort', 2, [])
                raise RuntimeError('abort outer')
        self.assertEqual(self.journal.revision, 2)

    def test_csv_roundtrip_and_invalid_records(self):
        for table in ('facilities', 'people', 'visits', 'referrals'):
            with self.subTest(table=table):
                text = encode_table(self.dataset, table)
                self.assertEqual(decode_table(table, text), self.dataset.document[table])
        exported = export_tables(self.dataset, self.root / 'tables')
        self.assertEqual(exported['input_digest'], self.dataset.fingerprint)
        self.assertEqual(import_tables(self.root / 'tables').fingerprint, self.dataset.fingerprint)
        with self.assertRaises(FileExistsError):
            export_tables(self.dataset, self.root / 'tables')
        with self.assertRaises(ValueError):
            decode_table('people', 'id,id\na,b\n')
        text = encode_table(self.dataset, 'people')
        with self.assertRaises(ValueError):
            decode_table('people', text.replace('false', 'False'))
        with self.assertRaises(ValueError):
            decode_table('people', text + 'short\n')

    def test_json_and_exchange_reject_ambiguous_files(self):
        path = self.root / 'input.json'
        path.write_text('{"a":1,"a":2}')
        with self.assertRaises(ValueError):
            load(path)
        path.write_text('{"a":NaN}')
        with self.assertRaises(ValueError):
            load(path)
        link = self.root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            load(link)
        export_tables(self.dataset, self.root / 'tables')
        (self.root / 'tables/extra.csv').write_text('id\n')
        with self.assertRaises(ValueError):
            import_tables(self.root / 'tables')
        self.assertTrue(path.exists())

    def test_difference_patch_reconstructs_modified_dataset(self):
        altered = copy.deepcopy(self.data)
        altered['people'][0]['county'] = 'west'
        after = Dataset(altered)
        difference = compare(self.dataset, after)
        self.assertEqual(difference['affected_people'], ['p1'])
        self.assertIn('v1', difference['affected_visits'])
        self.assertIn('r1', difference['affected_referrals'])
        self.assertTrue(difference['recompute_all_cohort_statistics'])
        planned = patch(self.dataset, after)
        self.journal.ingest('patch', 1, planned['operations'])
        self.assertEqual(self.journal.dataset().fingerprint, after.fingerprint)
        self.assertEqual(len(planned['operations']), 1)
        self.assertEqual(difference['changes'][0]['fields'], ['county'])

    def test_catalog_lineage_inventory_and_paging(self):
        detail = explain_entity(self.dataset, 'people', 'p1')
        self.assertEqual(detail['record_digest'], digest(self.dataset.people['p1']))
        self.assertEqual(detail['related_visits'], ['v1', 'v2', 'v3'])
        self.assertEqual(detail['related_referrals'], ['r1', 'r2'])
        totals = inventory(self.dataset)
        self.assertEqual(totals['tables']['visits'], 8)
        first = table_page(self.dataset, 'people', limit=2)
        self.assertEqual(first['next_after'], 'p2')
        last = table_page(self.dataset, 'people', after='p4', limit=2)
        self.assertIsNone(last['next_after'])
        self.assertEqual([row['id'] for row in last['rows']], ['p5'])
        with self.assertRaises(ValueError):
            table_page(self.dataset, 'unknown')

    def test_ndjson_and_mapping_reject_partial_data(self):
        path = self.root / 'changes.ndjson'
        path.write_text('\n')
        with self.assertRaises(ValueError):
            operations_from_ndjson(path)
        definition = {'version': 1, 'table': 'people',
                      'columns': {'id': 'person', 'county': 'area', 'age_band': 'age', 'rural': 'rural'},
                      'defaults': {}, 'values': {'rural': {'Y': True, 'N': False}}}
        translated = translate('person,area,age,rural\np6,east,adult,Y\np7,east,adult,?\n', definition)
        self.assertFalse(translated['complete'])
        self.assertEqual(translated['accepted'], 1)
        self.assertEqual(translated['rejected'][0]['line'], 3)
        with self.assertRaises(ValueError):
            ingest(self.journal, 'partial', 1, 'person,area,age,rural\np6,east,adult,?\n', definition)
        self.assertEqual(self.journal.revision, 1)
        result = ingest(self.journal, 'mapped', 1, 'person,area,age,rural\np6,east,adult,Y\n', definition)
        self.assertEqual(result['changed_entities'], 1)
        self.assertTrue(self.journal.dataset().people['p6']['rural'])

    def test_repeated_operation_targets_rollback_the_batch(self):
        row = self.dataset.people['p1']
        change = {'table': 'people', 'id': 'p1', 'before_digest': digest(row), 'after': {**row, 'county': 'west'}}
        with self.assertRaises(ValueError):
            self.journal.ingest('duplicate-target', 1, [change, change])
        self.assertEqual(self.journal.revision, 1)
        self.assertEqual(self.journal.dataset().fingerprint, self.dataset.fingerprint)
        self.assertEqual(len(self.journal.history('people', 'p1')), 1)
        self.assertEqual(self.journal.audit()['batches'], 1)
        self.assertEqual(self.journal.dataset().people['p1']['county'], 'east')

    def test_mapping_defaults_and_unknown_columns_are_explicit(self):
        definition = {'version': 1, 'table': 'people', 'columns': {'id': 'person'},
                      'defaults': {'county': 'east', 'age_band': 'older', 'rural': False}, 'values': {}}
        result = translate('person\np6\n', definition)
        self.assertTrue(result['complete'])
        self.assertEqual(result['rows'][0]['id'], 'p6')
        self.assertEqual(result['rows'][0]['age_band'], 'older')
        self.assertFalse(result['rows'][0]['rural'])
        with self.assertRaises(ValueError):
            translate('person,extra\np6,ignored\n', definition)
        permissive = translate('person,extra\np6,ignored\n', definition, strict=False)
        self.assertEqual(permissive['unmapped_columns'], ['extra'])
        self.assertEqual(permissive['accepted'], 1)
        self.assertEqual(self.journal.revision, 1)
