"""Snapshot filesystem names must not acquire SQLite URI semantics."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from finix import core


class SQLiteUriPathTests(unittest.TestCase):
    def test_reserved_names_roundtrip_without_source_creation(self):
        # '?' cannot be a native Windows filesystem name; encoded '?' still
        # exercises URI escaping there. Windows invalid-name tests cover rejection.
        components = ('store#prod', 'store%23prod', 'store%3Fprod') if os.name=='nt' else ('store?prod', 'store#prod', 'store%23prod', 'store%3Fprod')
        for component in components:
            for in_directory in (False, True):
                with self.subTest(component=component, in_directory=in_directory):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        source = root / 'profile'
                        source.mkdir()
                        relative = Path(component) / 'database' if in_directory else Path(component)
                        path = source / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        db = sqlite3.connect(path)
                        try:
                            db.execute('pragma journal_mode=WAL')
                            db.execute('create table fixture(value text)')
                            db.execute('insert into fixture values (?)', ('committed WAL marker',))
                            db.commit()
                            before = {p.relative_to(source) for p in source.rglob('*')}
                            with core.locked(source) as descriptor:
                                archive = core.make_archive(descriptor)
                            self.assertEqual({p.relative_to(source) for p in source.rglob('*')}, before)
                            entries = core.validate_archive(archive)
                            restored = root / 'snapshot.sqlite'
                            restored.write_bytes(entries[relative.as_posix()])
                            with closing(sqlite3.connect(restored)) as connection:
                                self.assertEqual(connection.execute('select value from fixture').fetchall(),
                                                 [('committed WAL marker',)])
                            key = root / 'recovery.key'
                            recipient = core.keygen(key)
                            result = core.backup(source, root / 'copies', root / 'state', recipient,
                                sqlite_temp_dir=core.private_dir(source/'cache/finix/sqlite-temp'))
                            core.restore(result['path'], key, root / 'restored')
                            with closing(sqlite3.connect(root / 'restored' / relative)) as connection:
                                self.assertEqual(connection.execute('select value from fixture').fetchall(),
                                                 [('committed WAL marker',)])
                        finally:
                            db.close()


if __name__ == '__main__':
    unittest.main()
