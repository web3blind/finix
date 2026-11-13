"""Preparation waits for work or explicit cancellation, never a clock."""
import unittest
from unittest.mock import patch
from finix import preview_budget


class PreparationCancellationTests(unittest.TestCase):
    def test_long_pause_without_progress_does_not_expire(self):
        now = [0.0]
        with patch('time.monotonic', side_effect=lambda: now[0]):
            try:
                with preview_budget.scope():
                    now[0] = 86400.0
                    self.assertIsNone(preview_budget.check())
            except preview_budget.PreviewBudgetError as exc:
                self.fail('Elapsed time must not terminate preparation: ' + exc.code)

    def test_cloud_worker_has_no_default_wall_clock_limit(self):
        import inspect
        from finix.workspace_skill_transport import WorkspaceSkillRunner
        self.assertIsNone(inspect.signature(WorkspaceSkillRunner).parameters['timeout'].default)

    def test_rclone_transfer_has_no_default_wall_clock_limit(self):
        import inspect
        from finix.rclone_storage import RcloneRunner
        self.assertIsNone(inspect.signature(RcloneRunner).parameters['transfer_timeout'].default)

    def test_manual_cancellation_still_stops_work(self):
        cancel = [False]
        with self.assertRaises(preview_budget.PreviewBudgetError) as caught:
            with preview_budget.scope(cancelled=lambda: cancel[0]):
                cancel[0] = True
                preview_budget.check()
        self.assertEqual(caught.exception.code, 'PREVIEW_CANCELLED')
        self.assertIsNone(preview_budget.check())

    def test_nested_scope_cannot_hide_parent_cancellation(self):
        cancel = [False]
        with self.assertRaises(preview_budget.PreviewBudgetError) as caught:
            with preview_budget.scope(cancelled=lambda: cancel[0]):
                with preview_budget.scope():
                    cancel[0] = True
                    preview_budget.check()
        self.assertEqual(caught.exception.code, 'PREVIEW_CANCELLED')
        self.assertIsNone(preview_budget.check())


if __name__ == '__main__':
    unittest.main()
