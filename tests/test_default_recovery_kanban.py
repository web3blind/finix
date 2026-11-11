"""Actual pinned SDK producer, private synthetic roots only."""
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import unittest

class EmptyKanbanTests(unittest.TestCase):
    def test_actual_producer_empty_admitted_and_nonempty_unknown_refused(self):
        from hermes_constants import get_default_hermes_root
        from hermes_cli.profiles import get_profile_dir
        from hermes_cli import kanban_db_connect as kanban_db
        from finix import core
        from finix.default_recovery_lifecycle import preflight
        from finix.default_recovery import RecoveryError
        root=get_default_hermes_root()
        scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        self.assertEqual(root,scope/'sdk-root')
        self.assertEqual(root,get_profile_dir('default'))
        core.private_dir(root)
        core.atomic_json(root/'config.yaml',{})
        path=root/'kanban.db'
        with closing(kanban_db.connect(path)) as db:
            self.assertEqual(db.execute('select count(*) from tasks').fetchone()[0],0)
        before=path.read_bytes()
        self.assertIn(str(root),preflight(root))
        self.assertEqual(path.read_bytes(),before)
        with closing(sqlite3.connect(path)) as db:
            db.execute("insert into tasks(id,title,status,created_at) values('sentinel','never run','todo',0)")
            db.commit()
        with self.assertRaises(RecoveryError) as caught:preflight(root)
        self.assertEqual(caught.exception.code,'DEFAULT_AUTORUN_NOT_SUPPORTED')
        with closing(sqlite3.connect(path)) as db:
            db.execute('delete from tasks');db.execute('create table unknown(value)');db.commit()
        with self.assertRaises(RecoveryError):preflight(root)
        with closing(sqlite3.connect(path)) as db:
            db.execute('drop table unknown');db.execute('create trigger unknown after insert on tasks begin select 1; end');db.commit()
        with self.assertRaises(RecoveryError):preflight(root)
        with closing(sqlite3.connect(path)) as db:
            db.execute('drop trigger unknown');db.commit()
        self.assertIn(str(root),preflight(root))

    def test_committed_wal_tasks_are_not_ignored(self):
        from finix import core
        from finix.default_recovery_lifecycle import _empty_kanban
        from hermes_cli.kanban_db_connect import connect
        scope=Path(os.environ['FINIX_DEFAULT_TEST_SCOPE'])
        path=core.private_dir(scope/'wal-synthetic')/'kanban.db'
        with closing(connect(path)) as db:
            db.execute('pragma wal_checkpoint(truncate)')
            db.execute('pragma wal_autocheckpoint=0')
            before=path.read_bytes()
            db.execute("insert into tasks(id,title,status,created_at) values('wal-task','never run','todo',0)")
            db.commit()
            self.assertEqual(path.read_bytes(),before)
            self.assertGreater(Path(str(path)+'-wal').stat().st_size,0)
            with self.assertRaises(ValueError):_empty_kanban(path)

    def test_active_sibling_board_refuses_before_stop(self):
        from hermes_constants import get_default_hermes_root
        from finix import core
        from finix.default_recovery_lifecycle import preflight
        from finix.default_recovery import RecoveryError
        from hermes_cli.kanban_db_connect import connect
        root=get_default_hermes_root()
        core.private_dir(root)
        sibling=core.private_dir(root/'profiles/kanban-sentinel')
        core.atomic_json(sibling/'config.yaml',{})
        with closing(connect(sibling/'kanban.db')) as db:
            db.execute("insert into tasks(id,title,status,created_at) values('sibling-task','never run','todo',0)")
            db.commit()
        try:
            with self.assertRaises(RecoveryError) as caught:preflight(root)
            self.assertEqual(caught.exception.code,'SIBLING_AUTORUN_NOT_SUPPORTED')
        finally:
            import shutil
            shutil.rmtree(sibling)  # Exact synthetic profile created by this test.

