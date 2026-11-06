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

    def test_workspace_http_protocol_rejects_before_body_and_bounds_reads(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from types import SimpleNamespace
        import requests
        from finix import workspace_helper

        fixture = {'mode': 'ok'}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args): pass
            def do_GET(self):
                mode = fixture['mode']
                self.send_response(200 if mode == 'ignored' else 302 if mode == 'redirect' else 206)
                content_range = 'bytes 0-3/4'
                if mode == 'wrong': content_range = 'bytes 1-4/4'
                if mode == 'total': content_range = 'bytes 0-3/5'
                self.send_header('Content-Range', content_range)
                if mode == 'ignored': self.send_header('Content-Length', str(1 << 40))
                if mode == 'length': self.send_header('Content-Length', '5')
                if mode == 'encoding': self.send_header('Content-Encoding', 'gzip')
                if mode == 'redirect': self.send_header('Location', 'https://untrusted.invalid/secret')
                self.end_headers()
                if mode not in {'ignored', 'redirect', 'wrong', 'total', 'encoding', 'length'}:
                    self.wfile.write(b'abc' if mode == 'short' else b'abcde' if mode == 'oversend' else b'abcd')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        credential = object()
        module = SimpleNamespace(get_credentials=lambda: credential)
        test = self
        responses = []
        class Session(requests.Session):
            def __init__(self, received):
                test.assertIs(received, credential)
                super().__init__()
            def get(self, url, **kwargs):
                test.assertEqual(url, 'https://www.googleapis.com/drive/v3/files/synthetic')
                test.assertFalse(self.trust_env)
                test.assertEqual(kwargs['headers']['Range'], 'bytes=0-3')
                test.assertEqual(kwargs['headers']['Accept-Encoding'], 'identity')
                test.assertEqual(kwargs['params'], {'alt': 'media'})
                test.assertTrue(kwargs['stream'])
                test.assertFalse(kwargs['allow_redirects'])
                result = super().get(f'http://127.0.0.1:{server.server_port}/media', **kwargs)
                responses.append(result)
                read = result.raw.read
                def guarded_read(amt=None, *args, **kw):
                    test.assertIn(fixture['mode'], {'ok', 'short', 'oversend'})
                    assert amt is not None
                    test.assertLessEqual(amt, 65536)
                    return read(amt, *args, **kw)
                result.raw.read = guarded_read
                return result

        with patch('google.auth.transport.requests.AuthorizedSession', Session):
            for mode in ('ok', 'ignored', 'redirect', 'wrong', 'total', 'length', 'encoding', 'short', 'oversend'):
                with self.subTest(mode=mode):
                    fixture['mode'] = mode
                    output = self.root / (mode + '.age')
                    if mode == 'ok':
                        workspace_helper._download(module, 'synthetic', str(output), 4)
                        self.assertEqual(output.read_bytes(), b'abcd')
                        with self.assertRaises(FileExistsError):
                            workspace_helper._download(module, 'synthetic', str(output), 4)
                    else:
                        with self.assertRaises(ValueError):
                            workspace_helper._download(module, 'synthetic', str(output), 4)
                        self.assertLessEqual(output.stat().st_size, 4)
                    self.assertTrue(responses[-1].raw.closed)
            before = len(responses)
            for file_id in ('https://untrusted.invalid/file', '../file', 'id?alt=media'):
                with self.assertRaises(ValueError):
                    workspace_helper._download(module, file_id, str(self.root / 'invalid'), 4)
            self.assertEqual(len(responses), before)

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
        with script.open('a') as stream:
            stream.write('\ncredential = object()\ndef get_credentials(): return credential\n')
        credentials = []
        test = self
        class Session:
            def __init__(self, credential):
                credentials.append(credential)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def get(self, url, **kwargs):
                test.assertFalse(self.trust_env)
                test.assertEqual(url, 'https://www.googleapis.com/drive/v3/files/synthetic')
                test.assertTrue(kwargs['stream'])
                test.assertFalse(kwargs['allow_redirects'])
                start, end = map(int, kwargs['headers']['Range'][6:].split('-'))
                test.assertLessEqual(end - start + 1, 8 * 1024 * 1024)
                class Response:
                    status_code = 206
                    headers = {'Content-Range': f'bytes {start}-{end}/{source.stat().st_size}',
                               'Content-Length': str(end - start + 1)}
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def iter_content(self, chunk_size):
                        test.assertLessEqual(chunk_size, 65536)
                        with source.open('rb') as stream:
                            stream.seek(start)
                            remaining = end - start + 1
                            while remaining:
                                block = stream.read(min(chunk_size, remaining))
                                remaining -= len(block)
                                yield block
                return Response()
        # Measure transfer allocations, not one-time optional SDK imports.
        import google.auth.transport.requests
        import googleapiclient.http
        tracemalloc.start()
        try:
            with patch('resource.setrlimit') as resource_limit, redirect_stdout(io.StringIO()), patch('google.auth.transport.requests.AuthorizedSession', Session):
                with patch.object(sys, 'argv', ['helper', str(script), str(source.stat().st_size), 'download', 'synthetic', '--output', str(target)]):
                    self.assertEqual(workspace_helper.main(), 0)
                _, download_peak = tracemalloc.get_traced_memory()
                self.assertLess(download_peak, 2 * 1024 * 1024)
                with patch.object(sys, 'argv', ['helper', str(script), '65536', 'upload', str(source), '--name', 'cipher.age', '--parent', 'folder']):
                    self.assertEqual(workspace_helper.main(), 0)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(credentials), 1)
        self.assertLess(peak, 20 * 1024 * 1024)
        self.assertEqual(cloud_files.describe(target), cloud_files.describe(source))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertIn((source.stat().st_size, source.stat().st_size), [c.args[1] for c in resource_limit.call_args_list])
