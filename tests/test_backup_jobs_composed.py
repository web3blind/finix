"""Actual HTTP + generated age + real RcloneRunner child pipes; offline only."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import ExitStack

CHILD = r'''
import json,os,sys,time,shutil
from pathlib import Path
store=Path(sys.argv[1]);op=sys.argv[2];args=sys.argv[3:]
def wait(name):
    deadline=time.monotonic()+15
    while not (store/name).exists():
        if time.monotonic()>deadline:raise SystemExit(10)
        time.sleep(.01)
if op=='listremotes':print(json.dumps([{'name':'cloud','type':'yandex','source':'file'}]))
elif op=='copyto':
    source=Path(args[0]);data=source.read_bytes();target=store/args[1].split('/')[-1]
    n=len(data);time.sleep(.3)
    os.write(2,(json.dumps({'stats':{'bytes':n//2,'totalBytes':n}})+'\n').encode())
    (store/'upload-start').touch();wait('upload-release')
    shutil.copyfile(source,target)
    os.write(2,(json.dumps({'stats':{'bytes':n,'totalBytes':n}})+'\n').encode())
elif op=='lsjson':
    p=store/args[0].split('/')[-1]
    if not p.exists():raise SystemExit(3)
    print(json.dumps({'Name':p.name,'Size':p.stat().st_size,'IsDir':False}))
elif op=='cat':
    data=(store/args[0].split('/')[-1]).read_bytes();n=len(data)
    time.sleep(.3);os.write(1,data[:n//2]);(store/'verify-start').touch();wait('verify-release')
    os.write(1,data[n//2:])
else:raise AssertionError('forbidden operation '+op)
'''

class ComposedProcessHTTP(unittest.TestCase):
    def test_http_202_lost_ack_root_locked_twenty_minutes_real_stats_readback_commit_resume(self):
        self.run_case()

    def test_legacy_no_jobs_http_real_runner_stats_same_cipher(self):
        self.run_case(legacy=True)

    def run_case(self, legacy=False):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        spec=importlib.util.spec_from_file_location('composed_process_api',Path(__file__).parents[1]/'dashboard/plugin_api.py')
        api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
        package=api._package
        core=__import__(package+'.core',fromlist=['core'])
        Profile=__import__(package+'.integration',fromlist=['Profile']).Profile
        transport=__import__(package+'.rclone_storage',fromlist=['RcloneStorage'])
        jobs_module=__import__(package+'.backup_jobs',fromlist=['_busy'])
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp, ExitStack() as stack:
            original_connect = socket.socket.connect
            def local_only(sock, address):
                if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
                    return original_connect(sock, address)  # Windows asyncio socketpair
                raise AssertionError('EXTERNAL NETWORK FORBIDDEN')
            stack.enter_context(patch.object(socket.socket,'connect',local_only))
            stack.enter_context(patch.object(socket,'create_connection',side_effect=AssertionError('NETWORK FORBIDDEN')))
            root=core.private_dir(Path(tmp)/'fixture');home=core.private_dir(root/'home');auth=core.private_dir(root/'auth');store=core.private_dir(root/'objects')
            (home/'work').write_bytes(b'offline-generated-source'*20000)
            config=auth/'rclone.conf'
            with core.regular_output(config) as fd:os.write(fd,b'[cloud]\ntype=yandex\n')
            binary=root/'offline-only.exe'
            with core.regular_output(binary) as fd:os.write(fd,b'NEVER EXECUTE')
            binary.chmod(0o700)
            script=root/'child.py';script.write_text(CHILD)
            profile=Profile(home,default_root=home);cfg=profile.config()
            cfg.update(storage='rclone',recipient=core.keygen(root/'generated.key'),rclone_config=str(config),rclone_remote='cloud',rclone_root='Finix',retention=0)
            core.atomic_json(profile.root/'settings.json',cfg)
            runner=transport.RcloneRunner(home,config,binary=str(binary),transfer_timeout=20)
            adapter=transport.RcloneStorage(home,profile.profile_id,profile.root/'rclone',str(config),'cloud','Finix',runner=runner)
            if os.name=='nt':
                win=__import__(package+'.windows_native',fromlist=['popen']);original=win.popen;target=package+'.windows_native.popen'
                stack.enter_context(patch.object(win,'trusted_binary',return_value=True))
            else:original=subprocess.Popen;target=package+'.rclone_storage.subprocess.Popen'
            calls=[]
            def launch(argv,**kw):
                if Path(argv[0]).name in ('age','age.exe','age-keygen','age-keygen.exe'):
                    return original(argv,**kw)  # real generated-key encryption
                self.assertEqual(argv[0],str(binary))
                index=next(i for i,x in enumerate(argv) if x in {'listremotes','copyto','lsjson','cat','deletefile'})
                calls.append(argv[index])
                return original([sys.executable,str(script),str(store),*argv[index:]],**kw)
            stack.enter_context(patch(target,side_effect=launch))
            stack.enter_context(patch.object(Profile,'rclone',return_value=adapter))
            stack.enter_context(patch.object(api,'current_profile',side_effect=lambda:Profile(home,default_root=home)))
            app=FastAPI();app.include_router(api.router)
            legacy_record = None
            if legacy:
                real_runner = adapter.runner
                def fail_before_copy(op, *args, **kw):
                    if op == 'copyto': raise transport.RcloneError('TIMEOUT')
                    return real_runner(op, *args, **kw)
                adapter.runner = fail_before_copy
                with self.assertRaises(transport.RcloneError): profile.backup()
                adapter.runner = real_runner
                with adapter._locked(): legacy_record = adapter.pending(adapter.ledger())
                self.assertFalse((profile.root/'backup-jobs/state.json').exists())
                stack.enter_context(patch.object(core, 'backup', side_effect=AssertionError('NO LEGACY RECAPTURE')))
            snapshots=[]
            failures=[]
            def flow(p):
                instance=jobs_module.SetupFlow(p);method=instance.check_and_run
                def run(**kw):
                    try:return method(**kw)
                    except BaseException as exc:
                        failures.append(repr(exc));raise
                instance.check_and_run=run;return instance
            api._backup_jobs.flow_factory=flow
            def wait_file(name):
                deadline=time.monotonic()+10
                while not (store/name).exists():
                    self.assertLess(time.monotonic(),deadline,(name,calls,failures,api._backup_jobs.current(profile)));time.sleep(.01)
            try:
                with TestClient(app) as client:
                    setup_before=client.get('/setup').json()
                    initial=client.get('/backup/jobs/current').json()
                    body={'profile_id':profile.profile_id,'request_id':'11111111-1111-4111-8111-111111111111'}
                    start=time.monotonic();ack=client.post('/backup/jobs',json=body)
                    self.assertEqual(ack.status_code,202,ack.text);self.assertLess(time.monotonic()-start,1)
                    identifier=ack.json()['job']['id'];wait_file('upload-start')
                    # A new HTTP client after a lost acknowledgement reconstructs
                    # Profile while the actual worker owns the operation lock.
                    with TestClient(app) as reopened:
                        clock=SimpleNamespace(time=lambda:ack.json()['job']['started_at']+1200,monotonic=time.monotonic,sleep=time.sleep)
                        with patch.object(jobs_module,'time',clock):
                            start=time.monotonic()
                            for _ in range(10):
                                row=reopened.get('/backup/jobs/current').json()['job'];snapshots.append(row)
                                self.assertEqual(row['id'],identifier);self.assertEqual(row['state'],'running')
                            self.assertLess(time.monotonic()-start,2)
                        self.assertEqual(reopened.post('/backup/jobs',json=body).json()['job']['id'],identifier)
                    self.assertTrue(any(r['phase']=='upload' and r['progress']['completed_bytes']>0 for r in snapshots),snapshots)
                    (store/'upload-release').touch();wait_file('verify-start')
                    time.sleep(.3)
                    row=client.get('/backup/jobs/current').json()['job'];snapshots.append(row)
                    self.assertEqual(row['phase'],'verify');self.assertGreater(row['progress']['completed_bytes'],0)
                    self.assertLess(row['progress']['percent'],100);self.assertFalse(row['verified'])
                    (store/'verify-release').touch()
                    deadline=time.monotonic()+10
                    while True:
                        final=client.get('/backup/jobs/current').json()['job']
                        if final['state']!='running':break
                        self.assertLess(time.monotonic(),deadline);time.sleep(.01)
                    self.assertEqual(final['state'],'succeeded',final);self.assertTrue(final['verified'])
                    if os.environ.get('FINIX_TEST_HTTP_EVIDENCE'):
                        evidence = Path(os.environ['FINIX_TEST_HTTP_EVIDENCE'])
                        if legacy: evidence = evidence.with_name(evidence.stem+'-legacy'+evidence.suffix)
                        evidence.write_text(json.dumps({
                            'initial':initial, 'setup_before':setup_before, 'setup_terminal':client.get('/setup').json(),
                            'ack':ack.json(), 'polls':[dict(code_version='backup-jobs-v1',profile_id=profile.profile_id,job=r) for r in snapshots],
                            'terminal':dict(code_version='backup-jobs-v1',profile_id=profile.profile_id,job=final)},indent=2))
                    while jobs_module._busy(profile.root/'backup-jobs'/'worker.lock'):time.sleep(.005)
                    ledger=adapter.ledger();self.assertEqual(len(ledger['records']),1);record=ledger['records'][0]
                    if legacy:
                        self.assertEqual(record, legacy_record)
                        state=json.loads((profile.root/'backup-jobs/state.json').read_text())
                        self.assertEqual(state['job']['capture']['legacy_pending'],record)
                    archive=next((profile.root/'copies').glob('*.age'))
                    self.assertEqual(core.digest_file(store/record['id']),core.digest_file(archive))
                    # Both real resumed branches emit actual cat-byte telemetry,
                    # and pending identity stays unchanged (no second copy).
                    intent=dict(archive=archive.name,sha256=core.digest_file(archive),size=archive.stat().st_size,complete=True,omitted_count=0)
                    for pending in (False,True):
                        if pending:
                            with adapter._locked():adapter._intent(adapter.ledger(),record)
                        events=[];cancel=lambda:False
                        profile._resume_backup(cfg,intent,progress=events.append,cancel=cancel)
                        self.assertTrue(any(e.get('phase')=='verify' and e.get('completed_bytes',0)>0 for e in events),events)
                    self.assertEqual(calls.count('copyto'),1)
                    self.assertEqual(adapter.ledger()['records'][0]['id'],record['id'])
                    core.restore(archive,root/'generated.key',root/'restored')
                    self.assertEqual((root/'restored/work').read_bytes(),(home/'work').read_bytes())
            finally:
                (store/'upload-release').touch();(store/'verify-release').touch()
                deadline=time.monotonic()+20
                while jobs_module._busy(profile.root/'backup-jobs'/'worker.lock') and time.monotonic()<deadline:time.sleep(.01)
