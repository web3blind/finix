"""Finix-owned validation, real SDK public export; no live profile/runtime."""
import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from finix import core, portable_history as subject
from finix.recovery_import import RecoveryImportError


class PortableHistoryContract(unittest.TestCase):
    def setUp(self):
        import hermes_state
        self.sdk = hermes_state
        self.temp = tempfile.TemporaryDirectory(dir=Path(os.environ['TMPDIR']), prefix='reviewer-history-')
        self.addCleanup(self.temp.cleanup)
        self.root = core.private_dir(Path(self.temp.name)/'private')
        self.source = self.root/'source.db'
        db = hermes_state.SessionDB(self.source)
        try:
            db.create_session('parent', source='desktop')
            db.create_session('child', source='desktop', parent_session_id='parent')
            db.set_session_title('parent', 'Original parent')
            db.set_session_title('child', 'Original child')
            for sid in ('parent','child'):
                db.append_message(sid, role='assistant', content='Original '+sid)
                db.append_message(sid, role='assistant', content=[{'type':'text','text':'Rich '+sid}])
        finally:
            db.close()
        # Inactive/hidden history is still part of the physical inventory.
        with closing(sqlite3.connect(self.source)) as c:
            c.execute("update messages set active=0, compacted=1 where id=(select max(id) from messages)")
            c.execute("update sessions set hidden=1 where id='child'")
            c.commit()
        self.before = self.source.read_bytes()
        self.payload = {'size':len(self.before),'sha256':core.digest_file(self.source)}

    def prepare(self, cls=None):
        stage = core.private_dir(self.root/'stage')
        if cls is None:
            return subject.prepare(self.source, stage, expected_payload=self.payload)
        with patch.object(self.sdk, 'SessionDB', cls):
            return subject.prepare(self.source, stage, expected_payload=self.payload)

    def public_only(self, transform=lambda row:row):
        real = self.sdk.SessionDB
        class PublicOnly:
            def __init__(self, path): self.delegate=real(path)
            def export_session(self, sid, **kw): return transform(self.delegate.export_session(sid, **kw))
            def close(self): self.delegate.close()
            def __getattr__(self, name):
                raise AssertionError('Finix accessed non-public SDK validation: '+name)
        return PublicOnly

    def test_missing_history_schema_is_a_closed_content_error(self):
        broken=self.root/'malformed.db'
        with closing(sqlite3.connect(broken)) as c:
            c.execute('create table sessions (name text)');c.commit()
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_INVALID_CONTENT'):
            subject.prepare(broken,core.private_dir(self.root/'broken-stage'),
                expected_payload={'size':broken.stat().st_size,'sha256':core.digest_file(broken)})

    def test_same_count_substitutions_cannot_masquerade_as_complete_export(self):
        def changed(row):
            row['messages'][0]['id'] += 100000
            return row
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_TRUNCATED'):
            self.prepare(self.public_only(changed))

    def test_original_title_and_parent_cannot_be_silently_changed(self):
        def changed(row):
            row['title']='silently replaced'
            row['parent_session_id']=None
            return row
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_RICH_DATA_LOSS'):
            self.prepare(self.public_only(changed))

    def test_persisted_plan_is_revalidated_before_first_rpc(self):
        import asyncio
        plan = self.prepare()
        plan['sessions'][0]['title'] = ['invalid']
        target = core.private_dir(self.root/'target')
        parked = core.private_dir(target/'.finix-paused')
        provenance = core.private_dir(parked/'history-original')
        with core.regular_output(provenance/'state.db') as fd:
            os.write(fd,self.before)
        core.atomic_json(provenance/'plan.json',plan)
        db=self.sdk.SessionDB(target/'state.db');db.close()
        class MockedRPC:
            calls=0
            async def rpc(self,*a,**kw):
                self.calls+=1
                raise AssertionError('mocked transport must not be dispatched')
        rpc=MockedRPC()
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_INVALID_CONTENT'):
            asyncio.run(subject._import_authenticated(rpc,target,owner_subject='fixture-owner',
                                                       expected_original=self.payload))
        self.assertEqual(rpc.calls,0)

    def projection_target(self, plan, label='target'):
        target = core.private_dir(self.root/label)
        parked = core.private_dir(target/'.finix-paused')
        provenance = core.private_dir(parked/'history-original')
        with core.regular_output(provenance/'state.db') as fd:
            os.write(fd,self.before)
        core.atomic_json(provenance/'plan.json',plan)
        db=self.sdk.SessionDB(target/'state.db');db.close()
        return target

    def test_real_public_session_import_preserves_lineage_titles_and_rich_messages(self):
        plan = self.prepare()
        target = self.projection_target(plan)
        db=self.sdk.SessionDB(target/'state.db')
        try:
            for raw in plan['sessions']:
                result=db.import_sessions([raw])
                self.assertTrue(result['ok'])
                self.assertEqual((result['imported'],result['skipped'],result['detached']),(1,0,0))
                observed=db.export_session(raw['id'],include_inactive=True)
                for field in ('id','parent_session_id','title'):
                    self.assertEqual(observed[field],raw[field])
                for old,new in zip(raw['messages'],observed['messages']):
                    self.assertEqual({k:v for k,v in old.items() if k not in {'id','session_id'}},
                                     {k:v for k,v in new.items() if k not in {'id','session_id'}})
        finally: db.close()

    def test_persisted_child_omission_refuses_before_first_rpc(self):
        import asyncio
        plan=self.prepare();plan['sessions']=plan['sessions'][:1]
        target=self.projection_target(plan)
        class NoRPC:
            calls=0
            async def rpc(self,*a,**kw):
                self.calls+=1
                raise AssertionError('persisted truncation reached RPC')
        rpc=NoRPC()
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_TRUNCATED'):
            asyncio.run(subject._import_authenticated(rpc,target,owner_subject='fixture-owner',
                expected_original=self.payload))
        self.assertEqual(rpc.calls,0)
        self.assertFalse((target/'.finix-paused/history-original/import-receipt.json').exists())
        self.assertEqual((target/'.finix-paused/history-original/state.db').read_bytes(),self.before)

    def test_persisted_inventory_and_rich_content_corruption_refuses_before_rpc(self):
        import asyncio
        plan=self.prepare()
        def message_edit(field,value):
            return lambda rows: rows[1]['messages'][-1].__setitem__(field,value)
        cases={
            'remove-first-message':lambda rows:rows[0]['messages'].pop(0),
            'remove-last-inactive-message':lambda rows:rows[1]['messages'].pop(),
            'same-count-id-substitution':message_edit('id',987654321),
            'durable-uid-substitution':message_edit('message_uid','substituted-uid'),
            'rich-content':message_edit('content',[{'type':'text','text':'substituted'}]),
            'rich-reasoning':message_edit('reasoning_content','substituted reasoning'),
            'rich-tools':message_edit('tool_calls',[{'id':'changed','function':{'name':'inert','arguments':'{}'}}]),
            'inactive-flag':message_edit('active',1),
            'title':lambda rows:rows[1].__setitem__('title','substituted title'),
            'lineage':lambda rows:rows[1].__setitem__('parent_session_id',None),
            'unknown-parent':lambda rows:rows[1].__setitem__('parent_session_id','absent'),
            'duplicate-session':lambda rows:rows.append(copy.deepcopy(rows[0])),
            'duplicate-message':lambda rows:rows[1]['messages'].append(copy.deepcopy(rows[1]['messages'][0])),
        }
        for label,mutate in cases.items():
            with self.subTest(corruption=label):
                corrupted=copy.deepcopy(plan);mutate(corrupted['sessions'])
                target=self.projection_target(corrupted,label)
                provenance=target/'.finix-paused/history-original'
                persisted=(provenance/'plan.json').read_bytes()
                class NoRPC:
                    calls=0
                    async def rpc(self,*a,**kw):
                        self.calls+=1
                        raise AssertionError('corrupted plan reached RPC: '+label)
                rpc=NoRPC()
                with self.assertRaises(RecoveryImportError):
                    asyncio.run(subject._import_authenticated(rpc,target,owner_subject='fixture-owner',
                        expected_original=self.payload))
                self.assertEqual(rpc.calls,0)
                self.assertFalse((provenance/'import-receipt.json').exists())
                self.assertEqual((provenance/'state.db').read_bytes(),self.before)
                self.assertEqual((provenance/'plan.json').read_bytes(),persisted)
                with closing(sqlite3.connect(target/'state.db')) as c:
                    self.assertEqual(c.execute('select count(*) from sessions').fetchone()[0],0)
        self.assertEqual(self.source.read_bytes(),self.before)

    def test_original_corruption_refuses_before_rpc(self):
        import asyncio
        plan=self.prepare();target=self.projection_target(plan)
        provenance=target/'.finix-paused/history-original'
        with open(provenance/'state.db','r+b') as f:f.write(b'corrupt')
        class NoRPC:
            async def rpc(self,*a,**kw):raise AssertionError('corrupted original reached RPC')
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_SOURCE_BINDING_REQUIRED'):
            asyncio.run(subject._import_authenticated(NoRPC(),target,owner_subject='fixture-owner',
                expected_original=self.payload))
        self.assertFalse((provenance/'import-receipt.json').exists())

    def test_valid_persisted_inventory_imports_all_rows_with_public_sdk_model_transport(self):
        import asyncio
        plan=self.prepare();target=self.projection_target(plan);sdk=self.sdk
        provenance=target/'.finix-paused/history-original'
        persisted=(provenance/'plan.json').read_bytes()
        class ModeledTransport:
            calls=0
            async def rpc(self,*a,**kw):
                self.calls+=1
                db=sdk.SessionDB(target/'state.db')
                try:return db.import_sessions(kw['payload']['sessions'])
                finally:db.close()
        rpc=ModeledTransport()
        receipt=asyncio.run(subject._import_authenticated(rpc,target,owner_subject='fixture-owner',
            expected_original=self.payload))
        self.assertEqual(rpc.calls,2)
        self.assertFalse(receipt['full_acceptance'])
        self.assertTrue((provenance/'import-receipt.json').is_file())
        self.assertEqual((provenance/'state.db').read_bytes(),self.before)
        self.assertEqual((provenance/'plan.json').read_bytes(),persisted)
        db=sdk.SessionDB(target/'state.db')
        try:
            for row in plan['sessions']:
                observed=db.export_session(row['id'],include_inactive=True)
                self.assertEqual(observed['title'],row['title'])
                self.assertEqual(observed['parent_session_id'],row['parent_session_id'])
                self.assertEqual(len(observed['messages']),len(row['messages']))
        finally:db.close()

    def test_target_readback_refuses_silent_title_loss(self):
        import asyncio
        plan=self.prepare(); target=self.projection_target(plan); sdk=self.sdk
        class MockedTransport:
            async def rpc(self,*args,**kw):
                db=sdk.SessionDB(target/'state.db')
                try:
                    result=db.import_sessions(kw['payload']['sessions'])
                    db.set_session_title(kw['session_id'],'changed '+kw['session_id'])
                    return result
                finally:db.close()
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_RICH_DATA_LOSS'):
            asyncio.run(subject._import_authenticated(MockedTransport(),target,
                owner_subject='fixture-owner',expected_original=self.payload))
        self.assertFalse((target/'.finix-paused/history-original/import-receipt.json').exists())

    def test_prepare_uses_only_public_export_preserving_full_physical_history(self):
        plan = self.prepare(self.public_only())
        self.assertEqual([s['id'] for s in plan['sessions']], ['parent','child'])
        self.assertEqual(plan['sessions'][1]['parent_session_id'], 'parent')
        self.assertEqual(plan['sessions'][1]['title'], 'Original child')
        self.assertEqual(sum(len(s['messages']) for s in plan['sessions']),4)
        self.assertEqual(plan['sessions'][1]['messages'][-1]['active'],0)
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertNotIn('sdk_bounds',plan)
        self.assertEqual(plan['validation']['owner'],'finix')


class FinixValidation(unittest.TestCase):
    def row(self):
        return {'id':'exact-source-id','messages':[{'id':1,'session_id':'exact-source-id',
                'role':'user','content':'private fixture marker','message_uid':'uid-1'}]}

    def test_schema_refuses_lossy_coercions_with_closed_codes(self):
        for field,value in [('title', []), ('parent_session_id', []), ('model_config', []),
                            ('input_tokens', 2**64), ('started_at', 'not a time')]:
            with self.subTest(field=field):
                row=self.row(); row[field]=value
                with self.assertRaises(RecoveryImportError) as caught: subject.validate_sessions([row])
                self.assertEqual(str(caught.exception),'HISTORY_INVALID_CONTENT')
        for field,value in [('role', 7),('role',''),('token_count',2**64),
                            ('tool_name',{}),('timestamp','bad')]:
            with self.subTest(message_field=field):
                row=self.row();row['messages'][0][field]=value
                with self.assertRaises(RecoveryImportError) as caught: subject.validate_sessions([row])
                self.assertEqual(str(caught.exception),'HISTORY_INVALID_CONTENT')

    def test_lineage_and_durable_message_references_refuse(self):
        cases=[]
        row=self.row();row['messages'][0]['session_id']='foreign';cases.append((row,'HISTORY_INVALID_LINEAGE'))
        row=self.row();row['messages']*=2;cases.append((row,'HISTORY_ID_COLLISION'))
        row=self.row();row['parent_session_id']='missing';cases.append((row,'HISTORY_INVALID_LINEAGE'))
        row=self.row();row['parent_session_id']=row['id'];cases.append((row,'HISTORY_INVALID_LINEAGE'))
        for row,code in cases:
            with self.subTest(code=code):
                with self.assertRaises(RecoveryImportError) as caught:subject.validate_sessions([row])
                self.assertEqual(str(caught.exception),code)

    def test_finite_json_and_owned_budgets_without_total_session_cap(self):
        row=self.row();row['messages'][0]['content']={'nested':[float('inf')]}
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_INVALID_CONTENT'):subject.validate_sessions([row])
        row=self.row();row['messages'][0]['content']='x'*(subject.MAX_SESSION_BYTES+1)
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_CONTENT_BOUNDS'):subject.validate_sessions([row])
        row=self.row();row['messages']=[{'role':'user'}]*(subject.MAX_SESSION_MESSAGES+1)
        with self.assertRaisesRegex(RecoveryImportError,'HISTORY_CONTENT_BOUNDS'):subject.validate_sessions([row])
        rows=[{'id':'session-'+str(i),'messages':[]} for i in range(501)]
        before=copy.deepcopy(rows)
        self.assertEqual(len(subject.validate_sessions(rows)),501)
        self.assertEqual(rows,before)


if __name__ == '__main__':
    unittest.main()
