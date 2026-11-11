"""Security regressions; runner supplies a private genuine SDK default."""
import copy
import os
from pathlib import Path
import unittest
import uuid
from finix import core, default_recovery as e

class JournalSchemaTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import get_default_hermes_root
        self.root=get_default_hermes_root()
        core.private_dir(self.root)
        self.job=uuid.uuid4().hex
        self.directory=core.private_dir(self.root/'cache/finix/default-recovery'/self.job)
        self.plan=dict(version=3,sqlite_families={},id=self.job,root=str(self.root),root_identity=e._identity(self.root),
            source=str(self.root/'cache/finix/default-copies'/uuid.uuid4().hex),source_identity=[1,2],
            source_digest={'config.yaml':'a'*64},content_digest='b'*64,epoch=uuid.uuid4().hex,
            entries={'config.yaml':'c'*64},role='default',retained_source_shared_files=0,
            shared_policy='preserve-existing-report-source-not-promoted',
            replacement_policy=e.REPLACEMENT_POLICY,replacement_roots=sorted(e.PROFILE_REPLACEMENT_ROOTS),
            target_entries={'config.yaml':None},target_owned={},deletions={},deletion_leaves={},kept_target={},kept_target_count=0,kept_target_digest=e._digest({}))
    def reopen(self,p):
        core.atomic_json(self.directory/'plan.json',p)
        return e.DefaultRecovery(self.directory,e._digest(p))
    def test_rejects_reserved_runtime_control_and_alias_members(self):
        for name in ['gateway.pid','profiles/x/data','cache/finix/keys/key','../x','/absolute',
                     'C:/drive','note/../x','NUL.txt','note.txt.','a\\b','gateway.sock']:
            with self.subTest(name=name):
                p=copy.deepcopy(self.plan);p['entries'][name]='d'*64
                with self.assertRaises(e.RecoveryError): self.reopen(p)
    def test_rejects_corrupt_status(self):
        recovery=self.reopen(self.plan)
        core.atomic_json(self.directory/'status.json',{'state':'succeeded','error':None})
        with self.assertRaises(e.RecoveryError): recovery.status()
    def test_rejects_oversized_plan_before_parse(self):
        path=self.directory/'plan.json'
        with path.open('wb') as stream: stream.truncate(e.MAX_RECORD+1)
        with self.assertRaises(e.RecoveryError): e.DefaultRecovery(self.directory,'a'*64)
    def test_rejects_wrong_schema_and_digest_types(self):
        for key,value in [('version',True),('epoch','bad'),('entries',[]),('source_digest','bad'),
                          ('root_identity',[True,2]),('role','named'),('retained_source_shared_files',-1)]:
            with self.subTest(key=key):
                p=copy.deepcopy(self.plan);p[key]=value
                with self.assertRaises(e.RecoveryError): self.reopen(p)
    def test_rejects_duplicate_json_keys_even_with_matching_last_value_digest(self):
        p=self.plan;core.atomic_json(self.directory/'plan.json',p)
        data=(self.directory/'plan.json').read_text()
        (self.directory/'plan.json').write_text(data.replace('"version": 3','"version": 0, "version": 3'))
        with self.assertRaises(e.RecoveryError): e.DefaultRecovery(self.directory,e._digest(p))
    @unittest.skipIf(os.name=='nt','POSIX ancestor mode regression')
    def test_reopen_rejects_untrusted_ancestor_without_repair(self):
        ancestor=self.root/'cache/finix'
        before=ancestor.stat().st_mode & 0o777
        try:
            ancestor.chmod(0o777)
            with self.assertRaises(e.RecoveryError): self.reopen(self.plan)
            self.assertEqual(ancestor.stat().st_mode & 0o777,0o777)
        finally: ancestor.chmod(before)
    def test_fake_service_cannot_issue_capability(self):
        class Fake:
            def __getattr__(self,name): raise AssertionError('fake service was consulted')
        with self.assertRaises(e.RecoveryError):
            e.verified_import(Fake(),'job',profile_id='default',activation='epoch',name='recovered')
