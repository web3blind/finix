"""Direct retention gates; no executable/provider/network access."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from finix import core, cloud_files
from finix.rclone_storage import RcloneStorage
from finix.workspace_drive import WorkspaceDrive
from finix.drive import Drive


def record(name, complete=None):
    row = {'id': name, 'sha256': 'a' * 64, 'size': 1}
    if complete is not None:
        row['completion'] = {'sha256': row['sha256'], 'complete': complete,
                             'omitted_count': 0 if complete else 1}
    return row


class DirectRetentionTests(unittest.TestCase):
    def test_direct_ledger_prune_never_deletes_with_incomplete_or_unknown_successor(self):
        for cls in (RcloneStorage, WorkspaceDrive):
            for complete in (False, None, 'wrong-digest'):
                adapter = object.__new__(cls)
                adapter.state = Path('unused')
                deleted = set()
                def download(row):
                    if row['id'] in deleted:
                        from finix.rclone_storage import RcloneError
                        raise RcloneError('NOT_FOUND')
                    return {'verified': True}
                def mutate(operation, identifier):
                    deleted.add('old')
                    return {'status': 'trashed', 'fileId': 'old', 'permanent': False}
                adapter.download = Mock(side_effect=download)
                adapter.runner = Mock(side_effect=mutate)
                adapter.metadata = Mock(side_effect=lambda identifier: {'trashed': identifier in deleted})
                adapter.object_path = Mock(return_value='fake')
                new = record('new', True if complete == 'wrong-digest' else complete)
                if complete == 'wrong-digest':
                    new['completion']['sha256'] = 'b' * 64
                journal = {'records': [record('old', True), new]}
                before = copy.deepcopy(journal)
                if cls is RcloneStorage:
                    # This fixture bypasses __init__; supply the state boundary,
                    # not a fake global anchor that can bypass thread ownership.
                    from contextlib import nullcontext
                    adapter._locked = nullcontext
                    adapter._ledger_locked = Mock(return_value=journal)
                with self.subTest(adapter=cls.__name__, complete=complete), patch.object(core, 'atomic_json'):
                    (adapter._prune_locked(journal, 1) if cls is WorkspaceDrive else adapter.prune(journal, 1))
                    self.assertEqual(journal, before)
                    adapter.runner.assert_not_called()

    def test_direct_drive_prune_requires_complete_current_and_protects_unknown_legacy(self):
        for current_complete in (False, None, True):
            adapter = Drive('a' * 64)
            adapter.snapshots = Mock(return_value=[{'id': 'old'}, {'id': 'new'}])
            def checked(identifier, *args, **kwargs):
                row = record(identifier, current_complete if identifier == 'new' else None)
                props = {'sha256': row['sha256'], 'size': '1', 'snapshot': 'b' * 32}
                if 'completion' in row:
                    import json
                    props['completion'] = json.dumps(row['completion'], sort_keys=True)
                return {'id': identifier, 'appProperties': props}
            adapter.checked = Mock(side_effect=checked)
            adapter.call = Mock()
            adapter.get = Mock(return_value=None)
            with self.subTest(complete=current_complete):
                adapter.prune('folder', 'new', 1)
                adapter.call.assert_not_called()

    def test_drive_upload_commits_provenance_and_direct_prune(self):
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cipher.age'
            path.write_bytes(b'age-encryption.org/v1\nsynthetic')
            objects = {}
            adapter = Drive('a' * 64)
            pending = {}
            def response(method, url, metadata, headers):
                pending.clear()
                pending.update(copy.deepcopy(metadata))
                return SimpleNamespace(headers={'Location': 'synthetic-session'})
            def upload_media(uri, staged, size):
                identifier = 'file' + str(len(objects))
                objects[identifier] = dict(copy.deepcopy(pending), id=identifier)
                return {'id': identifier}
            def call(method, suffix, payload=None, **kwargs):
                identifier = suffix[1:]
                if method == 'PATCH':
                    objects[identifier].update(copy.deepcopy(payload))
                elif method == 'DELETE':
                    objects.pop(identifier)
                else:
                    self.fail(method)
            adapter.response = response
            adapter.checked = lambda identifier, *a, **kw: objects.get(identifier, {'id': identifier})
            adapter.call = call
            adapter.get = lambda identifier, **kw: objects.get(identifier)
            adapter.read_media = Mock(return_value={'verified': True})
            adapter.snapshots = lambda folder: list(reversed([{'id': key} for key in objects]))
            complete = dict(sha256=cloud_files.describe(path)['sha256'], complete=True, omitted_count=0)
            with patch('finix.drive.upload_media', side_effect=upload_media):
                old = adapter.upload(path, 'folder', completion=complete)
                legacy = adapter.upload(path, 'folder')
                new = adapter.upload(path, 'folder', keep=1, completion=dict(complete, complete=False, omitted_count=123456789))
                props = objects[new['id']]['appProperties']
                self.assertLessEqual(len('completion') + len(props['completion'].encode()), 124)
                self.assertFalse(adapter.completion(objects[new['id']])['complete'])
                adapter.prune('folder', new['id'], 1)
                self.assertEqual(len(objects), 3)
                successor = adapter.upload(path, 'folder', keep=1, completion=complete)
                self.assertEqual(set(objects), {legacy['id'], successor['id']})

    def test_real_adapter_upload_persists_digestbound_provenance_and_direct_prune(self):
        import json
        from test_rclone_storage import MemoryRunner
        from test_workspace_drive import FakeDrive, ACCOUNT_DIGEST
        for kind in ('rclone', 'workspace'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                home = core.private_dir(base / 'home')
                auth = core.private_dir(base / 'auth')
                core.atomic_json(auth / 'config', {'synthetic': True})
                path = base / 'cipher.age'
                path.write_bytes(b'age-encryption.org/v1\nsynthetic fixture')
                state = base / 'state'
                fake = MemoryRunner() if kind == 'rclone' else FakeDrive()
                def adapter():
                    if kind == 'rclone':
                        return RcloneStorage(home, 'a' * 64, state, str(auth / 'config'), 'cloud', 'backups/Finix', runner=fake)
                    return WorkspaceDrive(home, 'a' * 64, state, runner=fake, account_digest=ACCOUNT_DIGEST)
                complete = {'sha256': cloud_files.describe(path)['sha256'], 'complete': True, 'omitted_count': 0}
                old = adapter().backup_cipher(path, completion=complete)
                path.write_bytes(path.read_bytes() + b'legacy capture')
                legacy = adapter().backup_cipher(path)
                path.write_bytes(path.read_bytes() + b'incomplete capture')
                incomplete = dict(complete, sha256=cloud_files.describe(path)['sha256'], complete=False, omitted_count=1)
                latest = adapter().backup_cipher(path, keep=1, completion=incomplete)
                restarted = adapter()
                journal = restarted.ledger()
                self.assertEqual(journal['records'][-1]['completion'], incomplete)
                restarted.prune(journal, 1)
                self.assertEqual(len(restarted.ledger()['records']), 3)
                path.write_bytes(path.read_bytes() + b'complete successor')
                complete = dict(complete, sha256=cloud_files.describe(path)['sha256'])
                successor = adapter().backup_cipher(path, keep=1, completion=complete)
                remaining = adapter().ledger()['records']
                self.assertEqual({r['id'] for r in remaining}, {legacy['id'], successor['id']})
                if kind == 'rclone':
                    self.assertNotIn(restarted.object_path(old), fake.objects)
                    self.assertNotIn(restarted.object_path(latest), fake.objects)
                else:
                    self.assertTrue(fake.objects[old['id']]['trashed'])
                    self.assertTrue(fake.objects[latest['id']]['trashed'])

    def test_removed_managed_backend_has_no_retention_or_upload_authority(self):
        from finix import connector_drive
        self.assertFalse(hasattr(connector_drive,'ManagedDrive'))
        self.assertFalse(hasattr(connector_drive,'sdk_client'))

    def test_complete_successor_still_protects_unknown_ledger_records(self):
        for cls in (RcloneStorage, WorkspaceDrive):
            adapter = object.__new__(cls)
            adapter.state = Path('unused')
            adapter.download = Mock()
            adapter.runner = Mock()
            adapter.metadata = Mock(return_value={'trashed': True})
            journal = {'records': [record('legacy'), record('new', True)]}
            before = copy.deepcopy(journal)
            if cls is RcloneStorage:
                from contextlib import nullcontext
                adapter._locked = nullcontext
                adapter._ledger_locked = Mock(return_value=journal)
            with self.subTest(adapter=cls.__name__), patch.object(core, 'atomic_json'):
                (adapter._prune_locked(journal, 1) if cls is WorkspaceDrive else adapter.prune(journal, 1))
                self.assertEqual(journal, before)
                adapter.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
