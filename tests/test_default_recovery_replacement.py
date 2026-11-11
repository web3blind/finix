"""Focused replacement-policy and sealed-deletion admission regressions."""
import copy
import unittest
import test_default_recovery_hardening as hardening
from finix import default_recovery as e

class ReplacementPolicyTests(unittest.TestCase):
    setUp=hardening.JournalSchemaTests.setUp
    reopen=hardening.JournalSchemaTests.reopen
    def test_policy_is_narrow_and_old_schema_is_fail_closed(self):
        self.assertEqual(e.PROFILE_REPLACEMENT_ROOTS,frozenset({'memories','skills'}))
        for old_version in (1,2):
            p=copy.deepcopy(self.plan);p['version']=old_version
            with self.assertRaises(e.RecoveryError): self.reopen(p)

    def test_cannot_authorize_deleting_protected_or_unknown_members(self):
        for name in ('profiles/x/data','tools/tool','installs/host','plugins/finix/code',
                     'cache/finix/key','unknown/context','hermes-agent/main.py'):
            p=copy.deepcopy(self.plan)
            p['target_owned']={name:'d'*64};p['deletions']={name:'d'*64}
            with self.subTest(name=name),self.assertRaises(e.RecoveryError): self.reopen(p)

    def test_kept_preview_inventory_is_digest_bound(self):
        p=copy.deepcopy(self.plan)
        p['kept_target']={'tools':{'kind':'opaque-retained-member','identity':[1,2]}}
        p['kept_target_count']=1
        with self.assertRaises(e.RecoveryError):self.reopen(p)
        p['kept_target_digest']=e._digest(p['kept_target'])
        public=self.reopen(p).public_plan()
        self.assertEqual(public['kept_target'],p['kept_target'])
        self.assertEqual(public['kept_target_count'],1)

    def test_sealed_stale_digest_cannot_be_forged_in_journal(self):
        from finix import core
        p=copy.deepcopy(self.plan)
        p['target_owned']={'memories/old-only.md':'d'*64}
        p['deletions']=dict(p['target_owned'])
        p['deletion_leaves']={'memories/old-only.md':dict(identity=[1,2],size=1,sha256='d'*64)}
        recovery=self.reopen(p)
        core.atomic_json(self.directory/'journal.json',dict(version=1,plan_digest=recovery.plan_digest,
            old={'config.yaml':None,'memories/old-only.md':'e'*64},pending=[],published=[]))
        with self.assertRaises(e.RecoveryError) as error: recovery._journal(p)
        self.assertEqual(error.exception.code,'INVALID_JOURNAL')
