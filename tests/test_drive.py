"""Controlled HTTP protocol; no real account, grant, token or DB is read."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

import httpx
from finix.drive import MAX_CIPHER


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.objects, self.media, self.requests, self.sessions = {}, {}, [], {}
        self.error, self.corrupt, self.put_status = None, False, 200
        self.patch_status = 200
        self.location = None
        self.hostile_listing = False
        self.final_id = None
        self.delete_error_after = None
        self.deleted_count = 0
        self.stale_entries = []
        self.delete_missing = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond('GET')

            def do_POST(self):
                self.respond('POST')

            def do_PATCH(self):
                self.respond('PATCH')

            def do_PUT(self):
                self.respond('PUT')

            def do_DELETE(self):
                self.respond('DELETE')

            def respond(self, method):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.requests.append((method, self.path, self.headers.get('Authorization'), raw,
                                       dict(self.headers)))
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                identifier = parts.path.rsplit('/', 1)[-1]
                status, body, headers = 200, {}, {}
                if outer.error:
                    status = outer.error
                elif method == 'POST':
                    data = json.loads(raw)
                    identifier = 'object-' + str(len(outer.objects) + len(outer.sessions) + 1)
                    data.update(id=identifier, createdTime=f'{len(outer.objects):010}')
                    if parts.path.startswith('/upload/'):
                        outer.sessions[identifier] = data
                        headers['Location'] = outer.location or (
                            'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=' + identifier)
                        body = b''
                    else:
                        outer.objects[identifier] = data
                        body = {'id': identifier}
                elif method == 'PUT':
                    status = outer.put_status
                    if status in (200, 201):
                        identifier = query['upload_id'][0]
                        outer.objects[identifier] = outer.sessions[identifier]
                        outer.media[identifier] = raw
                        body = {'id': outer.final_id or identifier}
                    elif status in (302, 307, 308):
                        headers['Location'] = 'https://evil.example/session-secret'
                elif method == 'PATCH':
                    status = outer.patch_status
                    if status == 200:
                        outer.objects[identifier].update(json.loads(raw))
                        body = dict(outer.objects[identifier])
                elif method == 'DELETE':
                    if outer.delete_error_after is not None and outer.deleted_count >= outer.delete_error_after:
                        status = 403
                    else:
                        outer.objects.pop(identifier, None)
                        outer.deleted_count += 1
                        status = 404 if outer.delete_missing else 204
                elif identifier == 'files':
                    folder = query['q'][0].split("'")[1]
                    entries = [v for v in outer.objects.values()
                               if folder in v.get('parents', []) and (
                                   outer.hostile_listing or (v['appProperties']['kind'] == 'snapshot'
                                   and v['appProperties']['owner'] == 'a' * 64))]
                    body = {'files': [{'id': v['id'], 'createdTime': v['createdTime']}
                                      for v in sorted(entries, key=lambda x: x['createdTime'], reverse=True)]
                                      + outer.stale_entries}
                elif identifier not in outer.objects:
                    status = 404
                elif query.get('alt') == ['media']:
                    body = outer.media[identifier]
                    if outer.corrupt:
                        body = b'corrupt' if outer.corrupt == 'length' else bytes([body[0] ^ 1]) + body[1:]
                else:
                    body = dict(outer.objects[identifier])
                encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
                if status == 204:
                    encoded = b''
                self.send_response(status)
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Content-Type', 'application/octet-stream' if isinstance(body, bytes) else 'application/json')
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(lambda: self.thread.join(timeout=2))
        original_client = httpx.Client
        port = self.server.server_port

        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                url = request.url.copy_with(scheme='http', host='127.0.0.1', port=port)
                rewritten = httpx.Request(request.method, url, headers=request.headers, content=request.content)
                with original_client(timeout=2, trust_env=False) as client:
                    response = client.send(rewritten)
                    return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        self.addCleanup(patch.stopall)
        # Keep production URI validation; rewrite only the established TLS socket
        # transport to controlled loopback, without credentials on the raw PUT.
        import http.client
        original_connection = http.client.HTTPConnection
        patch('http.client.HTTPSConnection', side_effect=lambda host, port, timeout:
              original_connection('127.0.0.1', self.server.server_port, timeout=timeout)).start()
        patch('httpx.Client', side_effect=lambda **kw: original_client(transport=Transport(), **kw)).start()
        plugin_dir = self.root / 'plugins' / 'finix'
        plugin_dir.mkdir(parents=True)
        shutil.copy(Path(__file__).parents[1] / 'plugin.yaml', plugin_dir / 'plugin.yaml')
        shutil.copytree(Path(__file__).parents[1] / 'finix', plugin_dir / 'finix')
        import sys
        import importlib
        package = '_installed_finix_test_' + self.root.name.replace('-', '_')
        spec = importlib.util.spec_from_file_location(package, plugin_dir / 'finix' / '__init__.py',
                          submodule_search_locations=[str(plugin_dir / 'finix')])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
        installed = importlib.import_module(package + '.drive')
        self.error_type = importlib.import_module(package + '.core').FinixError
        patch('hermes_constants.get_hermes_home', return_value=self.root).start()
        patch('hermes_cli.plugin_oauth._plugin_oauth_spec', return_value=(
            frozenset({('https', 'www.googleapis.com', 443)}),
            lambda url: ({'Authorization': 'Bearer synthetic-test-only'}, 'https://www.googleapis.com'))).start()
        self.drive = installed.Drive('a' * 64, sleep=lambda value: None)
        self.folder = self.drive.create_folder()
        self.file = self.root / 'backup.age'
        self.file.write_bytes(bytes(range(256)) * 8193)  # >1 MiB, not JSON/UTF-8

    def test_http_resumable_media_readback_and_scoped_retention(self):
        first = self.drive.upload(self.file, self.folder)
        foreign = self.drive.create('foreign', 'pending', self.folder)
        self.objects[foreign]['appProperties']['owner'] = 'b' * 64
        second = self.drive.upload(self.file, self.folder, keep=1)
        self.assertTrue(second['verified'])
        self.assertEqual(self.drive.download(self.folder, second['id']), self.file.read_bytes())
        self.assertNotIn(first['id'], self.objects)
        self.assertIn(foreign, self.objects)
        puts = [r for r in self.requests if r[0] == 'PUT']
        self.assertEqual(len(puts), 2)
        self.assertTrue(all(r[2] is None and r[3] == self.file.read_bytes() for r in puts))
        self.assertTrue(all(r[2] == 'Bearer synthetic-test-only' for r in self.requests if r[0] != 'PUT'))
        initiations = [r for r in self.requests if r[0] == 'POST' and r[1].startswith('/upload/')]
        self.assertEqual(len(initiations), 2)
        self.assertEqual(initiations[0][4]['X-Upload-Content-Length'], str(self.file.stat().st_size))
        self.assertEqual(initiations[0][4]['X-Upload-Content-Type'], 'application/octet-stream')
        self.assertNotIn('description', self.objects[second['id']])

    def test_corrupt_readback_never_commits_or_prunes(self):
        first = self.drive.upload(self.file, self.folder)
        self.corrupt = True
        before = len(self.requests)
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertIn(first['id'], self.objects)
        self.assertFalse(any(r[0] in ('DELETE', 'PATCH') for r in self.requests[before:]))
        self.assertTrue(any(o['appProperties']['kind'] == 'pending' for o in self.objects.values()))

    def test_commit_failure_never_prunes(self):
        first = self.drive.upload(self.file, self.folder)
        self.patch_status = 403
        before = len(self.requests)
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertIn(first['id'], self.objects)
        self.assertFalse(any(r[0] == 'DELETE' for r in self.requests[before:]))

    def test_short_media_readback_is_rejected(self):
        self.corrupt = 'length'
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertFalse(any(r[0] in ('PATCH', 'DELETE') for r in self.requests))

    def test_cipher_limit_matches_core_age_allowance(self):
        from finix.core import MAX_BYTES
        self.assertEqual(MAX_CIPHER, MAX_BYTES + 1024 * 1024)
        valid = {'appProperties': {'size': str(MAX_CIPHER), 'sha256': 'a' * 64, 'snapshot': 'b' * 32}}
        self.assertEqual(self.drive.descriptor(valid)['size'], MAX_CIPHER)
        valid['appProperties']['size'] = str(MAX_CIPHER + 1)
        with self.assertRaises(self.error_type):
            self.drive.descriptor(valid)

    def test_bounded_errors_no_post_retry_and_no_delete(self):
        self.error = 503
        before = len(self.requests)
        with self.assertRaises(self.error_type):
            self.drive.create_folder()
        self.assertEqual(len(self.requests) - before, 1)
        before = len(self.requests)
        with self.assertRaises(self.error_type):
            self.drive.get(self.folder)
        self.assertEqual(len(self.requests) - before, 3)
        self.assertFalse(any(r[0] == 'DELETE' for r in self.requests))

    def test_bounds_ownership_and_invalid_descriptor(self):
        with self.file.open('wb') as stream:
            stream.truncate(MAX_CIPHER + 1)
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder)
        self.objects[self.folder]['appProperties']['owner'] = 'other'
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder)
        with self.assertRaises(self.error_type):
            self.drive.descriptor({'appProperties': {'size': 'not-an-int'}})

    def test_unsafe_session_uris_never_send_put_or_leak(self):
        base = 'https://www.googleapis.com/upload/drive/v3/files'
        suffix = '?uploadType=resumable&upload_id=session-secret'
        unsafe = ['http://www.googleapis.com/upload/drive/v3/files' + suffix,
                  'https://evil.example/upload/drive/v3/files' + suffix,
                  'https://www.googleapis.com.evil.example/upload/drive/v3/files' + suffix,
                  'https://user@www.googleapis.com/upload/drive/v3/files' + suffix,
                  'https://www.googleapis.com:444/upload/drive/v3/files' + suffix,
                  base + '/other' + suffix, base + suffix + '#fragment',
                  base + suffix + '&upload_id=duplicate', base + suffix + '&other=value',
                  base + '?uploadType=resumable', base + '?uploadType=media&upload_id=secret']
        for uri in unsafe:
            with self.subTest(uri=uri):
                self.location = uri
                before = len(self.requests)
                with self.assertRaises(self.error_type) as caught:
                    self.drive.upload(self.file, self.folder, keep=1)
                self.assertNotIn('session-secret', str(caught.exception))
                self.assertFalse(any(r[0] in ('PUT', 'PATCH', 'DELETE') for r in self.requests[before:]))

    def test_put_failures_and_redirects_do_not_commit_prune_or_follow(self):
        first = self.drive.upload(self.file, self.folder)
        for status in (302, 307, 308, 403, 503):
            self.put_status = status
            before = len(self.requests)
            with self.assertRaises(self.error_type) as caught:
                self.drive.upload(self.file, self.folder, keep=1)
            self.assertNotIn('upload_id', str(caught.exception))
            requests = self.requests[before:]
            self.assertEqual(sum(r[0] == 'PUT' for r in requests), 1)
            self.assertFalse(any(r[0] in ('PATCH', 'DELETE') or 'session-secret' in r[1] for r in requests))
            self.assertIn(first['id'], self.objects)

    def test_wrong_completed_id_is_rejected_before_readback_or_prune(self):
        self.final_id = self.folder
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertFalse(any(r[0] in ('PATCH', 'DELETE') or 'alt=media' in r[1] for r in self.requests))

    def test_hostile_listing_cannot_delete_foreign_file(self):
        foreign = self.drive.create('foreign', 'pending', self.folder)
        self.objects[foreign]['appProperties']['owner'] = 'b' * 64
        self.hostile_listing = True
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertIn(foreign, self.objects)
        self.assertFalse(any(r[0] == 'DELETE' for r in self.requests))

    def test_current_snapshot_pinned_despite_stale_order(self):
        first = self.drive.upload(self.file, self.folder)
        self.objects[first['id']]['createdTime'] = '9999999999'
        second = self.drive.upload(self.file, self.folder, keep=1)
        self.assertNotIn(first['id'], self.objects)
        self.assertIn(second['id'], self.objects)

    def test_partial_retention_restarts_with_stale_deleted_entry(self):
        first = self.drive.upload(self.file, self.folder)
        second = self.drive.upload(self.file, self.folder)
        self.delete_error_after = 1
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        removed = next(identifier for identifier in (first['id'], second['id']) if identifier not in self.objects)
        self.stale_entries = [{'id': removed, 'createdTime': '0'}]
        self.delete_error_after = None
        current = self.drive.upload(self.file, self.folder, keep=1)
        self.assertEqual(set(self.objects), {self.folder, current['id']})

    def test_delete_404_after_owned_metadata_is_reconciled(self):
        first = self.drive.upload(self.file, self.folder)
        self.delete_missing = True
        second = self.drive.upload(self.file, self.folder, keep=1)
        self.assertNotIn(first['id'], self.objects)
        self.assertIn(second['id'], self.objects)

    def test_sdk_refuses_non_https_or_foreign_origin(self):
        before = len(self.requests)
        for url in ['http://www.googleapis.com/drive/v3/files', 'https://evil.example/drive/v3/files']:
            with self.assertRaises(PermissionError):
                self.drive.request('GET', url)
        self.assertEqual(len(self.requests), before)


if __name__ == '__main__':
    unittest.main()
