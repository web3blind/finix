"""Mounted ASGI protocol: only modeled data service/lifecycle, real state/IO."""
import os,uuid,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from finix import core, recovery_http, default_recovery_api as api
from fastapi import FastAPI
from fastapi.testclient import TestClient

class HTTPPreviewTests(unittest.TestCase):
    def test_ack_loss_can_only_poll_exact_known_job_without_adapter_worker_lock(self):
        root=core.private_dir(Path(os.environ['FINIX_PREVIEW_TEST_ROOT'])/uuid.uuid4().hex)
        profile=SimpleNamespace(home=root,root=core.private_dir(root/'plugin-state'),profile_id='a'*64)
        profile.directories=lambda:profile.root
        app=FastAPI()
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override,get_hermes_home_override
        @app.middleware('http')
        async def authenticated_context(request,call_next):
            token=set_hermes_home_override(str(root))
            try:return await call_next(request)
            finally:reset_hermes_home_override(token)
        router=recovery_http.create_router(lambda:profile);app.include_router(router)
        adapter=router.recovery_adapter;release=threading.Event();entered=threading.Event()
        def prepare(*a,**kw):
            self.assertEqual(get_hermes_home_override(),str(root))
            entered.set();release.wait(5);return {'sealed':True}
        record={'service_id':'c'*32,'service':object()}
        with patch.object(api,'authority',return_value=root),patch.object(api,'activation',return_value='epoch'),patch.object(recovery_http,'binding',return_value='epoch'),patch.object(adapter,'lookup',return_value=record) as lookup,patch.object(adapter,'view',return_value={'state':'succeeded','complete':True}),patch.object(api.DefaultRecoveryAPI,'prepare',side_effect=prepare):
            with TestClient(app,client=('127.0.0.1',1234)) as client:
                body=dict(profile_id=profile.profile_id,recovery_job='b'*32,consent_prepare=True,preview_contract='default-preview-job-v1')
                started=time.monotonic();reply=client.post('/recovery/default/prepare',json=body)
                self.assertEqual(reply.status_code,200,reply.text)
                self.assertLess(time.monotonic()-started,1)
                self.assertEqual(reply.json()['contract'],'default-preview-job-v1')
                self.assertEqual(reply.json()['state'],'running');self.assertTrue(entered.wait(1))
                try:
                    # Discarded POST ACK: no repeat POST, GET known decrypt job id.
                    self.assertTrue(adapter.lock.acquire(False));adapter.lock.release()
                    polled=client.get('/recovery/default/preview/'+body['recovery_job'])
                    self.assertEqual(polled.status_code,200,polled.text)
                    self.assertEqual(polled.json()['state'],'running')
                    cancel=client.post('/recovery/default/preview/'+body['recovery_job']+'/cancel',json={'profile_id':profile.profile_id})
                    self.assertEqual(cancel.status_code,200,cancel.text)
                    self.assertFalse(cancel.json()['cancellable'])
                finally:release.set()
                for _ in range(200):
                    polled=client.get('/recovery/default/preview/'+body['recovery_job'])
                    if polled.json()['state']!='running':break
                    time.sleep(.01)
                self.assertEqual(polled.json()['state'],'cancelled')
                self.assertEqual(lookup.call_count,1)
                # Version refusal never falls back to the old synchronous path.
                body.pop('preview_contract')
                self.assertEqual(client.post('/recovery/default/prepare',json=body).status_code,422)

if __name__=='__main__':unittest.main()
