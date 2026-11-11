"""Negative classifier/lease regressions; actual host proof is integration-only."""
from types import SimpleNamespace
import pytest
from test_recovery_history_ownership import fenced_root
from finix.recovery_import import RecoveryImportError


@pytest.mark.parametrize('mode',['absent','named','foreign','unknown','root-single','wrong-pid'])
def test_owner_mode_does_not_invent_root_authority(tmp_path,monkeypatch,mode):
    from finix import core
    from finix.recovery_shared_host import shared_host
    from gateway import host_attach
    from hermes_cli import gateway_runtime_discovery as discovery,profiles
    from gateway import status
    from hermes_constants import get_hermes_home,set_hermes_home_override,reset_hermes_home_override
    root=profiles.get_profile_dir('default');source=core.private_dir(profiles.get_profile_dir('source'))
    target=core.private_dir(profiles.get_profile_dir('recovered-new'));foreign=core.private_dir(tmp_path/'foreign')
    home=source if mode=='named' else foreign if mode=='foreign' else root
    from gateway import host_rendezvous as hr
    owner=None if mode=='absent' else SimpleNamespace(pid=111,home=str(home))
    monkeypatch.setattr(hr,'read_record',lambda *a:owner)
    monkeypatch.setattr(hr,'record_names_live_owner',lambda *a:True)
    monkeypatch.setattr(status,'get_process_start_time',lambda pid:22)
    def query(home,**kw):
        if mode=='absent' or (mode=='named' and home==root):raise FileNotFoundError
        return {'pid':999 if mode=='wrong-pid' else 111,'start_time':22,'instance_id':'one','authority_epoch':1,'state':'starting' if mode=='unknown' else 'ready','hermes_home':str(home),'served_profiles':[{'home':str(home),'profile_id':str(home)}]}
    monkeypatch.setattr(discovery,'query_identify',query)
    monkeypatch.setattr(discovery,'missing_owner_state',lambda *a:'absent')
    from finix import recovery_shared_host as s
    monkeypatch.setattr(s,'_bound',lambda home,*a:query(home))
    record={'root':root,'target':target,'service':SimpleNamespace(profile=root if mode=='root-single' else source)}
    token=set_hermes_home_override(source)
    try:
        if mode in ('foreign','unknown','wrong-pid'):
            with pytest.raises(RecoveryImportError):shared_host(record)
        else:assert shared_host(record) is None
        assert get_hermes_home()==source
        assert not (target/'config.yaml').exists()
    finally:reset_hermes_home_override(token)


@pytest.mark.parametrize('answer',[{}, {'pending':True}, {'error':'not ready'}, {'served':'other','served_profiles':['other']}])
def test_lost_or_foreign_ack_never_falls_back_to_standalone_or_stop(monkeypatch,answer):
    from finix import recovery_shared_host as shared
    identity={'served_profiles':[{'home':'root','profile_id':'root'}]}
    runtime=shared.SharedTargetRuntime({'root':'root','name':'new','target':'new'},identity)
    checks=[]
    monkeypatch.setattr(runtime,'check',lambda:checks.append(True))
    writes=[]
    monkeypatch.setattr(shared,'_bound',lambda *a,**k:writes.append(a) or answer)
    with pytest.raises(RecoveryImportError,match='SERVING_NOT_CONFIRMED'):runtime.ensure()
    assert checks and len(writes)==1
    assert not hasattr(runtime,'child') and not hasattr(runtime,'cleanup')


@pytest.mark.parametrize('change',['pid','start_time','instance_id','authority_epoch','hermes_home','sibling','new-sibling'])
def test_shared_identity_cannot_change_generation_or_siblings(monkeypatch,change):
    from finix import recovery_shared_host as shared,recovery_pipeline as pipeline
    from pathlib import Path
    identity={'pid':1,'start_time':2,'instance_id':'i','authority_epoch':1,'hermes_home':'root','state':'ready','served_profiles':[{'home':'root','profile_id':'root'},{'home':'sibling','profile_id':'sibling'}]}
    record={'root':Path('root'),'target':Path('new'),'root_identity':1,'native':1,'source_native':1,'service':SimpleNamespace(profile=Path('source'),_fence=lambda *a:None),'profile_id':'p','activation':'a'}
    runtime=shared.SharedTargetRuntime(record,identity)
    cap=object();pipeline._CAPS[cap]=record
    monkeypatch.setattr(pipeline,'_admit',lambda cap:record)
    monkeypatch.setattr(pipeline,'_identity',lambda path:1)
    changed=dict(identity)
    if change=='sibling':changed['served_profiles']=identity['served_profiles'][:1]
    elif change=='new-sibling':changed['served_profiles']=[*identity['served_profiles'],{'home':'unrequested','profile_id':'unrequested'}]
    else:changed[change]='changed'
    monkeypatch.setattr(shared,'_bound',lambda *a,**k:changed)
    try:
        with pytest.raises(RecoveryImportError):runtime.check()
    finally:pipeline._CAPS.pop(cap)
