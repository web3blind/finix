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
        with patch('finix.core.backup', return_value={'path': 'unused'}) as backup:
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

    def test_workspace_helper_file_sinks_and_fixed_media_chunks(self):
        import sys
        from contextlib import redirect_stdout
        from finix import workspace_helper
        source = self.root / 'source.age'
        with source.open('wb') as stream:
            stream.write(b'age-encryption.org/v1\n')
            stream.truncate(129 * 1024 * 1024 + 17)
        script = self.root / 'synthetic_api.py'
        script.write_text('''
from pathlib import Path
class Service:
 def files(self): return self
 def get_media(self, fileId): return fileId
 def create(self, body, media_body, fields):
  assert media_body.chunksize() == 8 * 1024 * 1024
  assert media_body.resumable()
  total = 0
  media_body.stream().seek(0)
  while True:
   block = media_body.stream().read(media_body.chunksize())
   if not block: break
   total += len(block)
  assert total == 129 * 1024 * 1024 + 17
  return self
 def execute(self): return {'id': 'synthetic', 'mimeType': 'application/octet-stream'}
def build_service(*args): return Service()
''')
        target = self.root / 'download.age'
        class Downloader:
            def __init__(self, sink, request, chunksize):
                self.sink, self.path, self.chunk = sink, request, chunksize
                self.offset = 0
                self.assertion = chunksize == 8 * 1024 * 1024
                if not self.assertion:
                    raise AssertionError('Unbounded media chunk')
            def next_chunk(self):
                with open(self.path, 'rb') as stream:
                    stream.seek(self.offset)
                    data = stream.read(self.chunk)
                self.sink.write(data)
                self.offset += len(data)
                return None, self.offset == Path(self.path).stat().st_size
        with patch('resource.setrlimit') as resource_limit, redirect_stdout(io.StringIO()), patch('googleapiclient.http.MediaIoBaseDownload', Downloader):
            with patch.object(sys, 'argv', ['helper', str(script), str(source.stat().st_size), 'download', str(source), '--output', str(target)]):
                self.assertEqual(workspace_helper.main(), 0)
            with patch.object(sys, 'argv', ['helper', str(script), '65536', 'upload', str(source), '--name', 'cipher.age', '--parent', 'folder']):
                self.assertEqual(workspace_helper.main(), 0)
        self.assertEqual(cloud_files.describe(target), cloud_files.describe(source))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIn((source.stat().st_size, source.stat().st_size), [c.args[1] for c in resource_limit.call_args_list])
