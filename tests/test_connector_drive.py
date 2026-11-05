"""Managed SDK wire fixtures; these are NOT discovered Google tool contracts."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from finix.core import FinixError


class ConnectorTests(unittest.TestCase):
    def test_module_contract(self):
        from finix.connector_drive import ManagedDrive, MAX_CIPHER
        self.assertEqual(MAX_CIPHER, 20 * 1024 * 1024)
        with self.assertRaises(FinixError):
            ManagedDrive('/unused', 'a' * 64, '', '', 'folder')

    def test_no_token_safe_actionable_status(self):
        from finix.connector_drive import ManagedDrive
        from tools.connectors.gateway.client import ConnectorClient
        client = ConnectorClient(endpoint_resolver=lambda: 'https://fixture.invalid', header_provider=lambda _: {})
        drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
        result = drive.status()
        self.assertEqual(result['code'], 'NO_TOKEN')
        self.assertIn('owner', result['action'])
        self.assertFalse(result['ready'])

    def test_discovery_never_executes_unknown_contract(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
        result = drive.status()
        self.assertEqual(result['code'], 'UNSUPPORTED_SCHEMA')
        self.assertEqual(result['schema_count'], 1)
        self.assertFalse(any('/execute' in url for _, url, _ in http.calls))
        with self.assertRaises(FinixError):
            drive.download('object', {'size': 3, 'sha256': 'a' * 64})

    def test_inactive_wrong_alias_and_wrong_connector_rejected(self):
        from finix.connector_drive import ManagedDrive
        for change in [{'status': 'expired'}, {'alias': 'other'}, {'connector': 'todoist'}, {'disabled': True}]:
            client, http = sdk(change)
            drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            self.assertEqual(drive.status()['code'], 'ACCOUNT_MISMATCH')
            self.assertFalse(any('/search' in url for _, url, _ in http.calls))

    def test_fixture_upload_download_readback_and_retention(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cipher = root / 'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\nfixture-ciphertext')
            drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
                first = drive.upload(cipher, root / 'ledger', keep=1)
                second = drive.upload(cipher, root / 'ledger', keep=1)
                self.assertTrue(first['verified'])
                self.assertEqual(drive.download(second['id'], second), cipher.read_bytes())
                self.assertNotIn(first['id'], http.objects)
                self.assertIn(second['id'], http.objects)
                self.assertTrue(all(body['call']['arguments']['connector_alias'] == 'chosen'
                    for method, url, body in http.calls if '/execute' in url))
                self.assertEqual(len(client._storage.uploads), 2)
                self.assertTrue(all('Authorization' not in headers for headers in client._storage.headers))

    def test_corrupt_readback_never_prunes_or_journals(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        client._storage.corrupt = True
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cipher = root / 'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\nx')
            drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
                with self.assertRaises(FinixError):
                    drive.upload(cipher, root / 'ledger', keep=1)
            self.assertFalse((root / 'ledger' / 'connector.json').exists())
            self.assertFalse(any(c[2]['call']['arguments'].get('operation') == 'delete'
                for c in http.calls if '/execute' in c[1]))

    def test_sdk_default_does_not_refresh_create_or_borrow_root_auth(self):
        from finix.connector_drive import ManagedDrive
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            # A local store exists, but no supported no-fallback provider selector.
            (root / 'auth.json').write_text('{}')
            drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder')
            with patch('tools.managed_tool_gateway.read_nous_access_token', side_effect=AssertionError('must not refresh')):
                self.assertEqual(drive.status()['code'], 'AUTH_POLICY_UNSUPPORTED')
            (root / 'auth.json').unlink()
            with patch.dict('os.environ', {'TOOL_GATEWAY_USER_TOKEN': 'must-not-use-launch-token'}):
                self.assertEqual(drive.status()['code'], 'NO_TOKEN')

    def test_schema_drift_and_foreign_tools_block_before_staging(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        contract = FixtureContract()
        contract.schemas = {**contract.schemas, 'foreign': {'connector': 'todoist'}}
        drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
        with patch('finix.connector_drive.REVIEWED_CONTRACTS', (contract,)):
            self.assertEqual(drive.status()['code'], 'UNSUPPORTED_SCHEMA')
        self.assertFalse(any('/files/' in c[1] or '/execute' in c[1] for c in http.calls))

    def test_explicit_nondefault_active_account_is_allowed(self):
        from finix.connector_drive import ManagedDrive
        client, _ = sdk({'active': False})  # wire.active means newest DEFAULT, not authorization
        drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
        with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
            self.assertTrue(drive.status()['ready'])

    def test_oversize_plaintext_symlink_refused_before_staging(self):
        from finix.connector_drive import ManagedDrive, MAX_CIPHER
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cipher = root / 'cipher.age'
            for mode in ('oversize', 'plaintext', 'symlink'):
                client, http = sdk()
                drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder', client=client)
                if mode == 'oversize':
                    with cipher.open('wb') as stream: stream.truncate(MAX_CIPHER + 1)
                elif mode == 'plaintext': cipher.write_bytes(b'secret plaintext')
                else:
                    cipher.unlink()
                    cipher.symlink_to(root / 'missing')
                with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
                    with self.assertRaises((FinixError, OSError)):
                        drive.upload(cipher, root / 'ledger')
                self.assertFalse(any('/files/upload' in c[1] for c in http.calls))

    def test_wrong_ledger_binding_and_metadata_cannot_delete_foreign_objects(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cipher = root / 'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\nx')
            drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
                first = drive.upload(cipher, root / 'ledger', keep=1)
                http.objects[first['id']]['owner'] = 'b' * 64
                with self.assertRaises(FinixError):
                    drive.upload(cipher, root / 'ledger', keep=1)
                self.assertIn(first['id'], http.objects)
                other = ManagedDrive(root, 'b' * 64, 'connection', 'chosen', 'folder', client=client)
                count = len(client._storage.uploads)
                with self.assertRaises(FinixError):
                    other.upload(cipher, root / 'ledger', keep=1)
                self.assertEqual(len(client._storage.uploads), count)

    def test_sdk_download_cap_and_redirect_policy(self):
        from finix.connector_drive import StorageTransport
        from tools.connectors.gateway.errors import ToolGatewayError
        client, _ = sdk()
        client._storage.uploads.append(b'1234')
        with self.assertRaises(ToolGatewayError):
            client.get_file('https://storage.invalid/0', lambda _: None, max_bytes=3)
        from unittest.mock import Mock
        session = Mock()
        with patch('requests.Session', return_value=session):
            StorageTransport().request('GET', 'https://storage.invalid/0', stream=True)
        self.assertFalse(session.trust_env)
        self.assertIs(session.request.call_args.kwargs['allow_redirects'], False)

    def test_cli_download_saved_descriptor_and_no_overwrite(self):
        from finix.connector_drive import ManagedDrive, main
        client, _ = sdk()
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cipher = root / 'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\nx')
            drive = ManagedDrive(root, 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            with patch('finix.connector_drive.REVIEWED_CONTRACTS', (FixtureContract(),)):
                record = drive.upload(cipher, root / 'ledger')
                descriptor = root / 'record.json'
                descriptor.write_text(json.dumps(record))
                output = root / 'download.age'
                args = ['download', '--home', str(root), '--profile-id', 'a' * 64,
                        '--account', 'connection', '--alias', 'chosen', '--folder', 'folder',
                        '--object', record['id'], '--descriptor', str(descriptor), '--output', str(output)]
                with patch('finix.connector_drive.ManagedDrive', return_value=drive), patch('sys.stdout'), patch('sys.stderr'):
                    self.assertEqual(main(args), 0)
                    self.assertEqual(main(args), 1)
                self.assertEqual(output.read_bytes(), cipher.read_bytes())
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_settings_explicit_backend_preserves_local_and_surfaces_gap(self):
        from finix.integration import Profile
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / 'profile'
            home.mkdir()
            profile = Profile(home)
            self.assertEqual(profile.config()['storage'], 'local')
            status = profile.configure('storage', 'connector-drive')
            self.assertEqual(status['managed_connector']['code'], 'ACCOUNT_REQUIRED')
            profile.configure('connector_account', 'connection')
            profile.configure('connector_alias', 'chosen')
            status = profile.configure('connector_folder', 'folder')
            self.assertEqual(status['managed_connector']['code'], 'NO_TOKEN')
            self.assertEqual(profile.configure('storage', 'drive')['storage'], 'drive')
            self.assertEqual(profile.configure('storage', 'local')['storage'], 'local')

    def test_catalogue_absence_not_source_name_controls_availability(self):
        from finix.connector_drive import ManagedDrive
        client, http = sdk()
        for rows in ([], [{'connector': 'todoist', 'enabled': True}],
                     [{'connector': 'googledrive', 'enabled': False}]):
            drive = ManagedDrive('/unused', 'a' * 64, 'connection', 'chosen', 'folder', client=client)
            with patch.object(client, 'list_connectors', return_value=rows):
                self.assertEqual(drive.status()['code'], 'CONNECTOR_UNAVAILABLE')
        self.assertEqual(http.calls, [])

    def test_profile_secret_scope_does_not_use_launch_environment(self):
        from finix.connector_drive import profile_scope
        from agent.secret_scope import get_secret, current_secret_scope_home
        from hermes_constants import get_hermes_home
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / '.env').write_text('FINIX_FIXTURE=profile-value\n')
            with patch.dict('os.environ', {'FINIX_FIXTURE': 'launch-secret', 'TOOL_GATEWAY_USER_TOKEN': 'launch-only'}):
                with profile_scope(root):
                    self.assertEqual(get_hermes_home(), root)
                    self.assertEqual(current_secret_scope_home(), str(root))
                    self.assertEqual(get_secret('FINIX_FIXTURE'), 'profile-value')
                    self.assertIsNone(get_secret('TOOL_GATEWAY_USER_TOKEN'))


SCHEMA = {'connector': 'googledrive', 'tool': 'fixture_only',
          'inputSchema': {'type': 'object', 'properties': {'file': {'file_uploadable': True}}}}


class FixtureContract:
    """Code-owned fake codec; production registry must remain empty until live review."""
    schemas = {'fixture_only': {'connector': 'googledrive', 'tool': 'fixture_only',
                               'input_schema': SCHEMA['inputSchema'], 'description': ''}}
    upload_tool = 'fixture_only'
    storage_hosts = frozenset({'storage.invalid'})

    def encode(self, operation, payload):
        return 'fixture_only', {'operation': operation, **payload}

    def decode(self, operation, data):
        return data


class Response:
    status_code = 200
    headers = {}
    def __init__(self, payload=None, content=b''):
        self.payload, self.content = payload, content
    def json(self): return self.payload
    def iter_content(self, chunk_size): yield self.content
    def close(self): pass


class Storage:
    def __init__(self):
        self.uploads, self.headers, self.corrupt = [], [], False
    def request(self, method, url, headers=None, data=None, **kwargs):
        self.headers.append(headers)
        if method == 'PUT':
            self.uploads.append(data)
            return Response()
        return Response(content=b'bad' if self.corrupt else self.uploads[int(url.rsplit('/', 1)[1])])


class HTTP:
    def __init__(self, storage, change):
        self.calls, self.objects, self.storage, self.change = [], {}, storage, change
    def request(self, method, url, headers=None, json=None, **kwargs):
        self.calls.append((method, url, json))
        if url.endswith('/v1/connectors?limit=50'):
            return Response({'items': [{'connector': 'googledrive', 'enabled': True}], 'nextCursor': None})
        if '/accounts/' in url:
            return Response({'connectionId': 'connection', 'connector': 'googledrive', 'status': 'active',
                'label': 'Fixture', 'alias': 'chosen', 'active': True, 'createdAt': '', 'updatedAt': '', **self.change})
        if '/search' in url:
            return Response({'results': [{'index': 0, 'tools': ['fixture_only']}], 'schemas': {}})
        if '/schemas' in url:
            return Response({'schemas': {'fixture_only': SCHEMA}})
        if '/files/upload' in url:
            return Response({'uploadUrl': 'https://storage.invalid/upload', 'headers': {},
                'descriptor': {'name': 'cipher.age', 'mimetype': 'application/octet-stream', 's3key': 'fixture'}, 'expiresAt': ''})
        args = json['call']['arguments']
        operation = args['operation']
        if operation == 'upload':
            identifier = str(len(self.storage.uploads) - 1)
            self.objects[identifier] = dict(args['metadata'], id=identifier)
            data = self.objects[identifier]
        elif operation == 'metadata': data = self.objects.get(args['id'])
        elif operation == 'download': data = {'url': 'https://storage.invalid/' + args['id']}
        elif operation == 'delete':
            self.objects.pop(args['id'], None)
            data = None
        return Response({'result': {'data': data, 'account': 'chosen'}})


def sdk(change=None):
    from tools.connectors.gateway.client import ConnectorClient
    storage = Storage()
    http = HTTP(storage, change or {})
    return ConnectorClient(transport=http, storage=storage,
        endpoint_resolver=lambda: 'https://fixture.invalid', header_provider=lambda _: {'Authorization': 'Bearer fixture'}), http
