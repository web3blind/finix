"""Actual ASGI route, durable worker, production adapter and generated age data."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from tests.test_workspace_reconcile import Transport, ACCOUNT_DIGEST

class WorkspaceHTTP(unittest.TestCase):
    def test_preintent_no_ledger_http_adopts_retained_cipher(self):
        self.run_case(preintent=True)

    def test_real_worker_adopts_retained_cipher_and_old_failure_replays_unchanged(self):
        self.run_case()

    def run_case(self, preintent=False):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        spec=importlib.util.spec_from_file_location('workspace_composed_http',Path(__file__).parents[1]/'dashboard/plugin_api.py')
        api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
        package=api._package
        core=importlib.import_module(package+'.core')
        Profile=importlib.import_module(package+'.integration').Profile
        workspace=importlib.import_module(package+'.workspace_drive')
        jobs=importlib.import_module(package+'.backup_jobs')
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            root=core.private_dir(Path(tmp)/'private');home=core.private_dir(root/'home')
            (home/'work').write_bytes(b'generated synthetic source\n'*100)
            profile=Profile(home,default_root=home);cfg=profile.config()
            key=root/'generated.key';cfg.update(storage='workspace',workspace_account=ACCOUNT_DIGEST,recipient=core.keygen(key),retention=0)
            core.atomic_json(profile.root/'settings.json',cfg)
            fake=Transport();fake.error=workspace.WorkspaceError;fake.lost=not preintent
            interrupted=[preintent]
            def transport(op,*args,**kwargs):
                if interrupted[0]:
                    interrupted[0]=False
                    raise workspace.WorkspaceError('TIMEOUT')
                return fake(op,*args,**kwargs)
            adapter=workspace.WorkspaceDrive(home,profile.profile_id,profile.root/'workspace',runner=transport,account_digest=ACCOUNT_DIGEST)
            app=FastAPI();app.include_router(api.router)
            def terminal(client):
                deadline=time.monotonic()+15
                while True:
                    result=client.get('/backup/jobs/current').json()
                    if result['job']['state']!='running' and not jobs._busy(profile.root/'backup-jobs/worker.lock'):return result
                    self.assertLess(time.monotonic(),deadline,result);time.sleep(.01)
            with patch.object(Profile,'workspace',return_value=adapter), patch.object(api,'current_profile',side_effect=lambda:Profile(home,default_root=home)), TestClient(app) as client:
                old=dict(profile_id=profile.profile_id,request_id='11111111-1111-4111-8111-111111111111')
                start=client.post('/backup/jobs',json=old);self.assertEqual(start.status_code,202,start.text)
                failed=terminal(client);self.assertEqual(failed['job']['state'],'failed')
                retained=next((profile.root/'copies').glob('*.age'));before=retained.read_bytes()
                receipt_path=profile.root/'backup-jobs/requests'/(old['request_id']+'.json');receipt=receipt_path.read_bytes()
                pending=None if preintent else json.loads((adapter.state/'workspace-pending.json').read_text())
                if preintent:self.assertFalse((adapter.state/'workspace.json').exists())
                new=dict(old,request_id='22222222-2222-4222-8222-222222222222')
                with patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                    ack=client.post('/backup/jobs',json=new);self.assertEqual(ack.status_code,202,ack.text)
                    success=terminal(client)
                self.assertEqual(success['job']['state'],'succeeded',success)
                self.assertNotEqual(success['job']['id'],failed['job']['id'])
                self.assertEqual(client.post('/backup/jobs',json=old).json(),failed)
                self.assertEqual(receipt_path.read_bytes(),receipt)
                self.assertEqual(retained.read_bytes(),before)
                row=json.loads((adapter.state/'workspace.json').read_text())['records'][0]
                if pending:self.assertEqual(row['id'],pending['record']['id'])
                self.assertEqual(fake.data[row['id']],before)
                self.assertEqual(sum(op=='upload' for op,_ in fake.calls),1)
                downloaded=root/'readback.age';adapter.download(row,downloaded)
                core.restore(downloaded,key,root/'restored')
                self.assertEqual((root/'restored/work').read_bytes(),(home/'work').read_bytes())
                self.assertNotIn(str(root),json.dumps(success))
