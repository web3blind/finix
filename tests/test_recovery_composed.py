"""Real ASGI JSON/multipart + age/SQLite + installed host; disposable homes only."""
import json
from contextlib import closing
import os
import sqlite3
import time
from pathlib import Path
import pytest
from test_recovery_api import api


def poll(c, jid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        r = c.get('/recovery/jobs/' + jid)
        assert r.status_code == 200, r.text
        if r.json()['state'] != 'running':
            return r.json()
        time.sleep(.01)
    pytest.fail('bounded restore deadline exceeded')


@pytest.mark.parametrize('bundle',[False,True])
def test_composed_json_multipart_restore_import_installed_host(api, monkeypatch,bundle):
    c, m, p, t = api
    core = __import__(m._package + '.core', fromlist=['core'])
    monkeypatch.setenv('HERMES_HOME', str(t / 'disposable-host'))
    monkeypatch.setenv('LOCALAPPDATA', str(t / 'account/local'))
    monkeypatch.setenv('HOME', str(t / 'account'))
    monkeypatch.setenv('USERPROFILE', str(t / 'account'))
    from hermes_cli import profiles as host
    target = Path(host.get_profile_dir('restored'))
    assert target == t / 'disposable-host/profiles/restored'
    (p.home / 'config.yaml').write_text('model: {default: synthetic-model}\nplugins: {enabled: [never-run]}\n')
    (p.home / 'plugins/never-run').mkdir(parents=True)
    (p.home / 'plugins/never-run/__init__.py').write_text('raise RuntimeError("NEVER RUN")')
    if bundle:
        extra = core.private_dir(t/'synthetic-extra')
        (extra/'outside-profile.txt').write_text('synthetic external payload')
        # Recovery fixture configuration, not acceptance of the unrelated
        # mutable extra-source configuration editor (Windows CAS is intermittent).
        import yaml
        cfg=yaml.safe_load((p.home/'config.yaml').read_text())
        cfg['plugins']['entries']={'finix':{'settings':{'extra_sources':[{'name':'fixture-extra','path':str(extra)}]}}}
        (p.home/'config.yaml').write_text(yaml.safe_dump(cfg))
    with closing(sqlite3.connect(p.home / 'state.db')) as db:
        db.execute('pragma journal_mode=wal')
        db.execute('create table evidence (v text)')
        db.execute("insert into evidence values ('synthetic committed row')")
        db.commit()
        key = t / 'synthetic-identity'
        recipient = core.keygen(key)
        core.backup(p.home, p.root / 'copies', p.root / 'index', recipient,
                    configured_extras=bundle,default_root=p.home,
                    sqlite_temp_dir=core.private_dir(p.home / 'cache/finix/sqlite-temp'))
    cap = c.get('/recovery/capabilities').json()
    assert cap['same_volume_only'] is True
    cat = c.get('/recovery/catalog').json()
    prep = c.post('/recovery/prepare', json=dict(profile_id=p.profile_id,
        candidate_id=cat['items'][0]['id'], destination_path=str(t / 'recovered'), consent_local=True))
    assert prep.status_code == 200, prep.text
    jid = prep.json()['id']
    uploaded = c.post('/recovery/jobs/' + jid + '/key', files={'file': ('key.txt', key.read_bytes(), 'text/plain')})
    assert uploaded.status_code == 200, uploaded.text
    result = poll(c, jid)
    assert result['state'] == 'succeeded', result
    assert result['progress'] == 100
    assert result['complete'] is True
    assert result['can_import'] is True
    assert result['extras_policy'] == 'separate_manual_import_only'
    url = '/recovery/jobs/' + jid + '/import'
    for extra_payload in ({'activation': 'client-fence'}, {'path': '/client-path'}, {'acknowledge_incomplete': 'true'}):
        r = c.post(url, json=dict(profile_id=p.profile_id, name='restored', consent_import=True, **extra_payload))
        assert r.status_code == 422, r.text
    r = c.post(url, json=dict(profile_id='f'*64, name='restored', consent_import=True))
    assert r.status_code == 409
    imported = c.post(url, json=dict(profile_id=p.profile_id, name='restored', consent_import=True))
    assert imported.status_code == 200, imported.text
    assert imported.json() == dict(name='restored', can_activate=True, complete=True, omitted_count=0, parked=True)
    assert (target / 'gateway.parked').is_file()
    assert not (target / 'plugins').exists()
    assert (target / '.finix-paused/plugins/never-run/__init__.py').is_file()
    if bundle:
        assert (t/'recovered/external/fixture-extra/outside-profile.txt').read_text() == 'synthetic external payload'
        assert not (target/'external').exists()
        assert (extra/'outside-profile.txt').read_text() == 'synthetic external payload'
    with closing(sqlite3.connect(target / 'state.db')) as db:
        assert db.execute('select * from evidence').fetchall() == [('synthetic committed row',)]
        assert db.execute('pragma integrity_check').fetchone() == ('ok',)
    assert c.post(url, json=dict(profile_id=p.profile_id, name='restored', consent_import=True)).json()['detail'] == 'PROFILE_EXISTS'
    assert list((p.home / 'cache/finix/restore').iterdir()) == []
    assert not list((p.home / 'cache/finix/import').rglob('*.tar.gz'))
    fixture = dict(synthetic_fixture=True, provenance='Captured from real ASGI/age/SQLite/installed-host test; NOT live user data',
                   capabilities=cap, catalog=cat, prepare=prep.json(), status=result, imported=imported.json())
    fixture_path = os.environ.get('FINIX_RESPONSE_FIXTURE')
    if fixture_path and not bundle:
        Path(fixture_path).write_text(json.dumps(fixture, indent=2))


def test_awaiting_key_status_contract(api):
    c, m, p, t = api
    (t / 'cipher.age').write_bytes(b'cipher')
    prep = c.post('/recovery/prepare', json=dict(profile_id=p.profile_id, archive_path=str(t/'cipher.age'), destination_path=str(t/'result'), consent_local=True))
    assert prep.status_code == 200
    state = c.get('/recovery/jobs/' + prep.json()['id']).json()
    assert state['state'] == state['status'] == 'awaiting_key'
    assert state['progress'] == 0
    assert state['can_import'] is False
