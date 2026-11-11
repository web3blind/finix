"""Independent review reproducer inverted to acceptance; offline providers only."""
import json
import threading
import unittest
from unittest.mock import patch
import test_workspace_reconcile as fixtures
import test_workspace_resume_checkpoint as checkpoints
from finix import core

class CancelContract(unittest.TestCase):
    def fixture(self):
        f = fixtures.ReconcileTests('test_lost_ack_reconciles_same_remote_id_and_committed_replay')
        f.setUp(); self.addCleanup(f.doCleanups)
        return f

    def test_profile_cancel_at_upload_never_commits_and_explicit_resume_converges(self):
        f = self.fixture()
        p = checkpoints.WorkspaceResumeCheckpoint('test_same_bytes_valid_owned_checkpoint')
        p.setUp(); self.addCleanup(p.doCleanups)
        cancelled = threading.Event(); fake = f.transport
        def transport(op, *args, **kwargs):
            result = fake(op, *args, **kwargs)
            if op == 'upload': cancelled.set()
            return result
        f.drive.runner = transport
        with patch.object(p.profile, 'workspace', return_value=f.drive):
            with self.assertRaises(core.FinixError):
                p.profile._resume_backup(p.cfg, p.intent, cancel=cancelled.is_set)
            pending = json.loads((f.state/'workspace-pending.json').read_text())
            self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
            self.assertFalse(any(op == 'download' for op, _ in fake.calls))
            cancelled.clear()
            result = p.profile._resume_backup(p.cfg, p.intent, cancel=cancelled.is_set)
        self.assertEqual(result['workspace']['id'], pending['record']['id'])
        self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)

    def test_cancel_at_readback_leaves_exact_pending_id(self):
        f = self.fixture(); cancelled = threading.Event(); fake = f.transport
        def transport(op, *args, **kwargs):
            result = fake(op, *args, **kwargs)
            if op == 'download': cancelled.set()
            return result
        f.drive.runner = transport
        with self.assertRaises(core.FinixError):
            f.drive.reconcile(f.path, cancel=cancelled.is_set)
        pending = json.loads((f.state/'workspace-pending.json').read_text())
        self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
        cancelled.clear(); f.drive.runner = fake
        self.assertEqual(f.drive.reconcile(f.path)['id'], pending['record']['id'])
        self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)

    def test_native_blocked_transfer_cancel_preserves_pending_and_converges(self):
        import time
        from finix.workspace_drive import WorkspaceRunner
        for boundary in ('upload', 'download'):
            with self.subTest(boundary=boundary):
                f = self.fixture(); cancelled = threading.Event(); fake = f.transport
                script = f.home/'blocked_provider.py'
                script.write_text('import time\ntime.sleep(30)\n')
                native = WorkspaceRunner(f.home, script=script)
                timers = []
                def transport(op, *args, **kwargs):
                    if op == boundary:
                        timer = threading.Timer(.3, cancelled.set)
                        timers.append(timer); timer.start()
                        import sys
                        return native._call([sys.executable, '-I', '-B', str(script)], op, kwargs['cancel'])
                    return fake(op, *args, **kwargs)
                f.drive.runner = transport
                started = time.monotonic()
                with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                    f.drive.reconcile(f.path, cancel=cancelled.is_set)
                for timer in timers: timer.join()
                self.assertLess(time.monotonic()-started, 4)
                pending = json.loads((f.state/'workspace-pending.json').read_text())
                self.assertEqual(json.loads((f.state/'workspace.json').read_text())['records'], [])
                cancelled.clear(); f.drive.runner = fake
                self.assertEqual(f.drive.reconcile(f.path)['id'], pending['record']['id'])

    def test_every_provider_operation_receives_cancel_and_phase_local_progress(self):
        f = self.fixture(); events = []; fake = f.transport
        cancel = lambda: False
        def transport(op, *args, **kwargs):
            self.assertIs(kwargs.get('cancel'), cancel, op)
            if op in {'upload', 'download'}:
                self.assertEqual(events[-1]['phase'], 'upload' if op == 'upload' else 'readback')
                self.assertIsNone(events[-1]['percent'])
                self.assertIsNone(events[-1]['total_bytes'])
            return fake(op, *args, **kwargs)
        f.drive.runner = transport
        self.assertTrue(f.drive.reconcile(f.path, cancel=cancel, progress=events.append)['verified'])
        self.assertTrue(any(e['phase'] == 'readback' and e['percent'] == 100 for e in events))

if __name__ == '__main__': unittest.main()
