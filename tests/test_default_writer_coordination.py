"""Real SDK writer-fence regression; archive bytes are not lock ownership."""
import os
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing
import test_sqlite_family_transaction as fixtures
from finix import core, default_recovery as engine
from finix.default_recovery_lifecycle import writer_fence

LOCKS=('.backup.lock','.checkpoints.lock')

class WriterCoordinationTests(unittest.TestCase):
    setUp=fixtures.FamilyTests.setUp
    capability=fixtures.FamilyTests.capability
    old_database=fixtures.FamilyTests.old_database
    clean_family=fixtures.FamilyTests.clean_family
    prepare=fixtures.FamilyTests.prepare
    check_old=fixtures.FamilyTests.check_old

    def controls(self):
        for name in LOCKS:
            p=self.root/name
            self.assertFalse(p.exists(),'fresh fixture required')
            p.write_bytes(b'current SDK coordination sentinel')
            self.addCleanup(lambda p=p:p.unlink(missing_ok=True))
        return {n:(self.root/n).stat().st_ino for n in LOCKS}

    def test_only_exact_top_level_sdk_locks_are_retained(self):
        for name in LOCKS:
            self.assertFalse(engine._member(name),name+' is live coordination, not incoming profile data')
        for name in ('.user-data.lock','.backup.lock.notes','.checkpoints.lock.notes','notes/.backup.lock','notes/.checkpoints.lock'):
            self.assertTrue(engine._member(name),'do not broaden lock-name exclusions: '+name)

    def test_current_coordination_inventory_never_reads_held_lock_bytes(self):
        identities=self.controls()
        with writer_fence(self.root):
            kept=engine._kept_inventory(self.root,{})
            for name in LOCKS:
                self.assertEqual(kept[name]['kind'],'opaque-retained-member')
                self.assertEqual(kept[name]['identity'][1],identities[name])
        for name in LOCKS:self.assertEqual((self.root/name).read_bytes(),b'current SDK coordination sentinel')

    def test_real_archive_apply_and_rollback_keep_coordination_file_identity(self):
        identities=self.controls();self.old_database()
        source=core.private_dir(self.directory/'source')
        for name in LOCKS:(source/name).write_bytes(b'archived foreign coordination bytes')
        (source/'.user-data.lock').write_bytes(b'ordinary incoming user data')
        cap=self.capability();plan=self.prepare(cap)
        with writer_fence(self.root):
            self.assertEqual(plan.apply(plan.consent())['state'],'published_offline')
            self.assertEqual({n:(self.root/n).stat().st_ino for n in LOCKS},identities)
            self.assertEqual((self.root/'.user-data.lock').read_bytes(),b'ordinary incoming user data')
            self.assertEqual(plan.rollback(plan.consent())['state'],'rolled_back')
            self.assertEqual({n:(self.root/n).stat().st_ino for n in LOCKS},identities)
        self.check_old()
        for name in LOCKS:self.assertEqual((self.root/name).read_bytes(),b'current SDK coordination sentinel')
        self.assertFalse((self.root/'.user-data.lock').exists())

    def test_recreated_current_checkpoint_lock_is_never_archive_authority(self):
        self.controls();self.old_database()
        source=core.private_dir(self.directory/'source')
        (source/'.checkpoints.lock').write_bytes(b'foreign archived control marker')
        cap=self.capability();plan=self.prepare(cap)
        target=self.root/'.checkpoints.lock';replacement=self.directory/'replacement-lock'
        replacement.write_bytes(target.read_bytes());os.replace(replacement,target)
        current_identity=target.stat().st_ino
        with writer_fence(self.root):
            self.assertEqual(plan.apply(plan.consent())['state'],'published_offline')
            self.assertEqual(target.stat().st_ino,current_identity)
            self.assertEqual(plan.rollback(plan.consent())['state'],'rolled_back')
        self.check_old()
        self.assertEqual(target.read_bytes(),b'current SDK coordination sentinel')
