from importlib import import_module
from test_recovery_api import api

def test_capability_root_warning_is_server_observation_not_client_authority(api,monkeypatch):
    c,m,p,t=api
    shared=import_module(m._package+'.recovery_shared_host')
    warning={'contract':'cooperative-root-boundaries-v1','condition':'no-concurrent-same-user-target-replacement','atomic_target_binding':False}
    seen=[]
    monkeypatch.setattr(shared,'check_bootstrap_compatibility',lambda service:seen.append(service.profile) or warning)
    response=c.get('/recovery/capabilities')
    assert response.status_code==200,response.text
    assert response.json()['bootstrap_condition']==warning
    assert seen==[p.home]
    assert str(t) not in response.text
