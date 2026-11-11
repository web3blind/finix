"""Real incomplete archive -> multipart restore -> HTTP import with extra ACK."""
from pathlib import Path
from test_recovery_api import api
from test_recovery_composed import poll


def test_incomplete_archive_http_import_installed_source(api,monkeypatch):
    c,m,p,t=api
    core=__import__(m._package+'.core',fromlist=['core'])
    for k,v in {'HERMES_HOME':t/'disposable-host','HOME':t/'account','USERPROFILE':t/'account','LOCALAPPDATA':t/'account/local'}.items():monkeypatch.setenv(k,str(v))
    from hermes_cli import profiles as host
    target=Path(host.get_profile_dir('incomplete'))
    assert target==t/'disposable-host/profiles/incomplete'
    (p.home/'config.yaml').write_text('model: {default: fixture}\nsecrets: {sources: [onepassword], onepassword: {enabled: true}}\n')
    gone=p.home/'vanished';gone.write_bytes(b'fixture')
    scan=core.scan
    def vanished(*args,**kwargs):
        result=scan(*args,**kwargs);gone.unlink(missing_ok=True);return result
    key=t/'generated-key';recipient=core.keygen(key)
    with monkeypatch.context() as patch:
        patch.setattr(core,'scan',vanished)
        backup=core.backup(p.home,p.root/'copies',p.root/'index',recipient,sqlite_temp_dir=core.private_dir(p.home/'cache/finix/sqlite-temp'))
    assert backup['complete'] is False and backup['omitted_count']==1
    cat=c.get('/recovery/catalog').json()
    prep=c.post('/recovery/prepare',json=dict(profile_id=p.profile_id,candidate_id=cat['items'][0]['id'],destination_path=str(t/'recovered'),consent_local=True))
    assert prep.status_code==200,prep.text
    jid=prep.json()['id'];base='/recovery/jobs/'+jid
    assert c.post(base+'/key',files={'file':('key.txt',key.read_bytes(),'text/plain')}).status_code==200
    result=poll(c,jid)
    assert result['state']=='succeeded' and result['complete'] is False and result['omitted_count']==1
    assert result['can_import'] is True
    body=dict(profile_id=p.profile_id,name='incomplete',consent_import=True)
    assert c.post(base+'/import',json=body).json()['detail']=='INCOMPLETE_ACK_REQUIRED'
    imported=c.post(base+'/import',json={**body,'acknowledge_incomplete':True})
    assert imported.status_code==200,imported.text
    assert imported.json()['complete'] is False and imported.json()['omitted_count']==1
    assert (target/'gateway.parked').is_file() and not (target/'vanished').exists()
