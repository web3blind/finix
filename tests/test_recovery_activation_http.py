"""Real mounted ASGI requests, installed importer, synthetic terminal handoff only."""
import threading
from pathlib import Path
from test_recovery_api import api, prepare


def terminal(api, monkeypatch):
    c,m,p,t=api
    monkeypatch.setenv('HERMES_HOME',str(t/'disposable-host'))
    for key in ('HOME','USERPROFILE','LOCALAPPDATA'):
        monkeypatch.setenv(key,str(t/'account'))
    from hermes_cli import profiles as host
    assert Path(host.get_profile_dir('restored')) == t/'disposable-host/profiles/restored'
    (t/'cipher.age').write_bytes(b'synthetic ciphertext')
    jid=prepare(c,p,t).json()['id']
    adapter=m._recovery_router.recovery_adapter
    job=adapter.jobs[jid]; service=job['service']
    payload=t/'candidate';payload.mkdir()
    (payload/'config.yaml').write_text('model: {default: fixture}\nsecrets: {sources: [onepassword], onepassword: {enabled: true}}\n')
    service._jobs['terminal']=({'status':'succeeded','result':{'complete':False,'omitted_count':2,'layout':'profile'}},threading.Event())
    service._handoffs['terminal']={'bundle_root':str(payload),'profile_root':str(payload),'complete':False,'activated':False,'cron_disabled':True}
    job['service_id']='terminal'
    return c,m,p,t,jid


def test_real_http_import_incomplete_requires_literal_separate_ack(api,monkeypatch):
    c,m,p,t,jid=terminal(api,monkeypatch)
    url='/recovery/jobs/'+jid+'/import'
    body=dict(profile_id=p.profile_id,name='restored',consent_import=True)
    assert c.post(url,json=body).json()['detail']=='INCOMPLETE_ACK_REQUIRED'
    for value in (1,'true',None):
        assert c.post(url,json={**body,'acknowledge_incomplete':value}).status_code==422
    result=c.post(url,json={**body,'acknowledge_incomplete':True})
    assert result.status_code==200,result.text
    assert result.json()['complete'] is False
    assert result.json()['omitted_count']==2
    assert (t/'disposable-host/profiles/restored/gateway.parked').is_file()


def test_start_requires_import_job_bound_name_and_positive_ack(api,monkeypatch):
    from types import SimpleNamespace
    c,m,p,t,jid=terminal(api,monkeypatch)
    base='/recovery/jobs/'+jid
    body=dict(profile_id=p.profile_id,name='restored',consent_start=True)
    assert c.post(base+'/start',json=body).status_code==409
    assert c.post(base+'/import',json=dict(profile_id=p.profile_id,name='restored',consent_import=True,acknowledge_incomplete=True)).status_code==200
    from gateway import host_attach,control_socket
    calls=[]
    owner=SimpleNamespace(home=t/'disposable-host',standalone=False)
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:owner)
    def serve(home,name):
        calls.append((home,name))
        assert not (t/'disposable-host/profiles/restored/gateway.parked').exists()
        return {'served':name,'served_profiles':['default',name]}
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',serve)
    for extra in ({'name':'OTHER'},{'consent_start':1},{'activation':'client'},{'profile_id':'OTHER'}):
        assert c.post(base+'/start',json={**body,**extra}).status_code in (409,422)
    assert calls==[]
    out=c.post(base+'/start',json=body)
    assert out.status_code==200,out.text
    assert out.json()==dict(name='restored',served='restored',integrations_paused=True)
    assert calls==[(t/'disposable-host','restored')]
    assert not (t/'disposable-host/profiles/restored/gateway.parked').exists()
