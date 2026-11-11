"""Generated ciphertext continuation, never user credentials or real cloud."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile


class WorkspaceResumeCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        home = core.private_dir(base / 'source')
        (home / 'work').write_text('synthetic work')
        self.profile = Profile(home, default_root=home)
        self.identity = base / 'generated.key'
        self.cfg = self.profile.config()
        self.cfg.update(recipient=core.keygen(self.identity), storage='workspace')
        core.atomic_json(self.profile.root / 'settings.json', self.cfg)
        self.result = core.backup(home, self.profile.root / 'copies', self.profile.root / 'index', self.cfg['recipient'])
        self.path = Path(self.result['path'])
        self.intent = dict(archive=self.path.name, sha256=self.result['sha256'], size=self.path.stat().st_size,
                           complete=self.result['complete'], omitted_count=self.result['omitted_count'])
        self.calls = []
        calls = self.calls
        class Adapter:
            def reconcile(self, path, *, completion=None, progress=None, cancel=None):
                calls.append((path, completion))
                return dict(verified=True, sha256=core.digest_file(path))
        self.adapter = Adapter()

    def run_resume(self):
        with patch.object(self.profile, 'workspace', return_value=self.adapter), \
             patch.object(core, 'backup', side_effect=AssertionError('NO RECAPTURE')):
            return self.profile.backup(resume=self.intent)

    def test_same_bytes_valid_owned_checkpoint(self):
        before = self.path.read_bytes()
        result = self.run_resume()
        self.assertEqual(result['sha256'], self.intent['sha256'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(result['workspace']['verified'])
        self.assertEqual(self.calls, [(self.path, self.intent)])
        core.restore(self.path, self.identity, self.path.parent.parent / 'restored')

    def test_rejects_unowned_digest_size_and_completeness_before_adapter_io(self):
        mutations = ({'sha256': 'a' * 64}, {'size': self.intent['size'] + 1},
                     {'complete': True, 'omitted_count': 1}, {'complete': False, 'omitted_count': 0},
                     {'complete': None, 'omitted_count': 0}, {'complete': 1, 'omitted_count': 0},
                     {'archive': '../escape.age'})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                original = dict(self.intent)
                self.intent.update(mutation)
                with self.assertRaises((core.FinixError, FileNotFoundError)):
                    self.run_resume()
                self.intent = original
                self.assertEqual(self.calls, [])

    def test_unknown_omissions_stay_unknown_no_retention(self):
        index = core.load_state(self.profile.root / 'index')
        row = index['backups'][0]
        row.pop('omitted_count', None)
        core.atomic_json(self.profile.root / 'index' / 'index.json', index)
        self.intent.update(complete=None, omitted_count=None)
        result = self.run_resume()
        self.assertIsNone(result['complete'])
        self.assertIsNone(result['omitted_count'])

    def test_null_index_provenance_stays_unknown(self):
        index = core.load_state(self.profile.root / 'index')
        index['backups'][0].update(complete=None, omitted_count=None)
        core.atomic_json(self.profile.root / 'index' / 'index.json', index)
        self.intent.update(complete=None, omitted_count=None)
        self.assertIsNone(self.run_resume()['complete'])

    def test_changed_index_source_has_no_provider_io(self):
        index = core.load_state(self.profile.root / 'index')
        index['source'] = 'b' * 64
        core.atomic_json(self.profile.root / 'index' / 'index.json', index)
        with self.assertRaises(core.FinixError):
            self.run_resume()
        self.assertEqual(self.calls, [])

    def test_missing_archive_has_no_adapter_io(self):
        self.path.unlink()
        with self.assertRaises((core.FinixError, FileNotFoundError)):
            self.run_resume()
        self.assertEqual(self.calls, [])

    def test_unsafe_old_workspace_adapter_fails_closed(self):
        self.adapter = object()
        with self.assertRaises(core.FinixError):
            self.run_resume()


if __name__ == '__main__':
    unittest.main()
