"""Setup contracts use disposable real age keys; never a user's profile/cloud."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix import core
from finix.integration import Profile
from finix.setup_flow import SetupFlow, parse_period


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'profile'
        self.home.mkdir(mode=0o700)
        (self.home / 'config.yaml').write_text('synthetic: true\n')
        self.key = self.base / 'identity'
        self.recipient = core.keygen(self.key)
        self.profile = Profile(self.home, default_root=self.home)
        self.flow = SetupFlow(self.profile)

    def register_key(self):
        self.profile.recipient(self.profile.profile_id, self.recipient, None)

    def test_strict_periods_and_existing_bounds(self):
        self.assertEqual(parse_period('24h'), 24)
        self.assertEqual(parse_period('7d'), 168)
        self.assertEqual(parse_period('365d'), 8760)
        self.assertEqual(parse_period('8760h'), 8760)
        for value in ('0h', '0d', '1.5h', '-1h', '24', '24H', ' 24h', '01h', '24h\n',
                      '366d', '8761h', '9999999999999999999999h', '* * * * *', True, None):
            with self.subTest(value=value), self.assertRaises(core.FinixError):
                parse_period(value)

    def test_key_public_only_and_immutable_all_expected_variants(self):
        self.assertEqual(self.flow.view()['stage'], 'key')
        self.assertIn('Local only', self.flow.view()['storage_label'])
        self.register_key()
        self.assertTrue(self.flow.key()['recipient_locked'])
        replacement = core.keygen(self.base / 'other-key')
        for expected in (None, self.recipient, replacement):
            with self.assertRaises(core.FinixError):
                self.profile.recipient(self.profile.profile_id, replacement, expected)
        self.profile.recipient(self.profile.profile_id, self.recipient, self.recipient)
        self.assertEqual(self.profile.config()['recipient'], self.recipient)
        self.assertNotIn('AGE-SECRET-KEY', json.dumps(self.flow.key()))

    def test_actual_first_archive_restore_and_schedule_restart(self):
        self.register_key()
        with self.assertRaises(core.FinixError):
            self.flow.schedule('24h')
        checked = self.flow.check_and_run()
        self.assertEqual(checked['stage'], 'schedule')
        self.assertTrue(checked['verified'])
        self.assertFalse(checked['proof']['cloud_readback'])
        archive = Path(checked['first_backup']['path'])
        self.assertTrue(archive.is_file())
        target = self.base / 'restore'
        core.restore(archive, self.key, target)
        self.assertEqual((target / 'config.yaml').read_bytes(), (self.home / 'config.yaml').read_bytes())
        self.assertFalse((target / 'setup-verification.json').exists())
        fresh = SetupFlow(Profile(self.home, default_root=self.home))
        result = fresh.schedule('7d')
        self.assertEqual(result['interval_hours'], 168)
        self.assertEqual(result['stage'], 'complete')
        self.assertTrue(fresh.view()['verified'])
        self.assertTrue(fresh.schedule('24h')['verified'])

    def test_recheck_pauses_existing_schedule_until_new_period(self):
        self.register_key()
        self.flow.check_and_run()
        self.flow.schedule('24h')
        result = self.flow.check_and_run()
        self.assertEqual(result['stage'], 'schedule')
        self.assertEqual(self.profile.config()['interval_hours'], 0)
        self.assertEqual(self.profile.config()['next_run'], 0)

    def test_missing_key_never_creates_receipt(self):
        with self.assertRaises(core.FinixError):
            self.flow.check_and_run()
        self.assertFalse(self.flow.receipt_path.exists())

    def test_changed_storage_invalidates_receipt(self):
        self.register_key()
        self.flow.check_and_run()
        self.profile.configure('storage', 'rclone')
        self.assertEqual(self.flow.view()['stage'], 'cloud')
        self.assertFalse(self.flow.view()['verified'])
        with self.assertRaises(core.FinixError):
            self.flow.schedule('24h')

    def test_changed_extra_sources_invalidates_receipt(self):
        self.register_key()
        self.flow.check_and_run()
        (self.home / 'config.yaml').write_text('plugins:\n  entries:\n    finix:\n      settings:\n        extra_sources:\n          - name: project\n            path: /synthetic/no-access\n')
        self.assertFalse(self.flow.view()['verified'])
        with self.assertRaises(core.FinixError):
            self.flow.schedule('24h')

    def test_non_setup_yaml_changes_do_not_invalidate_receipt(self):
        self.register_key()
        self.flow.check_and_run()
        (self.home / 'config.yaml').write_text('synthetic: updated\n')
        self.assertTrue(self.flow.view()['verified'])

    def test_failed_recheck_revokes_old_success_and_preserves_archive(self):
        self.register_key()
        first = self.flow.check_and_run()['first_backup']['path']
        with patch.object(self.profile, 'backup', side_effect=core.FinixError('synthetic failure')):
            with self.assertRaises(core.FinixError):
                self.flow.check_and_run()
        self.assertTrue(Path(first).exists())
        self.assertFalse(self.flow.view()['verified'])
        with self.assertRaises(core.FinixError):
            self.flow.schedule('24h')

    def test_config_mutation_during_backup_never_certifies(self):
        self.register_key()
        actual = self.profile.backup
        def mutate():
            result = actual()
            self.profile.configure('storage', 'rclone')
            return result
        with patch.object(self.profile, 'backup', side_effect=mutate):
            with self.assertRaises(core.FinixError):
                self.flow.check_and_run()
        self.assertFalse(self.flow.receipt_path.exists())

    def test_yaml_mutation_during_backup_never_certifies(self):
        self.register_key()
        actual = self.profile.backup
        def mutate():
            result = actual()
            (self.home / 'config.yaml').write_text('synthetic: changed-during-io\n')
            return result
        with patch.object(self.profile, 'backup', side_effect=mutate):
            with self.assertRaises(core.FinixError):
                self.flow.check_and_run()
        self.assertFalse(self.flow.receipt_path.exists())

    def test_config_mutation_during_receipt_io_never_schedules(self):
        self.register_key()
        self.flow.check_and_run()
        actual = self.flow._receipt
        def mutate(binding):
            result = actual(binding)
            cfg = self.profile.config()
            cfg['storage'] = 'workspace'
            core.atomic_json(self.profile.root / 'settings.json', cfg)
            return result
        with patch.object(self.flow, '_receipt', side_effect=mutate):
            with self.assertRaises(core.FinixError):
                self.flow.schedule('24h')
        self.assertEqual(self.profile.config()['interval_hours'], 0)

    def test_cloud_backup_failure_no_receipt_or_pruning(self):
        self.register_key()
        first = self.flow.check_and_run()['first_backup']['path']
        self.profile.configure('retention', 1)
        self.profile.configure('storage', 'drive')
        cfg = self.profile.config()
        cfg['folder_id'] = 'synthetic-folder'
        core.atomic_json(self.profile.root / 'settings.json', cfg)
        from unittest.mock import Mock
        drive = Mock()
        drive.upload.side_effect = core.FinixError('synthetic readback failure')
        with patch('finix.drive.Drive', return_value=drive):
            with self.assertRaises(core.FinixError):
                self.flow.check_and_run()
        self.assertTrue(Path(first).exists())
        self.assertFalse(self.flow.view()['verified'])

    def test_cloud_success_uses_actual_full_archive_adapter(self):
        self.register_key()
        self.profile.configure('storage', 'drive')
        from unittest.mock import Mock
        drive = Mock()
        drive.create_folder.return_value = 'synthetic-folder'
        def upload(path, folder, keep, *, completion):
            self.assertEqual(completion['sha256'], core.digest_file(path))
            self.assertIs(completion['complete'], True)
            self.assertEqual(folder, 'synthetic-folder')
            self.assertTrue(Path(path).is_file())
            self.assertGreater(Path(path).stat().st_size, 0)
            return {'synthetic_readback': True}
        drive.upload.side_effect = upload
        with patch('finix.drive.Drive', return_value=drive):
            result = self.flow.check_and_run()
        self.assertTrue(result['verified'])
        self.assertTrue(result['proof']['cloud_readback'])
        drive.upload.assert_called_once()

    def test_refresh_is_stable_explicit_connection_change_invalidates_binding(self):
        self.register_key()
        credential = core.private_dir(self.base / 'credentials') / 'rclone.conf'
        with core.regular_output(credential) as fd:
            os.write(fd, b'synthetic, not a credential')
        # This test exercises metadata binding, not provider discovery. Seed the
        # synthetic settings directly: configure(storage) calls status/rclone.
        cfg = self.profile.config()
        cfg.update(storage='rclone', rclone_config=str(credential),
                   rclone_remote='test', rclone_root='finix')
        core.atomic_json(self.profile.root / 'settings.json', cfg)
        before = self.flow._binding()
        credential.write_text('different synthetic connection')
        self.assertEqual(before, self.flow._binding())
        cfg['rclone_remote'] = 'explicit-different-connection'
        core.atomic_json(self.profile.root / 'settings.json', cfg)
        self.assertNotEqual(before, self.flow._binding())


if __name__ == '__main__':
    unittest.main()
