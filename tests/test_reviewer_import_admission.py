"""Ordinary unsupported-import admission; transport modeled, no activation."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from finix import core, recovery_http, recovery_import, recovery_operations, recovery_shared_host


class ImportAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'], prefix='import-admission-')
        self.addCleanup(self.tmp.cleanup)
        home = core.private_dir(Path(self.tmp.name)/'source')
        self.service = SimpleNamespace(profile=home, profile_id='a'*64, activation='fixture')
        self.service._fence = lambda *a: None
        self.service.close = lambda: None
        self.service.catalog = lambda *a: [{'id':'b'*64, 'complete':True}]
        self.service.allocate_destination = lambda *a: self.fail('destination allocated before refusal')
        self.profile = SimpleNamespace(home=home, profile_id=self.service.profile_id)
        self.geometry = patch.object(recovery_shared_host, 'shared_host', return_value=None)
        self.geometry.start(); self.addCleanup(self.geometry.stop)

    def test_capability_refuses_unsupported_import_without_native_probe(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        router = recovery_http.create_router(lambda: self.profile)
        app = FastAPI(); app.include_router(router)
        with patch.object(router.recovery_adapter, 'service', return_value=self.service):
            with TestClient(app, client=('127.0.0.1',51000)) as client:
                result = client.get('/recovery/capabilities')
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json()['detail'],'HOST_IMPORT_CONTRACT_UNSUPPORTED')
        recovery_shared_host.shared_host.assert_not_called()

    def test_begin_refuses_before_any_durable_job_or_worker(self):
        ops = recovery_operations.RecoveryOperations()
        self.addCleanup(ops.close)
        with patch.object(ops, '_save') as save, patch.object(ops, '_execute'):
            with self.assertRaisesRegex(recovery_import.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
                ops.begin(self.service, request_id=str(uuid.uuid4()), candidate_id='b'*64)
        save.assert_not_called()
        self.assertFalse(ops._workers)
        self.assertFalse((self.service.profile/'cache/finix/recovery-operations/current.json').exists())

    def test_worker_refuses_before_catalog_destination_or_awaiting_key(self):
        ops = recovery_operations.RecoveryOperations()
        owned = {'id':'c'*32,'intent':{'candidate_id':'b'*64,'acknowledge_incomplete':False}}
        ops._workers[owned['id']] = {}
        with patch.object(ops,'_publish') as publish:
            with self.assertRaisesRegex(recovery_import.RecoveryImportError, 'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
                ops._execute(self.service,owned)
        publish.assert_not_called()

    def test_http_key_refuses_before_reading_request_body(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        router=recovery_http.create_router(lambda:self.profile)
        app=FastAPI();app.include_router(router)
        with patch.object(router.recovery_adapter,'service',return_value=self.service):
            with TestClient(app,client=('127.0.0.1',51000)) as client:
                result=client.post('/recovery/operations/'+'c'*32+'/key',content=b'not a key')
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json()['detail'],'HOST_IMPORT_CONTRACT_UNSUPPORTED')

    def test_direct_key_refuses_and_wipes_without_state_creation(self):
        ops=recovery_operations.RecoveryOperations(); key=bytearray(b'generated-test-only')
        with self.assertRaisesRegex(recovery_import.RecoveryImportError,'HOST_IMPORT_CONTRACT_UNSUPPORTED'):
            ops.key(self.service,'c'*32,request_id=str(uuid.uuid4()),identity=key)
        self.assertEqual(key,bytearray(len(key)))
        self.assertFalse((self.service.profile/'cache/finix/recovery-operations').exists())

    def test_http_admission_refuses_without_job_or_worker(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        router=recovery_http.create_router(lambda:self.profile)
        self.addCleanup(router.recovery_operations.close)
        app=FastAPI();app.include_router(router)
        with patch.object(router.recovery_adapter,'service',return_value=self.service), \
                patch.object(router.recovery_operations,'_execute'):
            with TestClient(app,client=('127.0.0.1',51000)) as client:
                for endpoint in ('/recovery/operations','/recovery/prepare-auto'):
                    result=client.post(endpoint,json=dict(profile_id=self.profile.profile_id,
                        candidate_id='b'*64,consent_restore=True,request_id=str(uuid.uuid4())))
                    self.assertEqual(result.status_code,409)
                    self.assertEqual(result.json()['detail'],'HOST_IMPORT_CONTRACT_UNSUPPORTED')
        self.assertFalse(router.recovery_operations._workers)
        self.assertFalse(router.recovery_adapter.jobs)

    def test_physical_default_prepare_remains_separate_model_contract(self):
        # Modeled authority and data service ONLY; not native lifecycle acceptance.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from finix import default_recovery_api
        self.service.allocate_destination=lambda *a: self.service.profile/'inert-fixture'
        with patch.object(recovery_http,'operation_lock',return_value=contextlib.nullcontext()), \
                patch.object(default_recovery_api,'authority',return_value=self.profile.home), \
                patch.object(default_recovery_api,'new_service',return_value=self.service), \
                patch.object(recovery_shared_host,'check_bootstrap_compatibility',side_effect=AssertionError('ordinary guard')):
            router=recovery_http.create_router(lambda:self.profile)
            app=FastAPI();app.include_router(router)
            with TestClient(app,client=('127.0.0.1',51000)) as client:
                result=client.post('/recovery/default/prepare-auto',json=dict(
                    profile_id=self.profile.profile_id,candidate_id='b'*64,consent_restore=True))
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['state'],'awaiting_key')

    def test_cli_refuses_before_resolving_or_reading_identity(self):
        from finix import plugin_cli
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        args=SimpleNamespace(recovery_action='restore',request_id=None,copy_id='b'*64,
                             acknowledge_incomplete=False,identity='must-not-open')
        token=set_hermes_home_override(self.profile.home)
        self.addCleanup(reset_hermes_home_override,token)
        output=io.StringIO()
        with patch.object(recovery_http,'binding',return_value=self.service.activation), \
                patch.object(recovery_http.RecoveryHTTP,'service',return_value=self.service), \
                patch.object(recovery_http,'operation_lock',return_value=contextlib.nullcontext()), \
                patch.object(recovery_http,'explicit_path',side_effect=AssertionError('key resolution')) as key, \
                contextlib.redirect_stdout(output):
            result=plugin_cli._recovery(self.profile,args)
        self.assertEqual(result,1)
        self.assertIn('HOST_IMPORT_CONTRACT_UNSUPPORTED',output.getvalue())
        key.assert_not_called()


if __name__ == '__main__':
    unittest.main()
