"""Explicit HTTP Start adopts owned pre-jobs ciphertext, never recaptures."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from finix import core
from finix.integration import Profile
from finix.backup_jobs import BackupJobs, _busy
from finix.rclone_storage import RcloneError
from tests import test_backup_jobs_review as fixtures

class LegacyPendingHTTP(unittest.TestCase):
    def finish_http(self, jobs, profile):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        spec=importlib.util.spec_from_file_location('legacy_matrix_http',Path(__file__).parents[1]/'dashboard/plugin_api.py')
        api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
        api.current_profile=lambda:profile;api._backup_jobs=jobs
        app=FastAPI();app.include_router(api.router)
        with TestClient(app) as client:
            body=dict(profile_id=profile.profile_id,request_id=str(uuid.uuid4()))
            self.assertEqual(client.post('/backup/jobs',json={**body,'profile_id':'different-owner'}).status_code,409)
            self.assertEqual(client.post('/backup/jobs',json={**body,'cipher':'forbidden-client-path'}).status_code,422)
            ack=client.post('/backup/jobs',json=body);self.assertEqual(ack.status_code,202,ack.text)
            for _ in range(1000):
                result=client.get('/backup/jobs/current').json()['job']
                if result['state']!='running':break
                time.sleep(.005)
            while _busy(profile.root/'backup-jobs/worker.lock'):time.sleep(.005)
            self.assertNotEqual(result['state'],'running')
            self.assertEqual(client.post('/backup/jobs',json=body).json()['job']['id'],result['id'])
            return result

    def test_historical_unknown_and_incomplete_never_produce_setup_receipt(self):
        from finix.setup_flow import SetupFlow
        for complete, count, variant in ((None, None, 'absent'), (None, None, 'bool-only'), (False, 2, 'known')):
            with self.subTest(complete=complete,variant=variant), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                p, adapter, runner, config = fixtures.ReviewRegressions().fixture(Path(tmp))
                def fail(op, *args, **kw):
                    if op == 'copyto': raise RcloneError('TIMEOUT')
                    return runner(op, *args, **kw)
                adapter.runner = fail
                with patch.object(Profile, 'rclone', return_value=adapter):
                    with self.assertRaises(RcloneError): p.backup()
                    index = core.load_state(p.root/'index')
                    row = index['backups'][0]
                    if complete is None:
                        row.pop('omitted_count')
                        if variant == 'absent': row.pop('complete')
                    else: row.update(complete=complete, omitted_count=count)
                    core.atomic_json(p.root/'index/index.json',index)
                    with adapter._locked():
                        journal = adapter.ledger(); pending = adapter.pending(journal)
                        pending['completion'] = None if complete is None else dict(sha256=pending['sha256'],complete=complete,omitted_count=count)
                        adapter._intent(journal,pending)
                    adapter.runner = runner
                    with patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                        result = self.finish_http(BackupJobs(),p)
                    self.assertEqual(result['state'],'incomplete',result)
                    self.assertIs(result['complete'],complete)
                    self.assertEqual(result['omitted_count'],count)
                    self.assertTrue(result['verified'])
                    self.assertFalse((p.root/'setup-verification.json').exists())
                    self.assertEqual(p.capture_status(),dict(complete=complete,omitted_count=count))
                    with self.assertRaises(core.FinixError): SetupFlow(p).schedule('24h')
                    self.assertEqual(p.config()['interval_hours'],0)
                    # Direct local retention must not treat a legacy bool-only
                    # index as a complete successor after unknown adoption.
                    latest=core.load_state(p.root/'index')
                    name='finix-'+latest['namespace']+'-'+uuid.uuid4().hex+'.age'
                    older=p.root/'copies'/name
                    with core.regular_output(older) as fd:os.write(fd,b'older private ciphertext fixture')
                    latest['backups'].insert(0,dict(name=name,sha256=core.digest_file(older),complete=True,omitted_count=0))
                    core.atomic_json(p.root/'index/index.json',latest)
                    p._prune_local(1)
                    self.assertTrue(older.exists(),'unknown successor authorized local deletion')

    def seed_pending(self, root):
        p, adapter, runner, config = fixtures.ReviewRegressions().fixture(root)
        def fail(op, *args, **kw):
            if op == 'copyto': raise RcloneError('TIMEOUT')
            return runner(op, *args, **kw)
        adapter.runner = fail
        with patch.object(Profile, 'rclone', return_value=adapter):
            with self.assertRaises(RcloneError): p.backup()
        adapter.runner = runner
        return p, adapter, runner, config

    def test_adoption_rejects_changed_ownership_without_provider_io(self):
        for fault in ('hash','size','missing','duplicate','path','namespace','binding','conflict','permissions','index_permissions'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                p, adapter, runner, config = self.seed_pending(Path(tmp))
                pending_path = p.root/'rclone/rclone-pending.json'
                saved = pending_path.read_bytes()
                index = core.load_state(p.root/'index'); row = index['backups'][0]
                cipher = p.root/'copies'/row['name']
                if fault == 'hash': cipher.write_bytes(cipher.read_bytes()[:-1]+b'!')
                elif fault == 'size': cipher.write_bytes(cipher.read_bytes()+b'!')
                elif fault == 'missing': index['backups']=[]
                elif fault == 'duplicate': index['backups'].append(dict(row))
                elif fault == 'path': row['name']='../escape.age'
                elif fault in ('namespace','binding','conflict'):
                    with adapter._locked():
                        ledger=adapter.ledger(); pending=adapter.pending(ledger)
                        if fault == 'namespace': ledger['namespace']=uuid.uuid4().hex
                        elif fault == 'binding': ledger['binding']['remote']='other'
                        else: ledger['records'].append(dict(pending,size=pending['size']+1))
                        adapter._write('rclone.json',ledger)
                elif fault == 'permissions':
                    if os.name == 'nt':
                        # Windows no-follow regular guard is also exercised by
                        # the missing/nonregular path below without symlink privilege.
                        cipher.unlink();cipher.mkdir()
                    else: cipher.chmod(0o644)
                if fault == 'index_permissions' and os.name == 'nt':
                    continue  # POSIX mode test; Windows uses native handle ACL validation.
                core.atomic_json(p.root/'index/index.json',index)
                if fault == 'index_permissions': (p.root/'index/index.json').chmod(0o644)
                before=len(runner.calls)
                with patch.object(Profile,'rclone',return_value=adapter), patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                    result=self.finish_http(BackupJobs(),p)
                self.assertEqual(result['state'],'failed',result)
                self.assertEqual(pending_path.read_bytes(),saved)
                self.assertFalse(any(op in ('copyto','cat','deletefile') for op,*_ in runner.calls[before:]))

    def test_partial_or_ambiguous_remote_never_copies_or_prunes(self):
        for fault in ('partial','same-size-corrupt','ambiguous'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                p,adapter,runner,config=self.seed_pending(Path(tmp))
                saved=(p.root/'rclone/rclone-pending.json').read_bytes();record=json.loads(saved)['record']
                cipher=next((p.root/'copies').glob('*.age'));data=cipher.read_bytes()
                if fault!='ambiguous':runner.objects[adapter.object_path(record)]=data[:12] if fault=='partial' else data[:-1]+bytes([data[-1]^1])
                def transport(op,*args,**kw):
                    if fault=='ambiguous' and op=='lsjson':raise RcloneError('TIMEOUT')
                    return runner(op,*args,**kw)
                adapter.runner=transport;before=len(runner.calls)
                with patch.object(Profile,'rclone',return_value=adapter),patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                    result=self.finish_http(BackupJobs(),p)
                self.assertEqual(result['state'],'failed',result)
                self.assertEqual(saved,(p.root/'rclone/rclone-pending.json').read_bytes())
                self.assertFalse(any(op in ('copyto','deletefile') for op,*_ in runner.calls[before:]))
                self.assertEqual(adapter.ledger()['records'],[])

    def test_interrupted_before_adoption_checkpoint_still_adopts_owned_pending(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p,adapter,runner,config=self.seed_pending(Path(tmp))
            with patch.object(Profile,'rclone',return_value=adapter),patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                with patch.object(p,'pending_backup_checkpoint',side_effect=core.FinixError('interrupted before checkpoint')):
                    first=self.finish_http(BackupJobs(),p)
                state_path=p.root/'backup-jobs/state.json';state=json.loads(state_path.read_text())
                self.assertNotIn('capture',state['job'])
                state['job']['state']='running';core.atomic_json(state_path,state)
                second=self.finish_http(BackupJobs(),p)
                self.assertEqual(second['state'],'succeeded',second)
                self.assertEqual(first['id'],second['id'])
                self.assertEqual(len(runner.objects),1)

    def test_durable_checkpoint_restart_refresh_context_and_cancel_preserve_identity(self):
        import contextvars
        selector=contextvars.ContextVar('legacy_request',default='wrong')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
            p,adapter,runner,config=self.seed_pending(Path(tmp))
            saved=(p.root/'rclone/rclone-pending.json').read_bytes()
            observed=[]
            def cancelled(op,*args,**kw):
                observed.append(selector.get())
                if op=='lsjson':
                    state=json.loads((p.root/'backup-jobs/state.json').read_text())
                    self.assertEqual(state['job']['capture']['legacy_pending'],json.loads(saved)['record'])
                    raise core.FinixError('Operation cancelled')
                return runner(op,*args,**kw)
            adapter.runner=cancelled
            with patch.object(Profile,'rclone',return_value=adapter), patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')):
                token=selector.set('authenticated-owner')
                try: first=self.finish_http(BackupJobs(),p)
                finally:selector.reset(token)
                self.assertEqual(first['state'],'failed')
                self.assertEqual((p.root/'rclone/rclone-pending.json').read_bytes(),saved)
                self.assertTrue(observed);self.assertEqual(set(observed),{'authenticated-owner'})
                # Simulate restart after interrupted worker; never fabricate a
                # capture, only retain the product's fsynced checkpoint.
                state_path=p.root/'backup-jobs/state.json'
                state=json.loads(state_path.read_text());state['job']['state']='running';core.atomic_json(state_path,state)
                replacement=config.with_suffix('.refresh');replacement.write_text('[cloud]\ntype=yandex\ntoken=synthetic-refresh\n');replacement.chmod(0o600);os.replace(replacement,config)
                adapter.runner=runner
                second=self.finish_http(BackupJobs(),p)
                self.assertEqual(first['id'],second['id'])
                self.assertEqual(second['state'],'succeeded',second)
                self.assertEqual(adapter.ledger()['records'][0]['id'],json.loads(saved)['record']['id'])
                self.assertEqual(len(runner.objects),1)
                # Committed-before-receipt crash: exact original object is
                # read back again, with no pending intent and no second copy.
                committed=json.loads(state_path.read_text());committed['job']['state']='running'
                core.atomic_json(state_path,committed);(p.root/'setup-verification.json').unlink()
                copies_before=sum(op=='copyto' for op,*_ in runner.calls)
                third=self.finish_http(BackupJobs(),p)
                self.assertEqual(third['state'],'succeeded',third);self.assertEqual(third['id'],first['id'])
                self.assertEqual(sum(op=='copyto' for op,*_ in runner.calls),copies_before)
                checkpoint=state['job']['capture']
                with self.assertRaisesRegex(core.FinixError,'cancelled'):
                    p.backup(resume=checkpoint,cancel=lambda:True)

    def test_no_initial_jobs_http_adopts_same_pending_before_and_after_copy(self):
        for fault in ('before', 'after'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as tmp:
                p, adapter, runner, config = fixtures.ReviewRegressions().fixture(Path(tmp))
                def fail(op, *args, **kw):
                    if op == 'copyto' and fault == 'before': raise RcloneError('TIMEOUT')
                    result = runner(op, *args, **kw)
                    if op == 'copyto' and fault == 'after': raise RcloneError('TIMEOUT')
                    return result
                adapter.runner = fail
                with patch.object(Profile, 'rclone', return_value=adapter):
                    with self.assertRaises(RcloneError): p.backup()
                    pending = p.root/'rclone/rclone-pending.json'
                    record = json.loads(pending.read_text())['record']
                    index = (p.root/'index/index.json').read_bytes()
                    copies = {f.name:core.digest_file(f) for f in (p.root/'copies').glob('*.age')}
                    self.assertFalse((p.root/'backup-jobs/state.json').exists())
                    adapter.runner = runner
                    events=[]
                    p.pending_backup_checkpoint(progress=events.append)
                    self.assertTrue(events)
                    self.assertTrue(all(event['phase']=='checksum' for event in events),events)
                    from fastapi import FastAPI
                    from fastapi.testclient import TestClient
                    spec = importlib.util.spec_from_file_location('legacy_pending_http',Path(__file__).parents[1]/'dashboard/plugin_api.py')
                    api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
                    api.current_profile = lambda:p; api._backup_jobs = BackupJobs()
                    app = FastAPI(); app.include_router(api.router)
                    before = len(runner.calls)
                    with patch.object(core,'backup',side_effect=AssertionError('NO RECAPTURE')), TestClient(app) as client:
                        self.assertIsNone(client.get('/backup/jobs/current').json()['job'])
                        self.assertEqual(len(runner.calls),before)
                        body = {'profile_id':p.profile_id,'request_id':str(uuid.uuid4())}
                        response = client.post('/backup/jobs',json=body)
                        self.assertEqual(response.status_code,202,response.text)
                        for _ in range(1000):
                            job = client.get('/backup/jobs/current').json()['job']
                            if job['state']!='running':break
                            time.sleep(.005)
                        while _busy(p.root/'backup-jobs/worker.lock'):time.sleep(.005)
                        self.assertEqual(job['state'],'succeeded',job)
                        self.assertEqual(client.post('/backup/jobs',json=body).json()['job']['id'],job['id'])
                        checkpoint = json.loads((p.root/'backup-jobs/state.json').read_text())['job']['capture']
                        self.assertEqual(checkpoint['legacy_pending'],record)
                    self.assertFalse(pending.exists())
                    self.assertEqual(adapter.ledger()['records'],[record])
                    self.assertEqual(index,(p.root/'index/index.json').read_bytes())
                    self.assertEqual(copies,{f.name:core.digest_file(f) for f in (p.root/'copies').glob('*.age')})
                    self.assertEqual(len(runner.objects),1)
