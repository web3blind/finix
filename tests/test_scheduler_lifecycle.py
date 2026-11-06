"""Synthetic profiles only; deterministic ticks and SDK lifecycle registration."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile, register
from finix.scheduler import Worker
from hermes_constants import (get_hermes_home, set_hermes_home_override,
                              reset_hermes_home_override)
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


class Ticks:
    def __init__(self, worker, count):
        self.worker, self.count, self.waits = worker, count, []

    def wait(self, seconds):
        self.waits.append(seconds)
        self.count -= 1
        return self.count < 0

    def is_set(self):
        return self.count < 0


class SchedulerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profiles' / 'owner'
        self.home.mkdir(parents=True)
        self.other = Path(self.tmp.name) / 'profiles' / 'other'
        self.other.mkdir()
        self.profile = Profile(self.home)

    def configure(self, hours=1, next_run=0):
        cfg = self.profile.config()
        cfg.update(interval_hours=hours, next_run=next_run)
        core.atomic_json(self.profile.root / 'settings.json', cfg)

    def test_captured_profile_scope_and_safe_retries(self):
        self.configure()
        worker = Worker(self.home)
        ticks = Ticks(worker, 2)
        worker.stop_event = ticks
        calls = []

        def backup(profile, scheduled):
            calls.append((profile.home, get_hermes_home(), scheduled))
            raise RuntimeError('must-not-log-secret')

        token = set_hermes_home_override(self.other)
        try:
            with patch.object(Profile, 'backup', backup), self.assertLogs('finix.scheduler') as logs:
                worker.run()
            self.assertEqual(get_hermes_home(), self.other)
        finally:
            reset_hermes_home_override(token)
        self.assertEqual(calls, [(self.home, self.home, True)] * 2)
        self.assertEqual(ticks.waits, [60, 60, 60])
        self.assertNotIn('must-not-log-secret', str(logs.output))

    def test_disabled_future_and_cancelled_do_not_backup(self):
        worker = Worker(self.home)
        with patch.object(Profile, 'backup') as backup:
            for hours, due in [(0, 0), (1, 200)]:
                self.configure(hours, due)
                with patch('finix.scheduler.time.time', return_value=100):
                    worker.check()
            self.configure()
            worker.stop_event.set()
            worker.check()
            backup.assert_not_called()

    def test_no_initialization_for_unconfigured_plugin(self):
        worker = Worker(self.other)
        with patch('finix.scheduler.Profile') as factory:
            worker.check()
        factory.assert_not_called()

    def test_durable_due_state_and_no_catchup_flood(self):
        self.configure()
        worker = Worker(self.home)
        worker.stop_event = Ticks(worker, 3)
        calls = []

        def backup(profile, scheduled):
            calls.append(profile.home)
            cfg = profile.config()
            cfg['next_run'] = 3700
            core.atomic_json(profile.root / 'settings.json', cfg)

        with patch.object(Profile, 'backup', backup), patch('finix.scheduler.time.time', return_value=100):
            worker.run()
            # Reload reads the durable next run, not a per-thread timer.
            Worker(self.home).check()
        self.assertEqual(calls, [self.home])
        self.assertEqual(Profile(self.home).config()['next_run'], 3700)

    def test_sdk_registration_unload_reload_and_command_profile(self):
        payload = self.home / 'plugins' / 'finix'
        payload.mkdir(parents=True)
        manager = PluginManager()
        workers = []
        real_start = Worker.start

        def start(worker):
            workers.append(worker)
            real_start(worker)

        token = set_hermes_home_override(self.home)
        try:
            with patch.object(Worker, 'start', start):
                for _ in range(2):
                    ctx = PluginContext(PluginManifest(name='finix', path=payload), manager)
                    callbacks = []
                    # Exercise real SDK on_unload and its public registration lease.
                    real_on_unload = ctx.on_unload
                    def on_unload(callback):
                        lease = real_on_unload(callback)
                        callbacks.append(lease)
                        return lease
                    ctx.on_unload = on_unload
                    register(ctx)
                    self.assertGreaterEqual(len(callbacks), 1)
                    self.assertTrue(workers[-1].thread.is_alive())
                    for callback in callbacks:
                        callback.dispose()
                    self.assertFalse(workers[-1].thread.is_alive())
            self.assertEqual(len(workers), 2)
            token2 = set_hermes_home_override(self.other)
            try:
                with patch('finix.integration.command', side_effect=lambda *a, **k: get_hermes_home()):
                    from hermes_cli.plugins_dispatch import resolve_plugin_command_result
                    self.assertEqual(resolve_plugin_command_result(manager._plugin_commands['finix']['handler']('status')), self.home)
            finally:
                reset_hermes_home_override(token2)
        finally:
            for worker in workers:
                worker.stop()
            reset_hermes_home_override(token)

    def test_unload_reports_nonpreemptive_drain(self):
        worker = Worker(self.home)
        with patch.object(worker.thread, 'join') as join, patch.object(worker.thread, 'is_alive', return_value=True):
            with self.assertLogs('finix.scheduler', level='WARNING'):
                self.assertFalse(worker.stop())
        join.assert_called_once_with(timeout=1)
        self.assertTrue(worker.stop_event.is_set())


if __name__ == '__main__':
    unittest.main()
