"""Exact subprocess fixture for OS lease/death, never a product authority API."""
import json,sys,threading
from pathlib import Path
from finix import core
from finix.recovery_operations import RecoveryOperations
from hermes_constants import get_default_hermes_root,get_hermes_home
from hermes_cli.profiles import get_profile_dir
scope=Path(__import__('os').environ['FINIX_BOUND_SCOPE'])
assert Path(get_default_hermes_root()).is_relative_to(scope)
assert Path(get_hermes_home()).is_relative_to(scope)
assert Path(get_profile_dir('fixture')).is_relative_to(scope)
class Service:
    profile=Path(sys.argv[1]);profile_id='a'*64;activation='source-epoch'
    def _fence(self,pid,epoch):assert (pid,epoch)==(self.profile_id,self.activation)
assert Service.profile.is_relative_to(scope)
ops=RecoveryOperations();release=threading.Event()
def execute(service,owned):
    ops._publish(service,owned,phase='importing',cancellable=False)
    print(json.dumps({'id':owned['id'],'phase':'importing'}),flush=True)
    release.wait(60)
ops._execute=execute
ops.begin(Service(),request_id=sys.argv[2],candidate_id='b'*64)
sys.stdin.readline();release.set();ops.close()
