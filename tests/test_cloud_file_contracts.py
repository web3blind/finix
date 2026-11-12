"""Bounded ciphertext transport contracts, without credentials or cloud accounts."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from finix import cloud_files, core
from finix.integration import Profile
from finix.rclone_storage import RcloneStorage, RcloneRunner, RcloneError


class CloudFileContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_copy_is_exact_private_bounded_and_removes_partial(self):
        source = self.root / 'source.age'
        source.write_bytes(b'age-encryption.org/v1\n' + b'x' * 4096)
        manifest = cloud_files.describe(source, native=True)
        target = self.root / 'target.age'
        cloud_files.copy(source, target, manifest)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cloud_files.describe(target), manifest)
        for size in (manifest['size'] - 1, manifest['size'] + 1):
            bad = self.root / ('bad' + str(size))
            with self.assertRaises(core.FinixError):
                cloud_files.copy(source, bad, dict(manifest, size=size))
            self.assertFalse(bad.exists())
        with self.assertRaises(FileExistsError):
            cloud_files.copy(source, target, manifest)

    def test_sqlite_temp_profile_validation_status_and_forwarding(self):
        home = self.root / 'profile'
        home.mkdir(mode=0o700)
        encrypted = self.root / 'encrypted-stage'
        encrypted.mkdir(mode=0o700)
        profile = Profile(home)
        for bad in ('relative', str(home), str(self.root)):
            with self.assertRaises(core.FinixError):
                profile.configure('sqlite_temp_dir', bad)
        status = profile.configure('sqlite_temp_dir', str(encrypted))
        self.assertEqual(status['sqlite_temp_dir'], str(encrypted))
        self.assertTrue(status['sqlite_temp']['ready'])
        recipient = core.keygen(self.root / 'identity')
        profile.recipient(profile.profile_id, recipient, None)
        with patch('finix.core.backup', return_value={'path': 'unused', 'complete': True, 'omitted_count': 0}) as backup:
            profile.backup()
        self.assertEqual(backup.call_args.kwargs['sqlite_temp_dir'], str(encrypted))

    @unittest.skipUnless(os.environ.get('FINIX_RCLONE_BINARY'), 'reviewed local-only rclone binary required')
    def test_real_rclone_over_128_mib_file_roundtrip_bounded_python_memory(self):
        home = self.root / 'profile'
        home.mkdir()
        auth = self.root / 'auth'
        auth.mkdir(mode=0o700)
        config = auth / 'rclone.conf'
        config.write_text('[cloud]\ntype = local\n')
        config.chmod(0o600)
        local = self.root / 'remote'
        local.mkdir()
        real = RcloneRunner(home, config, binary=os.environ['FINIX_RCLONE_BINARY'])
        def transport(op, *args, **kwargs):
            if op == 'listremotes':
                rows = json.loads(real(op, *args))
                self.assertEqual(rows[0]['type'], 'local')
                return json.dumps([dict(rows[0], type='yandex')]).encode()
            args = tuple(a.replace('cloud:backups/Finix', 'cloud:' + str(local)) if isinstance(a, str) else a for a in args)
            return real(op, *args, **kwargs)
        adapter = RcloneStorage(home, 'a' * 64, self.root / 'state', str(config), 'cloud', 'backups/Finix', runner=transport)
        source = self.root / 'large.age'
        with source.open('wb') as stream:
            stream.write(b'age-encryption.org/v1\nsynthetic transport fixture\n')
            stream.truncate(129 * 1024 * 1024 + 137)
        expected = cloud_files.describe(source)
        tracemalloc.start()
        try:
            row = adapter.backup_cipher(source)
            result = adapter.download(row, self.root / 'recovered.age')
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(result['verified'])
        self.assertEqual(cloud_files.describe(self.root / 'recovered.age'), expected)
        self.assertLess(peak, 12 * 1024 * 1024)
        self.assertEqual(row['size'], 129 * 1024 * 1024 + 137)

    def test_rclone_cat_rejects_oversend_and_short_download(self):
        home = self.root / 'home'
        home.mkdir()
        auth = self.root / 'auth'
        auth.mkdir(mode=0o700)
        config = auth / 'config'
        config.write_text('[cloud]\ntype=local\n')
        config.chmod(0o600)
        binary = self.root / 'fake-rclone'
        binary.write_text('#!/usr/bin/python3\nimport sys\nsys.stdout.buffer.write(b"x" * 65536)\n')
        binary.chmod(0o700)
        runner = RcloneRunner(home, config, binary=str(binary))
        for expected, code in ((5, 'OUTPUT_LIMIT'), (65537, 'SHORT_DOWNLOAD')):
            sink = io.BytesIO()  # tiny test sink only, production sink is a private file
            with self.assertRaisesRegex(RcloneError, code):
                runner('cat', 'cloud:test', output=sink, expected_size=expected)
            self.assertLessEqual(sink.tell(), expected)

    def test_workspace_http_protocol_rejects_before_body_and_bounds_reads(self):
        from test_workspace_portability import PublicProviderBoundaries
        case=PublicProviderBoundaries('test_range_overflow_short_wrong_range_encoding_and_redirect')
        case.setUp()
        try:
            case.test_range_overflow_short_wrong_range_encoding_and_redirect()
        finally:
            case.doCleanups()

    def test_workspace_public_transport_exclusive_file_sinks_and_ranges(self):
        from test_workspace_portability import PublicProviderBoundaries
        case=PublicProviderBoundaries('test_exclusive_sink_exact_bytes_explicit_range')
        case.setUp()
        try:
            case.test_exclusive_sink_exact_bytes_explicit_range()
        finally:
            case.doCleanups()
