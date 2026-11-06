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
        for component in ('store?prod', 'store#prod', 'store%23prod', 'store%3Fprod'):
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
                            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
                            try:
                                archive = core.make_archive(descriptor)
                            finally:
                                os.close(descriptor)
                            self.assertEqual({p.relative_to(source) for p in source.rglob('*')}, before)
                            entries = core.validate_archive(archive)
                            restored = root / 'snapshot.sqlite'
                            restored.write_bytes(entries[relative.as_posix()])
                            with closing(sqlite3.connect(restored)) as connection:
                                self.assertEqual(connection.execute('select value from fixture').fetchall(),
                                                 [('committed WAL marker',)])
                            key = root / 'recovery.key'
                            recipient = core.keygen(key)
                            result = core.backup(source, root / 'copies', root / 'state', recipient)
                            core.restore(result['path'], key, root / 'restored')
                            with closing(sqlite3.connect(root / 'restored' / relative)) as connection:
                                self.assertEqual(connection.execute('select value from fixture').fetchall(),
                                                 [('committed WAL marker',)])
                        finally:
                            db.close()


if __name__ == '__main__':
    unittest.main()
