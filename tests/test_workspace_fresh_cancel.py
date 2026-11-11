"""Generated-age fresh Profile composition; offline provider, real reconciliation."""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from finix import core
from finix.integration import Profile
from finix.workspace_drive import WorkspaceDrive
import test_workspace_reconcile as fixtures

class FreshCancellation(unittest.TestCase):
    def fixture(self):
        import tempfile, os
        f = fixtures.ReconcileTests('test_lost_ack_reconciles_same_remote_id_and_committed_replay')
        tmp = tempfile.TemporaryDirectory(dir=os.environ['FINIX_OWNED_FIXTURE_ROOT'])
        self.addCleanup(tmp.cleanup)
        f.root = core.private_dir(Path(tmp.name)/'private')
        f.home = core.private_dir(f.root/'home')
        f.transport = fixtures.Transport()
        p = Profile(f.home, default_root=f.home)
        cfg = p.config()
        cfg.update(storage='workspace', recipient=core.keygen(f.root/'generated-key'), retention=0)
        core.atomic_json(p.root/'settings.json', cfg)
        adapter = WorkspaceDrive(p.home, p.profile_id, p.root/'workspace', runner=f.transport)
        return f, p, adapter

    def cancelled_transfer(self, boundary):
        f, p, adapter = self.fixture()
        event = threading.Event(); events = []
        def transport(op, *args, **kwargs):
            result = f.transport(op, *args, **kwargs)
            if op == boundary: event.set()
            return result
        adapter.runner = transport
        with patch.object(p, 'workspace', return_value=adapter):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                p.backup(cancel=event.is_set, progress=events.append)
            pending = json.loads((adapter.state/'workspace-pending.json').read_text())
            self.assertEqual(json.loads((adapter.state/'workspace.json').read_text())['records'], [])
            self.assertFalse(list((p.root/'copies').glob('*.workspace.json')))
            if boundary == 'upload':
                self.assertFalse(any(op == 'download' for op, _ in f.transport.calls))
            checkpoint = next(e for e in events if e['phase'] == 'captured')
            event.clear(); adapter.runner = f.transport
            with patch.object(core, 'backup', side_effect=AssertionError('must not recapture')):
                result = p.backup(resume=checkpoint, cancel=event.is_set, progress=events.append)
        self.assertTrue(result['workspace']['verified'])
        self.assertEqual(result['workspace']['id'], pending['record']['id'])
        self.assertEqual(result['sha256'], checkpoint['sha256'])
        self.assertEqual(sum(op == 'upload' for op, _ in f.transport.calls), 1)
        self.assertFalse((adapter.state/'workspace-pending.json').exists())

    def test_fresh_upload_cancel_pending_then_explicit_resume(self):
        self.cancelled_transfer('upload')

    def test_fresh_readback_cancel_pending_then_explicit_resume(self):
        self.cancelled_transfer('download')

    def test_fresh_happy_path_phase_reset_and_verified_readback(self):
        f, p, adapter = self.fixture(); events = []; cancel = lambda: False
        def transport(op, *args, **kwargs):
            self.assertIs(kwargs.get('cancel'), cancel)
            if op in ('upload', 'download'):
                self.assertEqual(events[-1]['phase'], 'upload' if op == 'upload' else 'readback')
                self.assertIsNone(events[-1]['percent'])
                self.assertIsNone(events[-1]['total_bytes'])
                self.assertEqual(events[-1]['completed_bytes'], 0)
            return f.transport(op, *args, **kwargs)
        adapter.runner = transport
        with patch.object(p, 'workspace', return_value=adapter):
            result = p.backup(cancel=cancel, progress=events.append)
        self.assertTrue(result['workspace']['verified'])
        self.assertTrue(result['complete']); self.assertEqual(result['omitted_count'], 0)
        self.assertTrue(any(op == 'download' for op, _ in f.transport.calls))
        self.assertEqual(len(json.loads((adapter.state/'workspace.json').read_text())['records']), 1)

    def test_cancel_after_capture_before_intent_has_no_provider_io(self):
        f, p, adapter = self.fixture(); event = threading.Event(); captured = []
        def progress(value):
            if value['phase'] == 'captured':
                captured.append(value); event.set()
        with patch.object(p, 'workspace', return_value=adapter):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                p.backup(cancel=event.is_set, progress=progress)
            self.assertEqual(f.transport.calls, [])
            self.assertFalse((adapter.state/'workspace-pending.json').exists())
            event.clear()
            with patch.object(core, 'backup', side_effect=AssertionError('must not recapture')):
                result = p.backup(resume=captured[0], cancel=event.is_set)
            self.assertTrue(result['workspace']['verified'])
            self.assertEqual(result['sha256'], captured[0]['sha256'])

    def test_cancel_before_capture_has_no_capture_or_provider_io(self):
        f, p, adapter = self.fixture()
        with patch.object(core, 'backup', side_effect=AssertionError('no capture')):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                p.backup(cancel=lambda: True)
        self.assertEqual(f.transport.calls, [])

    def test_terminal_cancel_does_not_publish_recovery_receipt_or_prune(self):
        f, p, adapter = self.fixture(); event = threading.Event()
        original = adapter.backup_cipher
        def late_cancel(*args, **kwargs):
            result = original(*args, **kwargs); event.set(); return result
        with patch.object(p, 'workspace', return_value=adapter), patch.object(adapter, 'backup_cipher', side_effect=late_cancel), patch.object(p, '_prune_local', side_effect=AssertionError('no prune')):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                p.backup(cancel=event.is_set)
        self.assertFalse(list((p.root/'copies').glob('*.workspace.json')))

    def test_unverified_adapter_result_never_publishes_receipt(self):
        f, p, adapter = self.fixture()
        original = adapter.backup_cipher
        def unverified(*args, **kwargs):
            return dict(original(*args, **kwargs), verified=False)
        with patch.object(p, 'workspace', return_value=adapter), patch.object(adapter, 'backup_cipher', side_effect=unverified):
            with self.assertRaisesRegex(core.FinixError, 'PENDING_RECONCILIATION'):
                p.backup()
        self.assertFalse(list((p.root/'copies').glob('*.workspace.json')))

    def test_adapter_terminal_cancel_prevents_retention(self):
        f, p, adapter = self.fixture(); event = threading.Event()
        def reconciled(*args, **kwargs):
            event.set(); return {'verified': True}
        with patch.object(adapter, 'reconcile', side_effect=reconciled), patch.object(adapter, 'prune', side_effect=AssertionError('no prune')):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                adapter.backup_cipher(f.root/'unused.age', 1, cancel=event.is_set)

    def test_cancel_after_provider_precheck_before_intent(self):
        f, p, adapter = self.fixture(); event = threading.Event(); events = []
        def transport(op, *args, **kwargs):
            value = f.transport(op, *args, **kwargs)
            if op == 'identity': event.set()
            return value
        adapter.runner = transport
        with patch.object(p, 'workspace', return_value=adapter):
            with self.assertRaisesRegex(core.FinixError, 'cancelled'):
                p.backup(cancel=event.is_set, progress=events.append)
            self.assertEqual([op for op, _ in f.transport.calls], ['identity'])
            self.assertFalse((adapter.state/'workspace-pending.json').exists())
            event.clear(); adapter.runner = f.transport
            checkpoint = next(e for e in events if e['phase'] == 'captured')
            with patch.object(core, 'backup', side_effect=AssertionError('must not recapture')):
                result = p.backup(resume=checkpoint, cancel=event.is_set)
            self.assertTrue(result['workspace']['verified'])
            self.assertEqual(result['sha256'], checkpoint['sha256'])

if __name__ == '__main__': unittest.main()
