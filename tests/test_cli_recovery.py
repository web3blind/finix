"""CLI parser/handler, local generated identities; no provider access."""
import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import pytest
from finix import core
from finix.integration import Profile
from finix.plugin_cli import run, setup_parser


def invoke(profile, *words):
    parser = argparse.ArgumentParser(prog='hermes finix')
    setup_parser(parser)
    args = parser.parse_args(list(words))
    output = io.StringIO()
    with redirect_stdout(output):
        code = run(args, profile_factory=lambda: profile)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.fixture
def local(tmp_path, monkeypatch):
    home = core.private_dir(tmp_path/'owner')
    (home/'config.yaml').write_text('model:\n  default: synthetic-no-provider\n')
    profile = Profile(home, default_root=home)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    return profile, tmp_path


def test_cli_uses_shared_source_operation_before_decryption(local,monkeypatch):
    profile,root=local
    copy,key=backup(local)
    from finix.recovery_operations import RecoveryOperations
    from finix.recovery_import import RecoveryImportError
    calls=[]
    def begin(self,service,**kwargs):
        calls.append(kwargs)
        raise RecoveryImportError('RECOVERY_INTERRUPTED')
    monkeypatch.setattr(RecoveryOperations,'begin',begin)
    code,events=invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code==1 and len(calls)==1
    assert calls[0]['candidate_id']==copy
    assert events[-1]['error']=='RECOVERY_INTERRUPTED'


def test_public_recovery_help():
    parser=argparse.ArgumentParser(prog='hermes finix')
    setup_parser(parser)
    for words in [['recovery','--help'],['recovery','restore','--help']]:
        with pytest.raises(SystemExit) as exc:parser.parse_args(words)
        assert exc.value.code == 0


def test_recovery_list_real_local_catalog(local):
    profile, root = local
    key = root/'fixture-key'
    recipient = core.keygen(key)
    core.backup(profile.home, profile.root/'copies', profile.root/'index', recipient,
        sqlite_temp_dir=core.private_dir(profile.home/'cache/finix/sqlite-temp'))
    code, events = invoke(profile, 'recovery', 'list')
    assert code == 0
    assert events[-1]['profile_id'] == profile.profile_id
    assert len(events[-1]['items']) == 1
    assert events[-1]['items'][0]['complete'] is True
    assert str(root) not in json.dumps(events)


def backup(local):
    profile, root = local
    from hermes_state import SessionDB
    db=SessionDB(profile.home/'state.db')
    try:
        for i in range(3):
            sid='finix-synthetic-'+str(i)
            db.create_session(sid,'cli')
            db.append_message(sid,'assistant','Synthetic fixture response '+str(i))
    finally:db.close()
    core.private_dir(profile.home/'hooks')
    (profile.home/'hooks/never.py').write_text('raise AssertionError("MUST_NOT_RUN")')
    key = root/'fixture-key'
    recipient = core.keygen(key)
    core.backup(profile.home, profile.root/'copies', profile.root/'index', recipient,
        sqlite_temp_dir=core.private_dir(profile.home/'cache/finix/sqlite-temp'))
    return invoke(profile,'recovery','list')[1][-1]['items'][0]['id'], key


def test_restore_real_import_then_requires_exact_serving_ack(local, monkeypatch):
    profile, root = local
    copy, key = backup(local)
    monkeypatch.setenv('HERMES_HOME', str(root/'host'))
    from gateway import host_attach, control_socket
    from types import SimpleNamespace
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:SimpleNamespace(home=root/'host',standalone=False))
    calls=[]
    def serve(home,name):
        assert home == root/'host'
        assert name.startswith('recovered-')
        assert not (home/'profiles'/name/'gateway.parked').exists()
        calls.append(name)
        return dict(served=name,served_profiles=['default',name])
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',serve)
    code, events = invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code == 0, events
    assert events[-1]['activated'] is True
    assert events[-1]['name'] == calls[0] == events[-1]['served']
    target=root/'host/profiles'/calls[0]
    import sqlite3
    with sqlite3.connect(target/'state.db') as db:
        assert db.execute('select count(*) from sessions').fetchone()[0] == 3
        assert db.execute('select count(*) from messages').fetchone()[0] == 3
        assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    assert (target/'.finix-paused/hooks/never.py').is_file()
    assert (profile.home/'hooks/never.py').is_file()
    assert events[-1]['use_command'] == ['hermes','--profile',calls[0]]
    assert events[0]['state'] == 'running'
    assert not list((profile.home/'cache/finix/recovered').glob('*/bundle'))


@pytest.mark.parametrize('case', ['wrong-key','symlink','foreign-home','corrupt','wrong-target','no-locality','unknown-copy'])
def test_closed_failures_never_activate(local, monkeypatch, case):
    profile, root = local
    copy, key = backup(local)
    monkeypatch.setenv('HERMES_HOME', str(root/'host'))
    from hermes_cli import profiles as host
    if case == 'wrong-key':
        key = root/'wrong-key'
        core.keygen(key)
    elif case == 'symlink':
        link = root/'identity-link'
        try:link.symlink_to(key)
        except OSError:pytest.skip('native symlink privilege unavailable')
        key = link
    elif case in {'foreign-home','no-locality'}:
        import hermes_constants
        monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:root/'not-owner')
    elif case == 'corrupt':
        archive=next((profile.root/'copies').glob('*.age'))
        archive.write_bytes(b'corrupt')
    elif case == 'wrong-target':
        monkeypatch.setattr(host,'get_profile_dir',lambda name:profile.home)
    elif case == 'unknown-copy':copy='invalid'
    code, events = invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code == 1, events
    assert not any(e.get('activated') or e.get('use_command') for e in events)
    assert str(key) not in json.dumps(events)
    assert 'AGE-SECRET-KEY' not in json.dumps(events)
    assert not list((root/'host/profiles').glob('*'))
    assert (profile.home/'state.db').exists()


@pytest.mark.parametrize('interrupt',[False,True])
def test_pending_sdk_ack_is_not_success_and_import_stays_parked(local, monkeypatch, interrupt):
    from types import SimpleNamespace
    profile, root = local
    copy, key = backup(local)
    monkeypatch.setenv('HERMES_HOME', str(root/'host'))
    from gateway import host_attach, control_socket
    monkeypatch.setattr(host_attach,'host_gateway',lambda **kw:SimpleNamespace(home=root/'host',standalone=False))
    def serve(home,name):
        if interrupt:raise KeyboardInterrupt
        return dict(pending=True,served=name,served_profiles=[name])
    monkeypatch.setattr(control_socket,'request_serve_profile_hot',serve)
    code, events=invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code == (130 if interrupt else 1)
    assert events[-1]['error'] == ('CANCELLED' if interrupt else 'SERVING_NOT_CONFIRMED')
    assert not any(e.get('use_command') for e in events)
    target=next((root/'host/profiles').iterdir())
    assert (target/'gateway.parked').is_file()
    assert (target/'.finix-paused/hooks/never.py').is_file()


def test_keyboard_abort_cancels_worker_and_never_imports(local, monkeypatch):
    profile, root = local
    copy, key = backup(local)
    from finix.recovery import RecoveryService
    original=RecoveryService.wait
    calls=[]
    def interrupt(self, job, timeout=None):
        if not calls:
            calls.append(True)
            raise KeyboardInterrupt
        return original(self, job, timeout)
    monkeypatch.setattr(RecoveryService,'wait',interrupt)
    code, events=invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code == 130
    assert events[-1]['error'] == 'CANCELLED'
    assert not any(e.get('activated') for e in events)


def test_preknown_corrupt_metadata_fails_before_worker(local, monkeypatch):
    profile, root=local
    copy, key=backup(local)
    index=core.load_state(profile.root/'index')
    index['backups'][0]['sha256']='not-a-digest'
    core.atomic_json(profile.root/'index/index.json',index)
    code, events=invoke(profile,'recovery','restore','--copy-id',copy,'--identity',str(key))
    assert code == 1
    assert events == [dict(error='INVALID_SOURCE',activated=False)]


def test_fake_cloud_read_uses_same_service_and_never_uploads(local, monkeypatch):
    profile, root=local
    copy, key=backup(local)
    from finix.recovery import CommittedSource
    from finix import recovery_http
    archive=next((profile.root/'copies').glob('*.age'))
    row=core.load_state(profile.root/'index')['backups'][0]
    row=dict(row,size=archive.stat().st_size)
    reads=[]
    def register(service,owner):
        assert owner is profile
        def read(record,output,cancel):
            reads.append(record['sha256'])
            output.write(archive.read_bytes())
        service.add_source(CommittedSource('offline-cloud','fixture-v1',lambda:[row],read))
    monkeypatch.setattr(recovery_http,'register_cloud',register)
    code, events=invoke(profile,'recovery','list')
    remote=next(r['id'] for r in events[-1]['items'] if r['id'] != copy)
    wrong=root/'wrong'
    core.keygen(wrong)
    code, events=invoke(profile,'recovery','restore','--copy-id',remote,'--identity',str(wrong))
    assert code == 1
    assert reads == [row['sha256']]
    assert not any(e.get('activated') for e in events)

