"""Reviewer provider regressions: offline mocked provider, never real cloud."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from finix.workspace_drive import WorkspaceRunner, WorkspaceError


class ProviderContract(unittest.TestCase):
    def test_public_transport_identity_id_and_metadata(self):
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            body = ({'user': {'permissionId': 'account-one'}} if '/about?' in url
                    else {'ids': ['allocated']} if '/generateIds?' in url
                    else {'id': 'allocated', 'trashed': False})
            return SimpleNamespace(status=200, headers={}, body=json.dumps(body).encode(), json=lambda: body)
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            runner = WorkspaceRunner(Path(home), request=request)
            self.assertEqual(runner('identity'), {'account_id': 'account-one'})
            self.assertEqual(runner('generate-id'), {'id': 'allocated'})
            self.assertEqual(runner('metadata', 'allocated')['id'], 'allocated')
        self.assertTrue(all(url.startswith('https://www.googleapis.com/drive/v3/') for _,url,_ in calls))
        self.assertFalse(any('Authorization' in (kwargs.get('headers') or {}) for _,_,kwargs in calls))
    def test_real_sdk_public_refusals_namespace_and_thread_context(self):
        """Actual SDK + mocked HTTP transport; no grant, real cloud or OAuth flow."""
        import importlib.util
        if importlib.util.find_spec('hermes_cli') is None:
            self.skipTest('SDK reader path required for actual SDK offline probe')
        import concurrent.futures
        import httpx
        from unittest.mock import patch
        from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, ProviderResponse
        from hermes_constants import get_hermes_home_override
        from finix.drive import profile_scope, managed_request
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            home = Path(temp)
            plugin = home / 'plugins' / 'finix'
            plugin.mkdir(parents=True)
            (plugin / 'plugin.yaml').write_bytes((root/'plugin.yaml').read_bytes())
            namespace = {}
            # The real SDK attributes this code to a scratch installed plugin.
            exec(compile('from hermes_cli.plugin_provider_requests import credentialed_provider_request\ndef request(url):\n return credentialed_provider_request("google", "GET", url)\n', str(plugin/'probe.py'), 'exec'), namespace)
            client = httpx.Client
            def no_network(*args, **kwargs):
                return client(*args, **kwargs, transport=httpx.MockTransport(lambda r: self.fail('unexpected provider HTTP')))
            previous = get_hermes_home_override()
            with patch.object(httpx, 'Client', no_network):
                with profile_scope(home):
                    with self.assertRaises(PermissionError):
                        namespace['request']('https://untrusted.invalid/')
                    with self.assertRaisesRegex(ProviderNotSignedIn, 'hermes auth add finix:google'):
                        namespace['request']('https://www.googleapis.com/drive/v3/files')
                other_home = home/'other-profile'; other_home.mkdir()
                other_plugin = other_home/'plugins'/'finix'; other_plugin.mkdir(parents=True)
                (other_plugin/'plugin.yaml').write_bytes((root/'plugin.yaml').read_bytes())
                other_namespace = {}
                exec(compile('from hermes_cli.plugin_provider_requests import credentialed_provider_request\ndef request(url):\n return credentialed_provider_request("google", "GET", url)\n', str(other_plugin/'probe.py'), 'exec'), other_namespace)
                import threading
                barrier = threading.Barrier(2)
                def work(selected_home, selected_namespace):
                    before = get_hermes_home_override()
                    try:
                        with profile_scope(selected_home):
                            barrier.wait(timeout=5)
                            self.assertEqual(Path(get_hermes_home_override()), selected_home)
                            with self.assertRaisesRegex(ProviderNotSignedIn, 'finix:google'):
                                selected_namespace['request']('https://www.googleapis.com/drive/v3/files')
                            raise RuntimeError('fixture reset path')
                    except RuntimeError as exc:
                        self.assertEqual(str(exc), 'fixture reset path')
                    self.assertEqual(get_hermes_home_override(), before)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(work, home, namespace), pool.submit(work, other_home, other_namespace)]
                    for future in futures: future.result()
            self.assertEqual(get_hermes_home_override(), previous)
            self.assertEqual(ProviderResponse(200, {}, b'{"ok":true}').json(), {'ok': True})
            self.assertFalse((home/'auth.json').exists())
        with self.assertRaisesRegex(Exception, 'profile context required'):
            managed_request('GET', 'https://www.googleapis.com/drive/v3/files')

    def test_actual_public_sdk_identity_pin_refuses_replaced_grant_offline(self):
        """Real SDK helper, isolated synthetic grant stub and HTTPX MockTransport."""
        import importlib.util
        if importlib.util.find_spec('hermes_cli') is None:
            self.skipTest('SDK reader path required for actual SDK offline probe')
        import hashlib
        import httpx
        import hermes_cli.plugin_oauth as oauth
        from unittest.mock import patch
        from finix import core
        from finix.workspace_drive import WorkspaceDrive
        root = Path(__file__).parents[1]
        account = ['account-A']; calls = []
        client = httpx.Client
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            home = core.private_dir(Path(temp)/'home')
            plugin = home/'plugins'/'finix'; plugin.mkdir(parents=True)
            (plugin/'plugin.yaml').write_bytes((root/'plugin.yaml').read_bytes())
            namespace = {'__name__':'finix.drive', '__package__':'finix'}
            exec(compile((root/'finix/drive.py').read_bytes(), str(plugin/'drive.py'), 'exec'), namespace)
            def transport(request):
                calls.append((request.method, str(request.url)))
                self.assertIn('/about?', str(request.url))
                return httpx.Response(200, json={'user': {'permissionId': account[0]}})
            def offline_client(*args, **kwargs):
                return client(*args, **kwargs, transport=httpx.MockTransport(transport))
            def isolated_grant(key):
                # Same isolated fixture seam as the frozen independent reviewer;
                # never read/write the real grant or replace the SDK request API.
                return {'fingerprint': oauth._declared_clients(plugin)[0].fingerprint,
                        'access_token':'synthetic-offline-fixture'}
            runner = WorkspaceRunner(home, request=namespace['Drive']('a'*64, home=home)._managed_request)
            adapter = WorkspaceDrive(home, 'a'*64, Path(temp)/'state', runner=runner,
                                     account_digest=hashlib.sha256(b'account-A').hexdigest())
            cipher = Path(temp)/'cipher.age'; cipher.write_bytes(b'age-encryption.org/v1\nfixture')
            with patch.object(httpx, 'Client', offline_client), patch.object(oauth, '_read_grant', isolated_grant):
                self.assertEqual(runner('identity'), {'account_id':'account-A'})
                account[0] = 'account-B'
                with self.assertRaisesRegex(WorkspaceError, 'ACCOUNT_CHANGED'): adapter.reconcile(cipher)
            self.assertEqual(len(calls), 2)
            self.assertFalse((adapter.state/'workspace.json').exists())
            self.assertFalse((adapter.state/'workspace-pending.json').exists())

    def test_journal_composes_public_rest_transport_with_full_cipher_readback(self):
        from unittest.mock import patch
        from urllib.parse import urlsplit, parse_qs
        from finix import core
        from finix.workspace_drive import WorkspaceDrive, MIME
        objects, media, calls = {}, {}, []
        pending = {}
        counter = 0
        def request(method, url, **kw):
            nonlocal counter
            calls.append((method,url))
            path = urlsplit(url).path
            payload = kw.get('json')
            status, headers, body = 200, {}, {}
            if '/about' in path:
                body = {'user': {'permissionId': 'synthetic-account'}}
            elif path.endswith('/generateIds'):
                counter += 1
                body = {'ids': ['id-'+str(counter)]}
            elif '/upload/' in path:
                pending.update(payload)
                headers = {'Location': 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=synthetic'}
            elif method == 'POST':
                body = dict(payload, owners=[{'emailAddress':'fixture@example.invalid'}], trashed=False)
                objects[body['id']] = body
            else:
                identifier = path.rsplit('/',1)[-1]
                if identifier not in objects:
                    status = 404
                elif method == 'PATCH':
                    objects[identifier].update(payload)
                    body = objects[identifier]
                elif parse_qs(urlsplit(url).query).get('alt') == ['media']:
                    body = media[identifier]
                    start,end = map(int,kw['headers']['Range'][6:].split('-'))
                    headers = {'Content-Range':f'bytes {start}-{end}/{len(body)}'}
                    body = body[start:end+1]
                    status = 206
                else:
                    body = objects[identifier]
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return SimpleNamespace(status=status, headers=headers, body=raw, json=lambda: json.loads(raw))
        def upload(uri, path, size, *, cancel=None):
            self.assertEqual(size, path.stat().st_size)
            identifier = pending['id']
            media[identifier] = path.read_bytes()
            objects[identifier] = dict(pending, size=str(size), trashed=False, owners=[{'emailAddress':'fixture@example.invalid'}])
            return {'id': identifier, 'mimeType': MIME}
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            home = core.private_dir(Path(tmp)/'home')
            state = Path(tmp)/'state'
            path = Path(tmp)/'cipher.age'
            path.write_bytes(b'age-encryption.org/v1\nmocked ciphertext')
            from test_workspace_reconcile import ACCOUNT_DIGEST
            adapter = WorkspaceDrive(home,'a'*64,state,runner=WorkspaceRunner(home, request=request),account_digest=ACCOUNT_DIGEST)
            with patch('finix.drive.upload_media', upload):
                result = adapter.backup_cipher(path, keep=0)
                self.assertTrue(result['verified'])
                self.assertEqual(result['credential_authority'], 'finix:google')
                self.assertEqual(result['sha256'], core.digest_file(path))
                restarted = WorkspaceDrive(home,'a'*64,state,runner=WorkspaceRunner(home,request=request),account_digest=ACCOUNT_DIGEST)
                self.assertEqual(restarted.reconcile(path)['id'], result['id'])
                self.assertEqual(sum(method == 'POST' and '/upload/' in url for method,url in calls), 1)
                self.assertFalse((state/'workspace-pending.json').exists())

    def test_explicit_legacy_record_is_not_adopted_as_new_authority(self):
        from finix.workspace_drive import WorkspaceDrive
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            adapter = WorkspaceDrive(home, 'a'*64, Path(home)/'state')
            record = {'profile_id': 'a'*64, 'id': 'old', 'folder_id': 'old-folder', 'size': 1, 'sha256': 'b'*64}
            with self.assertRaisesRegex(WorkspaceError, 'MIGRATION_REQUIRED'):
                adapter.descriptor(record)

    def test_applied_account_survives_activation_and_blocks_fresh_wrong_grant(self):
        import hashlib
        from unittest.mock import patch
        from finix import core
        from finix.integration import Profile
        from finix.connections import Connections
        from test_workspace_reconcile import Transport
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            profile = Profile(core.private_dir(Path(temp)/'home'))
            manager = Connections()
            ticket = manager.view(profile)['ticket']
            with patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'account_id':'account-A'}):
                view = manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                    action='start', provider='workspace', confirmed=True, local_browser=True)
                manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                    action='apply', session_id=view['session']['id'])
            digest = hashlib.sha256(b'account-A').hexdigest()
            self.assertEqual(profile.config().get('workspace_account'), digest)
            from finix.setup_flow import SetupFlow
            binding = SetupFlow(profile)._binding()
            changed_cfg = dict(profile.config(), workspace_account=hashlib.sha256(b'account-B').hexdigest())
            self.assertNotEqual(SetupFlow(profile)._binding(changed_cfg), binding)
            core.atomic_json(profile.home/'auth.json', {'synthetic_fixture':'refreshed-token-bytes'})
            self.assertEqual(SetupFlow(profile)._binding(), binding)
            cipher = Path(temp)/'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\nsynthetic cipher')
            transport = Transport(); transport.account = 'account-B'
            adapter = profile.workspace(); adapter.runner = transport
            with self.assertRaisesRegex(WorkspaceError, 'ACCOUNT_CHANGED'):
                adapter.reconcile(cipher)
            self.assertFalse((adapter.state/'workspace.json').exists())
            self.assertFalse((adapter.state/'workspace-pending.json').exists())
            self.assertEqual([op for op,_ in transport.calls], ['identity'])
            transport.account = 'account-A'
            self.assertTrue(adapter.reconcile(cipher)['verified'])
            journal_bytes = (adapter.state/'workspace.json').read_bytes()
            from finix.connections import ConnectionError
            with patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'account_id':'account-B'}):
                view = manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                    action='start', provider='workspace', confirmed=True, local_browser=True)
                self.assertEqual(profile.config()['workspace_account'], digest)  # Start is not Apply.
                core.atomic_json(profile.root/'settings.json', dict(profile.config(), retention=1))
                with self.assertRaisesRegex(ConnectionError, 'SETTINGS_CHANGED'):
                    manager.action(profile, profile_id=profile.profile_id, ticket=ticket,
                        action='apply', session_id=view['session']['id'])
            self.assertEqual(profile.config()['workspace_account'], digest)
            self.assertEqual(profile.config()['retention'], 1)
            self.assertEqual((adapter.state/'workspace.json').read_bytes(), journal_bytes)

    def test_recovery_catalog_pins_journal_account_and_native_binding(self):
        import hashlib
        from unittest.mock import patch
        from finix import core
        from finix.integration import Profile
        from finix.recovery_sources import register_cloud
        from test_workspace_reconcile import Transport, ACCOUNT_DIGEST
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            home = core.private_dir(Path(temp)/'home'); profile = Profile(home)
            core.atomic_json(profile.root/'settings.json', dict(profile.config(), storage='workspace', workspace_account=ACCOUNT_DIGEST))
            adapter = profile.workspace(); adapter.runner = Transport()
            cipher = Path(temp)/'cipher.age'; cipher.write_bytes(b'age-encryption.org/v1\nfixture')
            adapter.reconcile(cipher)
            journal_path = adapter.state/'workspace.json'
            saved = json.loads(journal_path.read_text())
            saved['credential_authority'] = 'finix:google'
            core.atomic_json(journal_path, saved)
            sources = []
            service = SimpleNamespace(activation='fixture', add_source=sources.append, stage_root=Path(temp)/'stage')
            with patch.object(profile, 'workspace', return_value=adapter):
                register_cloud(service, profile)
            source = sources[0]
            self.assertEqual(len(source.records()), 1)
            for key, value in [('account', hashlib.sha256(b'other').hexdigest()),
                               ('binding', {'provider':'other'}), ('credential_authority', 'legacy')]:
                with self.subTest(key=key):
                    core.atomic_json(journal_path, dict(saved, **{key:value}))
                    with self.assertRaises(core.FinixError): source.records()
            core.atomic_json(journal_path, saved)
            cfg = profile.config(); cfg['workspace_account'] = hashlib.sha256(b'other').hexdigest()
            core.atomic_json(profile.root/'settings.json', cfg)
            with self.assertRaises(core.FinixError): source.records()

    def test_selected_account_swap_between_operations_is_refused_before_mutation(self):
        import test_workspace_reconcile as fixtures
        f = fixtures.ReconcileTests('test_lost_ack_reconciles_same_remote_id_and_committed_replay')
        f.setUp(); self.addCleanup(f.doCleanups)
        fake = f.transport
        def changed(op, *args, **kwargs):
            result = fake(op, *args, **kwargs)
            if op == 'generate-id': fake.account = 'different-account'
            return result
        f.drive.runner = changed
        with self.assertRaisesRegex(WorkspaceError, 'ACCOUNT_CHANGED'): f.drive.reconcile(f.path)
        self.assertFalse(any(op in {'create-folder','upload','delete'} for op,_ in fake.calls))
        self.assertFalse((f.state/'workspace-pending.json').exists())

    def test_legacy_unselected_account_is_not_adopted(self):
        from finix import core
        from finix.integration import Profile
        from test_workspace_reconcile import Transport
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as temp:
            profile = Profile(core.private_dir(Path(temp)/'home'))
            core.atomic_json(profile.root/'settings.json', dict(profile.config(), storage='workspace'))
            adapter = profile.workspace(); transport = Transport(); adapter.runner = transport
            cipher = Path(temp)/'cipher.age'; cipher.write_bytes(b'age-encryption.org/v1\nfixture')
            with self.assertRaisesRegex(WorkspaceError, 'RESELECTION_REQUIRED'):
                adapter.reconcile(cipher)
            self.assertEqual(transport.calls, [])
            self.assertFalse((adapter.state/'workspace.json').exists())
            legacy = adapter.state/'workspace.json'
            core.atomic_json(legacy, {'profile_id':profile.profile_id, 'records':[], 'credential_authority':'legacy'})
            original = legacy.read_bytes()
            with self.assertRaisesRegex(WorkspaceError, 'RESELECTION_REQUIRED'):
                adapter.reconcile(cipher)
            self.assertEqual(legacy.read_bytes(), original)
            self.assertFalse((adapter.state/'workspace-pending.json').exists())
            self.assertEqual(transport.calls, [])

    def test_google_apply_rechecks_the_exact_verified_account(self):
        import hashlib
        from unittest.mock import patch
        from finix.connections import Connections, ConnectionError
        profile = SimpleNamespace(home=Path(os.environ['TMPDIR']))
        session = {'public': {'provider':'workspace'}, 'account_digest':hashlib.sha256(b'first-account').hexdigest()}
        with patch('finix.workspace_drive.WorkspaceRunner.__call__', return_value={'account_id':'different-account'}):
            with self.assertRaisesRegex(ConnectionError,'GOOGLE_ACCOUNT_CHANGED'):
                Connections()._apply(profile,session)

    def test_setup_strings_are_english_and_declare_public_google_auth(self):
        root = Path(__file__).parents[1]
        import re
        for filename in ('finix/telegram_menu.py', 'finix/connection_worker.py'):
            self.assertIsNone(re.search('[А-Яа-яЁё]', (root/filename).read_text(encoding='utf-8')), filename)
        self.assertIn('hermes auth add finix:google', (root/'desktop/src/cloud.mjs').read_text(encoding='utf-8'))
        self.assertNotIn('google_client_secret.json', (root/'desktop/src/cloud.mjs').read_text(encoding='utf-8'))
        text = (root/'dashboard/plugin_api.py').read_text(encoding='utf-8')
        self.assertIn("'ru' if", text)

    def test_legacy_auth_and_managed_backend_are_not_executable(self):
        root = Path(__file__).parents[1]
        for filename in ('finix/connection_worker.py', 'finix/connections.py',
                         'finix/workspace_drive.py', 'finix/workspace_helper.py', 'finix/connector_drive.py'):
            text = (root / filename).read_text(encoding='utf-8')
            for forbidden in ('google_auth_oauthlib', 'googleapiclient', 'peek_nous_access_token',
                              'managed_gateway_auth', 'google_token.json', 'google_client_secret.json',
                              'skills/productivity/google-workspace'):
                self.assertNotIn(forbidden, text, filename)
        from finix.integration import Profile
        from finix import core
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            private = core.private_dir(Path(home) / 'private')
            profile = Profile.__new__(Profile)
            profile.root = private
            profile.directories = lambda: private
            profile.config = lambda: {'storage': 'local'}
            with self.assertRaisesRegex(Exception, 'unsupported|Unsupported|removed'):
                profile.configure('storage', 'connector-drive')

    def test_download_ranges_are_verified_before_writing(self):
        def request(method, url, **kwargs):
            return SimpleNamespace(status=200, headers={}, body=b'wrong')
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            output = Path(home) / 'cipher.age'
            runner = WorkspaceRunner(Path(home), request=request)
            with self.assertRaises(WorkspaceError):
                runner('download', 'file', '--output', str(output), expected_size=5)
            self.assertEqual(output.read_bytes(), b'')

    def test_legacy_journal_requires_explicit_migration_before_provider_call(self):
        from finix.workspace_drive import WorkspaceDrive
        from finix import core
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as home:
            state = core.private_dir(Path(home) / 'journal')
            core.atomic_json(state / 'workspace.json', {'profile_id': 'a'*64, 'records': []})
            runner = WorkspaceRunner(Path(home), request=lambda *a, **k: self.fail('provider called'))
            adapter = WorkspaceDrive(home, 'a'*64, state, runner=runner)
            with self.assertRaisesRegex(WorkspaceError, 'MIGRATION_REQUIRED'):
                adapter.ledger()


if __name__ == '__main__':
    unittest.main()
