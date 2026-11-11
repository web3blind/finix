"""Actual installed control socket + lifecycle handler; inert runtime roster fixture.
No model, integrations, external source, or real gateway process starts.
"""
import asyncio
from types import SimpleNamespace
import yaml
from test_recovery_api import api
from test_recovery_activation_http import terminal


def test_installed_host_control_transport_and_serving_verb_ack(api,monkeypatch):
    from gateway import host_attach
    from gateway.control_socket import GatewayControlServer
    from gateway.run_profile_reconcile import serve_profile_verb
    c,m,p,t,jid=terminal(api,monkeypatch)
    base='/recovery/jobs/'+jid
    assert c.post(base+'/import',json=dict(profile_id=p.profile_id,name='restored',consent_import=True,acknowledge_incomplete=True)).status_code==200
    home=t/'disposable-host';target=home/'profiles/restored'
    owner=SimpleNamespace(home=home,standalone=False)
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:owner)
    evidence=[]
    class Runner:
        _running=True
        _primary_profile_name='default'
        _served_profile_homes={}
        def _multiplex_on(self):return True
        def _reconcile_lock(self):return self.lock
        async def _apply_profile_changes(self,known,added,removed,changed,**kw):
            assert added==['restored'] and not removed and not changed
            cfg=yaml.safe_load((target/'config.yaml').read_text())
            assert cfg['plugins']['enabled']==[] and cfg['hooks']=={}
            assert cfg['secrets']['sources']==['onepassword']
            assert cfg['secrets']['onepassword']['enabled'] is False
            assert not (target/'gateway.parked').exists()
            self._served_profile_homes=known
            evidence.append('installed handler accepted inert imported profile')
            return {'served_profiles':['default',*known]}
    async def exercise():
        runner=Runner();runner.lock=asyncio.Lock()
        server=GatewayControlServer(home,verb_handlers={'serve-profile':serve_profile_verb(runner)})
        assert await server.start()
        try:
            out=await asyncio.to_thread(c.post,base+'/start',json=dict(profile_id=p.profile_id,name='restored',consent_start=True))
            assert out.status_code==200,out.text
            assert out.json()==dict(name='restored',served='restored',integrations_paused=True)
            assert runner._served_profile_homes=={'restored':target}
        finally:await server.stop()
    asyncio.run(exercise())
    assert evidence==['installed handler accepted inert imported profile']
