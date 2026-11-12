"""No live SDK/root; modeled SQLite BUSY exercises the real callback."""
import contextlib
import sqlite3
import unittest
from unittest.mock import patch
from finix import streaming


class BudgetTests(unittest.TestCase):
    def test_default_preview_busy_callbacks_observe_operation_deadline(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('finix.preview_budget'), 'missing preview operation budget')
        from finix import preview_budget
        now = [0.0]
        class Source:
            def execute(self, sql): return self
            def fetchone(self): return (4096,)
            def backup(self, destination, *, pages, progress, sleep):
                for _ in range(3):
                    now[0] += 16
                    progress(sqlite3.SQLITE_BUSY, 1, 1)
        class Destination:
            def execute(self, sql): return self
        with patch.object(preview_budget.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(Exception) as caught:
                with preview_budget.scope(seconds=20):
                    streaming._sqlite_backup(Source(), Destination(), None, None)
        self.assertEqual(getattr(caught.exception, 'code', None), 'PREVIEW_DEADLINE')
        self.assertLessEqual(now[0], 32)

    def test_public_api_enters_budget_before_prepare_live(self):
        from finix import default_recovery_api as api, preview_budget
        from types import SimpleNamespace
        from pathlib import Path
        now=[0.0]
        def busy(*args, **kwargs):
            now[0]=25
            preview_budget.check()
            self.fail('public prepare omitted budget')
        profile=SimpleNamespace(profile_id='a'*64)
        with patch.object(api,'authority',return_value=Path('synthetic')), patch.object(api,'activation',return_value='epoch'), patch.object(api.cold,'verified_import'), patch.object(api.lifecycle,'prepare_live',side_effect=busy), patch.object(preview_budget.time,'monotonic',side_effect=lambda:now[0]):
            with self.assertRaises(Exception) as caught:
                api.DefaultRecoveryAPI().prepare(profile,object(),'b'*32,consent=True)
        self.assertEqual(getattr(caught.exception,'code',None),'PREVIEW_DEADLINE')

    def test_copy_and_hash_phases_observe_cancelled_preview_budget(self):
        import os, uuid
        from pathlib import Path
        from finix import core, preview_budget, default_recovery as cold
        from finix.recovery_activation import profile_digest
        root=core.private_dir(Path(os.environ['FINIX_PREVIEW_TEST_ROOT'])/uuid.uuid4().hex)
        source=root/'input';source.write_bytes(b'synthetic')
        for function in (lambda:core.digest_file(source),lambda:cold._copy(source,root/'output'),lambda:profile_digest(root)):
            active=[False]
            with self.assertRaises(preview_budget.PreviewBudgetError):
                with preview_budget.scope(cancelled=lambda:active[0]):
                    active[0]=True
                    function()
                    active[0]=False

    def test_scope_is_not_leaked_to_ordinary_backup(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('finix.preview_budget'), 'missing preview operation budget')
        from finix import preview_budget
        with self.assertRaises(Exception):
            with preview_budget.scope(seconds=0): preview_budget.check()
        preview_budget.check()


if __name__ == '__main__': unittest.main()
