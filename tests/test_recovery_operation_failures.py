"""Known pre-import failures release only after data worker/cleanup, never atomic ambiguity."""
import threading,time,uuid
import pytest
from finix import core

@pytest.mark.parametrize('code',['WRONG_KEY','CORRUPT_ARCHIVE','TARGET_NOT_EMPTY','COMPLETENESS_CHANGED'])
def test_known_preimport_failure_has_exact_terminal_code_and_no_import(tmp_path,monkeypatch,code):
    from finix.recovery_operations import RecoveryOperations
    import finix.recovery_pipeline as pipeline
    home=core.private_dir(tmp_path/'source');calls=[]
    class Service:
        profile=home;profile_id='a'*64;activation='fixture'
        def _fence(self,*a):pass
        def catalog(self,*a):return [dict(id='b'*64,complete=True)]
        def allocate_destination(self,*a):return object()
        def start(self,*a,**kw):calls.append('decrypt');return 'd'*32
        def wait(self,*a):return dict(status='succeeded',result={'complete':False}) if code=='COMPLETENESS_CHANGED' else dict(status='failed',error=code)
        def cleanup_managed_result(self,*a):calls.append('cleanup')
    def forbidden(*a,**kw):calls.append('import');pytest.fail('failure reached import')
    monkeypatch.setattr(pipeline,'complete_restore',forbidden)
    s=Service();ops=RecoveryOperations();rid=str(uuid.uuid4())
    ack=ops.begin(s,request_id=rid,candidate_id='b'*64)
    deadline=time.monotonic()+3
    while ops.current(s)['phase']!='awaiting_key' and time.monotonic()<deadline:time.sleep(.01)
    ops.key(s,ack['id'],request_id=str(uuid.uuid4()),identity=bytearray(b'fixture'))
    ops.close();state=ops.current(s)
    assert state['state']=='failed' and state['error']==code
    assert calls==(['decrypt','cleanup'] if code=='COMPLETENESS_CHANGED' else ['decrypt'])
    assert ops.replay(s,request_id=rid,candidate_id='b'*64)==state
    # Fresh consent may begin only after the original worker actually relinquishes its lease.
    imported=[]
    if code=='COMPLETENESS_CHANGED':
        def acknowledged(*a,**kw):
            imported.append(kw['acknowledge_incomplete']);return {'unit_receipt':True}
        monkeypatch.setattr(pipeline,'complete_restore',acknowledged)
    fresh=ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64,acknowledge_incomplete=True)
    assert fresh['id']!=ack['id']
    if code=='COMPLETENESS_CHANGED':
        deadline=time.monotonic()+3
        while ops.current(s)['phase']!='awaiting_key' and time.monotonic()<deadline:time.sleep(.01)
        ops.key(s,fresh['id'],request_id=str(uuid.uuid4()),identity=bytearray(b'fixture'))
        ops.close()
        assert ops.current(s)['state']=='succeeded' and imported==[True]
    else:
        ops.cancel(s,fresh['id']);ops.close()


@pytest.mark.parametrize('phase',['importing','target_bootstrap','history_importing','cleanup'])
def test_known_code_cannot_release_atomic_or_ambiguous_owner(tmp_path,monkeypatch,phase):
    from finix.recovery_operations import RecoveryOperations
    from finix.recovery_import import RecoveryImportError
    home=core.private_dir(tmp_path/'source')
    class Service:
        profile=home;profile_id='a'*64;activation='fixture'
        def _fence(self,*a):pass
    def execute(self,service,owned):
        self._publish(service,owned,phase=phase,cancellable=False)
        raise RecoveryImportError('WRONG_KEY')
    monkeypatch.setattr(RecoveryOperations,'_execute',execute)
    s=Service();ops=RecoveryOperations();rid=str(uuid.uuid4())
    ack=ops.begin(s,request_id=rid,candidate_id='b'*64);ops.close()
    state=ops.current(s)
    assert state['state']=='interrupted' and state['error']=='RECOVERY_INTERRUPTED'
    assert state['id']==ack['id'] and state['phase']==phase
    with pytest.raises(RecoveryImportError,match='BUSY'):
        ops.begin(s,request_id=str(uuid.uuid4()),candidate_id='b'*64)
