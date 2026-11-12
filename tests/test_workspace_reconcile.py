"""Offline production adapter: synthetic transport, no cloud authorization."""
import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from finix import core
from finix.workspace_drive import WorkspaceDrive, WorkspaceError, MIME, FOLDER_MIME

ACCOUNT_DIGEST = hashlib.sha256(b'synthetic-account').hexdigest()

class Transport:
    def __init__(self):
        self.objects, self.data, self.calls = {}, {}, []
        self.account = 'synthetic-account'
        self.error = WorkspaceError
        self.lost = False
    def __call__(self, op, *args, **kw):
        self.calls.append((op,args))
        if op == 'identity': return {'account_id': self.account}
        if op == 'generate-id': return {'id': 'object-' + str(len(self.calls))}
        if op == 'metadata':
            if args[0] not in self.objects: raise self.error('NOT_FOUND')
            return dict(self.objects[args[0]])
        if op in {'upload', 'create-folder'}:
            obj=args[args.index('--id')+1]
            if obj in self.objects: raise self.error('PROVIDER_ERROR')
            row=dict(id=obj, name=args[args.index('--name')+1], trashed=False,
                     owners=[{'emailAddress':'fixture@example.invalid'}], mimeType=FOLDER_MIME)
            if op=='upload':
                self.data[obj]=Path(args[0]).read_bytes()
                row.update(mimeType=MIME, size=str(len(self.data[obj])),parents=[args[args.index('--parent')+1]])
            self.objects[obj]=row
            if op=='upload' and self.lost:
                self.lost=False
                raise self.error('TIMEOUT')
            return dict(row,status='uploaded' if op=='upload' else 'created')
        if op=='download':
            Path(args[args.index('--output')+1]).write_bytes(self.data[args[0]])
            return {'id':args[0],'status':'downloaded'}
        raise AssertionError(op)

class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']);self.addCleanup(self.tmp.cleanup)
        self.root=core.private_dir(Path(self.tmp.name)/'private');self.home=core.private_dir(self.root/'home')
        self.state=self.root/'state';self.transport=Transport()
        self.path=self.root/'cipher.age';self.path.write_bytes(b'age-encryption.org/v1\nsynthetic ciphertext')
        self.drive=WorkspaceDrive(self.home,'a'*64,self.state,runner=self.transport,account_digest=ACCOUNT_DIGEST)
    def test_invalid_local_inputs_have_no_provider_io(self):
        for completion in ({'sha256':'0'*64,'complete':True,'omitted_count':0},
                           {'sha256':core.digest_file(self.path),'complete':True},
                           {'sha256':core.digest_file(self.path),'complete':True,'omitted_count':1}):
            with self.assertRaises(core.FinixError):self.drive.reconcile(self.path,completion=completion)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):self.drive.reconcile(self.path)
        self.assertEqual(self.transport.calls,[])

    def test_account_refresh_stable_but_account_change_blocks_transfer(self):
        self.transport.lost=True
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        (self.home/'google_token.json').write_text('synthetic refreshed token')
        self.transport.account='other-account'
        before=len(self.transport.calls)
        with self.assertRaisesRegex(WorkspaceError,'ACCOUNT_CHANGED'):self.drive.reconcile(self.path)
        self.assertEqual([op for op,_ in self.transport.calls[before:]],['identity'])
        self.transport.account='synthetic-account'
        self.assertTrue(self.drive.reconcile(self.path)['verified'])

    def test_pending_binding_change_and_digest_change_rejected_before_io(self):
        self.transport.lost=True
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        pending=json.loads((self.state/'workspace-pending.json').read_text())
        before=len(self.transport.calls)
        for key,value in [('profile_id','b'*64),('binding',{'provider':'other'})]:
            changed=dict(pending);changed[key]=value
            core.atomic_json(self.state/'workspace-pending.json',changed)
            with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
            self.assertEqual(len(self.transport.calls),before)
        core.atomic_json(self.state/'workspace-pending.json',pending)
        self.path.write_bytes(self.path.read_bytes()+b'changed')
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        self.assertEqual(len(self.transport.calls),before)

    def test_active_lease_rejects_second_adapter_without_io(self):
        core.private_dir(self.state)
        other=WorkspaceDrive(self.home,'a'*64,self.state,runner=self.transport,account_digest=ACCOUNT_DIGEST)
        with self.drive._locked():
            with self.assertRaises((core.FinixError,OSError)):other.reconcile(self.path)
        self.assertEqual(self.transport.calls,[])

    def test_legacy_ledger_upgrade_preserves_old_rows(self):
        first=self.drive.reconcile(self.path)
        journal=json.loads((self.state/'workspace.json').read_text())
        journal.pop('binding');journal.pop('account');journal.pop('version')
        journal['records'][0].pop('completion')
        core.atomic_json(self.state/'workspace.json',journal)
        self.path.write_bytes(self.path.read_bytes()+b'new')
        self.drive.reconcile(self.path)
        upgraded=json.loads((self.state/'workspace.json').read_text())
        self.assertEqual(upgraded['records'][0],journal['records'][0])
        self.assertEqual(upgraded['version'],2)
        self.assertFalse(self.transport.objects[first['id']]['trashed'])

    def test_commit_crash_gap_replays_same_id_without_upload(self):
        from unittest.mock import patch
        original=self.drive._write_private
        def commit_then_crash(name,value):
            original(name,value)
            if name=='workspace.json' and value['records']:
                raise OSError('synthetic crash after durable commit')
        with patch.object(self.drive,'_write_private',side_effect=commit_then_crash):
            with self.assertRaises(OSError):self.drive.reconcile(self.path)
        self.assertTrue((self.state/'workspace-pending.json').exists())
        committed=json.loads((self.state/'workspace.json').read_text())['records'][0]
        self.assertEqual(self.drive.reconcile(self.path)['id'],committed['id'])
        self.assertFalse((self.state/'workspace-pending.json').exists())
        self.assertEqual(sum(op=='upload' for op,_ in self.transport.calls),1)

    def test_full_readback_corruption_leaves_pending_not_committed(self):
        self.transport.lost=True
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        pending=json.loads((self.state/'workspace-pending.json').read_text())
        obj=pending['record']['id'];original=self.transport.data[obj]
        self.transport.data[obj]=b'corrupt'
        with self.assertRaises(core.FinixError):self.drive.reconcile(self.path)
        self.assertEqual(json.loads((self.state/'workspace.json').read_text())['records'],[])
        self.assertTrue((self.state/'workspace-pending.json').exists())
        self.transport.data[obj]=original
        self.assertEqual(self.drive.reconcile(self.path)['id'],obj)
        self.assertEqual(sum(op=='upload' for op,_ in self.transport.calls),1)

    def test_adapter_threads_serialize_until_commit(self):
        import threading
        entered,release=threading.Event(),threading.Event();results=[];failures=[]
        fake=self.transport
        def transport(op,*args,**kw):
            if op=='upload':
                entered.set()
                if not release.wait(5):raise AssertionError('thread timeout')
            return fake(op,*args,**kw)
        self.drive.runner=transport
        def run():
            try:results.append(self.drive.reconcile(self.path))
            except BaseException as exc:failures.append(exc)
        first=threading.Thread(target=run);second=threading.Thread(target=run)
        first.start();self.assertTrue(entered.wait(2));second.start()
        release.set();first.join(5);second.join(5)
        self.assertEqual(failures,[]);self.assertEqual(len(results),2)
        self.assertEqual(results[0]['id'],results[1]['id'])
        self.assertEqual(sum(op=='upload' for op,_ in fake.calls),1)

    def test_pending_requires_binding_and_account_before_provider_io(self):
        self.transport.lost=True
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        pending=json.loads((self.state/'workspace-pending.json').read_text());before=len(self.transport.calls)
        for key in ['binding','account','name','folder_name']:
            changed=dict(pending);changed.pop(key)
            core.atomic_json(self.state/'workspace-pending.json',changed)
            with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
            self.assertEqual(len(self.transport.calls),before)

    def test_stale_prune_rejected_before_remote_io(self):
        row=self.drive.reconcile(self.path)
        stale=json.loads((self.state/'workspace.json').read_text())
        stale['records']=[]
        before=len(self.transport.calls)
        with self.assertRaises(WorkspaceError):self.drive.prune(stale,1)
        self.assertEqual(len(self.transport.calls),before)

    def test_lost_ack_reconciles_same_remote_id_and_committed_replay(self):
        self.transport.lost=True
        with self.assertRaises(WorkspaceError):self.drive.reconcile(self.path)
        pending=json.loads((self.state/'workspace-pending.json').read_text())
        self.assertIn(pending['record']['id'],self.transport.objects)
        row=WorkspaceDrive(self.home,'a'*64,self.state,runner=self.transport,account_digest=ACCOUNT_DIGEST).reconcile(self.path)
        self.assertEqual(row['id'],pending['record']['id'])
        self.assertEqual(self.drive.reconcile(self.path)['id'],row['id'])
        self.assertEqual(sum(op=='upload' for op,_ in self.transport.calls),1)
        self.assertEqual(sum(op=='download' for op,_ in self.transport.calls),2)
        self.assertIsNone(row['completion'])

if __name__=='__main__':unittest.main()
