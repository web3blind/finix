"""Real stock root fixture; no SDK patch, fake registry or ACK injection."""
import json,os,time,asyncio
from pathlib import Path
from finix import core
from finix.recovery_activation import _WindowsLaunch
from finix.recovery_pipeline import _peer
from finix.recovery_import import _safe_config,_yaml_mapping


def start_root(root,request):
    from hermes_constants import get_default_hermes_root
    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
    from hermes_cli.gateway_runtime_discovery import query_identify
    assert root.samefile(get_default_hermes_root()) and root.samefile(get_profile_dir('default'))
    config=_yaml_mapping(_safe_config((root/'config.yaml').read_bytes()))
    config['gateway'].update(multiplex_profiles=True,standalone=False)
    core.atomic_json(root/'config.yaml',config)
    core.atomic_json(get_profile_dir('source')/'config.yaml',{'model':{},'plugins':{'enabled':[]},'gateway':{'standalone':False}})
    os.environ['FINIX_REGISTERED_ROOT']=str(root.resolve())
    child=spawn_unmanaged_gateway(root,deadline=time.monotonic()+60)
    native=_WindowsLaunch(child) if os.name=='nt' else None
    class OwnedFixture:
        def cleanup(self):
            if native:native.stop()
            if child.poll() is None:child.terminate();child.wait(timeout=15)
            if native:native.close()
            core.atomic_json(root/'root-fixture-cleanup.json',{'launcher_pid':child.pid,'reaped':child.poll() is not None})
        def verify(self,target):
            after=query_identify(root,timeout=2)
            assert all(after[k]==before[k] for k in ('pid','start_time','instance_id','authority_epoch','hermes_home'))
            assert all(row in after['served_profiles'] for row in before['served_profiles'])
            assert sibling=={name:core.digest_file(root/'profiles/source'/name) for name in sibling}
            _peer(root,after['pid'])
            assert {'home':str(target),'profile_id':str(target)} in after['served_profiles']
            from hermes_cli.gateway_runtime import discover_gateway_endpoint
            from hermes_cli.gateway_client import connect_gateway
            from hermes_constants import set_hermes_home_override,reset_hermes_home_override
            token=set_hermes_home_override(target)
            try:
                endpoint=discover_gateway_endpoint(target).endpoint
                assert endpoint.instance_id==before['instance_id']
                async def describe():
                    async with connect_gateway(endpoint) as rpc:return await rpc.rpc('profiles.describe',name=target.name)
                described=asyncio.run(describe())
                assert described['name']==target.name
            finally:reset_hermes_home_override(token)
            core.atomic_json(root/'root-shared-proof.json',{'before':before,'after':after,'profiles_describe':described,'shared_instance':True,'target':str(target),'sibling_state_unchanged':True,'kernel_peer':{'pid':after['pid'],'api':'GetNamedPipeServerProcessId' if os.name=='nt' else 'SO_PEERCRED'}})
    owned=OwnedFixture();request.addfinalizer(owned.cleanup)
    deadline=time.monotonic()+60
    while time.monotonic()<deadline and child.poll() is None:
        if native:native.capture()
        try:before=query_identify(root,timeout=.5)
        except (OSError,ValueError):before={}
        if before.get('state')=='ready' and len(before.get('served_profiles',[]))==2:
            assert native.matches(before['pid']) if native else before['pid']==child.pid
            _peer(root,before['pid'])
            sibling={name:core.digest_file(root/'profiles/source'/name) for name in ('state.db','config.yaml')}
            core.atomic_json(root/'root-fixture-ready.json',before)
            return owned
        time.sleep(.05)
    raise AssertionError('genuine root multiplexer not ready')
