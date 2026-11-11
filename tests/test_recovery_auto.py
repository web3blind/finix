"""Automatic recovery uses generated fixtures and an isolated host only."""
import importlib
from pathlib import Path
import pytest
from test_recovery_api import api
from test_recovery_composed import poll


def fixture_backup(api):
    c,m,p,t = api
    core = importlib.import_module(m._package+'.core')
    import sqlite3
    with sqlite3.connect(p.home/'state.db') as db:
        db.execute('create table evidence(v)')
        db.execute("insert into evidence values ('committed')")
    (p.home/'config.yaml').write_text('model: {default: synthetic-model}\n')
    key=t/'fixture-key'
    recipient=core.keygen(key)
    core.backup(p.home,p.root/'copies',p.root/'index',recipient,
        sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    return c.get('/recovery/catalog').json()['items'][0], key


def test_auto_prepare_inert_bound_name_no_client_authority(api,monkeypatch):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    monkeypatch.setenv('LOCALAPPDATA',str(t/'account/local'))
    monkeypatch.setenv('HOME',str(t/'account'))
    monkeypatch.setenv('USERPROFILE',str(t/'account'))
    item,key=fixture_backup(api)
    body=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True)
    for extra in ({'name':'client'},{'destination_path':str(t/'bad')},{'allow_protected':True}):
        assert c.post('/recovery/prepare-auto',json=dict(body,**extra)).status_code==422
    r=c.post('/recovery/prepare-auto',json=body)
    assert r.status_code==200,r.text
    result=r.json()
    assert set(result)=={'id','profile_id','state','name'}
    assert result['state']=='awaiting_key'
    from hermes_cli import profiles as host
    assert not Path(host.get_profile_dir(result['name'])).exists()
    job=m._recovery_router.recovery_adapter.jobs[result['id']]
    assert job['service_id'] is None
    assert not list((p.home/'cache/finix/recovered').rglob('config.yaml'))
    assert c.post('/recovery/jobs/'+result['id']+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True)).status_code==409


def test_auto_real_age_and_supported_host_import(api,monkeypatch):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    before=(p.home/'config.yaml').read_bytes()
    r=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    assert r.status_code==200,r.text
    jid,name=r.json()['id'],r.json()['name']
    url='/recovery/jobs/'+jid
    assert c.post(url+'/key',files={'file':('key.txt',key.read_bytes(),'text/plain')}).status_code==200
    result=poll(c,jid)
    assert result['state']=='succeeded',result
    imported=c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True))
    assert imported.status_code==200,imported.text
    assert imported.json()['name']==name
    from hermes_cli import profiles as host
    target=Path(host.get_profile_dir(name))
    assert (target/'gateway.parked').is_file()
    import sqlite3
    with sqlite3.connect(target/'state.db') as db:
        assert db.execute('select * from evidence').fetchall()==[('committed',)]
        assert db.execute('pragma integrity_check').fetchone()==('ok',)
    assert (p.home/'config.yaml').read_bytes()==before
    assert list((p.home/'cache/finix/restore').iterdir())==[]
    assert c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True)).json()['detail']=='PROFILE_EXISTS'
    from gateway import host_attach,control_socket
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:SimpleNamespace(home=t/'host',standalone=False))
    def serve(home,requested):
        calls.append((home,requested))
        assert not (target/'gateway.parked').exists()
        return {'served':requested,'served_profiles':['default',requested]}
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',serve)
    body=dict(profile_id=p.profile_id,name=name,consent_start=True)
    assert c.post(url+'/start',json=dict(body,name='another')).status_code==409
    assert calls==[]
    started=c.post(url+'/start',json=body)
    assert started.status_code==200,started.text
    assert started.json()['served']==name
    assert calls==[(t/'host',name)]
    assert (p.home/'config.yaml').read_bytes()==before


@pytest.mark.parametrize('advertised',[False,None,True])
def test_auto_incomplete_ack_and_unexpected_change(api,monkeypatch,advertised):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    core=importlib.import_module(m._package+'.core')
    (p.home/'config.yaml').write_text('model: {default: fixture}\n')
    gone=p.home/'gone'; gone.write_text('disappears')
    scan=core.scan
    def vanished(*a,**kw):
        result=scan(*a,**kw); gone.unlink(missing_ok=True); return result
    key=t/'key'; recipient=core.keygen(key)
    with monkeypatch.context() as patch:
        patch.setattr(core,'scan',vanished)
        backup=core.backup(p.home,p.root/'copies',p.root/'index',recipient,
            sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    assert backup['complete'] is False
    adapter=m._recovery_router.recovery_adapter
    service=adapter.service(p)
    source=service._sources['local']
    row=source.records()[0]
    row.pop('completion',None)
    row.pop('complete',None); row.pop('omitted_count',None)
    if advertised is not None:
        row.update(complete=advertised,omitted_count=0 if advertised else 1)
    Recovery=importlib.import_module(m._package+'.recovery')
    service.add_source(Recovery.CommittedSource('local',source.generation,lambda:[row],source.transfer))
    item=c.get('/recovery/catalog').json()['items'][0]
    assert item['complete'] is advertised
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    assert prep.status_code==200,prep.text
    jid=prep.json()['id']; url='/recovery/jobs/'+jid
    assert c.post(url+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code==200
    assert poll(c,jid)['state']=='succeeded'
    body=dict(profile_id=p.profile_id,consent_restore=True)
    assert c.post(url+'/import-auto',json=body).json()['detail']==('COMPLETENESS_CHANGED' if advertised is True else 'INCOMPLETE_ACK_REQUIRED')
    r=c.post(url+'/import-auto',json=dict(body,acknowledge_incomplete=True))
    if advertised is True:
        assert r.json()['detail']=='COMPLETENESS_CHANGED'
    else:
        assert r.status_code==200,r.text


def test_catalog_and_auto_consent_use_same_unknown_provenance(api,monkeypatch):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    service=m._recovery_router.recovery_adapter.service(p)
    source=service._sources['local']
    row=source.records()[0]
    row.pop('completion',None)
    row.update(complete=True,omitted_count='invalid')
    Recovery=importlib.import_module(m._package+'.recovery')
    service.add_source(Recovery.CommittedSource('local',source.generation,lambda:[row],source.transfer))
    item=c.get('/recovery/catalog').json()['items'][0]
    assert item['complete'] is None
    r=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    assert r.status_code==200,r.text
    job=m._recovery_router.recovery_adapter.jobs[r.json()['id']]
    assert job['catalog_complete'] is None


@pytest.mark.parametrize('replace_bundle',[False,True])
def test_managed_result_rejects_replaced_parent_before_import(api,monkeypatch,replace_bundle):
    import shutil
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True)).json()
    jid=prep['id']; url='/recovery/jobs/'+jid
    assert c.post(url+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code==200
    assert poll(c,jid)['state']=='succeeded'
    job=m._recovery_router.recovery_adapter.jobs[jid]
    cap=job['destination']
    original=cap.path if replace_bundle else cap.path.parent
    moved=original.with_name(original.name+'-moved')
    original.rename(moved)
    shutil.copytree(moved,original)
    result=c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True))
    assert result.status_code==409,result.text
    assert result.json()['detail']=='INVALID_DESTINATION'


def test_no_path_or_forged_capability_authorizes_protected_tree(api,monkeypatch):
    from dataclasses import replace
    c,m,p,t=api
    item,key=fixture_backup(api)
    service=m._recovery_router.recovery_adapter.service(p)
    Recovery=importlib.import_module(m._package+'.recovery')
    cap=service.allocate_destination(p.profile_id,service.activation)
    assert service._destination(cap)==cap.path
    for destination in [cap.path,replace(cap),replace(cap,path=p.home/'data'),p.home/'cache/finix/recovered/other']:
        with pytest.raises(Recovery.RecoveryError,match='INVALID_DESTINATION'):
            service._destination(destination)
    assert not (p.home/'data').exists()


def test_auto_name_race_and_manual_route_cannot_change_bound_name(api,monkeypatch):
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True)).json()
    jid,name=prep['id'],prep['name']; url='/recovery/jobs/'+jid
    from hermes_cli import profiles as host
    target=Path(host.get_profile_dir(name))
    assert target==t/'host/profiles'/name
    target.mkdir(parents=True)
    (target/'preserve').write_text('existing profile')
    assert c.post(url+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code==200
    assert poll(c,jid)['state']=='succeeded'
    r=c.post(url+'/import',json=dict(profile_id=p.profile_id,name='changed',consent_import=True))
    assert r.json()['detail']=='INVALID_STATE'
    assert c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True,name='changed')).status_code==422
    assert c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True)).json()['detail']=='PROFILE_EXISTS'
    assert (target/'preserve').read_text()=='existing profile'


def test_auto_name_allocator_skips_existing_host_profile(api,monkeypatch):
    from types import SimpleNamespace
    c,m,p,t=api
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    item,key=fixture_backup(api)
    from hermes_cli import profiles as host
    collision='recovered-'+('a'*16)
    target=Path(host.get_profile_dir(collision))
    assert target==t/'host/profiles'/collision
    target.mkdir(parents=True)
    (target/'preserve').write_text('untouched')
    http=importlib.import_module(m._package+'.recovery_http')
    original=http.uuid.uuid4
    calls=[]
    def uuid4():
        calls.append(True)
        return SimpleNamespace(hex='a'*32) if len(calls)==1 else original()
    monkeypatch.setattr(http.uuid,'uuid4',uuid4)
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    assert prep.status_code==200,prep.text
    assert prep.json()['name']!=collision
    assert (target/'preserve').read_text()=='untouched'


def test_auto_restore_trusts_0755_ancestors_not_secret_subtrees(api,monkeypatch):
    import os,stat
    if os.name=='nt':
        pytest.skip('POSIX ownership/mode contract')
    c,m,p,t=api
    item,key=fixture_backup(api)
    for path in (p.home,p.home/'cache',p.home/'cache/finix'):
        path.chmod(0o755)
    for variable,value in [('HERMES_HOME',t/'host'),('LOCALAPPDATA',t/'account/local'),('HOME',t/'account'),('USERPROFILE',t/'account')]:
        monkeypatch.setenv(variable,str(value))
    prep=c.post('/recovery/prepare-auto',json=dict(profile_id=p.profile_id,candidate_id=item['id'],consent_restore=True))
    assert prep.status_code==200,prep.text
    job=m._recovery_router.recovery_adapter.jobs[prep.json()['id']]
    for path in (job['destination'].path.parent,job['service'].recovered_root):
        assert stat.S_IMODE(path.stat().st_mode)==0o700
    jid=prep.json()['id']; url='/recovery/jobs/'+jid
    assert c.post(url+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code==200
    assert poll(c,jid)['state']=='succeeded'
    assert c.post(url+'/import-auto',json=dict(profile_id=p.profile_id,consent_restore=True)).status_code==200
    for path in (p.home,p.home/'cache',p.home/'cache/finix'):
        assert stat.S_IMODE(path.stat().st_mode)==0o755
    cap=job['destination']
    cap.path.chmod(0o755)
    Recovery=importlib.import_module(m._package+'.recovery')
    with pytest.raises(Recovery.RecoveryError,match='RESTORE_FAILED|INVALID_DESTINATION'):
        job['service'].handoff(job['service_id'],p.profile_id,job['service'].activation)
