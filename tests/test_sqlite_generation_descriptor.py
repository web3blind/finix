"""Independent generation seam tests; not full maintenance acceptance."""
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from finix import core, default_recovery as engine


class SQLiteDescriptorTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import get_default_hermes_root,get_hermes_home
        from hermes_cli.profiles import get_profile_dir
        scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        root=get_default_hermes_root()
        self.assertEqual(root,scope/'sdk-root')
        self.assertEqual(root,get_hermes_home())
        self.assertEqual(root,get_profile_dir('default'))
        self.directory=core.private_dir(root/'cache/finix/generation-test'/__import__('uuid').uuid4().hex)
        self.path=self.directory/'state.db'
        self.temp=core.private_dir(self.directory/'snapshots')

    def test_checkpoint_matches_and_committed_change_does_not(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('pragma journal_mode=WAL')
            db.execute('pragma wal_autocheckpoint=0')
            db.execute('create table fixture(value text)')
            db.execute("insert into fixture values ('before')")
            db.commit()
            before=core.digest_file(self.path)
            descriptor=engine._sqlite_generation(self.path,self.temp)
            db.execute('pragma wal_checkpoint(FULL)')
            self.assertNotEqual(before,core.digest_file(self.path))
            self.assertEqual(descriptor,engine._sqlite_generation(self.path,self.temp))
            db.execute("insert into fixture values ('after')")
            db.commit()
            self.assertNotEqual(descriptor['snapshot_sha256'],engine._sqlite_generation(self.path,self.temp)['snapshot_sha256'])

    def test_verifier_refuses_real_schema_version_row_and_identity_changes(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('create table fixture(value text)');db.commit()
        for statement in ("insert into fixture values ('new committed row')",
                          'create table another(value blob)','pragma user_version=42'):
            sealed=engine._sqlite_generation(self.path,self.temp)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute(statement);db.commit()
            with self.assertRaises(engine.RecoveryError) as caught:
                engine._verify_sqlite_generation(self.path,self.temp,sealed)
            self.assertEqual(caught.exception.code,'TARGET_CHANGED_REPREPARE_REQUIRED')
        sealed=engine._sqlite_generation(self.path,self.temp)
        replacement=self.directory/'replacement'
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement,self.path)
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,sealed)

    def test_verifier_refuses_new_companion_and_hardlinked_main(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('create table fixture(value text)');db.commit()
        sealed=engine._sqlite_generation(self.path,self.temp)
        journal=Path(str(self.path)+'-journal')
        legacy=dict(sealed,kind='sqlite-consistent-v1')
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,legacy)
        journal.write_bytes(b'')
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,sealed)
        journal.unlink()
        os.link(self.path,self.directory/'alias')
        with self.assertRaises(core.FinixError):
            engine._verify_sqlite_generation(self.path,self.temp,sealed)
        (self.directory/'alias').unlink()
        original=self.directory/'original-main'
        os.replace(self.path,original)
        try: self.path.symlink_to(original)
        except OSError as exc:
            print('MAIN_SYMLINK_CONTROL_UNAVAILABLE',getattr(exc,'winerror',None))
        else:
            with self.assertRaises(core.FinixError):
                engine._verify_sqlite_generation(self.path,self.temp,sealed)
            self.path.unlink()
        os.replace(original,self.path)


class SQLiteLoaderNamespaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        import sys
        name='hermes_dashboard_plugin_finix_finix'
        package=Path(engine.__file__).parent
        spec=importlib.util.spec_from_file_location(name,package/'__init__.py',
            submodule_search_locations=[str(package)])
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module
        spec.loader.exec_module(module)
        import importlib
        cls.alias=importlib.import_module(name+'.default_recovery')
        cls.alias_streaming=importlib.import_module(name+'.streaming')

    def setUp(self):
        import uuid
        self.directory=core.private_dir(Path(os.environ['FINIX_PRODUCER_TEST_ROOT'])/uuid.uuid4().hex)
        self.path=self.directory/'state.db'
        self.temp=core.private_dir(self.directory/'snapshots')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('create table fixture(value text)')
            db.execute("insert into fixture values ('synthetic')");db.commit()

    def test_real_dashboard_loader_and_detached_namespace_seal_identically(self):
        canonical=engine._sqlite_generation(self.path,self.temp)
        dashboard=self.alias._sqlite_generation(self.path,self.temp)
        self.assertEqual(canonical,dashboard)
        engine._verify_sqlite_generation(self.path,self.temp,dashboard)

    def test_changed_producer_body_is_not_hidden_by_loader_stable_identity(self):
        from unittest.mock import patch
        original=self.alias_streaming._sqlite_backup
        canonical=engine._sqlite_generation(self.path,self.temp)
        def changed_backup(*args,**kwargs):
            return original(*args,**kwargs)
        with patch.object(self.alias_streaming,'_sqlite_backup',changed_backup):
            changed=self.alias._sqlite_generation(self.path,self.temp)
        self.assertEqual(set(canonical['producer_pins']['functions']),set(changed['producer_pins']['functions']))
        self.assertNotEqual(canonical['producer_pins']['functions'],changed['producer_pins']['functions'])
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,changed)

