"""Actual HTTP protocol fixture; no real account, grant, token or DB is read."""
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
from finix.drive import Drive, MAX_CIPHER
from finix.core import FinixError


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.objects, self.requests = {}, []
        self.error, self.corrupt = None, False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond('GET')

            def do_POST(self):
                self.respond('POST')

            def do_DELETE(self):
                self.respond('DELETE')

            def respond(self, method):
                outer.requests.append((method, self.path, self.headers.get('Authorization')))
                parts = urlsplit(self.path)
                identifier = parts.path.rsplit('/', 1)[-1]
                status, body = 200, {}
                if outer.error:
                    status = outer.error
                elif method == 'POST':
                    raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    data = json.loads(raw)
                    identifier = 'object-' + str(len(outer.objects) + 1)
                    data.update(id=identifier, createdTime=f'{len(outer.objects):010}')
                    outer.objects[identifier] = data
                    body = {'id': identifier}
                elif method == 'DELETE':
                    outer.objects.pop(identifier, None)
                    status = 204
                elif identifier == 'files':
                    query = parse_qs(parts.query)['q'][0]
                    folder = query.split("'")[1]
                    entries = [v for v in outer.objects.values()
                               if folder in v.get('parents', []) and v['appProperties']['kind'] == 'snapshot'
                               and v['appProperties']['owner'] == 'a' * 64]
                    body = {'files': [{'id': v['id'], 'createdTime': v['createdTime']}
                                      for v in sorted(entries, key=lambda x: x['createdTime'], reverse=True)]}
                elif identifier not in outer.objects:
                    status = 404
                else:
                    body = dict(outer.objects[identifier])
                    if outer.corrupt and body['appProperties']['kind'] == 'part':
                        body['description'] = 'AAAA'
                encoded = json.dumps(body).encode() if status != 204 else b''
                self.send_response(status)
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(lambda: self.thread.join(timeout=2))
        # Real SDK helper, real HTTP socket; transport alone maps Google's host
        # to our controlled loopback server. The fake grant resolver owns no real secret.
        original_client = httpx.Client
        port = self.server.server_port

        class Transport(httpx.BaseTransport):
            def handle_request(self, request):
                url = request.url.copy_with(scheme='http', host='127.0.0.1', port=port)
                rewritten = httpx.Request(request.method, url, headers=request.headers, content=request.content)
                with original_client(timeout=2) as client:
                    response = client.send(rewritten)
                    return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        self.addCleanup(patch.stopall)
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
        self.file.write_bytes(b'encrypted synthetic bytes' * 1000)

    def test_http_chunked_upload_readback_and_scoped_retention(self):
        first = self.drive.upload(self.file, self.folder)
        # An unrelated object in the same folder must survive even a hostile listing.
        foreign = self.drive.create('foreign', 'part', self.folder, 'foreign')
        self.objects[foreign]['appProperties']['owner'] = 'b' * 64
        second = self.drive.upload(self.file, self.folder, keep=1)
        self.assertTrue(second['verified'])
        self.assertEqual(self.drive.download(self.folder, second['id']), self.file.read_bytes())
        self.assertNotIn(first['id'], self.objects)
        self.assertIn(foreign, self.objects)
        self.assertTrue(all(auth == 'Bearer synthetic-test-only' for _, _, auth in self.requests))
        self.assertGreater(len(self.drive.descriptor(self.objects[second['id']])['parts']), 1)

    def test_corrupt_readback_never_prunes(self):
        first = self.drive.upload(self.file, self.folder)
        self.corrupt = True
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder, keep=1)
        self.assertIn(first['id'], self.objects)
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.requests))

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
        self.assertFalse(any(method == 'DELETE' for method, _, _ in self.requests))

    def test_bounds_ownership_and_invalid_descriptor(self):
        self.file.write_bytes(b'x' * (MAX_CIPHER + 1))
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder)
        self.objects[self.folder]['appProperties']['owner'] = 'other'
        with self.assertRaises(self.error_type):
            self.drive.upload(self.file, self.folder)
        with self.assertRaises(self.error_type):
            self.drive.descriptor({'description': '{"parts": []}'})

    def test_sdk_refuses_non_https_or_foreign_origin(self):
        from hermes_cli.plugin_provider_requests import credentialed_provider_request
        before = len(self.requests)
        for url in ['http://www.googleapis.com/drive/v3/files', 'https://evil.example/drive/v3/files']:
            with self.assertRaises(PermissionError):
                self.drive.request('GET', url)
        self.assertEqual(len(self.requests), before)


if __name__ == '__main__':
    unittest.main()
