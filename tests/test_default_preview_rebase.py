"""Published-base lock compatibility; no retirement authority is introduced."""
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
from finix import core
from finix.recovery_http import operation_lock

class PublishedLockTests(unittest.TestCase):
    def setUp(self):
        self.root=core.private_dir(Path(os.environ['FINIX_PREVIEW_TEST_ROOT'])/uuid.uuid4().hex)
        self.backend=core.private_dir(self.root/'backend')
        self.profile=SimpleNamespace(home=self.root,profile_id='a'*64,directories=lambda:self.backend)

    def test_exact_async_call_accepts_preview_owner_without_backend_lease(self):
        # This is the actual new child call, not a signature-only oracle.
        try:
            with operation_lock(self.profile,preview_job='a'*32,backend_lease=False):
                self.assertTrue((self.root/'cache/finix/recovery-operations/worker.lock').exists())
        except TypeError as exc:
            self.fail('Published operation_lock rejects actual async child call: '+str(exc))

    def test_nonterminal_ordinary_record_still_refuses_preview_owner(self):
        import json
        root=core.private_dir(self.root/'cache/finix/recovery-operations')
        for state in ('running','interrupted','awaiting_key'):
            (root/'current.json').write_text(json.dumps({'state':state}))
            with self.subTest(state=state),self.assertRaises(Exception) as caught:
                with operation_lock(self.profile,preview_job='a'*32,backend_lease=False):
                    self.fail('Nonterminal ordinary owner bypassed')
            self.assertEqual(getattr(caught.exception,'detail',None),'BUSY')

    def test_backend_lease_is_optional_only_for_explicit_child(self):
        with patch.object(core,'locked',side_effect=RuntimeError('backend-positive-control')) as lock:
            with operation_lock(self.profile,preview_job='a'*32,backend_lease=False):pass
            lock.assert_not_called()
            with self.assertRaisesRegex(RuntimeError,'backend-positive-control'):
                with operation_lock(self.profile,preview_job='a'*32):pass

    def test_real_dispatch_gap_excludes_ordinary_lock_and_precommit(self):
        from finix import default_recovery_api as api
        from finix.default_preview_jobs import PreviewJobs
        from finix.recovery_operations import RecoveryOperations
        owner=PreviewJobs();checked=[]
        def launch(job,nonce):
            # Dispatch has persisted intent and released the short operation
            # lease, but no child has claimed the preview worker lease yet.
            with self.assertRaises(Exception) as busy:
                with operation_lock(self.profile):self.fail('ordinary lock admitted in dispatch gap')
            self.assertEqual(getattr(busy.exception,'detail',None),'BUSY')
            service=SimpleNamespace(profile=self.root,profile_id=self.profile.profile_id,activation='epoch',_fence=lambda *a:None)
            ops=RecoveryOperations()
            with patch('finix.recovery_shared_host.check_bootstrap_compatibility'),patch.object(ops,'_execute'):
                with self.assertRaises(Exception) as refused:
                    ops.begin(service,request_id='11111111-1111-4111-8111-111111111111',candidate_id='a'*64)
            self.assertEqual(getattr(refused.exception,'code',None),'BUSY')
            self.assertFalse((self.root/'cache/finix/recovery-operations/current.json').exists())
            checked.append('ordinary-lock-and-precommit-refused')
            raise RuntimeError('modeled launcher died before claim')
        with patch.object(api,'authority',return_value=self.root),patch.object(api,'activation',return_value='epoch'):
            with self.assertRaises(api.RecoveryError) as unknown:
                owner.dispatch(self.profile,'b'*32,{},launch)
            self.assertEqual(unknown.exception.code,'PREVIEW_ACK_UNCERTAIN')
            self.assertEqual(checked,['ordinary-lock-and-precommit-refused'])
            self.assertEqual(owner.status(self.profile,'b'*32)['state'],'interrupted')
            with self.assertRaises(Exception) as preserved:
                with operation_lock(self.profile):pass
            self.assertEqual(getattr(preserved.exception,'detail',None),'BUSY')

    def test_ordinary_lock_checks_preview_reservation(self):
        with patch('finix.default_preview_jobs.PreviewJobs.assert_available',side_effect=RuntimeError('reservation-positive-control')) as available:
            with self.assertRaisesRegex(RuntimeError,'reservation-positive-control'):
                with operation_lock(self.profile):pass
            available.assert_called_once_with(self.profile)
            with operation_lock(self.profile,preview_job='a'*32):pass
            self.assertEqual(available.call_count,1)

if __name__=='__main__':unittest.main()
