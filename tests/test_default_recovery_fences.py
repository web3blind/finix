"""Generated-age worker regressions; no lifecycle/provider operations."""
import io
import os
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from finix import core, default_recovery_api as api
from finix.integration import Profile
from finix.recovery import RecoveryService, RecoveryError, CommittedSource

class Fences(unittest.TestCase):
    def fixture(self):
        scope=Path(os.environ['FINIX_PUBLIC_TEST_ROOT']).parent
        root=core.private_dir(scope/('fixture-'+uuid.uuid4().hex)/'sdk-root')
        previous=os.environ['HERMES_HOME']
        os.environ['HERMES_HOME']=str(root)
        self.addCleanup(os.environ.__setitem__,'HERMES_HOME',previous)
        from hermes_constants import get_default_hermes_root,get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        self.assertEqual(root,get_default_hermes_root())
        self.assertEqual(root,get_hermes_home())
        self.assertEqual(root,get_profile_dir('default'))
        core.atomic_json(root/'config.yaml',{})
        (root/'note.md').write_text('generated fixture')
        profile=Profile(root,default_root=root)
        core.atomic_json(profile.root/'settings.json',dict(recipient=None,storage='local',retention=0,interval_hours=0,next_run=0,folder_id=None))
        key=root.parent/('generated-'+uuid.uuid4().hex)
        recipient=core.keygen(key)
        core.backup(root,profile.root/'copies',profile.root/'index',recipient,sqlite_temp_dir=core.private_dir(root/'cache/finix/sqlite-temp'))
        service=api.new_service(profile); self.addCleanup(service.close)
        self.assertIs(type(service),RecoveryService)
        row=service.catalog(profile.profile_id,service.activation)[-1]
        dest=service.allocate_destination(profile.profile_id,service.activation)
        return root,profile,key,service,row,dest

    def start(self,f,identity=None):
        root,p,k,s,row,d=f
        return s.start(row['id'],identity=identity or k,destination=d,profile_id=p.profile_id,activation=s.activation)

    def change(self,p):
        cfg=p.config();cfg['storage']='drive';core.atomic_json(p.root/'settings.json',cfg)

    def test_backend_identity_rotation_before_key(self):
        f=self.fixture();r,p,k,s,row,d=f
        backend=(p.root/'backend.json').read_bytes()
        self.addCleanup((p.root/'backend.json').write_bytes,backend)
        core.atomic_json(p.root/'backend.json',{'profile_id':'a'*64})
        class NoRead(io.BytesIO):
            def read(self,*a):
                raise AssertionError('key consumed')
        with self.assertRaises(RecoveryError):self.start(f,NoRead())

    def test_declared_default_root_change_before_key(self):
        f=self.fixture();r,p,k,s,row,d=f
        p.default_root=core.private_dir(r.parent/'other-default')
        with self.assertRaises(RecoveryError):self.start(f)

    def test_backend_identity_rotation_during_transfer(self):
        f=self.fixture();r,p,k,s,row,d=f
        src=s._sources['local'];backend=(p.root/'backend.json').read_bytes()
        self.addCleanup((p.root/'backend.json').write_bytes,backend)
        def transfer(row,out,cancel):
            src.transfer(row,out,cancel)
            core.atomic_json(p.root/'backend.json',{'profile_id':'b'*64})
        s.add_source(CommittedSource(src.connection,src.generation,src.records,transfer));s.catalog(p.profile_id,s.activation)
        j=self.start(f)
        self.assertNotEqual(s.wait(j,10)['status'],'succeeded')
        self.assertNotIn(j,s._handoffs)
        self.assertFalse(d.path.exists())

    def test_sdk_default_root_switch_during_transfer(self):
        from hermes_constants import get_default_hermes_root,get_hermes_home
        f=self.fixture();r,p,k,s,row,d=f
        src=s._sources['local'];other=core.private_dir(r.parent/'new-sdk-default')
        original=os.environ['HERMES_HOME']
        self.addCleanup(os.environ.__setitem__,'HERMES_HOME',original)
        def transfer(row,out,cancel):
            src.transfer(row,out,cancel)
            os.environ['HERMES_HOME']=str(other)
            self.assertEqual(get_default_hermes_root(),other)
            self.assertEqual(get_hermes_home(),other)
        s.add_source(CommittedSource(src.connection,src.generation,src.records,transfer));s.catalog(p.profile_id,s.activation)
        j=self.start(f)
        self.assertNotEqual(s.wait(j,10)['status'],'succeeded')
        self.assertFalse(d.path.exists())

    def test_pre_key_activation_refused(self):
        f=self.fixture();self.change(f[1])
        with self.assertRaises(RecoveryError):self.start(f)
        self.assertFalse(f[-1].path.exists())

    def test_worker_lease_blocks_competing_operation(self):
        f=self.fixture();r,p,k,s,row,d=f
        entered=threading.Event();release=threading.Event()
        src=s._sources['local']
        def transfer(row,out,cancel):
            entered.set();release.wait(5);src.transfer(row,out,cancel)
        s.add_source(CommittedSource(src.connection,src.generation,src.records,transfer))
        s.catalog(p.profile_id,s.activation)
        j=self.start(f)
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises((OSError,core.FinixError)):
                with core.locked(p.directories()):pass
        finally:release.set()
        self.assertEqual(s.wait(j,10)['status'],'succeeded')
        with core.locked(p.directories()):pass

    def test_publication_boundary_invalidates_and_erases(self):
        f=self.fixture();r,p,k,s,row,d=f
        original=core.publish_directory
        def publish(*args,**kwargs):
            original(*args,**kwargs);self.change(p)
        with patch.object(core,'publish_directory',publish):
            j=self.start(f);state=s.wait(j,10)
        self.assertNotEqual(state['status'],'succeeded')
        self.assertNotIn(j,s._handoffs)
        self.assertFalse(d.path.exists())

    def test_handoff_rechecks_live_owner(self):
        f=self.fixture();r,p,k,s,row,d=f
        j=self.start(f);self.assertEqual(s.wait(j,10)['status'],'succeeded')
        self.change(p)
        with self.assertRaises(RecoveryError):s.handoff(j,p.profile_id,s.activation)

    def test_authenticated_http_dispatch_preserves_sdk_context(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override,get_hermes_home_override
        from finix.recovery_http import create_router
        f=self.fixture();r,p,k,s,row,d=f;seen=[];src=s._sources['local']
        def transfer(row,out,cancel):
            seen.append(get_hermes_home_override());src.transfer(row,out,cancel)
        s.add_source(CommittedSource(src.connection,src.generation,src.records,transfer));s.catalog(p.profile_id,s.activation)
        app=FastAPI()
        @app.middleware('http')
        async def authenticated_scope(request,call_next):
            token=set_hermes_home_override(r)
            try:return await call_next(request)
            finally:reset_hermes_home_override(token)
        app.include_router(create_router(lambda:p))
        with patch.object(api,'new_service',return_value=s),TestClient(app,client=('127.0.0.1',1234)) as client:
            reply=client.post('/recovery/default/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=row['id'],consent_restore=True))
            self.assertEqual(reply.status_code,200)
            ident=reply.json()['id']
            reply=client.post('/recovery/jobs/'+ident+'/key',files={'file':('generated',k.read_bytes(),'text/plain')})
            self.assertEqual(reply.status_code,200)
            s._worker.join(10)
            states=[job for job,event in s._jobs.values()]
            self.assertEqual([j['status'] for j in states],['succeeded'])
        self.assertEqual(seen,[str(r)])

    def test_sdk_request_scope_is_copied_per_worker(self):
        from hermes_constants import set_hermes_home_override,reset_hermes_home_override,get_hermes_home_override,get_hermes_home
        f=self.fixture();r,p,k,s,row,d=f;seen=[];src=s._sources['local']
        def transfer(row,out,cancel):
            seen.append((get_hermes_home_override(),get_hermes_home()))
            src.transfer(row,out,cancel)
        s.add_source(CommittedSource(src.connection,src.generation,src.records,transfer));s.catalog(p.profile_id,s.activation)
        token=set_hermes_home_override(r)
        try:j=self.start(f)
        finally:reset_hermes_home_override(token)
        self.assertEqual(s.wait(j,10)['status'],'succeeded')
        self.assertEqual(seen,[(str(r),r)])
        self.assertIsNone(get_hermes_home_override())
