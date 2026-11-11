"""Private durable pipeline ownership; no authority reconstructed on reopen."""
import threading,time,json
from types import SimpleNamespace
import pytest
from finix import core
from finix import recovery_pipeline as pipeline

def service(tmp_path):
    home=core.private_dir(tmp_path/'source')
    def fence(profile_id,activation):
        if (profile_id,activation)!=('source','epoch'):raise RuntimeError('STALE_PROFILE')
    return SimpleNamespace(profile=home,_fence=fence)

def test_blocked_owner_status_independent_and_second_owner_refused(tmp_path):
    s=service(tmp_path);entered=threading.Event();release=threading.Event();errors=[]
    def owner():
        try:
            with pipeline.pipeline_owner(s,'a'*32,profile_id='source',activation='epoch',name='recovered-a') as publish:
                publish('history_importing');entered.set();release.wait(5)
        except BaseException as e:errors.append(e)
    t=threading.Thread(target=owner);t.start()
    try:
        assert entered.wait(2)
        start=time.monotonic()
        status=pipeline.pipeline_current(s,profile_id='source',activation='epoch')
        assert time.monotonic()-start<.5
        assert status['phase']=='history_importing' and status['state']=='running'
        with pytest.raises(Exception,match='BUSY'):
            with pipeline.pipeline_owner(s,'b'*32,profile_id='source',activation='epoch',name='recovered-b'):pass
        with pytest.raises(RuntimeError,match='STALE_PROFILE'):
            pipeline.pipeline_current(s,profile_id='foreign',activation='epoch')
    finally:release.set();t.join(5)
    assert not t.is_alive() and not errors
    assert pipeline.pipeline_current(s,profile_id='source',activation='epoch')['state']=='interrupted'
    prior = json.loads((s.profile/'cache/finix/recovery-pipeline/current.json').read_text())
    with pytest.raises(Exception,match='RECOVERY_INTERRUPTED'):
        with pipeline.pipeline_owner(s,'b'*32,profile_id='source',activation='epoch',name='recovered-b'):pass


def test_completed_record_is_not_clobbered_by_duplicate(tmp_path):
    s=service(tmp_path)
    with pipeline.pipeline_owner(s,'a'*32,profile_id='source',activation='epoch',name='recovered-a') as publish:
        publish('backend_target_ready',{'name':'recovered-a','backend_target_ready':True})
    original=(s.profile/'cache/finix/recovery-pipeline/current.json').read_bytes()
    with pytest.raises(Exception,match='RECOVERY_INTERRUPTED'):
        with pipeline.pipeline_owner(s,'a'*32,profile_id='source',activation='epoch',name='recovered-a'):pass
    assert (s.profile/'cache/finix/recovery-pipeline/current.json').read_bytes()==original
