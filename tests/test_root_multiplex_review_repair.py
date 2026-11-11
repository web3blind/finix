from types import SimpleNamespace
from contextlib import ExitStack
import json
import pytest

@pytest.mark.parametrize('invalidate',['generation','roster','target','source','handoff','pins',None])
def test_cleanup_final_authority(tmp_path,monkeypatch,invalidate):
    from finix import core,recovery_pipeline as p
    from finix.recovery_import import RecoveryImportError
    core.private_dir(tmp_path)
    changed=[];checks=[];admits=[]
    identity=dict(pid=123,instance_id='original',authority_epoch=1,hermes_home='root')
    class Runtime:
        def ready(self,**kwargs):
            checks.append(bool(changed))
            if changed and invalidate in ('generation','roster'):
                raise RecoveryImportError('SERVING_NOT_CONFIRMED')
            return dict(identity)
    handoff={'new_copy_payload':None}
    def cleanup(job):
        changed.append('cleanup')
        return dict(handoff)
    service=SimpleNamespace(profile=tmp_path,_fence=lambda *a:None,cleanup_managed_result=cleanup)
    record=dict(runtime=Runtime(),identity=identity,target=tmp_path,original=None,handoff=handoff,native_stack=ExitStack())
    cap=object();p._CAPS[cap]=record
    monkeypatch.setattr(p,'RecoveryImporter',lambda s:SimpleNamespace(import_job=lambda *a,**k:dict(complete=True,omitted_count=0)))
    monkeypatch.setattr(p,'_mint',lambda *a:cap)
    monkeypatch.setattr(p,'_bootstrap',lambda c:record)
    def admit(c):
        admits.append(bool(changed))
        if changed and invalidate in ('target','source','handoff','pins'):
            raise RecoveryImportError('HISTORY_TARGET_MISMATCH')
        return record
    monkeypatch.setattr(p,'_admit',admit)
    job='1'*32
    if invalidate:
        with pytest.raises(RecoveryImportError):
            p.complete_restore(service,job,profile_id='source',activation='epoch',name='recovered-new')
        for name in (job+'.json','current.json'):
            state=json.loads((tmp_path/'cache/finix/recovery-pipeline'/name).read_text())
            assert state['state']=='interrupted' and 'receipt' not in state
        assert json.loads((tmp_path/'cache/finix/recovery-pipeline'/(job+'.json')).read_text())['target_preserved'] is True
    else:
        receipt=p.complete_restore(service,job,profile_id='source',activation='epoch',name='recovered-new')
        assert receipt['backend_target_ready'] and checks==[False,True] and admits[-1] is True
    assert cap not in p._CAPS


@pytest.mark.parametrize('change',[None,'target','retirement','source'])
def test_final_admit_native_directory_and_retired_handoff(tmp_path,monkeypatch,change):
    from finix import core,recovery_pipeline as p
    from finix.recovery_import import RecoveryImportError
    from hermes_cli import profiles
    root=profiles.get_profile_dir('default')
    target=core.private_dir(profiles.get_profile_dir('recovered-final'))
    handoff={'new_copy_payload':None}
    def fence(*a):
        if change=='source':raise RecoveryImportError('STALE_PROFILE')
    service=SimpleNamespace(_fence=fence)
    record=dict(service=service,job='a'*32,profile_id='source',activation='epoch',
        name='recovered-final',target=target,root=root,anchors=[],purpose='ordinary-new-copy-bootstrap',
        native=p._identity(target),root_identity=p._identity(root),original=None,
        handoff=dict(handoff),cleaned_handoff=dict(handoff))
    if change=='target':
        # Preallocate replacement while original identity remains alive.
        replacement=core.private_dir(target.with_name('replacement'))
        target.rename(target.with_name('preserved-old'))
        replacement.rename(target)
    if change=='retirement':record['cleaned_handoff']={}
    cap=object();p._CAPS[cap]=record
    try:
        if change:
            with pytest.raises(RecoveryImportError):p._admit(cap)
        else:assert p._admit(cap) is record
    finally:p._CAPS.pop(cap)


from types import SimpleNamespace
import pytest
from test_recovery_history_ownership import fenced_root
from finix.recovery_import import RecoveryImportError

def test_shared_bootstrap_rechecks_before_config_park_or_dispatch(tmp_path,monkeypatch):
    from finix import core,recovery_pipeline as p
    from hermes_cli import profiles
    root=profiles.get_profile_dir('default');target=core.private_dir(profiles.get_profile_dir('recovered-new'))
    core.atomic_json(target/'config.yaml',{'model':{}})
    (target/'gateway.parked').write_text('parked')
    before=(target/'config.yaml').read_bytes();writes=[]
    record={'target':target,'root':root,'name':'recovered-new','service':object(),'job':'a'*32,'digest':p.profile_digest(target)}
    monkeypatch.setattr(p,'_admit',lambda cap:record)
    def refuse():raise RecoveryImportError('SERVING_NOT_CONFIRMED')
    shared=SimpleNamespace(check=refuse,ensure=lambda:writes.append('serve'),ready=lambda:dict())
    monkeypatch.setattr(p,'_shared_host',lambda record:shared)
    with pytest.raises(RecoveryImportError,match='SERVING_NOT_CONFIRMED'):
        p._bootstrap(object())
    assert not writes and (target/'gateway.parked').read_text()=='parked'
    assert (target/'config.yaml').read_bytes()==before

def test_shared_ensure_refuses_failed_precheck_without_control_write(monkeypatch):
    from finix import recovery_shared_host as s
    writes=[]
    runtime=s.SharedTargetRuntime({'root':'root','name':'new'},{'served_profiles':[{'home':'root','profile_id':'root'}]})
    def refuse():raise RecoveryImportError('SERVING_NOT_CONFIRMED')
    monkeypatch.setattr(runtime,'check',refuse)
    monkeypatch.setattr(s,'_bound',lambda *a,**k:writes.append(a) or {'served':'new','served_profiles':['new']})
    with pytest.raises(RecoveryImportError,match='SERVING_NOT_CONFIRMED'):runtime.ensure()
    assert writes==[]


def test_known_shared_geometry_warns_without_target_or_key(tmp_path,monkeypatch):
    from finix import core,recovery_shared_host as s
    from gateway import host_attach,status
    from hermes_cli import profiles,gateway_runtime_discovery as d
    root=profiles.get_profile_dir('default');source=core.private_dir(profiles.get_profile_dir('source'))
    from gateway import host_rendezvous as hr
    monkeypatch.setattr(hr,'read_record',lambda *a:SimpleNamespace(home=str(root),pid=123))
    monkeypatch.setattr(hr,'record_names_live_owner',lambda *a:True)
    monkeypatch.setattr(s,'_bound',lambda *a:identity)
    monkeypatch.setattr(status,'get_process_start_time',lambda pid:22)
    identity=dict(pid=123,start_time=22,instance_id='root',authority_epoch=1,state='ready',hermes_home=str(root),
        served_profiles=[dict(home=str(h),profile_id=str(h)) for h in (root,source)])
    monkeypatch.setattr(d,'query_identify',lambda *a,**k:identity)
    warning=s.check_bootstrap_compatibility(SimpleNamespace(profile=source))
    assert warning=={'contract':'cooperative-root-boundaries-v1','condition':'no-concurrent-same-user-target-replacement','atomic_target_binding':False}


def test_new_operation_compatibility_precedes_worker(tmp_path,monkeypatch):
    from finix.recovery_operations import RecoveryOperations
    from finix import recovery_shared_host as s
    service=SimpleNamespace(profile=tmp_path,profile_id='source',activation='a',_fence=lambda *a:None)
    def refuse(service):raise RecoveryImportError('ROOT_SHARED_BOOTSTRAP_UNSUPPORTED')
    monkeypatch.setattr(s,'check_bootstrap_compatibility',refuse)
    ops=RecoveryOperations()
    try:
        with pytest.raises(RecoveryImportError,match='ROOT_SHARED_BOOTSTRAP_UNSUPPORTED'):
            ops.begin(service,request_id='11111111-1111-4111-8111-111111111111',candidate_id='a'*64)
        assert not ops._workers
    finally:ops.close()


def test_completed_replay_does_not_reopen_compatibility(tmp_path,monkeypatch):
    import uuid
    from finix.recovery_operations import RecoveryOperations
    from finix import recovery_shared_host as s
    service=SimpleNamespace(profile=tmp_path,profile_id='source',activation='a',_fence=lambda *a:None)
    monkeypatch.setattr(s,'check_bootstrap_compatibility',lambda service:None)
    def execute(self,service,owned):
        self._publish(service,owned,state='succeeded',receipt={'historical':True})
    monkeypatch.setattr(RecoveryOperations,'_execute',execute)
    ops=RecoveryOperations();request=str(uuid.uuid4())
    ops.begin(service,request_id=request,candidate_id='a'*64);ops.close()
    def refuse(service):pytest.fail('historical replay must not inspect current host')
    monkeypatch.setattr(s,'check_bootstrap_compatibility',refuse)
    assert ops.begin(service,request_id=request,candidate_id='a'*64)['receipt']=={'historical':True}


def test_registered_cli_incompatible_host_precedes_identity(tmp_path,monkeypatch,capsys):
    import uuid
    from finix import core,plugin_cli,recovery_shared_host as s
    from finix.recovery_http import RecoveryHTTP
    import hermes_constants
    home=core.private_dir(tmp_path/'source')
    service=SimpleNamespace(profile=home,profile_id='a'*64,activation='fixture',
        _fence=lambda *a:None,close=lambda:None,
        catalog=lambda *a:[{'id':'b'*64,'complete':True}])
    monkeypatch.setattr(RecoveryHTTP,'service',lambda self,p:service)
    from finix import recovery_http
    monkeypatch.setattr(recovery_http,'binding',lambda p:'fixture')
    monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:home)
    def refuse(service):raise RecoveryImportError('ROOT_SHARED_BOOTSTRAP_UNSUPPORTED')
    monkeypatch.setattr(s,'check_bootstrap_compatibility',refuse)
    profile=SimpleNamespace(home=home,profile_id=service.profile_id,directories=lambda:home)
    args=SimpleNamespace(finix_action='recovery',recovery_action='restore',copy_id='b'*64,
        request_id=str(uuid.uuid4()),acknowledge_incomplete=False,identity=str(tmp_path/'NONEXISTENT-IDENTITY'))
    assert plugin_cli.run(args,profile_factory=lambda:profile)==1
    result=json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result['error']=='ROOT_SHARED_BOOTSTRAP_UNSUPPORTED' and result['activated'] is False


def test_native_managed_cleanup_returns_only_exact_retired_handoff(tmp_path):
    from finix import core
    from finix.recovery import RecoveryService,_identity
    home=core.private_dir(tmp_path/'source')
    service=RecoveryService(profile_id='source',profile_root=home,activation='epoch',protected_roots=[])
    cap=service.allocate_destination('source','epoch')
    bundle=core.private_dir(cap.path)
    (bundle/'config.yaml').write_text('model: {}')
    handoff=dict(bundle_root=str(bundle),profile_root=str(bundle),
        bundle_identity=_identity(bundle.stat(follow_symlinks=False)),
        profile_identity=_identity(bundle.stat(follow_symlinks=False)),new_copy_payload=None)
    # Synthetic registered service state; native allocation/guard/erasure are real.
    service._handoffs['a'*32]=dict(handoff)
    try:
        retired=service.cleanup_managed_result('a'*32)
        assert retired==handoff and retired is not handoff
        assert not bundle.exists() and not service._handoffs and cap not in service._managed
        assert service.cleanup_managed_result('a'*32) is None
    finally:service.close()
