"""Synthetic, local-only recovery contracts. No cloud commands or real identities."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from finix import core, recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.profile = core.private_dir(self.base / 'profile')
        (self.profile / 'config.yaml').write_text('test: synthetic')
        core.private_dir(self.profile / 'cron')
        (self.profile / 'cron/jobs.json').write_text(json.dumps({'jobs': [{'enabled': True}]}))
        self.db = sqlite3.connect(self.profile / 'state.db')
        self.addCleanup(self.db.close)
        self.db.execute('pragma journal_mode=wal')
        self.db.execute('create table data(value text)')
        self.db.execute("insert into data values ('committed WAL')")
        self.db.commit()
        self.identity = self.base / 'synthetic-identity'
        recipient = core.keygen(self.identity)
        self.output, self.state = self.base / 'output', self.base / 'state'
        self.archive = core.backup(self.profile, self.output, self.state, recipient,
            sqlite_temp_dir=core.private_dir(self.profile / 'cache/finix/sqlite-temp'))

    def service(self, **kwargs):
        from finix.recovery import RecoveryService
        service = RecoveryService(profile_id='synthetic', profile_root=self.profile,
            activation='generation-1', protected_roots=[self.output, self.state], **kwargs)
        self.addCleanup(service.close)
        service.add_local('local', self.output, self.state)
        return service

    def test_local_catalog_real_restore_sqlite_and_paused_cron(self):
        service = self.service()
        catalog = service.catalog('synthetic', 'generation-1')
        self.assertEqual(len(catalog), 1)
        self.assertNotIn(str(self.base), json.dumps(catalog))
        self.assertEqual(catalog, service.catalog('synthetic', 'generation-1'))
        target = self.base / 'restored'
        key_before = self.identity.read_bytes()
        job = service.start(catalog[0]['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, timeout=30)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertNotIn(str(self.base), json.dumps(result))
        with closing(sqlite3.connect(target / 'state.db')) as db:
            self.assertEqual(db.execute('select * from data').fetchall(), [('committed WAL',)])
            self.assertEqual(db.execute('pragma integrity_check').fetchone(), ('ok',))
        jobs = json.loads((target / 'cron/jobs.json').read_text())
        self.assertFalse(jobs['jobs'][0]['enabled'])
        self.assertTrue(result['result']['cron_disabled'])
        self.assertEqual(self.identity.read_bytes(), key_before)
        self.assertEqual(list((self.profile / 'cache/finix/restore').iterdir()), [])

    def test_candidate_handoff_requires_separate_confirmed_host_activation(self):
        service = self.service()
        candidate = service.catalog('synthetic', 'generation-1')[0]
        target = self.base / 'recovered-candidate'
        job = service.start(candidate['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        status = service.wait(job, 30)
        self.assertEqual(status['status'], 'succeeded', status)
        self.assertEqual(status['result']['activation'], 'requires_host_confirmation')
        handoff = service.handoff(job, 'synthetic', 'generation-1')
        self.assertEqual(handoff['profile_root'], str(target))
        self.assertEqual(handoff['bundle_root'], str(target))
        self.assertEqual(handoff['extras_policy'], 'separate_manual_import_only')
        self.assertTrue(handoff['config_present'])
        self.assertTrue(handoff['cron_disabled'])
        self.assertFalse(handoff['activated'])
        self.assertEqual((self.profile / 'config.yaml').read_text(), 'test: synthetic')

    def test_committed_journal_download_and_hash_verification(self):
        from finix.recovery import committed_journal_source
        row = {'id': 'synthetic-remote', 'profile_id': 'synthetic',
               'sha256': self.archive['sha256'], 'size': Path(self.archive['path']).stat().st_size,
               'completion': dict(sha256=self.archive['sha256'], complete=True, omitted_count=0)}
        journal = self.base / 'synthetic-journal.json'
        core.atomic_json(journal, {'profile_id': 'synthetic', 'connection': 'test', 'records': [row]})
        calls = []
        def download(record, output, cancel):
            calls.append(record)
            output.write(Path(self.archive['path']).read_bytes())
        source = committed_journal_source(connection='remote', generation='test-1',
            journal=journal, expected_binding={'profile_id': 'synthetic', 'connection': 'test'},
            validate_record=lambda r: r['profile_id'] == 'synthetic', download=download)
        service = self.service()
        service.add_source(source)
        catalog = service.catalog('synthetic', 'generation-1')
        remote_id = next(k for k, (s, _) in service._catalog.items() if s.connection == 'remote')
        self.assertTrue(next(r for r in catalog if r['id'] == remote_id)['complete'])
        job = service.start(remote_id, identity=self.identity, destination=self.base / 'remote-restore',
                            profile_id='synthetic', activation='generation-1')
        self.assertEqual(service.wait(job, 30)['status'], 'succeeded')
        self.assertEqual(calls, [row])
        core.atomic_json(journal, {'profile_id': 'other-profile', 'connection': 'test', 'records': [row]})
        from finix.recovery import RecoveryError
        with self.assertRaisesRegex(RecoveryError, '^INVALID_SOURCE$'):
            service.catalog('synthetic', 'generation-1')

    def test_deactivation_rejects_reusing_old_activation_token(self):
        from finix.recovery import RecoveryError
        service = self.service()
        service.deactivate()
        with self.assertRaisesRegex(RecoveryError, '^STALE_PROFILE$'):
            service.catalog('synthetic', 'generation-1')

    def test_wrong_key_and_corrupt_cipher_leave_no_target_or_private_stage(self):
        service = self.service()
        candidate = service.catalog('synthetic', 'generation-1')[0]
        wrong_key = self.base / 'wrong-key'
        core.keygen(wrong_key)
        target = self.base / 'failed-restore'
        job = service.start(candidate['id'], identity=wrong_key, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, 30)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'RESTORE_FAILED')
        self.assertFalse(target.exists())
        archive = Path(self.archive['path'])
        data = archive.read_bytes()
        archive.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        job = service.start(candidate['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, 30)
        self.assertEqual(result['error'], 'INTEGRITY_FAILED')
        self.assertNotIn(str(self.base), json.dumps(result))
        self.assertFalse(target.exists())
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_destination_sentinels_empty_target_and_profile_fences(self):
        from finix.recovery import RecoveryError
        other = core.private_dir(self.base / 'other-profile')
        (other / 'sentinel').write_text('keep')
        service = self.service()
        service.protected.append(other)
        candidate = service.catalog('synthetic', 'generation-1')[0]
        for target in [self.profile, self.profile / 'cache/empty', self.output,
                       self.state, other, self.base, Path(self.base.anchor)]:
            with self.subTest(target=target), self.assertRaisesRegex(RecoveryError, '^INVALID_DESTINATION$'):
                service.start(candidate['id'], identity=self.identity, destination=target,
                              profile_id='synthetic', activation='generation-1')
        with self.assertRaisesRegex(RecoveryError, '^STALE_PROFILE$'):
            service.catalog('other', 'generation-1')
        target = core.private_dir(self.base / 'empty')
        job = service.start(candidate['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        self.assertEqual(service.wait(job, 30)['status'], 'succeeded')
        self.assertEqual((other / 'sentinel').read_text(), 'keep')

    def test_cancel_inflight_fences_progress_and_bounds_jobs(self):
        from finix.recovery import CommittedSource, RecoveryError
        import threading
        service = self.service(max_jobs=2)
        entered, release = threading.Event(), threading.Event()
        data = Path(self.archive['path']).read_bytes()
        row = dict(sha256=self.archive['sha256'], size=len(data), id='synthetic')
        def transfer(record, output, cancel):
            entered.set()
            release.wait(5)
            output.write(data)
        service.add_source(CommittedSource('delayed', '1', lambda: [row], transfer))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'delayed')
        target = self.base / 'cancelled'
        job = service.start(candidate, identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        self.assertTrue(entered.wait(5))
        with self.assertRaisesRegex(RecoveryError, '^BUSY$'):
            service.start(candidate, identity=self.identity, destination=target,
                          profile_id='synthetic', activation='generation-1')
        service.cancel(job)
        service.deactivate()
        release.set()
        result = service.wait(job, 30)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['bytes'], 0)
        self.assertFalse(target.exists())
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_journal_generation_change_invalidates_catalog_id(self):
        service = self.service()
        candidate = service.catalog('synthetic', 'generation-1')[0]
        index = core.load_state(self.state)
        index['backups'][0]['created_at'] = 'synthetic changed generation'
        core.atomic_json(self.state / 'index.json', index)
        target = self.base / 'stale-record'
        job = service.start(candidate['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        self.assertEqual(service.wait(job, 30)['error'], 'INVALID_CANDIDATE')
        self.assertFalse(target.exists())

    def test_index_filename_traversal_cannot_read_sentinel(self):
        from finix.recovery import RecoveryError
        service = self.service()
        index = core.load_state(self.state)
        index['backups'][0]['name'] = '../synthetic-identity'
        core.atomic_json(self.state / 'index.json', index)
        with self.assertRaisesRegex(RecoveryError, '^INVALID_SOURCE$'):
            service.catalog('synthetic', 'generation-1')

    def test_incomplete_restore_preserves_omission_report_and_count(self):
        from unittest.mock import patch
        gone = self.profile / 'vanishing-synthetic'
        gone.write_text('omit me')
        scan = core.scan
        def race(*args, **kwargs):
            result = scan(*args, **kwargs)
            gone.unlink(missing_ok=True)
            return result
        key = self.base / 'incomplete-key'
        recipient = core.keygen(key)
        with patch.object(core, 'scan', race):
            archive = core.backup(self.profile, self.output, self.state, recipient,
                sqlite_temp_dir=self.profile / 'cache/finix/sqlite-temp')
        self.assertFalse(archive['complete'])
        service = self.service()
        candidate = next(r for r in service.catalog('synthetic', 'generation-1') if r['complete'] is False)
        target = self.base / 'incomplete'
        job = service.start(candidate['id'], identity=key, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, 30)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(result['result']['complete'])
        self.assertEqual(result['result']['omitted_count'], 1)
        handoff = service.handoff(job, 'synthetic', 'generation-1')
        report = json.loads((target / handoff['omission_report']).read_text())
        self.assertEqual(report['omitted_count'], 1)
        self.assertEqual(report['omitted'][0]['path'], 'vanishing-synthetic')
        self.assertFalse((target / 'vanishing-synthetic').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX permission contract; native ACL backend is separate')
    def test_insecure_existing_stage_root_is_rejected_without_plaintext(self):
        service = self.service()
        core.private_dir(service.stage_root)
        service.stage_root.chmod(0o777)
        candidate = service.catalog('synthetic', 'generation-1')[0]
        target = self.base / 'insecure-stage-target'
        job = service.start(candidate['id'], identity=self.identity, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, 30)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(target.exists())
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_terminal_status_is_published_only_after_private_cleanup(self):
        from unittest.mock import patch
        import threading
        import shutil
        service = self.service()
        candidate = service.catalog('synthetic', 'generation-1')[0]
        entered, release = threading.Event(), threading.Event()
        if os.name == 'nt':
            original = shutil.rmtree
            def cleanup(path, *args, **kwargs):
                if Path(path).parent == service.stage_root:
                    entered.set()
                    self.assertTrue(release.wait(5))
                return original(path, *args, **kwargs)
            context = patch.object(shutil, 'rmtree', cleanup)
        else:
            original = recovery._erase_directory
            def cleanup(fd):
                entered.set()
                self.assertTrue(release.wait(5))
                return original(fd)
            context = patch.object(recovery, '_erase_directory', cleanup)
        with context:
            job = service.start(candidate['id'], identity=self.identity, destination=self.base / 'cleanup',
                                profile_id='synthetic', activation='generation-1')
            try:
                self.assertTrue(entered.wait(5))
                status = service.status(job)
            finally:
                release.set()
            result = service.wait(job, 30)
        self.assertEqual(status['status'], 'running')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_corrupt_remote_cipher_is_rejected_before_decrypt(self):
        from finix.recovery import CommittedSource
        from unittest.mock import patch
        data = Path(self.archive['path']).read_bytes()
        row = dict(sha256=self.archive['sha256'], size=len(data), id='synthetic')
        service = self.service()
        service.add_source(CommittedSource('corrupt', '1', lambda: [row],
            lambda r, out, cancel: out.write(b'x' * len(data))))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'corrupt')
        target = self.base / 'bad-cloud'
        with patch.object(recovery, '_restore_anchored', wraps=recovery._restore_anchored) as decrypt:
            job = service.start(candidate, identity=self.identity, destination=target,
                                profile_id='synthetic', activation='generation-1')
            result = service.wait(job, 30)
            decrypt.assert_not_called()
            # The same probe must observe a valid job at the actual native
            # boundary, otherwise "never decrypts" would be vacuous.
            valid = next(k for k, (s, _) in service._catalog.items() if s.connection == 'local')
            control = service.start(valid, identity=self.identity, destination=self.base / 'decrypt-control',
                profile_id='synthetic', activation='generation-1')
            self.assertEqual(service.wait(control, 30)['status'], 'succeeded')
            decrypt.assert_called_once()
        self.assertEqual(result['error'], 'INTEGRITY_FAILED')
        self.assertFalse(target.exists())

    def test_cancel_during_real_decryption_leaves_no_published_target(self):
        from unittest.mock import patch
        service = self.service()
        candidate = service.catalog('synthetic', 'generation-1')[0]
        original = recovery._restore_anchored
        callbacks = []
        def restore(*args, **kwargs):
            progress = kwargs['progress']
            def cancel_on_progress(value):
                callbacks.append(True)
                service.deactivate()
                progress(value)
            kwargs['progress'] = cancel_on_progress
            return original(*args, **kwargs)
        target = self.base / 'cancel-real'
        with patch.object(recovery, '_restore_anchored', restore):
            job = service.start(candidate['id'], identity=self.identity, destination=target,
                                profile_id='synthetic', activation='generation-1')
            result = service.wait(job, 30)
        self.assertTrue(callbacks)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['bytes'], 0)
        self.assertFalse(target.exists())
        self.assertEqual(list(service.stage_root.iterdir()), [])

    def test_bundle_profile_handoff_never_writes_original_extras(self):
        from finix import streaming
        extra = core.private_dir(self.base / 'original-extras')
        (extra / 'sentinel').write_text('original extra bytes')
        key = self.base / 'bundle-key'
        recipient = core.keygen(key)
        cipher = self.base / 'bundle.age'
        with core.locked(self.profile) as p, core.locked(extra) as e, cipher.open('wb') as stream:
            streaming.write_encrypted_snapshot(p, recipient, stream,
                self.profile / 'cache/finix/sqlite-temp',
                extra_fds=[({'name': 'synthetic', 'path': str(extra)}, e)])
        service = self.service()
        service.protected.append(extra)
        from finix.recovery import CommittedSource
        row = dict(sha256=core.digest_file(cipher), size=cipher.stat().st_size)
        service.add_source(CommittedSource('bundle', '1', lambda: [row],
            lambda r, out, cancel: out.write(cipher.read_bytes())))
        service.catalog('synthetic', 'generation-1')
        candidate = next(k for k, (s, _) in service._catalog.items() if s.connection == 'bundle')
        target = self.base / 'bundle-candidate'
        job = service.start(candidate, identity=key, destination=target,
                            profile_id='synthetic', activation='generation-1')
        result = service.wait(job, 30)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['result']['layout'], 'bundle')
        handoff = service.handoff(job, 'synthetic', 'generation-1')
        self.assertEqual(handoff['profile_root'], str(target / 'profile'))
        self.assertEqual((target / 'external/synthetic/sentinel').read_text(), 'original extra bytes')
        self.assertEqual((extra / 'sentinel').read_text(), 'original extra bytes')

    def test_retained_jobs_are_bounded_and_invalid_identity_never_decrypts(self):
        from finix.recovery import RecoveryError
        from unittest.mock import patch
        import io
        service = self.service(max_jobs=2)
        candidate = service.catalog('synthetic', 'generation-1')[0]
        jobs = []
        with patch.object(recovery, '_restore_anchored', wraps=recovery._restore_anchored) as decrypt:
            for n in range(4):
                job = service.start(candidate['id'], identity=io.BytesIO(b'AGE-PLUGIN-private-path'),
                    destination=self.base / ('invalid-' + str(n)),
                    profile_id='synthetic', activation='generation-1')
                jobs.append(job)
                result = service.wait(job, 30)
                self.assertEqual(result['error'], 'INVALID_IDENTITY')
            decrypt.assert_not_called()
            control = service.start(candidate['id'], identity=self.identity,
                destination=self.base / 'identity-decrypt-control',
                profile_id='synthetic', activation='generation-1')
            self.assertEqual(service.wait(control, 30)['status'], 'succeeded')
            decrypt.assert_called_once()
        self.assertEqual(len(service._jobs), 2)
        with self.assertRaisesRegex(RecoveryError, '^NOT_FOUND$'):
            service.status(jobs[0])
        self.assertEqual(list(service.stage_root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
