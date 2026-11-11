"""No untrusted import can authorize a host lifecycle call."""
import pytest
from types import SimpleNamespace
from test_recovery_api import api
from test_recovery_activation_http import terminal


@pytest.mark.parametrize('answer',[None,{'pending':True},{'served':'OTHER','served_profiles':['OTHER']},{'served':'restored','error':'private-path'}, {'served':'restored','served_profiles':[]}])
def test_unconfirmed_start_reparks_and_does_not_claim_usable(api,monkeypatch,answer):
    from gateway import host_attach,control_socket
    c,m,p,t,jid=terminal(api,monkeypatch);base='/recovery/jobs/'+jid
    assert c.post(base+'/import',json=dict(profile_id=p.profile_id,name='restored',consent_import=True,acknowledge_incomplete=True)).status_code==200
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:SimpleNamespace(home=t/'disposable-host',standalone=False))
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',lambda *args:answer)
    out=c.post(base+'/start',json=dict(profile_id=p.profile_id,name='restored',consent_start=True))
    assert out.status_code==409 and out.json()=={'detail':'SERVING_NOT_CONFIRMED'}
    assert (t/'disposable-host/profiles/restored/gateway.parked').read_bytes()==b'finix-recovered-review-required\n'


def test_closed_recovery_cannot_authorize_start(api,monkeypatch):
    from gateway import host_attach
    c,m,p,t,jid=terminal(api,monkeypatch);base='/recovery/jobs/'+jid
    assert c.post(base+'/import',json=dict(profile_id=p.profile_id,name='restored',consent_import=True,acknowledge_incomplete=True)).status_code==200
    calls=[]
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:calls.append(True))
    m._recovery_router.recovery_adapter.jobs[jid]['service'].deactivate()
    out=c.post(base+'/start',json=dict(profile_id=p.profile_id,name='restored',consent_start=True))
    assert out.status_code==409 and out.json()['detail'] in {'CLOSED','STALE_PROFILE','NOT_FOUND'}
    assert calls==[]


def test_changed_import_refused_before_host_discovery(api,monkeypatch):
    from gateway import host_attach
    c,m,p,t,jid=terminal(api,monkeypatch);base='/recovery/jobs/'+jid
    assert c.post(base+'/import',json=dict(profile_id=p.profile_id,name='restored',consent_import=True,acknowledge_incomplete=True)).status_code==200
    (t/'disposable-host/profiles/restored/config.yaml').write_text('model: danger\n')
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:pytest.fail('must not discover host'))
    out=c.post(base+'/start',json=dict(profile_id=p.profile_id,name='restored',consent_start=True))
    assert out.status_code==409 and out.json()=={'detail':'IMPORTED_PROFILE_CHANGED'}
    assert (t/'disposable-host/profiles/restored/gateway.parked').is_file()
