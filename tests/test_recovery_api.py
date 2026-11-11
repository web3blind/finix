"""Synthetic-only HTTP acceptance; no provider or real profile access."""
import importlib.util
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

@pytest.fixture
def api(tmp_path):
    spec = importlib.util.spec_from_file_location('recovery_http_test', Path(__file__).parents[1]/'dashboard/plugin_api.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    from importlib import import_module
    integration = import_module(m._package+'.integration')
    tmp_path = integration.core.private_dir(tmp_path/'private')
    home = integration.core.private_dir(tmp_path/'home')
    integration.core.private_dir(home/'cache')
    integration.core.private_dir(home/'cache/finix')
    p = integration.Profile(home, default_root=home)
    m.current_profile = lambda: p
    app = FastAPI(); app.include_router(m.router)
    c = TestClient(app, client=('127.0.0.1', 50000))
    yield c, m, p, tmp_path
    for service in m._recovery_router.recovery_adapter.services.values():
        service.close()
    c.close()

def test_public_sdk_key_phase_query_is_preserved_and_header_conflict_refused(api,monkeypatch):
    import uuid
    c,m,p,t=api
    request_id=str(uuid.uuid4()); seen=[]
    def key(service,identifier,*,request_id,identity):
        seen.append(request_id)
        return {'id':identifier,'request_id':request_id}
    monkeypatch.setattr(m._recovery_router.recovery_operations,'key',key)
    path='/recovery/operations/'+('a'*32)+'/key?request_id='+request_id
    files={'file':('recovery.txt',b'synthetic-key','text/plain')}
    reply=c.post(path,files=files)
    assert reply.status_code==200,reply.text
    assert seen==[request_id]
    reply=c.post(path,files=files,headers={'X-Finix-Request-Id':str(uuid.uuid4())})
    assert reply.status_code==422
    assert seen==[request_id]


def test_capabilities_are_safe_local_and_profile_bound(api):
    c,m,p,t = api
    r = c.get('/recovery/capabilities')
    assert r.status_code == 200
    assert r.json()['code_version'] == 'recovery-v1'
    assert r.json()['operation_protocol'] == 'recovery-operations-v1'
    assert r.json()['profile_id'] == p.profile_id
    assert r.json()['local_only'] is True
    assert str(t) not in r.text
    assert c.get('/recovery/catalog').json() == {'profile_id':p.profile_id,'items':[]}
    remote = TestClient(c.app, client=('192.0.2.1',50000))
    assert remote.get('/recovery/capabilities').status_code == 403


def test_catalog_never_projects_unvalidated_omission_fields(api):
    import hashlib
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    output=core.private_dir(p.root/'copies'); state=core.private_dir(p.root/'index')
    name='finix-'+('a'*32)+'-'+('b'*32)+'.age'
    (output/name).write_bytes(b'cipher')
    core.atomic_json(state/'index.json',dict(namespace='a'*32,output=str(output),
        source=hashlib.sha256(str(p.home).encode()).hexdigest(),backups=[dict(name=name,
        sha256=hashlib.sha256(b'cipher').hexdigest(),complete=True,omitted_count='PRIVATE-KEY PRIVATE-PATH')]))
    r=c.get('/recovery/catalog')
    assert r.status_code == 200,r.text
    assert 'PRIVATE' not in r.text
    assert r.json()['items'][0]['omitted_count'] is None
    assert r.json()['items'][0]['complete'] is None


def test_profile_resolution_errors_are_closed(api):
    c,m,p,t = api
    def broken():
        raise OSError('PRIVATE-KEY PRIVATE-PATH')
    m.current_profile = broken
    r = c.get('/recovery/capabilities')
    assert r.status_code == 409
    assert r.json() == {'detail':'PROFILE_UNAVAILABLE'}


def prepare(c,p,t, **more):
    return c.post('/recovery/prepare', json=dict(profile_id=p.profile_id,
        archive_path=str(t/'cipher.age'), destination_path=str(t/'restored'),
        consent_local=True, **more))


def test_prepare_is_inert_and_rejects_private_json_wrong_profile_and_paths(api):
    c,m,p,t = api
    (t/'cipher.age').write_bytes(b'age-encryption.org/v1\nsynthetic')
    r = prepare(c,p,t)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j['state'] == 'awaiting_key'
    assert not (t/'restored').exists()
    assert str(t) not in r.text
    assert c.get('/recovery/jobs/'+j['id']).json()['state'] == 'awaiting_key'
    assert prepare(c,p,t).status_code == 409
    assert c.post('/recovery/jobs/'+j['id']+'/cancel').json()['state'] == 'cancelled'
    for payload in [dict(profile_id='wrong', archive_path=str(t/'cipher.age'), destination_path=str(t/'x'), consent_local=True),
                    dict(profile_id=p.profile_id, archive_path=str(t/'cipher.age'), destination_path=str(p.home/'x'), consent_local=True),
                    dict(profile_id=p.profile_id, archive_path=str(t/'../cipher.age'), destination_path=str(t/'x'), consent_local=True),
                    dict(profile_id=p.profile_id, archive_path=str(t/'cipher.age'), destination_path=str(t/'x'), consent_local=False),
                    dict(profile_id=p.profile_id, identity_path='PRIVATE-PATH', private_key='PRIVATE-KEY')]:
        r = c.post('/recovery/prepare',json=payload)
        assert r.status_code in (409,422)
        assert 'PRIVATE' not in r.text
        assert str(t) not in r.text
    assert c.get('/recovery/jobs/not-a-job').status_code == 404


def test_real_age_upload_restore_and_private_cleanup(api):
    import time, sqlite3
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    (p.home/'config.yaml').write_text('synthetic: true')
    db = sqlite3.connect(p.home/'state.db')
    db.execute('pragma journal_mode=wal'); db.execute('create table x(v)')
    db.execute("insert into x values ('committed')"); db.commit()
    key = t/'fixture-key'; recipient = core.keygen(key)
    core.backup(p.home,p.root/'copies',p.root/'index',recipient,
                        sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    db.close()
    catalog = c.get('/recovery/catalog')
    assert catalog.status_code == 200, catalog.text
    item = catalog.json()['items'][0]
    assert set(item) == {'id','label','source','size','sha256','complete','omitted_count','created_at'}
    assert item['created_at'] == core.load_state(p.root/'index')['backups'][0]['created_at']
    r = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,candidate_id=item['id'],
             destination_path=str(t/'restored'),consent_local=True))
    assert r.status_code == 200,r.text
    jid = r.json()['id']
    r = c.post('/recovery/jobs/'+jid+'/key',files={'file':('recovery.txt',key.read_bytes(),'text/plain')})
    assert r.status_code == 200,r.text
    for _ in range(300):
        r = c.get('/recovery/jobs/'+jid)
        if r.json()['state'] != 'running': break
        time.sleep(.03)
    assert r.json()['state'] == 'succeeded',r.text
    with closing(sqlite3.connect(t/'restored/state.db')) as restored:
        # Stock SDK supports browsing an absent-model config; readiness is not
        # an AI-turn claim. Keep the explicit contract assertion after that fix.
        assert r.json()['can_import'] is True
        assert restored.execute('select * from x').fetchall() == [('committed',)]
        assert restored.execute('pragma integrity_check').fetchone() == ('ok',)
    assert list((p.home/'cache/finix/restore').iterdir()) == []
    assert str(t) not in r.text
    assert 'AGE-SECRET' not in r.text


def test_multipart_caps_wrong_field_profile_and_json_never_echo(api):
    c,m,p,t = api
    (t/'cipher.age').write_bytes(b'cipher')
    jid = prepare(c,p,t).json()['id']; url = '/recovery/jobs/'+jid+'/key'
    for files in [ {'other':('PRIVATE-KEY',b'PRIVATE-KEY','text/plain')},
                   {'file':('key',b'x'*16385,'text/plain')},
                   [('file',('a',b'one')),('file',('b',b'two'))] ]:
        r = c.post(url,files=files)
        assert r.status_code in (413,422),r.text
        assert 'PRIVATE-KEY' not in r.text
    assert c.post(url,json={'private_key':'PRIVATE-KEY'}).status_code == 422
    assert c.post(url,content=b'x',headers={'Content-Length':'999999999'}).status_code == 413
    old = m.current_profile
    m.current_profile = lambda: SimpleNamespace(profile_id='f'*64)
    assert c.post(url,files={'file':('key',b'PRIVATE-KEY')}).status_code == 404
    m.current_profile = old


def test_cross_volume_rejected_before_key(api,monkeypatch):
    c,m,p,t = api
    (t/'cipher.age').write_bytes(b'cipher')
    old = Path.stat
    def changed(path,*a,**kw):
        info = old(path,*a,**kw)
        if path == t:
            from types import SimpleNamespace
            return SimpleNamespace(st_dev=-123,st_mode=info.st_mode)
        return info
    monkeypatch.setattr(Path,'stat',changed)
    r = prepare(c,p,t)
    assert r.status_code == 409
    assert r.json()['detail'] == 'DESTINATION_VOLUME_UNSUPPORTED'


def test_committed_rclone_full_readback_and_cancel_no_network(api,monkeypatch):
    import json, time, threading
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    storage = __import__(m._package+'.rclone_storage',fromlist=['RcloneStorage'])
    (p.home/'config.yaml').write_text('model: {default: synthetic-model}\n')
    seed_sqlite(p)
    key = t/'fixture-key'; recipient = core.keygen(key)
    result = core.backup(p.home,p.root/'copies',p.root/'index',recipient,
                        sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    data = Path(result['path']).read_bytes()
    config = t/'rclone.conf'; config.write_text('[fixture]\ntype = drive\n')
    config.chmod(0o600)
    entered, release = threading.Event(), threading.Event()
    mode = ['good']; calls = []
    def runner(op,*args,**kw):
        calls.append((op,args))
        if op == 'listremotes':
            return json.dumps([{'name':'fixture','source':'file','type':'dropbox'}]).encode()
        if op == 'lsjson':
            return json.dumps(dict(IsDir=False,Size=len(data),Name='b'*32+'.age')).encode()
        if op == 'cat':
            if mode[0] == 'wait':
                entered.set(); release.wait(3)
            kw['output'].write(data if mode[0] != 'corrupt' else b'x'*len(data))
            return b''
        raise AssertionError(op)
    adapter = storage.RcloneStorage(p.home,p.profile_id,p.root/'rclone',str(config),'fixture','Finix',runner=runner)
    monkeypatch.setattr(p,'rclone',lambda cfg=None: adapter)
    cfg = p.config(); cfg.update(storage='rclone',rclone_config=str(config),rclone_remote='fixture',rclone_root='Finix')
    core.atomic_json(p.root/'settings.json',cfg)
    core.private_dir(adapter.state)
    row = dict(id='b'*32+'.age',namespace='a'*32,profile_id=p.profile_id,remote='fixture',root='Finix',
               type='dropbox',size=len(data),sha256=result['sha256'])
    core.atomic_json(adapter.state/'rclone.json',dict(profile_id=p.profile_id,binding=adapter.binding(),namespace='a'*32,records=[row]))
    cat = c.get('/recovery/catalog')
    assert cat.status_code == 200,cat.text
    remote = [x for x in cat.json()['items'] if x['source'] == 'rclone']
    assert len(remote) == 1
    assert not any(op in {'lsjson','cat'} for op,_ in calls)
    for mode_value in ('good','corrupt','wait'):
        mode[0] = mode_value
        dest = t/('restore-'+mode_value)
        r = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,candidate_id=remote[0]['id'],
                 destination_path=str(dest),consent_local=True))
        assert r.status_code == 200,r.text
        jid = r.json()['id']
        assert c.post('/recovery/jobs/'+jid+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code == 200
        if mode_value == 'wait':
            assert entered.wait(2)
            reading = c.get('/recovery/jobs/'+jid).json()
            assert reading['phase'] == 'cloud_reading'
            assert reading['state'] == 'running'
            assert c.post('/recovery/jobs/'+jid+'/cancel').status_code == 200
            release.set()
        for _ in range(300):
            r = c.get('/recovery/jobs/'+jid)
            if r.json()['state'] != 'running': break
            time.sleep(.01)
        expected = {'good':'succeeded','corrupt':'failed','wait':'cancelled'}[mode_value]
        assert r.json()['state'] == expected,r.text
        assert dest.exists() == (mode_value == 'good')
        if mode_value == 'good':
            assert_http_import(c,p,t,jid,monkeypatch)
        else:
            assert list((p.home/'cache/finix/restore').iterdir()) == []
    cfg['rclone_root'] = 'changed'; core.atomic_json(p.root/'settings.json',cfg)
    assert c.get('/recovery/jobs/'+jid).json()['detail'] == 'STALE_PROFILE'
    stale = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,candidate_id=remote[0]['id'],
                   destination_path=str(t/'stale-target'),consent_local=True))
    assert stale.json()['detail'] == 'INVALID_CANDIDATE'


def test_busy_and_key_upload_fences_before_receiving_bytes(api,monkeypatch):
    import threading, time
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    (t/'cipher.age').write_bytes(b'cipher')
    # Backup/setup already owns the same cross-process operation lock.
    with core.locked(p.root):
        r = prepare(c,p,t)
        assert r.status_code == 409
        assert r.json()['detail'] == 'BUSY'
        r = c.get('/recovery/catalog')
        assert r.status_code == 409
        assert r.json()['detail'] == 'BUSY'
    jid = prepare(c,p,t).json()['id']
    adapter = m._recovery_router.recovery_adapter
    entered,release = threading.Event(),threading.Event()
    def holder():
        with adapter.lock:
            entered.set(); release.wait(2)
    thread = threading.Thread(target=holder); thread.start(); assert entered.wait(1)
    try:
        start = time.monotonic()
        r = c.get('/recovery/jobs/'+jid)
        assert time.monotonic()-start < 1
        assert r.status_code == 409
        assert r.json()['detail'] == 'BUSY'
    finally:
        release.set(); thread.join()


def test_authoritative_profile_lookup_and_cancel_survive_restore_lock(api):
    c,m,p,t = api
    integration = __import__(m._package+'.integration',fromlist=['Profile'])
    m.current_profile = lambda: integration.Profile(p.home,default_root=p.home)
    with integration.core.locked(p.root):
        # The host's real current_profile constructs Profile on each request.
        # Existing immutable identity reads must not contend on the backup lock.
        r = c.get('/recovery/capabilities')
        assert r.status_code == 200
        assert r.json()['profile_id'] == p.profile_id


def test_reparse_and_hardlink_sources_rejected_with_sentinels(api):
    import os, subprocess
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    source = core.private_dir(t/'source')
    (source/'secret.age').write_bytes(b'PRIVATE-PATH-AND-KEY')
    linked = t/'linked.age'
    os.link(source/'secret.age',linked)
    for path in [linked]:
        r = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,archive_path=str(path),
                   destination_path=str(t/'restored'),consent_local=True))
        assert r.status_code == 409
        assert 'PRIVATE' not in r.text and str(t) not in r.text
    linked.unlink()
    junction = t/'junction'
    if os.name == 'nt':
        subprocess.run(['cmd.exe','/c','mklink','/J',str(junction),str(source)],check=True,capture_output=True)
    else:
        junction.symlink_to(source,target_is_directory=True)
    try:
        r = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,archive_path=str(junction/'secret.age'),
                   destination_path=str(t/'restored'),consent_local=True))
        assert r.status_code == 409
        assert 'PRIVATE' not in r.text and str(t) not in r.text
    finally:
        if os.name == 'nt': junction.rmdir()
        else: junction.unlink()
    assert (source/'secret.age').read_bytes() == b'PRIVATE-PATH-AND-KEY'


def test_missing_cloud_journals_never_call_discovery_or_create(api,monkeypatch):
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    sources = __import__(m._package+'.recovery_sources',fromlist=['register_cloud'])
    from unittest.mock import Mock
    for kind in ('workspace','connector-drive','drive'):
        cfg = p.config(); cfg.update(storage=kind,connector_account='fixture',connector_alias='fixture',connector_folder='fixture')
        core.atomic_json(p.root/'settings.json',cfg)
        provider = Mock(state=p.root/'workspace')
        monkeypatch.setattr(p,'workspace',lambda:provider)
        monkeypatch.setattr(p,'connector',lambda cfg:provider)
        service = Mock(stage_root=p.home/'cache/finix/restore')
        sources.register_cloud(service,p)
        provider.ledger.assert_not_called()
        provider.discover.assert_not_called()
        provider.download.assert_not_called()
        service.add_source.assert_not_called()


def test_stream_caps_and_header_caps_never_start_job(api):
    c,m,p,t = api
    (t/'cipher.age').write_bytes(b'cipher')
    jid = prepare(c,p,t).json()['id']; url = '/recovery/jobs/'+jid+'/key'
    for content,headers in [
        (b'PRIVATE-KEY',{'Content-Length':'1','Content-Type':'text/plain'}),
        (b'--x\r\n'+b'Z'*1100+b'\r\n\r\nPRIVATE-KEY\r\n--x--\r\n',{'Content-Type':'multipart/form-data; boundary=x'}),
        (b'PRIVATE-KEY',{'Content-Type':'text/plain','Transfer-Encoding':'chunked'})]:
        r = c.post(url,content=content,headers=headers)
        assert r.status_code == 413,r.text
        assert 'PRIVATE-KEY' not in r.text
    assert c.get('/recovery/jobs/'+jid).json()['state'] == 'awaiting_key'


@pytest.mark.parametrize('kind',['workspace','connector-drive','drive'])
def test_other_committed_cloud_adapters_restore_exact_bytes(api,monkeypatch,kind):
    import time
    c,m,p,t = api
    core = __import__(m._package+'.core',fromlist=['core'])
    drive_module = __import__(m._package+'.drive',fromlist=['Drive'])
    (p.home/'config.yaml').write_text('model: {default: synthetic-model}\n')
    seed_sqlite(p)
    key = t/'fixture-key'; recipient = core.keygen(key)
    result = core.backup(p.home,p.root/'copies',p.root/'index',recipient,
                        sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    data = Path(result['path']).read_bytes(); calls = []
    row = dict(size=len(data),sha256=result['sha256'],id='fixture-object',profile_id=p.profile_id,
               folder_id='fixture-folder',snapshot='a'*32,owners=[{'emailAddress':'fixture@example.invalid'}])
    class Provider:
        account='fixture-account'; alias='fixture-alias'; folder='fixture-folder'
        state=p.root/'workspace'
        def descriptor(self,r): return {k:r[k] for k in ('size','sha256')}
        def identifier(self,v):
            assert v in {'fixture-object','fixture-folder'}
            return v
        def folder_metadata(self,*a): calls.append('folder')
        def file_metadata(self,*a): calls.append('file')
        def runner(self,op,*a,**kw):
            assert op == 'download'
            calls.append('download'); Path(a[2]).write_bytes(data)
            return {'id':'fixture-object','status':'downloaded'}
        def checked_descriptor(self,r): return {k:r[k] for k in ('size','sha256')}
        def download(self,obj,descriptor):
            assert obj == row['id']; assert descriptor == self.checked_descriptor(row)
            calls.append('download'); return data
        def checked(self,obj,kind,*a):
            calls.append('checked'); return row
        def response(self,*a,**kw):
            assert kw['headers']['Range'] == f'bytes=0-{len(data)-1}'
            calls.append('download')
            return SimpleNamespace(status=206,body=data,headers={'Content-Range':f'bytes 0-{len(data)-1}/{len(data)}'})
    provider = Provider()
    cfg = p.config(); cfg.update(storage=kind,folder_id='fixture-folder')
    core.atomic_json(p.root/'settings.json',cfg)
    if kind == 'workspace':
        monkeypatch.setattr(p,'workspace',lambda:provider)
        path = provider.state/'workspace.json'
        saved = dict(profile_id=p.profile_id,folder_id=provider.folder,owners=row['owners'],records=[row])
    elif kind == 'connector-drive':
        monkeypatch.setattr(p,'connector',lambda cfg:provider)
        path = p.root/'connector-index/connector.json'
        saved = dict(owner=p.profile_id,account=provider.account,alias=provider.alias,folder=provider.folder,records=[row])
    else:
        from contextlib import contextmanager
        connector_module = __import__(m._package+'.connector_drive',fromlist=['profile_scope'])
        @contextmanager
        def scope(home):
            assert home == p.home
            calls.append('scope')
            yield
        monkeypatch.setattr(connector_module,'profile_scope',scope)
        monkeypatch.setattr(drive_module,'Drive',lambda profile_id:provider)
        path = p.root/'drive-index/drive.json'
        saved = dict(profile_id=p.profile_id,folder_id=provider.folder,records=[row])
    core.private_dir(path.parent); core.atomic_json(path,saved)
    cat = c.get('/recovery/catalog')
    assert cat.status_code == 200,cat.text
    candidate = next(r['id'] for r in cat.json()['items'] if r['source'] == kind)
    assert calls == []
    prep = c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,candidate_id=candidate,
                  destination_path=str(t/'restored'),consent_local=True))
    assert prep.status_code == 200,prep.text
    jid = prep.json()['id']
    assert c.post('/recovery/jobs/'+jid+'/key',files={'file':('key',key.read_bytes(),'text/plain')}).status_code == 200
    for _ in range(300):
        r = c.get('/recovery/jobs/'+jid)
        if r.json()['state'] != 'running': break
        time.sleep(.01)
    assert r.json()['state'] == 'succeeded',r.text
    assert (t/'restored/config.yaml').read_text() == 'model: {default: synthetic-model}\n'
    assert 'download' in calls
    assert_http_import(c,p,t,jid,monkeypatch)


def seed_sqlite(p):
    import sqlite3
    from contextlib import closing
    # sqlite Connection.__exit__ commits but does NOT close: a late GC close
    # during backup can remove companions nondeterministically in this fixture.
    with closing(sqlite3.connect(p.home/'state.db')) as db:
        db.execute('pragma journal_mode=wal')
        db.execute('create table synthetic (v text)')
        db.execute("insert into synthetic values ('committed provider row')")
        db.commit()


def assert_http_import(c,p,t,jid,monkeypatch):
    import sqlite3
    for key,value in {'HERMES_HOME':t/'disposable-host','HOME':t/'account',
                      'USERPROFILE':t/'account','LOCALAPPDATA':t/'account/local'}.items():
        monkeypatch.setenv(key,str(value))
    from hermes_cli import profiles as host
    target = Path(host.get_profile_dir('provider-restored'))
    assert target == t/'disposable-host/profiles/provider-restored'
    response = c.post('/recovery/jobs/'+jid+'/import',json=dict(profile_id=p.profile_id,
                      name='provider-restored',consent_import=True))
    assert response.status_code == 200,response.text
    assert response.json()['can_activate'] is True
    assert (target/'gateway.parked').is_file()
    with closing(sqlite3.connect(target/'state.db')) as db:
        assert db.execute('select * from synthetic').fetchall() == [('committed provider row',)]
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)
