"""Real preparation callbacks retain cancellation, without a clock limit."""
import os
import sqlite3
import unittest
import uuid
from pathlib import Path

from finix import core, streaming, preview_budget


class PreparationCancellationIntegrationTests(unittest.TestCase):
    def test_sqlite_busy_can_wait_and_remains_manually_cancellable(self):
        active = [False]
        class Source:
            def execute(self, sql): return self
            def fetchone(self): return (4096,)
            def backup(self, destination, *, pages, progress, sleep):
                for _ in range(3):
                    progress(sqlite3.SQLITE_BUSY, 1, 1)
                progress(sqlite3.SQLITE_DONE, 0, 1)
        class Destination:
            def execute(self, sql): return self
        with preview_budget.scope(cancelled=lambda: active[0]):
            streaming._sqlite_backup(Source(), Destination(), None, None)
            active[0] = True
            with self.assertRaises(preview_budget.PreviewBudgetError) as caught:
                streaming._sqlite_backup(Source(), Destination(), None, None)
            self.assertEqual(caught.exception.code, 'PREVIEW_CANCELLED')
            active[0] = False

    def test_copy_and_hash_observe_manual_cancellation(self):
        from finix import default_recovery as cold
        from finix.recovery_activation import profile_digest
        root = core.private_dir(Path(os.environ['FINIX_PREVIEW_TEST_ROOT']) / uuid.uuid4().hex)
        source = root / 'input'
        with core.regular_output(source) as fd: os.write(fd, b'synthetic')
        for work in (lambda: core.digest_file(source),
                     lambda: cold._copy(source, root / 'output'),
                     lambda: profile_digest(root)):
            active = [False]
            with self.assertRaises(preview_budget.PreviewBudgetError) as caught:
                with preview_budget.scope(cancelled=lambda: active[0]):
                    active[0] = True
                    work()
            self.assertEqual(caught.exception.code, 'PREVIEW_CANCELLED')
        self.assertIsNone(preview_budget.check())

    def test_public_prepare_checks_cancellation_before_work(self):
        from finix.default_recovery_api import DefaultRecoveryAPI
        active = [False]
        with self.assertRaises(preview_budget.PreviewBudgetError):
            with preview_budget.scope(cancelled=lambda: active[0]):
                active[0] = True
                DefaultRecoveryAPI().prepare(None, None, 'b' * 32, consent=True)


if __name__ == '__main__': unittest.main()
