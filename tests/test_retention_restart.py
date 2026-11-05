"""Interrupted local pruning must recover without deleting changed files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile


class RestartRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        home = root / 'profile'
        home.mkdir(mode=0o700)
        (home / 'config.yaml').write_text('synthetic: true\n')
        self.profile = Profile(home)
        recipient = core.keygen(root / 'identity')
        self.profile.recipient(self.profile.profile_id, recipient, None)

    def test_partial_delete_recovers_on_next_backup(self):
        for _ in range(3):
            self.profile.backup()
        self.profile.configure('retention', '1')
        original = Path.unlink
        calls = 0

        def fail_second_age(path, *args, **kwargs):
            nonlocal calls
            if path.suffix == '.age':
                calls += 1
                if calls == 2:
                    raise OSError('synthetic transient deletion failure')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_second_age):
            with self.assertRaises(OSError):
                self.profile.backup()
        recovered = self.profile.backup()
        index = core.load_state(self.profile.root / 'index')
        self.assertEqual(len(index['backups']), 1)
        self.assertEqual(len(list((self.profile.root / 'copies').glob('*.age'))), 1)
        self.assertEqual(index['backups'][0]['name'], Path(recovered['path']).name)

    def test_changed_candidate_prevents_any_pruning(self):
        results = [self.profile.backup() for _ in range(3)]
        Path(results[1]['path']).write_bytes(b'changed ciphertext')
        self.profile.configure('retention', '1')
        with self.assertRaises(core.FinixError):
            self.profile.backup()
        self.assertTrue(Path(results[0]['path']).exists())
        self.assertEqual(len(list((self.profile.root / 'copies').glob('*.age'))), 4)

    def test_missing_owned_record_is_reconciled(self):
        first = self.profile.backup()
        Path(first['path']).unlink()
        self.profile.configure('retention', '1')
        latest = self.profile.backup()
        index = core.load_state(self.profile.root / 'index')
        self.assertEqual([r['name'] for r in index['backups']], [Path(latest['path']).name])


if __name__ == '__main__':
    unittest.main()
