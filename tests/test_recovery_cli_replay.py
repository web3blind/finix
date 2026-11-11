"""Registered CLI replay through real durable records and native kernel lease."""
import json,threading,uuid
from types import SimpleNamespace
import pytest
from finix import core


def test_registered_cli_exact_replay_reads_running_and_interrupted_without_catalog_or_key(tmp_path,monkeypatch,capsys):
    from finix.recovery_operations import RecoveryOperations
    from finix import plugin_cli
    from finix.recovery_http import RecoveryHTTP
    import hermes_constants
    home=core.private_dir(tmp_path/'source')
    class Service:
        profile=home
        profile_id='a'*64
        activation='fixture-epoch'
        def _fence(self,pid,epoch):
            assert (pid,epoch)==(self.profile_id,self.activation)
        def close(self):pass
    s=Service();ops=RecoveryOperations();entered=threading.Event();release=threading.Event();calls=[]
    def execute(self,service,owned):
        calls.append('worker');entered.set();release.wait(5)
        self._publish(service,owned,state='interrupted',cancellable=False,error='RECOVERY_INTERRUPTED')
    monkeypatch.setattr(RecoveryOperations,'_execute',execute)
    monkeypatch.setattr(RecoveryHTTP,'service',lambda self,p:s)
    monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:s.profile)
    def forbidden_catalog(*a):
        calls.append('catalog');pytest.fail('replay must precede catalog/key admission')
    s.catalog=forbidden_catalog
    profile=SimpleNamespace(home=home,profile_id=s.profile_id,directories=lambda:home)
    rid=str(uuid.uuid4());ack=ops.begin(s,request_id=rid,candidate_id='b'*64)
    assert entered.wait(1)
    def invoke(request=rid,candidate='b'*64):
        args=SimpleNamespace(finix_action='recovery',recovery_action='restore',copy_id=candidate,request_id=request,acknowledge_incomplete=False,identity=str(tmp_path/'NONEXISTENT-IDENTITY'))
        code=plugin_cli.run(args,profile_factory=lambda:profile)
        lines=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
        return code,lines
    try:
        code,lines=invoke()
        assert code==0 and lines[-1]['id']==ack['id'] and lines[-1]['state']=='running'
        assert lines[-1]['activated'] is False
        code,lines=invoke(str(uuid.uuid4()))
        assert code==1 and lines[-1]['error']=='BUSY'
        code,lines=invoke(candidate='c'*64)
        assert code==1 and lines[-1]['error']=='REQUEST_CONFLICT'
    finally:release.set();ops.close()
    code,lines=invoke()
    assert code==0 and lines[-1]['id']==ack['id'] and lines[-1]['state']=='interrupted'
    assert lines[-1]['activated'] is False
    code,lines=invoke(str(uuid.uuid4()))
    assert code==1 and lines[-1]['error']=='BUSY'
    assert calls==['worker']
