"""Disposable native-filesystem regressions; never uses cloud or user identities."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from test_recovery_service import RecoveryTests
from finix import core, recovery


class AnchorSafety(unittest.TestCase):
    def setUp(self):
        self.f = RecoveryTests('test_local_catalog_real_restore_sqlite_and_paused_cron')
        if os.name == 'nt':
            self.f.setUp()
        else:
            # The legacy backup fixture creates parents through mkdir(parents).
            # Exercise recovery with explicitly private synthetic intermediates;
            # never repair modes inside production admission.
            previous = os.umask(0o077)
            try:
                self.f.setUp()
            finally:
                os.umask(previous)
        self.addCleanup(self.f.doCleanups)

    def run_job(self, service, candidate=None):
        candidate = candidate or service.catalog('synthetic', 'generation-1')[0]['id']
        job = service.start(candidate, identity=self.f.identity,
            destination=self.f.base / 'target', profile_id='synthetic', activation='generation-1')
        status = service.wait(job, 30)
        self.assertNotEqual(status['status'], 'running')
        self.assertNotIn(str(self.f.base), json.dumps(status))
        self.assertNotIn('AGE-SECRET', json.dumps(status))
        return status

    @unittest.skipIf(os.name == 'nt', 'POSIX rename race')
    def test_protected_ancestor_swap_after_destination_validation(self):
        f = self.f
        service = f.service()
        victim = core.private_dir(f.base / 'protected')
        core.private_dir(victim / 'nested')
        service.protected.append(victim)
        outer = core.private_dir(f.base / 'export')
        parent = core.private_dir(outer / 'nested')
        candidate = service.catalog('synthetic', 'generation-1')[0]['id']
        original = service._destination
        calls = []
        def destination(path):
            value = original(path)
            calls.append(1)
            if len(calls) == 2:
                outer.rename(f.base / 'saved-export')
                outer.symlink_to(victim, target_is_directory=True)
            return value
        with patch.object(service, '_destination', destination):
            job = service.start(candidate, identity=f.identity, destination=parent / 'restored',
                profile_id='synthetic', activation='generation-1')
            status = service.wait(job, 30)
        self.assertEqual(len(calls), 2)
        self.assertEqual(status['status'], 'failed', status)
        self.assertFalse((victim / 'nested/restored').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX stage rename')
    def test_stage_rename_cleans_key_cipher_and_partial_payload_before_terminal(self):
        f = self.f
        service = f.service()
        data = Path(f.archive['path']).read_bytes()
        row = dict(sha256=f.archive['sha256'], size=len(data))
        escaped = f.base / 'escaped'
        def transfer(record, output, cancel):
            service.stage_root.rename(escaped)
            output.write(data)
        service.add_source(recovery.CommittedSource('race', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'race')
        status = self.run_job(service, candidate)
        self.assertEqual(status['status'], 'failed', status)
        self.assertEqual(list(escaped.rglob('identity')), [])
        self.assertEqual(list(escaped.rglob('snapshot.age')), [])
        self.assertEqual(list(escaped.rglob('config.yaml')), [])
        self.assertEqual(list(escaped.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX private mode admission')
    def test_insecure_intermediate_is_rejected_before_key_is_read(self):
        import io
        f = self.f
        for relative in ['cache', 'cache/finix', 'cache/finix/restore']:
            with self.subTest(relative=relative):
                service = f.service()
                core.private_dir(service.stage_root)
                directory = f.profile / relative
                directory.chmod(0o777)
                identity = io.BytesIO(f.identity.read_bytes())
                candidate = service.catalog('synthetic', 'generation-1')[0]['id']
                job = service.start(candidate, identity=identity, destination=f.base / ('target-' + relative.replace('/', '-')),
                    profile_id='synthetic', activation='generation-1')
                status = service.wait(job, 30)
                directory.chmod(0o700)
                self.assertEqual(status['status'], 'failed', status)
                self.assertEqual(identity.tell(), 0)

    @unittest.skipIf(os.name == 'nt', 'POSIX owner and permission admission')
    def test_ancestor_permission_matrix_rejects_before_identity_read(self):
        import io
        f = self.f
        service = f.service()
        core.private_dir(service.stage_root)
        directories = [f.profile, f.profile / 'cache', f.profile / 'cache/finix', service.stage_root]
        for directory in directories:
            for mode in (0o775, 0o777, 0o1777) + ((0o755,) if directory == service.stage_root else ()):
                with self.subTest(directory=directory.name, mode=oct(mode)):
                    candidate = service.catalog('synthetic', 'generation-1')[0]['id']
                    identity = io.BytesIO(f.identity.read_bytes())
                    directory.chmod(mode)
                    try:
                        job = service.start(candidate, identity=identity, destination=f.base / 'unsafe-target',
                            profile_id='synthetic', activation='generation-1')
                        status = service.wait(job, 30)
                        self.assertEqual(status['status'], 'failed', status)
                        self.assertEqual(identity.tell(), 0)
                        self.assertEqual(directory.stat().st_mode & 0o7777, mode)
                        self.assertFalse((f.base / 'unsafe-target').exists())
                    finally:
                        directory.chmod(0o700)
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX no-follow ownership anchors')
    def test_foreign_owned_and_symlink_ancestors_are_rejected(self):
        f = self.f
        service = f.service()
        core.private_dir(service.stage_root)
        for directory in [f.profile, f.profile / 'cache', f.profile / 'cache/finix', service.stage_root]:
            with self.subTest(directory=directory.name):
                saved = directory.with_name(directory.name + '-saved')
                directory.rename(saved)
                directory.symlink_to(saved, target_is_directory=True)
                try:
                    with self.assertRaises((OSError, recovery.RecoveryError)):
                        with recovery._stage_area(service):
                            self.fail('symlink stage ancestor admitted')
                finally:
                    directory.unlink()
                    saved.rename(directory)
        if os.geteuid() != 0:
            self.skipTest('actual foreign-owner mutation requires disposable Linux root fixture')
        for directory in [f.profile, f.profile / 'cache', f.profile / 'cache/finix', service.stage_root]:
            with self.subTest(foreign_owner=directory.name):
                os.chown(directory, 65534, -1)
                try:
                    with self.assertRaises(recovery.RecoveryError):
                        with recovery._stage_area(service):
                            self.fail('foreign-owned stage ancestor admitted')
                finally:
                    os.chown(directory, os.geteuid(), -1)
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX retained descriptor revalidation')
    def test_stage_check_revalidates_owned_ancestors_and_private_job(self):
        f = self.f
        service = f.service()
        for directory in (f.profile, f.profile / 'cache', f.profile / 'cache/finix'):
            directory.chmod(0o755)
        with recovery._stage_area(service) as (stage, check):
            check()
            for directory in [f.profile, f.profile / 'cache', f.profile / 'cache/finix', service.stage_root, stage]:
                original = directory.stat().st_mode & 0o7777
                directory.chmod(0o775 if directory != stage else 0o755)
                try:
                    with self.assertRaises(recovery.RecoveryError):
                        check()
                finally:
                    directory.chmod(original)
                check()
            if os.geteuid() == 0:
                for directory in [f.profile, f.profile / 'cache', f.profile / 'cache/finix', service.stage_root, stage]:
                    os.chown(directory, 65534, -1)
                    try:
                        with self.assertRaises(recovery.RecoveryError):
                            check()
                    finally:
                        os.chown(directory, os.geteuid(), -1)
                    check()
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX stage rename cancellation')
    def test_cancel_after_stage_rename_cleans_before_terminal(self):
        import threading
        f = self.f
        service = f.service()
        entered, release = threading.Event(), threading.Event()
        escaped = f.base / 'cancel-escaped'
        row = dict(sha256=f.archive['sha256'], size=1)
        def transfer(record, output, cancel):
            service.stage_root.rename(escaped)
            entered.set()
            self.assertTrue(release.wait(10))
        service.add_source(recovery.CommittedSource('cancel', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'cancel')
        job = service.start(candidate, identity=f.identity, destination=f.base / 'target',
            profile_id='synthetic', activation='generation-1')
        try:
            self.assertTrue(entered.wait(10))
            service.cancel(job)
        finally:
            release.set()
        status = service.wait(job, 30)
        self.assertEqual(status['status'], 'cancelled', status)
        self.assertEqual(list(escaped.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX pinned protected identity')
    def test_pinned_protected_identity_rejects_renamed_non_symlink_alias(self):
        from contextlib import ExitStack
        f = self.f
        victim = core.private_dir(f.base / 'protected-object')
        core.private_dir(victim / 'nested')
        renamed = f.base / 'renamed-protected'
        with ExitStack() as stack:
            pins = recovery._pin_protected(stack, [victim])
            victim.rename(renamed)
            with self.assertRaises(recovery.RecoveryError):
                with recovery._publish_parent(renamed / 'nested/new', [victim], protected_pins=pins):
                    self.fail('Protected physical identity was admitted')
        self.assertFalse((renamed / 'nested/new').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor cleanup')
    def test_cleanup_never_follows_payload_symlink_to_sentinel(self):
        f = self.f
        service = f.service()
        victim = core.private_dir(f.base / 'sentinel-dir')
        (victim / 'keep').write_text('KEEP')
        data = Path(f.archive['path']).read_bytes()
        row = dict(sha256=f.archive['sha256'], size=len(data))
        def transfer(record, output, cancel):
            job_stage = next(service.stage_root.iterdir())
            (job_stage / 'payload-alias').symlink_to(victim, target_is_directory=True)
            output.write(b'x' * len(data))
        service.add_source(recovery.CommittedSource('symlink', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'symlink')
        status = self.run_job(service, candidate)
        self.assertEqual(status['error'], 'INTEGRITY_FAILED', status)
        self.assertEqual((victim / 'keep').read_text(), 'KEEP')
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX descriptor cleanup')
    def test_cleanup_failure_is_not_reported_as_successful_cancellation(self):
        service = self.f.service()
        original = recovery._erase_directory
        def cleanup(fd):
            original(fd)
            service.deactivate()
            raise OSError('synthetic private diagnostic, must not escape')
        with patch.object(recovery, '_erase_directory', cleanup):
            status = self.run_job(service)
        self.assertEqual(status['status'], 'failed', status)
        self.assertEqual(status['error'], 'RESTORE_FAILED')
        self.assertNotIn('synthetic private diagnostic', json.dumps(status))

    @unittest.skipIf(os.name == 'nt', 'POSIX anchored payload cleanup')
    def test_rename_during_partial_decryption_cleans_plaintext(self):
        from finix import streaming
        f = self.f
        service = f.service()
        escaped = f.base / 'partial-escaped'
        original = streaming.report
        moved = []
        def progress(*args, **kwargs):
            if not moved:
                service.stage_root.rename(escaped)
                moved.append(True)
            return original(*args, **kwargs)
        with patch.object(streaming, 'report', progress):
            status = self.run_job(service)
        self.assertTrue(moved)
        self.assertEqual(status['status'], 'failed', status)
        self.assertEqual(list(escaped.iterdir()), [])
        self.assertFalse((f.base / 'target').exists())

    def test_cancellation_during_real_decryption_cleans_before_terminal(self):
        f = self.f
        service = f.service()
        original = recovery._restore_anchored
        callbacks = []
        def restore(*args, **kwargs):
            progress = kwargs['progress']
            def cancel(value):
                callbacks.append(True)
                service.deactivate()
                progress(value)
            kwargs['progress'] = cancel
            return original(*args, **kwargs)
        with patch.object(recovery, '_restore_anchored', restore):
            status = self.run_job(service)
        self.assertTrue(callbacks)
        self.assertEqual(status['status'], 'cancelled', status)
        self.assertEqual(list(service.stage_root.iterdir()), [])
        self.assertFalse((f.base / 'target').exists())

    def test_terminal_status_waits_for_native_private_cleanup(self):
        import threading
        import shutil
        f = self.f
        service = f.service()
        entered, release = threading.Event(), threading.Event()
        if os.name == 'nt':
            original = shutil.rmtree
            def cleanup(path, *args, **kwargs):
                if Path(path).parent == service.stage_root:
                    entered.set()
                    self.assertTrue(release.wait(10))
                return original(path, *args, **kwargs)
            context = patch.object(shutil, 'rmtree', cleanup)
        else:
            original = recovery._erase_directory
            def cleanup(fd):
                entered.set()
                self.assertTrue(release.wait(10))
                return original(fd)
            context = patch.object(recovery, '_erase_directory', cleanup)
        candidate = service.catalog('synthetic', 'generation-1')[0]['id']
        with context:
            job = service.start(candidate, identity=f.identity, destination=f.base / 'target',
                profile_id='synthetic', activation='generation-1')
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(service.status(job)['status'], 'running')
            finally:
                release.set()
            status = service.wait(job, 30)
        self.assertEqual(status['status'], 'succeeded', status)
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'native Windows no-delete-share holding')
    def test_windows_stage_ancestors_remain_held_during_transfer(self):
        f = self.f
        service = f.service()
        data = Path(f.archive['path']).read_bytes()
        row = dict(sha256=f.archive['sha256'], size=len(data))
        attempts = []
        def transfer(record, output, cancel):
            for path in [service.stage_root, service.stage_root.parent, f.profile / 'cache', f.profile]:
                try:
                    path.rename(f.base / ('escaped-' + path.name))
                except OSError:
                    attempts.append(True)
                else:
                    self.fail('Native stage ancestor was not held')
            output.write(data)
        service.add_source(recovery.CommittedSource('held', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'held')
        status = self.run_job(service, candidate)
        self.assertEqual(status['status'], 'succeeded', status)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX whole-profile ancestor rename')
    def test_profile_ancestor_rename_still_removes_private_stage(self):
        f = self.f
        service = f.service()
        data = Path(f.archive['path']).read_bytes()
        row = dict(sha256=f.archive['sha256'], size=len(data))
        escaped = f.base / 'moved-profile'
        def transfer(record, output, cancel):
            f.profile.rename(escaped)
            output.write(data)
        service.add_source(recovery.CommittedSource('profile-move', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'profile-move')
        status = self.run_job(service, candidate)
        self.assertEqual(status['status'], 'failed', status)
        self.assertEqual(list((escaped / 'cache/finix/restore').iterdir()), [])

    def test_destination_content_raced_in_before_publication_is_preserved(self):
        f = self.f
        service = f.service()
        target = core.private_dir(f.base / 'target')
        original = recovery._restore_anchored
        def restore(*args, **kwargs):
            result = original(*args, **kwargs)
            (target / 'sentinel').write_text('KEEP')
            return result
        with patch.object(recovery, '_restore_anchored', restore):
            status = self.run_job(service)
        self.assertEqual(status['error'], 'INVALID_DESTINATION', status)
        self.assertEqual((target / 'sentinel').read_text(), 'KEEP')
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_atomic_destination_substitution_never_overwrites_sentinel(self):
        f = self.f
        service = f.service()
        original = core.publish_directory
        target = f.base / 'target'
        def publish(stage, destination):
            if Path(stage).name == 'bundle':
                target.mkdir()
                (target / 'sentinel').write_text('KEEP')
            return original(stage, destination)
        with patch.object(core, 'publish_directory', publish):
            status = self.run_job(service)
        self.assertEqual(status['status'], 'failed', status)
        self.assertEqual((target / 'sentinel').read_text(), 'KEEP')
        self.assertEqual(list(service.stage_root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX ordinary ancestor modes')
    def test_ordinary_0755_ancestors_real_restore_and_private_live_stage(self):
        import sqlite3
        import threading
        from contextlib import closing
        f = self.f
        # Deliberately reproduce umask022 products, regardless of runner umask.
        ancestors = [f.base, f.profile, f.profile / 'cache', f.profile / 'cache/finix']
        for directory in ancestors:
            directory.chmod(0o755)
        for existing in (False, True):
            with self.subTest(existing_restore=existing):
                service = f.service()
                if existing:
                    core.private_dir(service.stage_root)
                else:
                    self.assertFalse(service.stage_root.exists())
                data = Path(f.archive['path']).read_bytes()
                row = dict(sha256=f.archive['sha256'], size=len(data))
                entered, release = threading.Event(), threading.Event()
                def transfer(record, output, cancel):
                    entered.set()
                    self.assertTrue(release.wait(10))
                    output.write(data)
                service.add_source(recovery.CommittedSource('ordinary', '1', lambda: [row], transfer))
                service.catalog('synthetic', 'generation-1')
                candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'ordinary')
                target = f.base / ('ordinary-' + str(existing))
                job = service.start(candidate, identity=f.identity, destination=target,
                    profile_id='synthetic', activation='generation-1')
                try:
                    self.assertTrue(entered.wait(10), service.status(job))
                    stage, = service.stage_root.iterdir()
                    for directory in (service.stage_root, stage):
                        self.assertEqual(directory.stat().st_mode & 0o7777, 0o700)
                    self.assertEqual((stage / 'identity').stat().st_mode & 0o7777, 0o600)
                finally:
                    release.set()
                status = service.wait(job, 30)
                self.assertEqual(status['status'], 'succeeded', status)
                with closing(sqlite3.connect(target / 'state.db')) as db:
                    self.assertEqual(db.execute('select * from data').fetchall(), [('committed WAL',)])
                    self.assertEqual(db.execute('pragma integrity_check').fetchone(), ('ok',))
                self.assertEqual(list(service.stage_root.iterdir()), [])
                self.assertEqual(list(target.rglob('identity')), [])
                for directory in ancestors:
                    self.assertEqual(directory.stat().st_mode & 0o7777, 0o755)

    def test_native_success_leaves_only_published_payload(self):
        service = self.f.service()
        status = self.run_job(service)
        self.assertEqual(status['status'], 'succeeded', status)
        self.assertEqual(list(service.stage_root.iterdir()), [])
        self.assertTrue((self.f.base / 'target/config.yaml').is_file())
        self.assertEqual(list((self.f.base / 'target').rglob('identity')), [])
