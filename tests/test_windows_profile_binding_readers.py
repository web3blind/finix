"""Real NTFS concurrent Profile readers; no SDK or live-profile substitution."""
import contextlib
import os
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch
from finix import core, integration


@unittest.skipUnless(os.name == 'nt', 'Windows native binding')
class BindingReaderTests(unittest.TestCase):
    def test_concurrent_readers_wait_for_same_process_binding_resolution(self):
        root = core.private_dir(Path(os.environ['TMPDIR']) / ('finix-binding-readers-' + uuid.uuid4().hex))
        home = core.private_dir(root / 'home')
        expected = integration._windows_profile_binding(home)
        original = core.locked
        held, release, started, finished = (threading.Event() for _ in range(4))
        results, errors = [], []

        @contextlib.contextmanager
        def controlled(path):
            with original(path) as anchor:
                if threading.current_thread().name == 'first-binding-reader':
                    held.set()
                    if not release.wait(5):
                        raise AssertionError('fixture release missing')
                yield anchor

        def read(second=False):
            if second: started.set()
            try:
                results.append(integration._windows_profile_binding(home))
            except BaseException as error:
                errors.append(error)
            finally:
                if second: finished.set()

        first = threading.Thread(target=read, name='first-binding-reader')
        second = threading.Thread(target=lambda: read(True), name='second-binding-reader')
        with patch.object(core, 'locked', side_effect=controlled):
            try:
                first.start()
                self.assertTrue(held.wait(5))
                second.start()
                self.assertTrue(started.wait(5))
                self.assertFalse(finished.wait(.25), 'ordinary second reader failed instead of waiting')
            finally:
                release.set()
                first.join(5)
                if second.ident is not None: second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [expected, expected])


if __name__ == '__main__': unittest.main()
