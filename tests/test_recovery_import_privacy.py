"""Importer child privacy: generated disposable data only, never user keys."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from finix import recovery_import as module


@pytest.mark.parametrize('platform',['posix','nt'])
def test_importer_child_private_creation_keyword(tmp_path, platform):
    host = SimpleNamespace(__file__=str(tmp_path / 'sdk/hermes_cli/profiles.py'))
    platform_os = SimpleNamespace(name=platform, environ=os.environ, path=os.path, pathsep=os.pathsep)
    with patch.object(module, 'os', platform_os), patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
        module._host_import(host, tmp_path / 'archive', 'recovered', tmp_path / 'target', tmp_path)
    kwargs = run.call_args.kwargs
    assert 'preexec_fn' not in kwargs
    if platform == 'posix':
        assert kwargs.get('umask') == 0o077
    else:
        assert 'umask' not in kwargs


@pytest.mark.skipif(os.name != 'posix', reason='Native POSIX SDK creation contract')
def test_actual_pinned_sdk_import_private_at_creation(tmp_path):
    """Existing SDK/dependencies only; explicit opt-in, no copy/install/network."""
    sdk = os.environ.get('FINIX_PRIVACY_TEST_SDK')
    if not sdk:
        pytest.skip('Requires explicit pinned native SDK path')
    root = tmp_path / 'sdk-root'
    root.mkdir(mode=0o700)
    stage = tmp_path / 'stage'
    stage.mkdir(mode=0o700)
    env = dict(os.environ, HERMES_HOME=str(root), PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=os.pathsep.join([str(Path(module.__file__).parent.parent), sdk]),
               TMPDIR=str(stage), TMP=str(stage), TEMP=str(stage))
    script = r'''
import os,pathlib,sys,tarfile,io,stat
root=pathlib.Path(sys.argv[1]);stage=pathlib.Path(sys.argv[2])
import pwd
assert os.environ['HOME']==pwd.getpwuid(os.getuid()).pw_dir and os.getuid()!=0
from hermes_constants import get_default_hermes_root
from hermes_cli import profiles as host
assert get_default_hermes_root()==host.get_profile_dir('default')==root
from finix.recovery_import import _host_import,verify_host_compatibility
verify_host_compatibility(host)
(root/'profiles').mkdir(mode=0o700)
def archive(name):
 p=stage/(name+'.tar.gz')
 with tarfile.open(p,'w:gz') as t:
  for leaf,data in [('config.yaml',b'{}\n'),('gateway.parked',b'fixture\n'),('memories/note.txt',b'SYNTHETIC NO PRIVATE KEY\n')]:
   m=tarfile.TarInfo(name+'/'+leaf);m.mode=0o600;m.size=len(data);t.addfile(m,io.BytesIO(data))
 return p
# Child starts under022, matching the ordinary counterexample. No os.umask.
baseline=host.import_profile(archive('baseline'),name='baseline')
assert stat.S_IMODE(baseline.stat().st_mode)==0o755
from finix.recovery import _PosixAnchor,RecoveryError
with __import__('contextlib').ExitStack() as stack:
 try:
  _PosixAnchor(stack,baseline,owned_from=root,private_from=root)
 except RecoveryError as exc:assert exc.code=='RESTORE_FAILED'
 else:raise AssertionError('0755 baseline unexpectedly admitted')
name='recovered';target=host.get_profile_dir(name)
assert target==root/'profiles'/name and not target.exists()
assert _host_import(host,archive(name),name,target,stage)==target
assert stat.S_IMODE(target.stat().st_mode)==0o700
parent_probe=stage/'parent-umask-unchanged';parent_probe.mkdir()
assert stat.S_IMODE(parent_probe.stat().st_mode)==0o755
with __import__('contextlib').ExitStack() as retained:
 anchor=_PosixAnchor(retained,target,owned_from=root,private_from=root)
 before=os.fstat(anchor.fd)
 from finix.recovery_pipeline import _identity
 assert _identity(target)==(before.st_dev,before.st_ino)
 anchor.check()
assert (target/'memories/note.txt').read_bytes()==b'SYNTHETIC NO PRIVATE KEY\n'
assert (target/'gateway.parked').is_file()
# Genuine generated-age handoff -> actual SDK importer -> native _mint.
# No existing key or receipt is read or adopted; no gateway is started.
import sqlite3
from contextlib import closing
from finix import core
from finix.recovery import RecoveryService
from finix.recovery_import import RecoveryImporter
from finix.recovery_pipeline import _mint,_CAPS
source=core.private_dir(root/'profiles/source')
(source/'config.yaml').write_bytes(b'{}\n')
(source/'note.txt').write_bytes(b'SYNTHETIC GENERATED FIXTURE ONLY\n')
with closing(sqlite3.connect(source/'state.db')) as db:
 db.execute('CREATE TABLE fixture(value TEXT)');db.execute("INSERT INTO fixture VALUES ('known-family-test-data')");db.commit()
key=stage/'generated-test-key';recipient=core.keygen(key)
core.backup(source,stage/'copies',stage/'index',recipient,
 sqlite_temp_dir=core.private_dir(source/'cache/finix/sqlite-temp'))
service=RecoveryService(profile_id='source',profile_root=source,activation='fixture-epoch',protected_roots=[])
try:
 service.add_local('fixture',stage/'copies',stage/'index')
 candidate=service.catalog('source','fixture-epoch')[0]
 destination=service.allocate_destination('source','fixture-epoch')
 job=service.start(candidate['id'],identity=key,destination=destination,profile_id='source',activation='fixture-epoch')
 outcome=service.wait(job,30);assert outcome['status']=='succeeded',outcome
 imported=RecoveryImporter(service).import_job(job,profile_id='source',activation='fixture-epoch',name='age-recovered',consent_import=True)
 final=host.get_profile_dir('age-recovered');generation=final.stat()
 cap=_mint(service,job,'source','fixture-epoch','age-recovered',imported)
 record=_CAPS.pop(cap)
 try:assert record['native']==(generation.st_dev,generation.st_ino)
 finally:record['native_stack'].close()
 assert stat.S_IMODE(final.stat().st_mode)==0o700
 with closing(sqlite3.connect(final/'state.db')) as db:
  assert db.execute('SELECT * FROM fixture').fetchall()==[('known-family-test-data',)]
  assert db.execute('PRAGMA integrity_check').fetchone()==('ok',)
 assert (final/'gateway.parked').is_file()
finally:service.close()
print('SDK022=0755/refused; Finix-child077=0700/admitted; generated age + real handoff + SDK + _mint passed')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(root), str(stage)],
                            env=env, capture_output=True, text=True, umask=0o022, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
