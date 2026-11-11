"""Resume must forward transport controls to the real adapter/runner boundary."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from finix import core
from finix.integration import Profile
from tests import test_backup_jobs_review as fixtures

class ResumeProgress(unittest.TestCase):
    def test_pending_and_committed_resume_forward_real_runner_keywords(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p, adapter, memory, config = fixtures.ReviewRegressions().fixture(Path(tmp))
            seen = []
            def runner(op, *args, progress=None, cancel=None, **kwargs):
                seen.append((op, progress, cancel))
                return memory(op, *args, **kwargs)
            adapter.runner = runner
            with patch.object(Profile, 'rclone', return_value=adapter):
                # Real generated archive, intent and committed ledger.
                result = p.backup()
                path = Path(result['path'])
                intent = dict(archive=path.name, sha256=core.digest_file(path), size=path.stat().st_size, complete=True, omitted_count=0)
                callback = lambda e: None
                cancel = lambda: False
                # Replay after committed-before-receipt interruption.
                seen.clear()
                p._resume_backup(p.config(), intent, progress=callback, cancel=cancel)
                cats = [row for row in seen if row[0]=='cat']
                self.assertTrue(cats)
                self.assertTrue(all(row[1] is callback and row[2] is cancel for row in cats), seen)
                # Recreate the valid durable pending entry of that SAME object;
                # no new name/capture, unknown provider outcomes keep its id.
                with adapter._locked():
                    ledger = adapter.ledger(); row = ledger['records'][0]
                    adapter._intent(ledger, row)
                seen.clear()
                p._resume_backup(p.config(), intent, progress=callback, cancel=cancel)
                cats = [row for row in seen if row[0]=='cat']
                self.assertTrue(cats)
                self.assertTrue(all(row[1] is callback and row[2] is cancel for row in cats), seen)
                self.assertEqual(adapter.ledger()['records'][0]['id'], row['id'])
