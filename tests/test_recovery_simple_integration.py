"""Exact HTTP contract, real generated age, installed inert lifecycle; no providers."""
import importlib,json,os
import pytest
from pathlib import Path
from types import SimpleNamespace
from test_recovery_api import api
from test_recovery_auto import fixture_backup
from test_recovery_composed import poll


def test_simple_http_installed_lifecycle_and_response_fixture(api,monkeypatch):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    core=importlib.import_module(m._package+'.core')
    saved=core.load_state(p.root/'index')['backups'][0]
    assert item['created_at']==saved['created_at']
    responses={}
    def record(path,**kwargs):
        response=c.post(path,**kwargs) if kwargs else c.get(path)
        assert response.status_code==200,response.text
        responses[path]=response.json()
        return response.json()
    record('/recovery/capabilities');record('/recovery/catalog')
    prep=record('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    jid,name=prep['id'],prep['name']; url='/recovery/jobs/'+jid
    record(url+'/key',files={'file':('key.txt',key.read_bytes(),'text/plain')})
    assert poll(c,jid)['state']=='succeeded';record(url)
    record(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True))
    job=m._recovery_router.recovery_adapter.jobs[jid]
    bundle=job['destination'].path
    assert bundle.exists(),'start guard needs the original handoff'
    from hermes_cli import profiles as host
    from gateway import host_attach,control_socket
    target=Path(host.get_profile_dir(name)); assert target==t/'host/profiles'/name
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:SimpleNamespace(home=t/'host',standalone=False))
    def serve(home,requested):
        assert requested==name and bundle.exists()
        assert not (target/'gateway.parked').exists()
        return dict(served=requested,served_profiles=['default',requested])
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',serve)
    if os.name == 'nt':
        Recovery=importlib.import_module(m._package+'.recovery')
        def deny_path_only_cleanup(*args,**kwargs):
            raise AssertionError('use exact handle-relative deletion, not path-only rmtree')
        monkeypatch.setattr(Recovery.shutil,'rmtree',deny_path_only_cleanup)
    record(url+'/start',json=dict(profile_id=p.profile_id,name=name,consent_start=True))
    assert not bundle.exists(),'managed second plaintext must be erased after exact serving ACK'
    assert (target/'config.yaml').is_file()
    assert job['cleanup_timer'].finished.is_set()
    if os.environ.get('FINIX_SIMPLE_RESPONSE_FIXTURE'):
        Path(os.environ['FINIX_SIMPLE_RESPONSE_FIXTURE']).write_text(json.dumps({'responses':responses,'name':name,'job':jid,'profile_id':p.profile_id,'candidate':item['id']},indent=2))


def test_catalog_times_validated_local_and_same_hash_cloud_only(api):
    c,m,p,t=api
    item,key=fixture_backup(api)
    service=m._recovery_router.recovery_adapter.service(p)
    Recovery=importlib.import_module(m._package+'.recovery')
    source=service._sources['local'];row=source.records()[0]
    same=dict(row);same.pop('created_at',None)
    different=dict(same,sha256='b'*64)
    invalid=dict(different,sha256='c'*64,created_at='not-a-date')
    persisted=dict(different,sha256='d'*64,created_at='2025-02-03T04:05:06+00:00')
    service.add_source(Recovery.CommittedSource('cloud','synthetic',lambda:[same,different,invalid,persisted],lambda *a:None))
    rows=c.get('/recovery/catalog').json()['items']; cloud=[r for r in rows if r['source']=='cloud']
    by_hash={r['sha256']:r for r in cloud}
    assert by_hash[row['sha256']]['created_at']==row['created_at']
    assert by_hash['b'*64]['created_at'] is None
    assert by_hash['c'*64]['created_at'] is None
    assert by_hash['d'*64]['created_at']=='2025-02-03T04:05:06+00:00'


@pytest.mark.parametrize('replaced',[False,True])
def test_abandoned_import_cleanup_deadline_is_exact_and_identity_anchored(api,monkeypatch,replaced):
    import shutil
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True)).json()
    jid=prep['id'];url='/recovery/jobs/'+jid
    assert c.post(url+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code==200
    assert poll(c,jid)['state']=='succeeded'
    assert c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True)).status_code==200
    job=m._recovery_router.recovery_adapter.jobs[jid];bundle=job['destination'].path
    timer=job['cleanup_timer'];assert timer.interval==600
    timer.cancel()
    sentinel=p.home/'sentinel';sentinel.write_text('preserve')
    if replaced:
        bundle.rename(bundle.with_name('original'))
        bundle.mkdir();(bundle/'preserve').write_text('replacement')
    timer.function()
    if replaced:
        assert (bundle/'preserve').read_text()=='replacement'
        assert job['cleanup_failed'] is True
    else:
        assert not bundle.exists()
    assert sentinel.read_text()=='preserve'
    assert job['imported']['target'].is_dir()

