"""No-network synthetic acceptance for explicitly incomplete per-file captures."""
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from finix import core, streaming


class IncompleteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = core.private_dir(self.base / 'profile')
        self.busy = self.home / 'busy'
        self.gone = self.home / 'gone'
        self.busy.write_bytes(b'initial')
        self.gone.write_bytes(b'gone')
        (self.home / 'good').write_bytes(b'exact survivor\x00\xff')
        self.attempts = 0

    @contextmanager
    def races(self):
        scan, payload, read = core.scan, streaming.ordinary_payload, os.read
        def vanished(*args, **kwargs):
            inventory = scan(*args, **kwargs)
            self.gone.unlink(missing_ok=True)
            return inventory
        @contextmanager
        def unstable(fd, parent, leaf, *args, **kwargs):
            if leaf != 'busy':
                with payload(fd, parent, leaf, *args, **kwargs) as result:
                    yield result
                return
            def mutate(target, size):
                position = os.lseek(fd, 0, os.SEEK_CUR)
                chunk = read(target, size)
                if target == fd and position == 0 and chunk and size != 16:
                    self.attempts += 1
                    self.busy.write_bytes(b'changing generation ' + bytes(self.attempts))
                return chunk
            with patch.object(os, 'read', mutate):
                with payload(fd, parent, leaf, *args, **kwargs) as result:
                    yield result
        with patch.object(core, 'scan', vanished), patch.object(streaming, 'ordinary_payload', unstable):
            yield

    def test_local_retention_protects_unknown_legacy_provenance(self):
        recipient = core.keygen(self.base / 'identity')
        previous = core.backup(self.home, self.base / 'out', self.base / 'index', recipient)
        index = core.load_state(self.base / 'index')
        index['backups'][0].pop('complete')
        index['backups'][0].pop('omitted_count')
        core.atomic_json(self.base / 'index/index.json', index)
        core.backup(self.home, self.base / 'out', self.base / 'index', recipient, keep=1)
        self.assertTrue(Path(previous['path']).exists())

    def test_busy_and_vanished_omit_only_failed_files_and_preserve_complete(self):
        recipient = core.keygen(self.base / 'identity')
        previous = core.backup(self.home, self.base / 'out', self.base / 'index', recipient)
        previous_bytes = Path(previous['path']).read_bytes()
        previous_record = core.load_state(self.base / 'index')['backups'][0]
        with self.races():
            result = core.backup(self.home, self.base / 'out', self.base / 'index', recipient, keep=1)
        self.assertIs(result['complete'], False)
        self.assertEqual(result['omitted_count'], 2)
        self.assertEqual(self.attempts, streaming.CAPTURE_ATTEMPTS)
        self.assertNotIn('omitted', result)
        self.assertEqual(Path(previous['path']).read_bytes(), previous_bytes)
        self.assertIn(previous_record, core.load_state(self.base / 'index')['backups'])
        archive = core.decrypt(result['path'], self.base / 'identity')
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            manifest = json.load(tar.extractfile('manifest.json'))
        self.assertEqual(manifest['omitted'], [
            {'path': 'busy', 'code': 'SOURCE_UNSTABLE_CAPTURE'},
            {'path': 'gone', 'code': 'LIVE_SOURCE_MISSING'}])
        self.assertIs(manifest['complete'], False)
        self.assertEqual(set(manifest['files']), {'good'})
        restored = core.restore(result['path'], self.base / 'identity', self.base / 'restored')
        self.assertIs(restored['complete'], False)
        self.assertEqual(restored['omitted_count'], 2)
        report = json.loads((self.base / 'restored' / restored['omission_report']).read_text())
        self.assertEqual(report['omitted'], manifest['omitted'])
        self.assertEqual((self.base / 'restored/good').read_bytes(), b'exact survivor\x00\xff')
        self.assertFalse((self.base / 'restored/busy').exists())
        self.assertFalse((self.base / 'restored/gone').exists())

    def test_resource_io_and_downstream_missing_are_not_softened(self):
        import errno
        import threading
        for failure in (FileNotFoundError('private-stage-missing'), OSError(errno.ENOSPC, 'disk full'), PermissionError('denied')):
            with self.subTest(failure=type(failure).__name__), core.locked(self.home) as fd:
                with patch.object(streaming, 'ordinary_payload', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        streaming.write_archive(fd, io.BytesIO())
        cancel = threading.Event()
        cancel.set()
        with core.locked(self.home) as fd:
            with self.assertRaises(core.FinixError):
                streaming.write_archive(fd, io.BytesIO(), cancel=cancel)

    def test_present_file_corruption_in_incomplete_archive_still_rejected(self):
        with core.locked(self.home) as fd, self.races():
            output = io.BytesIO()
            streaming.write_archive(fd, output)
        corrupt = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(output.getvalue()), mode='r:gz') as source, tarfile.open(fileobj=corrupt, mode='w:gz') as target:
            for info in source:
                data = source.extractfile(info).read()
                if info.name == 'profile/good':
                    data = b'!' * len(data)
                target.addfile(info, io.BytesIO(data))
        with self.assertRaises(core.FinixError):
            core.validate_archive(corrupt.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(core.FinixError):
                streaming.restore_stream(io.BytesIO(corrupt.getvalue()), Path(tmp))

    def test_setup_incomplete_has_no_receipt_or_schedule_and_retains_prior(self):
        from finix.integration import Profile
        from finix.setup_flow import SetupFlow
        recipient = core.keygen(self.base / 'identity')
        profile = Profile(self.home, default_root=self.home)
        profile.recipient(profile.profile_id, recipient, None)
        profile.configure('retention', '1')
        flow = SetupFlow(profile)
        previous = flow.check_and_run()['first_backup']
        with self.races():
            checked = flow.check_and_run()
        self.assertFalse(checked['verified'])
        self.assertEqual(checked['capture'], {'complete': False, 'omitted_count': 2})
        self.assertFalse(flow.receipt_path.exists())
        self.assertTrue(Path(previous['path']).exists())
        self.assertEqual(profile.status()['capture'], checked['capture'])
        profile._prune_local(1)
        self.assertTrue(Path(previous['path']).exists())
        with self.assertRaises(core.FinixError):
            flow.schedule('24h')

    def test_schedule_rejects_persisted_incomplete_with_stale_valid_receipt(self):
        from finix.integration import Profile
        from finix.setup_flow import SetupFlow
        profile = Profile(self.home, default_root=self.home)
        profile.recipient(profile.profile_id, core.keygen(self.base / 'identity'), None)
        flow = SetupFlow(profile)
        flow.check_and_run()
        self.assertTrue(flow.view()['verified'])
        core.atomic_json(profile.root / 'capture-status.json', {'complete': False, 'omitted_count': 1})
        self.assertTrue(flow.receipt_path.exists())
        self.assertFalse(flow.view()['verified'])
        with self.assertRaises(core.FinixError):
            flow.schedule('24h')
        self.assertEqual(profile.config()['interval_hours'], 0)

    def test_cloud_incomplete_readback_disables_every_adapter_retention(self):
        from finix.integration import Profile
        from unittest.mock import Mock
        recipient = core.keygen(self.base / 'identity')
        profile = Profile(self.home, default_root=self.home)
        profile.recipient(profile.profile_id, recipient, None)
        previous = profile.backup()
        for storage in ('drive', 'workspace', 'rclone'):
            with self.subTest(storage=storage):
                cfg = profile.config()
                cfg.update(storage=storage, retention=1, folder_id='synthetic')
                core.atomic_json(profile.root / 'settings.json', cfg)
                self.gone.write_bytes(b'again')
                fake = Mock()
                fake.upload.return_value = {'verified': True}
                def emitted_receipt(path, keep, **kwargs):
                    return {'verified': True, 'storage': 'workspace',
                            'sha256': core.digest_file(Path(path))}
                fake.backup_cipher.side_effect = emitted_receipt
                with patch('finix.drive.Drive', return_value=fake), patch.object(profile, 'workspace', return_value=fake), patch.object(profile, 'rclone', return_value=fake), self.races():
                    result = profile.backup()
                call = fake.upload.call_args if storage == 'drive' else fake.backup_cipher.call_args
                self.assertEqual(call.args[-1], 0)
                self.assertFalse(result['complete'])
                self.assertTrue(Path(previous['path']).exists())

    def test_removed_connector_refuses_before_capture_or_adapter_creation(self):
        from finix.integration import Profile
        profile = Profile(self.home, default_root=self.home)
        cfg = profile.config()
        cfg['storage'] = 'connector-drive'
        core.atomic_json(profile.root / 'settings.json', cfg)
        before = {p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        with patch.object(core, 'backup', side_effect=AssertionError('REMOVED_BACKEND_CAPTURE')), \
                patch.object(profile, 'workspace', side_effect=AssertionError('REMOVED_BACKEND_ADAPTER')):
            with self.assertRaisesRegex(core.FinixError, 'Managed Drive backend removed'):
                profile.backup()
        self.assertEqual({p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}, before)
        self.assertEqual(profile.config()['storage'], 'connector-drive')

    def test_workspace_wrong_readback_hash_still_refused(self):
        from finix.integration import Profile
        from unittest.mock import Mock
        recipient = core.keygen(self.base / 'identity')
        profile = Profile(self.home, default_root=self.home)
        profile.recipient(profile.profile_id, recipient, None)
        cfg = profile.config()
        cfg.update(storage='workspace', retention=1)
        core.atomic_json(profile.root / 'settings.json', cfg)
        fake = Mock()
        fake.backup_cipher.return_value = {'verified': True, 'storage': 'workspace', 'sha256': '0' * 64}
        with patch.object(profile, 'workspace', return_value=fake):
            with self.assertRaisesRegex(core.FinixError, '^PENDING_RECONCILIATION$'):
                profile.backup()
        self.assertEqual(fake.backup_cipher.call_count, 1)

    def test_buffered_archive_marks_incomplete(self):
        with core.locked(self.home) as fd, self.races():
            archive = core.make_archive(fd)
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
            manifest = json.load(tar.extractfile('manifest.json'))
        self.assertIs(manifest['complete'], False)
        self.assertEqual(len(manifest['omitted']), 2)
        self.assertEqual(core.validate_archive(archive), {'good': b'exact survivor\x00\xff'})

    def test_multiroot_omissions_are_scoped_and_all_present_bytes_verified(self):
        extra = core.private_dir(self.base / 'extra')
        (extra / 'good').write_bytes(b'external good')
        with core.locked(self.home) as fd, core.locked(extra) as other, self.races():
            output = io.BytesIO()
            outcome = streaming.write_archive(fd, output, extra_fds=[({'name': 'projects', 'path': str(extra)}, other)])
        self.assertEqual(outcome, {'complete': False, 'omitted_count': 2})
        self.assertEqual(core.validate_archive(output.getvalue()), {
            'profile/good': b'exact survivor\x00\xff', 'external/projects/good': b'external good'})
        stage = core.private_dir(self.base / 'stage')
        payload, count, scope, completion = streaming.restore_stream(io.BytesIO(output.getvalue()), stage)
        self.assertEqual((count, scope), (2, 'bundle'))
        self.assertEqual((payload / 'external/projects/good').read_bytes(), b'external good')

    def test_encrypted_multiroot_restore_reports_scoped_omissions(self):
        extra = core.private_dir(self.base / 'extra')
        (extra / 'good').write_bytes(b'external good')
        (extra / 'gone').write_bytes(b'vanishes')
        recipient = core.keygen(self.base / 'identity')
        core.atomic_json(self.home / 'config.yaml', {'plugins': {'entries': {'finix': {'settings': {
            'extra_sources': [{'name': 'projects', 'path': str(extra)}]}}}}})
        scan = core.scan
        def extra_vanished(fd, *args, **kwargs):
            inventory = scan(fd, *args, **kwargs)
            if core.root_stat(fd).st_ino == extra.stat().st_ino:
                (extra / 'gone').unlink(missing_ok=True)
            return inventory
        import sys
        from types import SimpleNamespace
        with patch.dict(sys.modules, hermes_constants=SimpleNamespace(named_profile_home=lambda path: None)), patch.object(core, 'scan', extra_vanished), self.races():
            result = core.backup(self.home, self.base / 'out', self.base / 'index', recipient,
                                 configured_extras=True, default_root=self.home)
        restored = core.restore(result['path'], self.base / 'identity', self.base / 'restored')
        self.assertEqual((restored['complete'], restored['omitted_count'], restored['layout']), (False, 3, 'bundle'))
        report = json.loads((self.base / 'restored' / restored['omission_report']).read_text())
        self.assertEqual({r['path'] for r in report['omitted']}, {'profile/busy', 'profile/gone', 'external/projects/gone'})
        self.assertEqual((self.base / 'restored/external/projects/good').read_bytes(), b'external good')

    def test_restore_omission_report_never_overwrites_archive_member(self):
        from types import SimpleNamespace
        name = 'finix-omissions-' + 'a' * 32 + '.json'
        (self.home / name).write_bytes(b'user report preserved')
        recipient = core.keygen(self.base / 'identity')
        with self.races():
            result = core.backup(self.home, self.base / 'out', self.base / 'index', recipient)
        choices = ([SimpleNamespace(hex='c' * 32)] if os.name == 'nt' else []) + [SimpleNamespace(hex='a' * 32), SimpleNamespace(hex='b' * 32)]
        with patch('uuid.uuid4', side_effect=choices) as names:
            restored = core.restore(result['path'], self.base / 'identity', self.base / 'restored')
        self.assertEqual(names.call_count, len(choices))
        self.assertEqual((self.base / 'restored' / name).read_bytes(), b'user report preserved')
        self.assertNotEqual(restored['omission_report'], name)
        self.assertEqual(json.loads((self.base / 'restored' / restored['omission_report']).read_text())['omitted_count'], 2)

    def test_invalid_incomplete_annotation_rejected_by_both_readers(self):
        for fields in ({'complete': True, 'omitted': [{'path': 'gone', 'code': 'LIVE_SOURCE_MISSING'}]},
                       {'complete': False, 'omitted': []},
                       {'complete': False, 'omitted': [{'path': '../secret', 'code': 'LIVE_SOURCE_MISSING'}]},
                       {'complete': False, 'omitted': [{'path': 'good', 'code': 'DISK_FULL'}]},
                       {'complete': False, 'omitted': [{'path': 'good', 'code': 'LIVE_SOURCE_MISSING'}]}):
            with self.subTest(fields=fields):
                with core.locked(self.home) as fd:
                    data = core.make_archive(fd)
                out = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as source, tarfile.open(fileobj=out, mode='w:gz') as target:
                    for info in source:
                        content = source.extractfile(info).read()
                        if info.name == 'manifest.json':
                            manifest = json.loads(content)
                            manifest.update(fields)
                            content = json.dumps(manifest).encode()
                        info.size = len(content)
                        target.addfile(info, io.BytesIO(content))
                with self.assertRaises(core.FinixError):
                    core.validate_archive(out.getvalue())
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(core.FinixError):
                        streaming.restore_stream(io.BytesIO(out.getvalue()), Path(tmp))


if __name__ == '__main__':
    unittest.main()
