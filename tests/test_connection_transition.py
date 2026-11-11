"""Offline HTTP admission: explicit Apply may park, never rebind, failed work."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch


class ConnectionTransition(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        path = Path(__file__).parents[1]/'dashboard'/'plugin_api.py'
        spec = importlib.util.spec_from_file_location('transition_http_fixture', path)
        self.m = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.m)
        self.core = importlib.import_module(self.m._package+'.core')
        self.jobs_module = importlib.import_module(self.m._package+'.backup_jobs')
        Profile = importlib.import_module(self.m._package+'.integration').Profile
        self.Flow = importlib.import_module(self.m._package+'.setup_flow').SetupFlow
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.temp.cleanup)
        root = self.core.private_dir(Path(self.temp.name)/'fixture')
        home = self.core.private_dir(root/'home')
        self.p = Profile(home, default_root=home)
        # Admission-only fixture; legacy adoption has its own real-cipher suite.
        self.p.pending_backup_checkpoint = lambda **kw: None
        auth = self.core.private_dir(root/'old-auth'); config = auth/'rclone.conf'
        config.write_text('[finix]\ntype = yandex\n'); config.chmod(0o600)
        cfg=self.p.config(); cfg.update(storage='rclone',rclone_config=str(config),rclone_remote='finix',rclone_root='Finix')
        self.core.atomic_json(self.p.root/'settings.json',cfg)
        self.pending=self.core.private_dir(self.p.root/'rclone')/'rclone-pending.json'
        self.pending.write_bytes(b'{"old-owned-pending":"DO NOT DELETE"}')
        self.calls=[]; owner=self
        class Flow:
            def _binding(self): return owner.Flow(owner.p)._binding()
            def check_and_run(self, **kw):
                owner.calls.append(kw)
                if len(owner.calls)==1:
                    kw['progress'](dict(phase='captured',archive='old-yandex.age',sha256='a'*64,size=123,namespace='b'*32,complete=True,omitted_count=0))
                    raise owner.core.FinixError('synthetic old provider failure')
                return dict(verified=True,first_backup=dict(complete=True,omitted_count=0))
        self.m._backup_jobs.flow_factory=lambda p:Flow()
        app=FastAPI();app.include_router(self.m.router)
        self.scope=patch.object(self.m,'current_profile',return_value=self.p);self.scope.start();self.addCleanup(self.scope.stop)
        self.client=TestClient(app); self.client.__enter__();self.addCleanup(lambda:self.client.__exit__(None,None,None))
        self.old_request=str(uuid.uuid4())
        self.old=self.start_finish(self.old_request)
        self.assertEqual(self.old['state'],'failed')
        self.old_private=self.state()['job']

    def state(self):
        return json.loads((self.p.root/'backup-jobs/state.json').read_text())

    def start_finish(self, request=None):
        r=self.client.post('/backup/jobs',json=dict(profile_id=self.p.profile_id,request_id=request or str(uuid.uuid4())))
        self.assertEqual(r.status_code,202,r.text)
        for _ in range(1000):
            job=self.client.get('/backup/jobs/current').json()['job']
            if job['state']!='running' and not self.jobs_module._busy(self.p.root/'backup-jobs/worker.lock'):return job
            time.sleep(.002)
        self.fail('worker stuck')

    def apply(self):
        c=self.m.connections
        stage=self.core.private_dir(self.p.root/'connections'/('d'*32))
        (stage/'rclone.conf').write_text('[finix]\ntype = onedrive\n');(stage/'rclone.conf').chmod(0o600)
        c.sessions[self.p.profile_id]=dict(public=dict(id='d'*32,provider='onedrive',folder='Finix',status='verified'),stage=stage,before=self.p.config(),reuse=False,deadline=time.monotonic()+300)
        ticket=self.client.get('/connection').json()['ticket']
        return self.client.post('/connection',json=dict(profile_id=self.p.profile_id,ticket=ticket,action='apply',session_id='d'*32))

    def test_failed_yandex_apply_onedrive_fresh_start_preserves_full_old_job_and_replay(self):
        pending=self.pending.read_bytes()
        state=self.state();state['job']['owned_metadata']='preserved-private-checkpoint-'*1000
        self.core.atomic_json(self.p.root/'backup-jobs/state.json',state)
        self.old_private=state['job']
        old_settings=self.p.config()
        old_receipt={'version':1,'profile_id':self.p.profile_id,'binding':self.old_private['binding'],'archive':'previous.age'}
        self.core.atomic_json(self.p.root/'setup-verification.json',old_receipt)
        self.assertEqual(self.apply().status_code,200)
        new=self.start_finish()
        self.assertNotEqual(new['id'],self.old['id'])
        self.assertEqual(new['state'],'succeeded',new)
        self.assertNotIn('resume',self.calls[1])
        archive=self.p.root/'backup-jobs/history'/(self.old['id']+'.json')
        self.assertEqual(json.loads(archive.read_text()),self.old_private)
        evidence=self.p.root/'backup-jobs/history-bindings'/(self.old['id']+'.json')
        self.assertEqual(json.loads(evidence.read_text()),dict(settings=old_settings,setup_receipt=old_receipt))
        replay=self.client.post('/backup/jobs',json=dict(profile_id=self.p.profile_id,request_id=self.old_request)).json()['job']
        self.assertEqual(replay,self.old)
        self.assertEqual(self.pending.read_bytes(),pending)
        self.assertEqual(self.p.rclone().state,self.p.root/'rclone'/('d'*32))

    def test_real_age_http_transition_retention_preserves_old_owned_cipher_and_pending(self):
        from tests.test_rclone_storage import MemoryRunner
        Storage=importlib.import_module(self.m._package+'.rclone_storage')
        self.pending.unlink()
        state=self.state();state['job']=None;self.core.atomic_json(self.p.root/'backup-jobs/state.json',state)
        cfg=self.p.config();cfg.update(recipient=self.core.keygen(Path(self.temp.name)/'fixture.key'),retention=1)
        self.core.atomic_json(self.p.root/'settings.json',cfg)
        (self.p.home/'generated.txt').write_text('generated isolated profile content')
        del self.p.pending_backup_checkpoint
        self.m._backup_jobs.flow_factory=self.Flow
        runners={}
        class Runner(MemoryRunner):
            def __init__(self, provider):super().__init__();self.provider=provider
            def __call__(self, op, *args, **kw):
                if op=='listremotes':return json.dumps([dict(name='finix',type=self.provider,source='file')]).encode()
                if op=='copyto' and self.provider=='yandex':raise Storage.RcloneError('TIMEOUT')
                try:return super().__call__(op,*args,**kw)
                except Exception as exc:
                    if hasattr(exc,'code'):raise Storage.RcloneError(exc.code) from None
                    raise
        def adapter(cfg=None):
            cfg=cfg or self.p.config();key=cfg.get('rclone_connection','old')
            runner=runners.setdefault(key,Runner('yandex' if key=='old' else 'onedrive'))
            path=self.p.root/'rclone'
            if key!='old':path=path/key
            return Storage.RcloneStorage(self.p.home,self.p.profile_id,path,cfg['rclone_config'],'finix','Finix',runner=runner)
        self.p.rclone=adapter
        old_request=str(uuid.uuid4());old=self.start_finish(old_request)
        self.assertEqual(old['state'],'failed',old)
        checkpoint=self.state()['job']['capture']
        cipher=self.p.root/'copies'/checkpoint['archive'];digest=self.core.digest_file(cipher)
        pending=self.pending.read_bytes();ledger=(self.pending.parent/'rclone.json').read_bytes()
        self.assertEqual(self.apply().status_code,200)
        new=self.start_finish();self.assertEqual(new['state'],'succeeded',new)
        self.assertNotEqual(new['id'],old['id'])
        self.assertTrue(cipher.is_file(),'new connection retention deleted old owned capture')
        self.assertEqual(self.core.digest_file(cipher),digest)
        self.assertEqual(self.pending.read_bytes(),pending)
        self.assertEqual((self.pending.parent/'rclone.json').read_bytes(),ledger)
        copied=[data for key,r in runners.items() if key!='old' for data in r.objects.values()]
        self.assertEqual(len(copied),1)
        self.assertNotEqual(copied[0],cipher.read_bytes(),'old ciphertext crossed account boundary')

    def test_interrupted_old_request_after_apply_only_replays_old_job(self):
        state=self.state();state['job']['state']='interrupted'
        self.core.atomic_json(self.p.root/'backup-jobs/state.json',state)
        self.assertEqual(self.apply().status_code,200)
        replay=self.start_finish(self.old_request)
        self.assertEqual(replay['id'],self.old['id'])
        self.assertEqual(replay['state'],'interrupted')
        self.assertEqual(len(self.calls),1)

    def test_completed_job_allows_new_independent_backup_after_ordinary_setting_change(self):
        previous=self.start_finish()
        self.assertEqual(previous['state'],'succeeded')
        cfg=self.p.config();cfg['retention']=2;self.core.atomic_json(self.p.root/'settings.json',cfg)
        self.assertEqual(self.apply().status_code,200)
        next_job=self.start_finish()
        self.assertEqual(next_job['state'],'succeeded')
        self.assertNotEqual(next_job['id'],previous['id'])
        self.assertNotIn('resume',self.calls[-1])

    def test_same_binding_fresh_request_resumes_exact_checkpoint(self):
        result=self.start_finish()
        self.assertEqual(result['id'],self.old['id'])
        self.assertEqual(self.calls[1]['resume'],self.old_private['capture'])

    def test_unapplied_binding_change_fails_closed_without_recapture(self):
        cfg=self.p.config();cfg['rclone_remote']='attacker';self.core.atomic_json(self.p.root/'settings.json',cfg)
        response=self.client.post('/backup/jobs',json=dict(profile_id=self.p.profile_id,request_id=str(uuid.uuid4())))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.state()['job'],self.old_private)

    def test_active_worker_lease_blocks_apply_and_keeps_settings(self):
        before=self.p.config()
        with self.jobs_module.file_lease(self.p.root/'backup-jobs/worker.lock'):
            result=self.apply()
        self.assertEqual(result.status_code,409,result.text)
        self.assertEqual(self.p.config(),before)

    def test_changed_settings_after_apply_do_not_authorize_transition(self):
        self.assertEqual(self.apply().status_code,200)
        cfg=self.p.config();cfg['rclone_root']='other';self.core.atomic_json(self.p.root/'settings.json',cfg)
        response=self.client.post('/backup/jobs',json=dict(profile_id=self.p.profile_id,request_id=str(uuid.uuid4())))
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(len(self.calls),1)
