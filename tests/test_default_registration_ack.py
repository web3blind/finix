"""Metadata registration must ACK before a long source verification."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import uuid

class RegistrationAckTests(TestCase):
    def test_register_defers_expensive_verification_to_worker(self):
        from finix import core,default_recovery_lifecycle as life
        from hermes_constants import get_default_hermes_root
        root=core.private_dir(get_default_hermes_root());directory=core.private_dir(root/'cache/finix/fixture-engine')
        engine=SimpleNamespace(directory=directory,plan_digest='d'*64,_plan=lambda:{'id':'e'*32,'root':str(root),'kept_target':{}},status=lambda:{'state':'prepared'})
        owner={'instance_id':'fixture','authority_epoch':2,'pid':1,'start_time':1,'hermes_home':str(root)}
        confirmation={'consent':{'fixture':True},'runtime':{k:owner[k] for k in owner}}
        controller=life.ColdController(root)
        with patch.object(controller,'confirmation',return_value=confirmation),patch.object(life,'preflight',return_value={}),patch.object(life,'writer_fence',side_effect=lambda r:nullcontext()),patch.object(life,'identify',return_value=owner),patch.object(life,'_exact',return_value=True),patch.object(life,'_verify_engine',side_effect=AssertionError('bulk verification blocks HTTP acknowledgement')) as verify:
            job=controller.register(engine,confirmation)
        self.assertEqual(controller.status(job)['state'],'registered');verify.assert_not_called()

    def test_worker_still_verifies_source_before_drain_or_apply(self):
        from finix import core,default_recovery_lifecycle as life
        from finix.default_recovery import RecoveryError
        from hermes_constants import get_default_hermes_root
        root=core.private_dir(get_default_hermes_root());job=uuid.uuid4().hex;directory=core.private_dir(life._job(root,job))
        record={'job':job,'engine':'e'*32,'plan_digest':'d'*64,'root_identity':life.cold._identity(root),'consent':{'fixture':True},'snapshot':{},'owner':{},'trusted_finix':None}
        life.cold._write(directory/'record.json',record);life.cold._write(directory/'status.json',{'state':'registered','error':None});life.cold._write(directory/'release.json',{'record_digest':life.cold._digest(record)})
        engine=SimpleNamespace(directory=core.private_dir(root/'cache/finix/worker-engine'),consent=lambda:record['consent'],_plan=lambda:{'version':3})
        with patch.object(life.cold,'DefaultRecovery',return_value=engine),patch.object(life,'preflight',return_value={}),patch.object(life,'writer_fence',side_effect=lambda r:nullcontext()),patch.object(life,'_verify_engine',side_effect=RecoveryError('SOURCE_CHANGED')),patch.object(life,'drain') as drain:
            with self.assertRaisesRegex(RecoveryError,'SOURCE_CHANGED'):life.run_job(root,job)
        drain.assert_not_called()
        self.assertEqual(life.ColdController(root).status(job),{'state':'refused','error':'SOURCE_CHANGED'})

    def test_detached_entrypoint_preserves_worker_terminal_refusal(self):
        import sys
        from finix import core,default_recovery_lifecycle as life,default_recovery_worker as worker
        from finix.default_recovery import RecoveryError
        from hermes_constants import get_default_hermes_root
        import hermes_constants
        root=core.private_dir(get_default_hermes_root());job=uuid.uuid4().hex;directory=core.private_dir(life._job(root,job))
        expected={'state':'refused','error':'SOURCE_CHANGED'}
        def fail_before_drain(_root,_job):
            self.assertEqual(_root,root);self.assertEqual(_job,job)
            life._write_status(directory,expected)
            raise RecoveryError('SOURCE_CHANGED')
        # Model only the failure-reporting seam; unchanged audit isolation is
        # not installed globally into the shared unittest process.
        with patch.object(sys,'argv',['worker',str(Path(hermes_constants.__file__).parent),job]),patch.object(sys,'path',list(sys.path)),patch.object(sys,'addaudithook'),patch.object(life,'run_job',side_effect=fail_before_drain):
            with self.assertRaisesRegex(RecoveryError,'SOURCE_CHANGED'):worker.main()
        self.assertEqual(life._read_status(directory),expected)
