"""Durable SOURCE ownership, using native kernel leases (not PID records)."""
import contextvars
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from finix import core


def service(tmp_path):
    home=core.private_dir(tmp_path/'source')
    class Service:
        profile=home
        profile_id='a'*64
        activation='source-epoch'
        def _fence(self, profile_id, activation):
            if (profile_id,activation)!=(self.profile_id,self.activation):
                raise RuntimeError('stale')
    return Service()


def wait_for(fn, predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        value=fn()
        if predicate(value):return value
        time.sleep(.01)
    pytest.fail('operation did not reach expected state: '+repr(value))


def test_ack_status_duplicate_and_context_while_worker_blocked(tmp_path,monkeypatch):
    from finix.recovery_operations import RecoveryOperations
    s=service(tmp_path);ops=RecoveryOperations();entered=threading.Event();release=threading.Event()
    context=contextvars.ContextVar('actual-dispatch-context');context.set('source-request')
    observed=[]
    def execute(self, service, owned):
        observed.append(context.get());entered.set();release.wait(4)
        self._publish(service,owned,phase='backend_target_ready',state='succeeded',receipt={'backend_target_ready':True})
    monkeypatch.setattr(RecoveryOperations,'_execute',execute)
    rid=str(uuid.uuid4())
    try:
        ack=ops.begin(s,request_id=rid,candidate_id='b'*64,acknowledge_incomplete=False)
        assert entered.wait(1)
        start=time.monotonic();current=RecoveryOperations().current(s)
        assert time.monotonic()-start<.5
        assert current['id']==ack['id'] and current['state']=='running'
        assert ops.begin(s,request_id=rid,candidate_id='b'*64,acknowledge_incomplete=False)['id']==ack['id']
        with pytest.raises(Exception):ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64,acknowledge_incomplete=False)
        with pytest.raises(Exception):ops.begin(s,request_id=rid,candidate_id='c'*64,acknowledge_incomplete=False)
        assert observed==['source-request']
    finally:release.set();ops.close()
    assert RecoveryOperations().current(s)['state']=='succeeded'
    assert ops.begin(s,request_id=rid,candidate_id='b'*64,acknowledge_incomplete=False)['id']==ack['id']


def test_whole_chain_waits_for_key_and_blocks_import_without_blocking_status(tmp_path,monkeypatch):
    from finix.recovery_operations import RecoveryOperations
    import finix.recovery_pipeline as pipeline
    s=service(tmp_path);ops=RecoveryOperations();entered=threading.Event();release=threading.Event();calls=[]
    s.catalog=lambda *a:[dict(id='b'*64,complete=True)]
    s.allocate_destination=lambda *a:object()
    streams=[]
    def start(candidate,*,identity,**kwargs):
        assert identity.read()==b'fixture-key';streams.append(identity);calls.append('decrypt');return 'd'*32
    s.start=start
    s.wait=lambda *a:dict(status='succeeded',result={'complete':True})
    s.cleanup_managed_result=lambda *a:calls.append('cleanup')
    def complete(*a,**kwargs):
        assert streams[0].closed,'identity stream must be erased before import or terminal'
        calls.append('import');entered.set();release.wait(4)
        return dict(backend_target_ready=True,name=kwargs['name'])
    monkeypatch.setattr(pipeline,'complete_restore',complete)
    rid=str(uuid.uuid4());phase_id=str(uuid.uuid4())
    try:
        ack=ops.begin(s,request_id=rid,candidate_id='b'*64,acknowledge_incomplete=False)
        status=wait_for(lambda:ops.current(s),lambda x:x['phase']=='awaiting_key')
        result=ops.key(s,ack['id'],request_id=phase_id,identity=bytearray(b'fixture-key'))
        assert result['id']==ack['id']
        assert entered.wait(2)
        status=ops.current(s)
        assert status['phase']=='importing' and not status['cancellable']
        assert ops.key(s,ack['id'],request_id=phase_id,identity=bytearray(b'fixture-key'))['id']==ack['id']
        with pytest.raises(Exception):ops.cancel(s,ack['id'])
        assert calls==['decrypt','import']
    finally:release.set();ops.close()
    assert ops.current(s)['receipt']['backend_target_ready']


def test_cancel_waiting_key_is_worker_confirmed_and_replay_cannot_restart(tmp_path):
    from finix.recovery_operations import RecoveryOperations
    s=service(tmp_path);ops=RecoveryOperations()
    s.catalog=lambda *a:[dict(id='b'*64,complete=True)]
    s.allocate_destination=lambda *a:object()
    rid=str(uuid.uuid4())
    ack=ops.begin(s,request_id=rid,candidate_id='b'*64)
    wait_for(lambda:ops.current(s),lambda x:x['phase']=='awaiting_key')
    cancelled=RecoveryOperations().cancel(s,ack['id'])
    assert cancelled['id']==ack['id']
    ops.close()
    assert ops.current(s)['state']=='cancelled'
    assert ops.begin(s,request_id=rid,candidate_id='b'*64)['state']=='cancelled'
    with pytest.raises(Exception):ops.key(s,ack['id'],request_id=str(uuid.uuid4()),identity=bytearray(b'no'))


def test_separate_process_blocks_owner_and_death_preserves_interruption(tmp_path):
    from finix.recovery_operations import RecoveryOperations
    import subprocess,sys,os
    s=service(tmp_path);ops=RecoveryOperations()
    env=dict(os.environ)
    # Use the same pre-import guard and candidate modules in this exact child.
    import hermes_constants
    env['PYTHONPATH']=os.pathsep.join([str(Path(__file__).parents[1]),str(Path(os.environ['FINIX_BOUND_SCOPE']).parent),str(Path(hermes_constants.__file__).parent)])
    child=subprocess.Popen([sys.executable,'-u',str(Path(__file__).with_name('recovery_operation_process.py')),str(s.profile),str(uuid.uuid4())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    try:
        ack=json.loads(child.stdout.readline())
        start=time.monotonic();status=ops.current(s)
        assert time.monotonic()-start<.5
        assert status['id']==ack['id'] and status['phase']=='importing' and status['state']=='running'
        with pytest.raises(Exception):ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64)
        assert child.poll() is None
    finally:
        if child.poll() is None:child.terminate()
        child.wait(timeout=10)
        child.stdin.close();child.stdout.close();child.stderr.close()
        core.atomic_json(tmp_path/'owner-process-cleanup.json',dict(pid=child.pid,reaped=True,returncode=child.returncode))
    reopened=RecoveryOperations().current(s)
    assert reopened['id']==ack['id'] and reopened['state']=='interrupted'
    with pytest.raises(Exception):ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64)


def test_legacy_paths_cannot_open_an_unowned_restore(tmp_path):
    from finix.integration import Profile
    from finix.recovery_http import create_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    home=core.private_dir(tmp_path/'legacy-source');profile=Profile(home,default_root=home)
    app=FastAPI();app.include_router(create_router(lambda:profile))
    with TestClient(app,client=('127.0.0.1',51000)) as client:
        result=client.post('/recovery/prepare-auto',json=dict(profile_id=profile.profile_id,candidate_id='b'*64,consent_restore=True))
        assert result.status_code==409 and result.json()['detail']=='OPERATION_PROTOCOL_REQUIRED'
        result=client.post('/recovery/prepare',json=dict(profile_id=profile.profile_id,archive_path=str(tmp_path/'archive.age'),destination_path=str(tmp_path/'out'),consent_local=True))
        assert result.status_code==409 and result.json()['detail']=='OPERATION_PROTOCOL_REQUIRED'


def test_actual_fenced_service_rechecks_source_binding(tmp_path,monkeypatch):
    from finix.integration import Profile
    from finix import recovery_http
    home=core.private_dir(tmp_path/'bound-source')
    profile=Profile(home,default_root=home)
    binding=['original']
    monkeypatch.setattr(recovery_http,'binding',lambda p:binding[0])
    service=recovery_http.FencedService(profile,binding[0])
    service._fence(profile.profile_id,'original')
    binding[0]='switched'
    with pytest.raises(Exception):service._fence(profile.profile_id,'original')


def test_reopen_does_not_reconstruct_worker_or_capability(tmp_path):
    from finix.recovery_operations import RecoveryOperations
    s=service(tmp_path);ops=RecoveryOperations()
    root=core.private_dir(s.profile/'cache/finix/recovery-operations')
    record=dict(id=uuid.uuid4().hex,profile_id=s.profile_id,activation=s.activation,state='running',phase='history_importing',cancellable=False,full_acceptance=False)
    core.atomic_json(root/'current.json',record)
    status=ops.current(s)
    assert status['state']=='interrupted' and status['phase']=='history_importing'
    with pytest.raises(Exception):ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64,acknowledge_incomplete=False)
    assert json.loads((root/'current.json').read_text())['id']==record['id']
