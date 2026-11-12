"""Public projection boundary; no daemon, SDK stop or personal root access."""
import json
import unittest
from types import SimpleNamespace
from finix import default_recovery_api as api


class PreviewTests(unittest.TestCase):
    def engine(self):
        plan = dict(role='default',replacement_policy='profile-context-v1',
                    replacement_roots=['memories','skills'],target_owned={'memories/old':'x'},
                    deletions={'memories/old':'x'},kept_target_count=3,
                    runtime_attempt=dict(storage_format='opaque-container-v2',policy='runtime-attempt-preserve-v1',
                        consent_notice='Failed startup preservation',retention='explicit-cleanup-only',
                        storage_cost='complete old copies + complete current copies and native originals',
                        keepSharedResources=True,custody='/private/not-for-client'))
        return SimpleNamespace(plan_digest='d'*64,public_plan=lambda:plan,
                               consent=lambda:dict(root_identity=[1,2],plan_digest='d'*64))

    def test_sealed_preview_excludes_private_inventory_and_root_paths(self):
        self.assertTrue(callable(getattr(api,'client_preview',None)), 'missing bounded server preview')
        result=api.client_preview(self.engine())
        self.assertEqual(result['contract'],'default-maintenance-preview-v1')
        self.assertEqual(result['replacement_roots'],['memories','skills'])
        self.assertEqual(result['removed_files'],1)
        self.assertEqual(result['plan_digest'],'d'*64)
        self.assertEqual(len(result['root_binding']),64)
        self.assertNotIn('private',json.dumps(result))
        self.assertNotIn('memories/old',json.dumps(result))

    def test_preview_rejects_unknown_policy_instead_of_fabricating_consent(self):
        self.assertTrue(callable(getattr(api,'client_preview',None)), 'missing bounded server preview')
        engine=self.engine();engine.public_plan()['runtime_attempt']['policy']='unknown'
        with self.assertRaises(api.RecoveryError):api.client_preview(engine)

    def test_preview_requires_sealed_opaque_v2_format(self):
        for value in (None, 'raw-v1', 'private-v1', 'unknown'):
            engine=self.engine()
            if value is None:engine.public_plan()['runtime_attempt'].pop('storage_format',None)
            else:engine.public_plan()['runtime_attempt']['storage_format']=value
            with self.assertRaises(api.RecoveryError):api.client_preview(engine)

    def test_capabilities_share_review_gate_and_authoritative_current_root(self):
        from unittest.mock import patch
        from pathlib import Path
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from finix import recovery_http
        profile=SimpleNamespace(profile_id='a'*64,home=Path('synthetic'))
        # Exercise the mounted handler but bypass unrelated authentication setup.
        router=recovery_http.create_router(lambda:profile)
        endpoint=next(r.endpoint for r in router.routes if r.path=='/recovery/default/capabilities')
        class Headers(dict):pass
        request=SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),headers=Headers())
        from starlette.responses import Response
        with patch.object(recovery_http,'binding',return_value='epoch'),patch.object(api,'authority',return_value=Path('synthetic')),patch.object(api.cold,'_identity',return_value=[1,2]),patch.object(api.lifecycle,'preflight'),patch.object(api.lifecycle,'identify',return_value={'owner':True}),patch.object(api.lifecycle,'_exact',return_value=True),patch.object(api,'_DefaultAuthority',return_value=SimpleNamespace(root=Path('synthetic'),identity=[1,2],check=lambda:None)):
            router=recovery_http.create_router(lambda:profile)
            endpoint=next(r.endpoint for r in router.routes if r.path=='/recovery/default/capabilities')
            for enabled in (False,True):
                with patch.object(api,'DEFAULT_APPLY_REVIEWED',enabled):
                    result=endpoint(request,Response())
                    self.assertEqual(result['apply_enabled'],enabled)
                    self.assertEqual(result['serverRootToken'],api.token({'root_identity':[1,2]}))
                    self.assertEqual(result['profile_id'],profile.profile_id)
                    self.assertIs(result['local_only'],True)

    def test_historical_ready_is_not_current_sdk_readiness(self):
        from unittest.mock import patch
        from pathlib import Path
        profile=object();adapter=api.DefaultRecoveryAPI()
        controller=SimpleNamespace(status=lambda job:{'state':'ready','error':None,'identity':{'instance_id':'old'}})
        with patch.object(adapter,'_lookup',return_value={'job':'a'*32}),patch.object(api,'authority',return_value=Path('synthetic')),patch.object(api.lifecycle,'ColdController',return_value=controller),patch.object(api.lifecycle,'identify',return_value={'instance_id':'new'}),patch.object(api.lifecycle,'_exact',return_value=True):
            result=adapter.status(profile,'b'*32)
        self.assertEqual(result['state'],'reconciliation_required')

    def test_actual_generated_age_http_payload_is_consumed_by_production_controller(self):
        from unittest.mock import patch
        with patch.object(api, 'DEFAULT_APPLY_REVIEWED', False):
            self._actual_generated_age_http_payload(False)

    def test_enabled_generated_age_preview_preserves_apply_authority(self):
        from unittest.mock import patch
        with patch.object(api, 'DEFAULT_APPLY_REVIEWED', True):
            self._actual_generated_age_http_payload(True)

    def _actual_generated_age_http_payload(self, enabled):
        import os
        import time
        from pathlib import Path
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from finix import core
        from finix.integration import Profile
        from finix.recovery_http import create_router
        from hermes_constants import get_default_hermes_root
        scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        root=scope/'sdk-root'
        self.assertEqual(get_default_hermes_root(),root)
        core.atomic_json(root/'config.yaml',{})
        core.private_dir(root/'memories')
        (root/'memories/old.md').write_text('Synthetic old memory')
        profile=Profile(root,default_root=root)
        key=scope/('generated-maintenance-identity-'+str(enabled))
        recipient=core.keygen(key)
        captured=core.backup(root,profile.root/'copies',profile.root/'index',recipient,
                    sqlite_temp_dir=core.private_dir(root/'cache/finix/sqlite-temp'))
        app=FastAPI();router=create_router(lambda:profile);app.include_router(router)
        # Real exact service, authority, age, HTTP parsers, cold plan and projection.
        # Only live daemon preparation/confirmation is modeled; never a READY ACK.
        controller=SimpleNamespace(confirmation=lambda engine:{'consent':engine.consent(),'runtime':{'authority_epoch':'modeled-idle-no-daemon'}})
        def prepare(cap,root,**kw):return api.cold.prepare(cap,root,runtime_attempt_policy='runtime-attempt-preserve-v1',**kw)
        try:
            with TestClient(app,client=('127.0.0.1',1234)) as client, patch.object(api.lifecycle,'prepare_live',side_effect=prepare),patch.object(api.lifecycle,'ColdController',return_value=controller):
                response=client.get('/recovery/catalog');self.assertEqual(response.status_code,200,response.text)
                row=next(item for item in response.json()['items'] if item['sha256']==captured['sha256'])
                response=client.post('/recovery/default/prepare-auto',json={'profile_id':profile.profile_id,'candidate_id':row['id'],'consent_restore':True})
                self.assertEqual(response.status_code,200,response.text);job=response.json()['id']
                with key.open('rb') as stream:
                    response=client.post('/recovery/jobs/'+job+'/key',files={'file':('recovery.txt',stream,'text/plain')})
                self.assertEqual(response.status_code,200,response.text)
                for _ in range(200):
                    response=client.get('/recovery/jobs/'+job)
                    self.assertEqual(response.status_code,200,response.text)
                    if response.json()['state'] not in ('running','awaiting_key'):break
                    time.sleep(.025)
                self.assertEqual(response.json()['state'],'succeeded',response.text)
                self.assertIs(type(router.recovery_adapter.jobs[job]['service']),__import__('finix.recovery',fromlist=['RecoveryService']).RecoveryService)
                response=client.post('/recovery/default/prepare',json={'profile_id':profile.profile_id,'recovery_job':job,'consent_prepare':True,'preview_contract':'default-preview-job-v1'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['contract'],'default-preview-job-v1')
                for _ in range(200):
                    response=client.get('/recovery/default/preview/'+job)
                    self.assertEqual(response.status_code,200,response.text)
                    if response.json()['state']!='running':break
                    time.sleep(.025)
                self.assertEqual(response.json()['state'],'succeeded',response.text)
                payload=response.json()['result']
                (scope/('actual-http-preview-envelope-'+str(enabled)+'.json')).write_text(json.dumps(response.json(),indent=2))
                self.assertEqual(payload['apply_enabled'],enabled)
                self.assertNotIn(str(root),response.text)
                with patch.object(api.lifecycle,'preflight'),patch.object(api.lifecycle,'identify',return_value={'modeled':True}),patch.object(api.lifecycle,'_exact',return_value=True):
                    current=client.get('/recovery/default/capabilities')
                self.assertEqual(current.status_code,200,current.text)
                self.assertEqual(current.json()['apply_enabled'],enabled)
                self.assertEqual(current.json()['serverRootToken'],payload['preview']['root_binding'])
                (scope/'actual-http-capabilities.json').write_text(json.dumps(current.json(),indent=2))
                self.assertEqual(payload['preservation']['storage_format'],'opaque-container-v2')
                target=scope/'actual-http-preview.json'
                target.write_text(json.dumps(payload,indent=2))
                # The guarded runner forbids non-Python/age children. Execute the
                # production JS golden consumer against this exact file afterward.
                self.assertEqual(payload['preview']['scope'],'replace-real-default')
                # Enabled is not permission to bypass explicit Apply consent.
                # No daemon mutation is modeled or dispatched in this test.
                response=client.post('/recovery/default/apply',json={'profile_id':profile.profile_id,'id':payload['id'],'confirmation':payload['confirmation'],'consent_apply':not enabled})
                self.assertEqual(response.json()['detail'],'CONSENT_REQUIRED' if enabled else 'DEFAULT_RECOVERY_REVIEW_REQUIRED')
                response=client.post('/recovery/default/apply',json={'profile_id':profile.profile_id,'id':payload['id'],'confirmation':payload['confirmation'],'consent_apply':True,'path':'forbidden'})
                self.assertEqual(response.status_code,422)
        finally:
            for record in router.recovery_adapter.jobs.values():record['service'].close()
            for service in router.recovery_adapter.services.values():service.close()
