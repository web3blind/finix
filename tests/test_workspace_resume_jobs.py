"""Request history adoption uses the same archive, never the same failed receipt."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from finix import core
from finix.backup_jobs import BackupJobs, _busy
from finix.integration import Profile
from finix.setup_flow import SetupFlow


class WorkspaceJobAdoption(unittest.TestCase):
    def test_new_request_adopts_checkpoint_preserving_old_failed_replay(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            home = core.private_dir(Path(tmp) / 'home')
            profile = Profile(home, default_root=home)
            cfg = profile.config()
            cfg.update(storage='workspace', recipient='synthetic-only')
            core.atomic_json(profile.root / 'settings.json', cfg)
            jobs = BackupJobs()
            old_request = '11111111-1111-4111-8111-111111111111'
            new_request = '22222222-2222-4222-8222-222222222222'
            checkpoint = dict(archive='retained.age', sha256='a'*64, size=123, complete=True, omitted_count=0)
            class Flow:
                def _binding(self): return SetupFlow(profile)._binding()
                def check_and_run(self, progress=None, resume=False, **kwargs):
                    if not resume:
                        progress(dict(phase='captured', **checkpoint))
                        raise core.FinixError('fixture upload failure')
                    assert resume == checkpoint
                    return dict(verified=True, first_backup=dict(complete=True, omitted_count=0))
            jobs.flow_factory = lambda p: Flow()
            def finish(request):
                ack = jobs.start(profile, profile.profile_id, request)
                for _ in range(500):
                    current = jobs.current(profile)
                    if current['job']['state'] != 'running' and not _busy(profile.root/'backup-jobs'/'worker.lock'):
                        return current
                    time.sleep(.01)
                self.fail('worker timeout')
            old = finish(old_request)
            self.assertEqual(old['job']['state'], 'failed')
            receipt_path = profile.root/'backup-jobs'/'requests'/(old_request+'.json')
            receipt = receipt_path.read_bytes()
            new = finish(new_request)
            self.assertEqual(new['job']['state'], 'succeeded')
            self.assertNotEqual(new['job']['id'], old['job']['id'])
            self.assertEqual(jobs.start(profile, profile.profile_id, old_request), old)
            self.assertEqual(receipt_path.read_bytes(), receipt)
            self.assertEqual(jobs.start(profile, profile.profile_id, new_request), new)


if __name__ == '__main__': unittest.main()
