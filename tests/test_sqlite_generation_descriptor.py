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

    def test_sdk_gateway_heartbeat_does_not_invalidate_target_user_data(self):
        # Public SDK heartbeat schema: bookkeeping, not conversation content.
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE fixture(value TEXT);
                INSERT INTO fixture VALUES ('preserved conversation');
                CREATE TABLE gateway_heartbeats (
                    backend_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    profile TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO gateway_heartbeats VALUES ('synthetic',1,10,11,'default','fixture');
            """)
            sealed=engine._sqlite_generation(self.path,self.temp)
            db.execute('UPDATE gateway_heartbeats SET last_heartbeat=12')
            db.commit()
            current=engine._sqlite_generation(self.path,self.temp)
            self.assertNotEqual(sealed['snapshot_sha256'],current['snapshot_sha256'])
            engine._verify_sqlite_generation(self.path,self.temp,sealed)

    @unittest.skipIf(os.name=='nt','POSIX descriptor-capability regression')
    def test_snapshot_proc_fd_path_remains_a_capability_not_lexical_ancestry(self):
        from finix import streaming
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE fixture(value TEXT)');db.commit()
        parent=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            with core.regular_input(self.path) as fd:
                with streaming.sqlite_payload(fd,parent,self.path.name,self.temp,None,None) as (stream,size):
                    self.assertTrue(str(stream.name).startswith('/proc/self/fd/'))
                    self.assertGreater(size,0)
            descriptor=engine._sqlite_generation(self.path,self.temp)
            engine._verify_sqlite_generation(self.path,self.temp,descriptor)
        finally: os.close(parent)

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


class SQLiteHeartbeatPolicyTests(unittest.TestCase):
    def setUp(self):
        SQLiteDescriptorTests.setUp(self)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE fixture(value);
                INSERT INTO fixture VALUES ('preserved');
                CREATE TABLE gateway_heartbeats (
                    backend_id TEXT PRIMARY KEY, pid INTEGER NOT NULL,
                    started_at REAL NOT NULL, last_heartbeat REAL NOT NULL,
                    profile TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '');
                INSERT INTO gateway_heartbeats VALUES ('one',1,10,11,'default','fixture');
            """)

    def test_insert_delete_and_pruning_of_heartbeat_rows_are_runtime_only(self):
        sealed=engine._sqlite_generation(self.path,self.temp)
        for sql in ("INSERT INTO gateway_heartbeats VALUES ('two',2,20,21,'default','fixture')",
                    "DELETE FROM gateway_heartbeats WHERE backend_id='one'",
                    'DELETE FROM gateway_heartbeats'):
            with closing(sqlite3.connect(self.path)) as db:
                db.execute(sql);db.commit()
            engine._verify_sqlite_generation(self.path,self.temp,sealed)
        self.assertIsNotNone(sealed['protected_content_sha256'])
        legacy=dict(sealed,kind='sqlite-consistent-v2-aux-lifecycle')
        legacy.pop('protected_content_sha256')
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,legacy)

    def test_user_rows_rowids_types_nuls_schema_and_versions_stay_protected(self):
        for sql,args in (("UPDATE fixture SET value=?",('prefix\0suffix',)),
                         ("UPDATE fixture SET value=?",('prefix\0different',)),
                         ('UPDATE fixture SET rowid=25',()),
                         ('UPDATE fixture SET value=1',()),
                         ('UPDATE fixture SET value=CAST(1 AS REAL)',()),
                         ('PRAGMA application_id=17',()),('PRAGMA user_version=18',()),
                         ('PRAGMA schema_version=99',()),('CREATE TABLE another(value)',())):
            sealed=engine._sqlite_generation(self.path,self.temp)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute(sql,args);db.commit()
            with self.assertRaises(engine.RecoveryError,msg=sql) as caught:
                engine._verify_sqlite_generation(self.path,self.temp,sealed)
            self.assertEqual(caught.exception.code,'TARGET_CHANGED_REPREPARE_REQUIRED')

    def test_fts_shadow_data_and_without_rowid_values_stay_protected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE VIRTUAL TABLE fixture_fts USING fts5(body)')
            db.execute("INSERT INTO fixture_fts VALUES ('preserved words')")
            db.execute('CREATE TABLE keyed(k TEXT PRIMARY KEY,v BLOB) WITHOUT ROWID')
            db.execute("INSERT INTO keyed VALUES ('key',X'0001')");db.commit()
        sealed=engine._sqlite_generation(self.path,self.temp)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE gateway_heartbeats SET last_heartbeat=12');db.commit()
        engine._verify_sqlite_generation(self.path,self.temp,sealed)
        for sql in ("UPDATE fixture_fts SET body='changed words'", "UPDATE keyed SET v=X'0002'"):
            sealed=engine._sqlite_generation(self.path,self.temp)
            with closing(sqlite3.connect(self.path)) as db: db.execute(sql);db.commit()
            with self.assertRaises(engine.RecoveryError):
                engine._verify_sqlite_generation(self.path,self.temp,sealed)

    def test_streams_large_text_and_blob_and_preserves_nul_suffix(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE large_values(body TEXT,payload BLOB)')
            db.execute('INSERT INTO large_values VALUES (?,zeroblob(?))',('before\0after',12*1024*1024));db.commit()
        sealed=engine._sqlite_generation(self.path,self.temp)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE gateway_heartbeats SET last_heartbeat=12');db.commit()
        engine._verify_sqlite_generation(self.path,self.temp,sealed)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('UPDATE large_values SET body=?',('before\0other',));db.commit()
        with self.assertRaises(engine.RecoveryError):
            engine._verify_sqlite_generation(self.path,self.temp,sealed)

    def test_without_rowid_record_limit_refuses_without_modifying_source(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE oversized(k TEXT PRIMARY KEY,v BLOB) WITHOUT ROWID')
            db.execute("INSERT INTO oversized VALUES ('key',zeroblob(?))",(9*1024*1024,));db.commit()
        with self.assertRaises(core.FinixError):
            engine._sqlite_generation(self.path,self.temp)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT length(v) FROM oversized').fetchone()[0],9*1024*1024)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT count(*) FROM gateway_heartbeats').fetchone()[0],1)

    def test_unknown_heartbeat_schema_and_dependencies_fail_closed(self):
        for sql,undo in (('CREATE TRIGGER heartbeat_trigger AFTER DELETE ON gateway_heartbeats BEGIN DELETE FROM fixture; END','DROP TRIGGER heartbeat_trigger'),
                         ('CREATE TABLE heartbeat_dependency(id TEXT REFERENCES gateway_heartbeats(backend_id))','DROP TABLE heartbeat_dependency'),
                         ('ALTER TABLE gateway_heartbeats ADD COLUMN user_data TEXT','ALTER TABLE gateway_heartbeats DROP COLUMN user_data')):
            with closing(sqlite3.connect(self.path)) as db: db.execute(sql);db.commit()
            with self.assertRaises(engine.RecoveryError,msg=sql): engine._sqlite_generation(self.path,self.temp)
            with closing(sqlite3.connect(self.path)) as db: db.execute(undo);db.commit()
            self.assertIsNotNone(engine._sqlite_generation(self.path,self.temp)['protected_content_sha256'])


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
            db.execute("insert into fixture values ('synthetic')")
            db.execute("CREATE TABLE gateway_heartbeats (backend_id TEXT PRIMARY KEY,pid INTEGER NOT NULL,started_at REAL NOT NULL,last_heartbeat REAL NOT NULL,profile TEXT NOT NULL DEFAULT '',host TEXT NOT NULL DEFAULT '')")
            db.commit()

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

