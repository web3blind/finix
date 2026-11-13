"""Durable preparation state, real kernel leases, no live default or SDK."""
import contextvars
import os
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from finix import core


class PreviewJobTests(unittest.TestCase):
    def setUp(self):
        from finix import default_recovery_api as api
        self.api = api
        import uuid
        self.root=core.private_dir(Path(os.environ['FINIX_PREVIEW_TEST_ROOT'])/uuid.uuid4().hex)
        self.profile=SimpleNamespace(home=self.root,profile_id='a'*64)
        self.patches=[patch.object(api,'authority',return_value=self.root),patch.object(api,'activation',return_value='epoch')]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def manager(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('finix.default_preview_jobs'),'missing durable preview owner')
        from finix.default_preview_jobs import PreviewJobs
        return PreviewJobs()

    def wait(self, manager, job):
        for _ in range(300):
            result=manager.status(self.profile,job)
            if result['state']!='running':return result
            time.sleep(.01)
        self.fail('worker did not stop')

    def test_prompt_ack_and_readable_status_while_worker_holds_operation(self):
        manager=self.manager();entered=threading.Event();release=threading.Event()
        def work(): entered.set();release.wait(5);return {'sealed':True}
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda p,**kw:nullcontext()):
            start=time.monotonic();ack=manager.begin(self.profile,'b'*32,work)
            self.assertLess(time.monotonic()-start,1)
            self.assertEqual(ack['state'],'running');self.assertTrue(entered.wait(1))
            try:
                self.assertEqual(manager.status(self.profile,'b'*32)['state'],'running')
                replay=manager.begin(self.profile,'b'*32,lambda:self.fail('duplicate work'))
                self.assertEqual(replay['id'],ack['id'])
                with self.assertRaises(Exception):manager.begin(self.profile,'c'*32,lambda:None)
            finally:release.set()
            result=self.wait(manager,'b'*32)
            self.assertEqual(result['state'],'succeeded');self.assertEqual(result['result'],{'sealed':True})

    def test_failed_original_replay_is_immutable_and_keeps_request_context(self):
        manager=self.manager();context=contextvars.ContextVar('preview_test_context');context.set('authenticated')
        def work():
            self.assertEqual(context.get(),'authenticated')
            raise self.api.RecoveryError('PREVIEW_DEADLINE')
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda p,**kw:nullcontext()):
            manager.begin(self.profile,'d'*32,work)
            result=self.wait(manager,'d'*32)
            self.assertEqual(result['state'],'failed');self.assertEqual(result['error'],'PREVIEW_DEADLINE')
            self.assertEqual(manager.begin(self.profile,'d'*32,lambda:self.fail('failed replay mutated')),result)

    def test_detached_dispatch_is_nonce_bound_and_exact_replay_never_relaunches(self):
        manager=self.manager();self.assertTrue(callable(getattr(manager,'dispatch',None)),'missing detached CLI preparation')
        entered=threading.Event();release=threading.Event();children=[]
        def work():entered.set();release.wait(5);return {'sealed':True}
        def launch(job,nonce):
            child=threading.Thread(target=lambda:manager.claim(self.profile,job,nonce,work))
            children.append(child);child.start()
            return SimpleNamespace(poll=lambda:None)
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()):
            intent={'copy_id':'f'*64,'identity':'synthetic-key-reference'}
            answer=manager.dispatch(self.profile,'f'*32,intent,launch)
            self.assertEqual(answer['state'],'running');self.assertTrue(entered.wait(1))
            try:
                replay=manager.dispatch(self.profile,'f'*32,intent,lambda *a:self.fail('relaunch'))
                self.assertEqual(replay['id'],answer['id'])
                with self.assertRaises(Exception):manager.claim(self.profile,'f'*32,'wrong',lambda:None)
            finally:release.set()
            for child in children:child.join(2)
            self.assertEqual(self.wait(manager,'f'*32)['state'],'succeeded')

    def test_dead_dispatch_is_interrupted_and_cannot_admit_another_operation(self):
        manager=self.manager()
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()):
            with self.assertRaises(Exception) as launch_error:
                manager.dispatch(self.profile,'1'*32,{'copy_id':'a'*64,'identity':'unopened'},lambda *a:(_ for _ in ()).throw(RuntimeError('lost launch')))
            self.assertEqual(getattr(launch_error.exception,'code',None),'PREVIEW_ACK_UNCERTAIN')
            state=manager.status(self.profile,'1'*32)
            self.assertEqual(state['state'],'interrupted')
            self.assertEqual(state['error'],'PREVIEW_INTERRUPTED')
            self.assertEqual(manager.begin(self.profile,'1'*32,lambda:self.fail('restarted dead owner')),state)
            with self.assertRaises(Exception):manager.begin(self.profile,'2'*32,lambda:self.fail('foreign admitted'))
            with self.assertRaises(Exception):manager.assert_available(self.profile)

    def test_reserved_preview_blocks_registered_ordinary_operation_before_commit(self):
        manager=self.manager()
        from finix.recovery_operations import RecoveryOperations
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()):
            with self.assertRaises(self.api.RecoveryError):manager.dispatch(self.profile,'3'*32,{},lambda *a:(_ for _ in ()).throw(RuntimeError('lost launch')))
        service=SimpleNamespace(profile=self.root,profile_id=self.profile.profile_id,activation='epoch',_fence=lambda *a:None)
        operations=RecoveryOperations()
        with patch('finix.recovery_shared_host.check_bootstrap_compatibility'),patch.object(operations,'_execute'):
            with self.assertRaises(Exception) as caught:
                operations.begin(service,request_id='11111111-1111-4111-8111-111111111111',candidate_id='a'*64)
        self.assertEqual(getattr(caught.exception,'code',None),'BUSY')
        self.assertFalse((self.root/'cache/finix/recovery-operations/current.json').exists())

    def test_worker_start_scheduling_does_not_drop_transferred_leases(self):
        manager=self.manager();pending=[]
        class DeferredThread:
            def __init__(self,*,target,**kwargs):pending.append(target)
            def start(self):pass
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()),patch('finix.default_preview_jobs.threading.Thread',DeferredThread):
            manager.begin(self.profile,'4'*32,lambda:{'sealed':True})
            # The thread is scheduled only AFTER begin has returned its ACK.
            pending[0]()
            self.assertEqual(manager.status(self.profile,'4'*32)['state'],'succeeded')

    def test_cancel_from_another_manager_is_durable_and_observed_before_success(self):
        manager=self.manager();entered=threading.Event();release=threading.Event()
        def work():entered.set();release.wait(5);return {'sealed':True}
        from contextlib import nullcontext
        with patch('finix.recovery_http.operation_lock',side_effect=lambda p,**kw:nullcontext()):
            manager.begin(self.profile,'e'*32,work);self.assertTrue(entered.wait(1))
            try:self.manager().cancel(self.profile,'e'*32)
            finally:release.set()
            result=self.wait(manager,'e'*32)
            self.assertEqual(result['state'],'cancelled');self.assertIsNone(result['result'])


    def test_hot_checks_do_not_reread_state_and_late_cancel_still_wins(self):
        from contextlib import nullcontext
        from finix import preview_budget
        manager=self.manager();pending=[];counts=[];clock=[100.0]
        class DeferredThread:
            def __init__(self,*,target,**kwargs):pending.append(target)
            def start(self):pass
        def work():
            for _ in range(200):preview_budget.check()
            counts.append(read.call_count)
            # Another owner cancels after the cached false read, with no clock
            # advance. The unconditional final lease must reject success.
            self.manager().cancel(self.profile,'5'*32)
            return {'sealed':True}
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()),patch('finix.default_preview_jobs.threading.Thread',DeferredThread):
            manager.begin(self.profile,'5'*32,work)
        with patch.object(manager,'_read',wraps=manager._read) as read,patch('time.monotonic',side_effect=lambda:clock[0]):
            pending[0]()
        result=manager.status(self.profile,'5'*32)
        self.assertEqual(result['state'],'cancelled');self.assertIsNone(result['result'])
        self.assertLessEqual(counts[0],2)

    def test_durable_cancel_is_observed_at_next_short_poll(self):
        from contextlib import nullcontext
        from finix import preview_budget
        manager=self.manager();pending=[];clock=[100.0]
        class DeferredThread:
            def __init__(self,*,target,**kwargs):pending.append(target)
            def start(self):pass
        def work():
            preview_budget.check()
            self.manager().cancel(self.profile,'6'*32)
            clock[0]+=1
            preview_budget.check()
            self.fail('durable cancellation was not observed')
        with patch('finix.recovery_http.operation_lock',side_effect=lambda *a,**k:nullcontext()),patch('finix.default_preview_jobs.threading.Thread',DeferredThread):
            manager.begin(self.profile,'6'*32,work)
        with patch('time.monotonic',side_effect=lambda:clock[0]):pending[0]()
        result=manager.status(self.profile,'6'*32)
        self.assertEqual(result['state'],'cancelled');self.assertIsNone(result['result'])


if __name__=='__main__':unittest.main()
