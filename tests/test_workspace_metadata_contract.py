"""Remote renames fail closed, without rewriting authority or remote objects."""
import json
import unittest
import test_workspace_reconcile as fixtures
from finix import core

class MetadataContract(unittest.TestCase):
    def fixture(self):
        f = fixtures.ReconcileTests('test_lost_ack_reconciles_same_remote_id_and_committed_replay')
        f.setUp(); self.addCleanup(f.doCleanups)
        return f

    def test_committed_file_and_folder_rename_refused(self):
        for field in ('id', 'folder_id'):
            with self.subTest(field=field):
                f = self.fixture(); row = f.drive.reconcile(f.path)
                before = (f.state/'workspace.json').read_bytes()
                f.transport.objects[row[field]]['name'] = 'foreign-name'
                with self.assertRaisesRegex(core.FinixError, 'CLOUD_METADATA'):
                    f.drive.reconcile(f.path)
                self.assertEqual((f.state/'workspace.json').read_bytes(), before)
                self.assertEqual(sum(op == 'upload' for op, _ in f.transport.calls), 1)

    def test_pending_file_and_folder_rename_refused(self):
        for field in ('id', 'folder_id'):
            with self.subTest(field=field):
                f = self.fixture(); f.transport.lost = True
                with self.assertRaises(core.FinixError): f.drive.reconcile(f.path)
                before = (f.state/'workspace-pending.json').read_bytes()
                row = json.loads(before)['record']
                f.transport.objects[row[field]]['name'] = 'foreign-name'
                with self.assertRaisesRegex(core.FinixError, 'CLOUD_METADATA'):
                    f.drive.reconcile(f.path)
                self.assertEqual((f.state/'workspace-pending.json').read_bytes(), before)
                self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])

    def test_expected_names_persist_in_record_and_legacy_missing_names_remain_readable(self):
        f = self.fixture(); row = f.drive.reconcile(f.path)
        self.assertEqual(row['name'], f.transport.objects[row['id']]['name'])
        self.assertEqual(row['folder_name'], f.transport.objects[row['folder_id']]['name'])
        journal = json.loads((f.state/'workspace.json').read_text())
        for target in (journal, journal['records'][0]):
            target.pop('name', None); target.pop('folder_name', None)
        core.atomic_json(f.state/'workspace.json', journal)
        before = (f.state/'workspace.json').read_bytes()
        f.transport.objects[row['id']]['name'] = 'legacy-externally-renamed'
        self.assertTrue(f.drive.reconcile(f.path)['verified'])
        self.assertEqual((f.state/'workspace.json').read_bytes(), before)

if __name__ == '__main__': unittest.main()
