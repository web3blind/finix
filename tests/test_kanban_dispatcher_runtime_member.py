"""A live SDK dispatcher lease is not restorable profile data."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

class DispatcherRuntimeMemberTests(unittest.TestCase):
    def test_only_canonical_dispatcher_lease_is_runtime(self):
        from finix import default_recovery as cold
        from finix.recovery_import import RUNTIME_FILES
        self.assertIn('kanban/.dispatcher.lock', RUNTIME_FILES)
        self.assertFalse(cold._member('kanban/.dispatcher.lock'))
        self.assertTrue(cold._member('kanban/tasks.json'))
        self.assertTrue(cold._member('kanban/other.lock'))

    @unittest.skipUnless(os.name == 'nt', 'native Windows dispatcher lease')
    def test_stock_live_dispatcher_reproduces_non_data_read_failure(self):
        from finix import core, default_recovery as cold
        from gateway.kanban_watchers import _acquire_singleton_lock
        with TemporaryDirectory() as folder:
            root=core.private_dir(Path(folder)/'root')
            path=core.private_dir(root/'kanban')/'.dispatcher.lock'
            handle,state=_acquire_singleton_lock(path)
            try:
                self.assertEqual(state,'held')
                self.assertIsNotNone(handle)
                with self.assertRaises(PermissionError):core.digest_file(path)
                self.assertFalse(cold._member('kanban/.dispatcher.lock'))
            finally:handle.close()
