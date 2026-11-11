"""Modeled scheduling at checked boundaries; not atomic same-UID exclusion."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from finix.recovery_import import RecoveryImportError
from test_recovery_history_ownership import fenced_root

@pytest.mark.parametrize('stage,change',[(stage,change) for stage in ('prewrite','postwrite') for change in ('source','root','target','pins','handoff','epoch','generation','roster',None)]+[('prewrite','already-served')])
def test_prewrite_and_postwrite_checks_preserve_shared_root(tmp_path,monkeypatch,stage,change):
 from finix import core,recovery_shared_host as s,recovery_pipeline as p
 from hermes_cli import profiles
 root=profiles.get_profile_dir('default');source=core.private_dir(profiles.get_profile_dir('source'));target=core.private_dir(profiles.get_profile_dir('recovered-boundary'))
 original=[dict(home=str(h),profile_id=str(h)) for h in (root,source)]
 identity=dict(pid=123,start_time=22,instance_id='i',authority_epoch=1,hermes_home=str(root),state='ready',served_profiles=original)
 state={'changed':False,'served':False};writes=[]
 def admit(cap):
  if state['changed'] and change in ('pins','handoff'):raise RecoveryImportError('HOST_UNAVAILABLE')
  return record
 def fence(*a):
  if state['changed'] and change=='source':raise RecoveryImportError('STALE_PROFILE')
 service=SimpleNamespace(profile=source,_fence=fence)
 record=dict(service=service,root=root,target=target,name=target.name,root_identity=1,native=1,source_native=1,profile_id='p',activation='e')
 cap=object();p._CAPS[cap]=record
 monkeypatch.setattr(p,'_admit',admit)
 monkeypatch.setattr(p,'_identity',lambda path:2 if state['changed'] and ((path==root and change=='root') or (path==target and change=='target')) else 1)
 def bound(home,expected,verb,params=None,*,before_write=None):
  if verb=='serve-profile':
   if stage=='prewrite':state['changed']=True
   before_write()
   writes.append(params);state['served']=True
   state['changed']=True
   return dict(served=target.name,served_profiles=['default','source',target.name])
  current=dict(identity)
  if state['changed']:
   if change=='epoch':current['authority_epoch']=2
   if change=='generation':current['start_time']=23
   if change=='roster':current['served_profiles']=original[:1]
  if state['served'] or (state['changed'] and change=='already-served'):current['served_profiles']=[*current['served_profiles'],dict(home=str(target),profile_id=str(target))]
  return current
 monkeypatch.setattr(s,'_bound',bound)
 runtime=s.SharedTargetRuntime(record,identity)
 try:
  if change:
   with pytest.raises(RecoveryImportError):runtime.ensure()
   assert len(writes)==(1 if stage=='postwrite' else 0)
  else:
   assert runtime.ensure()['served']==target.name
   assert writes==[{'name':target.name}]
  assert not hasattr(runtime,'cleanup') and not hasattr(runtime,'child') and not p._OWNED_TARGETS
 finally:p._CAPS.pop(cap)


def test_lost_mutation_ack_interrupts_without_root_cleanup_or_pipeline_replay(tmp_path,monkeypatch):
 from contextlib import ExitStack
 from finix import core,recovery_pipeline as p,recovery_shared_host as s
 core.private_dir(tmp_path)
 service=SimpleNamespace(profile=tmp_path,_fence=lambda *a:None)
 record=dict(native_stack=ExitStack())
 cap=object();p._CAPS[cap]=record
 runtime=s.SharedTargetRuntime(dict(root='root',target='new',name='new'),dict(served_profiles=[dict(home='root',profile_id='root')]))
 writes=[]
 monkeypatch.setattr(runtime,'check',lambda:dict(served_profiles=[dict(home='root',profile_id='root')]))
 def lost(*a,**kw):
  kw['before_write']();writes.append('serve-profile');raise ConnectionError('fixture lost ACK')
 monkeypatch.setattr(s,'_bound',lost)
 monkeypatch.setattr(p,'RecoveryImporter',lambda service:SimpleNamespace(import_job=lambda *a,**k:{}))
 monkeypatch.setattr(p,'_mint',lambda *a:cap)
 monkeypatch.setattr(p,'_bootstrap',lambda cap:runtime.ensure())
 job='e'*32
 with pytest.raises(ConnectionError):p.complete_restore(service,job,profile_id='source',activation='epoch',name='recovered-new')
 state=p.pipeline_current(service,profile_id='source',activation='epoch')
 assert state['state']=='interrupted' and 'receipt' not in state
 with pytest.raises(RecoveryImportError,match='RECOVERY_INTERRUPTED'):
  p.complete_restore(service,job,profile_id='source',activation='epoch',name='recovered-new')
 assert writes==['serve-profile'] and not p._OWNED_TARGETS and cap not in p._CAPS
 assert not hasattr(runtime,'cleanup')
