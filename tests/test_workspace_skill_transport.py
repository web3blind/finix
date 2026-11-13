"""Offline contract tests. Google API doubles are NOT live cloud acceptance."""
import hashlib
import importlib
import unittest
import contextlib
import os
from pathlib import Path
import shutil
import uuid
import tempfile

FIXTURE = Path(os.environ['FINIX_SKILL_TEST_ROOT']) if os.environ.get('FINIX_SKILL_TEST_ROOT') else Path(os.environ.get('TMPDIR') or tempfile.gettempdir()) / 'finix-skill-provider-tests'


@contextlib.contextmanager
def private_stage():
    from finix import core
    core.private_dir(FIXTURE)
    stage = FIXTURE / uuid.uuid4().hex
    stage.mkdir(mode=0o700)
    try:
        yield stage
    finally:
        shutil.rmtree(stage)


class Request:
    def __init__(self, value):
        self.value = value
    def execute(self, **kwargs):
        return self.value


class GoogleDouble:
    def __init__(self, identity='requester-1'):
        self.identity = identity
        self.calls = []
    def about(self):
        return self
    def get(self, **kwargs):
        self.calls.append(('about', kwargs))
        return Request({'user': {'permissionId': self.identity}})
    def files(self):
        return self
    def generateIds(self, **kwargs):
        self.calls.append(('generate', kwargs))
        return Request({'ids': ['allocated-1']})


def pin(identity='requester-1'):
    return hashlib.sha256(identity.encode()).hexdigest()


class SkillWorkerTests(unittest.TestCase):
    def worker(self):
        return importlib.import_module('finix.workspace_skill_worker')

    def test_identity_binds_requester_not_file_owner(self):
        worker = self.worker()
        service = GoogleDouble()
        result = worker.perform(service, {'operation': 'identity', 'args': []})
        self.assertEqual(result, {'account_id': 'requester-1'})
        self.assertEqual(service.calls, [('about', {'fields': 'user(permissionId)'})])

    def test_folder_create_uses_allocated_id_and_reads_metadata(self):
        worker = self.worker()
        class Files(GoogleDouble):
            def create(self, **kwargs):
                self.created = kwargs
                return Request({'id': kwargs['body']['id']})
        service = Files()
        result = worker.perform(service, {'operation': 'create-folder', 'account_pin': pin(),
            'args': ['--id', 'folder-1', '--name', 'Finix-private']})
        self.assertEqual(result, {'status': 'created', 'id': 'folder-1'})
        self.assertEqual(service.created['body'], {'id': 'folder-1', 'name': 'Finix-private',
            'mimeType': 'application/vnd.google-apps.folder', 'parents': ['root']})
        self.assertNotIn('permissions', service.created['body'])

    def test_binary_upload_metadata_download_roundtrip(self):
        import io
        import tempfile
        from pathlib import Path
        worker = self.worker()
        data = bytes(range(256)) * 4097
        class Upload:
            def __init__(self, stream, **kwargs):
                self.stream, self.options = stream, kwargs
        class Download:
            def __init__(self, stream, request, **kwargs):
                self.stream, self.request = stream, request
            def next_chunk(self, **kwargs):
                self.stream.write(self.request.value)
                return None, True
        class Files(GoogleDouble):
            def create(self, **kwargs):
                self.body = kwargs['body']
                self.binary = kwargs['media_body'].stream.read()
                class Next:
                    def next_chunk(inner, **kw):
                        return None, dict(self.body)
                return Next()
            def get(self, **kwargs):
                if 'fileId' not in kwargs:
                    return super().get(**kwargs)
                return Request(dict(self.body, size=str(len(self.binary)), trashed=False,
                    owners=[{'emailAddress': 'owner@example.invalid'}]))
            def get_media(self, **kwargs):
                return Request(self.binary)
        service = Files()
        with private_stage() as stage:
            source, output = Path(stage)/'in.age', Path(stage)/'out.age'
            source.write_bytes(data)
            result = worker.perform(service, {'operation': 'upload', 'account_pin': pin(),
                'args': [str(source), '--id', 'binary-1', '--name', 'opaque.age', '--parent', 'folder-1', '--mime-type', worker.MIME]},
                upload_factory=Upload)
            self.assertEqual(result['id'], 'binary-1')
            self.assertEqual(service.body['mimeType'], 'application/octet-stream')
            self.assertEqual(service.body['parents'], ['folder-1'])
            meta = worker.perform(service, {'operation': 'metadata', 'account_pin': pin(), 'args': ['binary-1']})
            self.assertEqual(meta['size'], str(len(data)))
            worker.perform(service, {'operation': 'download', 'account_pin': pin(),
                'args': ['binary-1', '--output', str(output)], 'expected_size': len(data)}, download_factory=Download)
            self.assertEqual(output.read_bytes(), data)
            with self.assertRaises((worker.SkillError, FileExistsError)):
                worker.perform(service, {'operation': 'download', 'account_pin': pin(),
                    'args': ['binary-1', '--output', str(output)], 'expected_size': len(data)}, download_factory=Download)

    def test_skill_discovery_is_profile_local_and_contract_pinned(self):
        transport = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            with self.assertRaises(transport.SetupNeeded):
                transport.discover_skill(home)
            skill = home/'skills'/'productivity'/'google-workspace'
            (skill/'scripts').mkdir(parents=True)
            (skill/'SKILL.md').write_text('---\nname: google-workspace\nversion: 1.2.0\n---\n')
            for filename in ('google_api.py', '_hermes_home.py'):
                (skill/'scripts'/filename).write_text('# incompatible external dependency')
            with self.assertRaises(transport.SetupNeeded) as error:
                transport.discover_skill(home)
            self.assertEqual(error.exception.code, 'SKILL_CONTRACT_CHANGED')

    def test_isolated_installed_skill_api_delegation_without_credentials(self):
        import sys
        from unittest.mock import patch
        transport = importlib.import_module('finix.workspace_skill_transport')
        installed = Path(os.environ['FINIX_INSTALLED_WORKSPACE_SKILL']) if os.environ.get('FINIX_INSTALLED_WORKSPACE_SKILL') else None
        if installed is None or not installed.exists():
            self.skipTest('installed-skill acceptance requires FINIX_INSTALLED_WORKSPACE_SKILL')
        with private_stage() as stage:
            home = stage/'profile'
            home.mkdir(mode=0o700)
            skill = home/'skills'/'productivity'/'google-workspace'
            (skill/'scripts').mkdir(parents=True)
            for relative in ('SKILL.md', 'scripts/google_api.py', 'scripts/_hermes_home.py'):
                (skill/relative).write_bytes((installed/relative).read_bytes())
            entry = stage/'offline_entry.py'
            entry.write_text('''import sys, types, builtins
sys.path.insert(0, REPO)
from finix import workspace_skill_worker as worker
from finix.workspace_skill_transport import load_skill
original_open = builtins.open
def guarded(file, *args, **kwargs):
    if any(s in str(file) for s in ('google_token', 'client_secret', 'auth.json')):
        raise AssertionError('credential access forbidden')
    return original_open(file, *args, **kwargs)
builtins.open = guarded
import io, socket
io.open = guarded
def no_network(*args, **kwargs):
    raise AssertionError('offline component forbids network')
socket.socket.connect = no_network
socket.create_connection = no_network
class Request:
    def execute(self, **kwargs):
        return {'user': {'permissionId': 'requester-1'}}
class Service:
    def about(self): return self
    def get(self, **kwargs): return Request()
    def close(self): pass
def factory(skill):
    module = load_skill(skill)
    module.get_credentials = lambda: object()  # offline test ONLY
    discovery = types.ModuleType('googleapiclient.discovery')
    discovery.build = lambda api, version, **kw: Service()
    package = types.ModuleType('googleapiclient')
    package.__path__ = []
    sys.modules['googleapiclient'] = package
    sys.modules['googleapiclient.discovery'] = discovery
    return module.build_service('drive', 'v3')
raise SystemExit(worker.main(service_factory=factory))
'''.replace('REPO', repr(str(Path(__file__).resolve().parents[1]))))
            # E: volume root does not pass production source trust. The test
            # harness is deliberately not a production worker: bypass its SOURCE
            # admission only; retain real native process containment and limits.
            with patch.object(transport, 'WORKER', entry), patch.object(transport, 'trusted_sources', lambda _: contextlib.nullcontext()):
                runner = transport.WorkspaceSkillRunner(home, enabled=True)
                self.assertEqual(runner.identity(), {'kind': 'google-drive-permission-id-sha256', 'digest': pin()})
                self.assertEqual(runner.last_exit_code, 0)
                self.assertTrue(runner.last_reaped)

    def test_skill_drive_lost_ack_preserves_pending_and_strict_authority(self):
        import json
        from unittest.mock import patch
        from finix import core
        from finix.workspace_drive import WorkspaceError
        from tests.test_workspace_reconcile import Transport, ACCOUNT_DIGEST
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as root, patch.dict(os.environ, {'TMPDIR': str(FIXTURE)}):
            home = core.private_dir(root/'home')
            fake = Transport()
            fake.home, fake.account_pin = home, ACCOUNT_DIGEST
            drive = module.WorkspaceSkillDrive(home, 'a'*64, root/'state', runner=fake, account_digest=ACCOUNT_DIGEST)
            cipher = root/'cipher.age'
            cipher.write_bytes(b'age-encryption.org/v1\n' + bytes(range(256))*4097)
            fake.lost = True
            with self.assertRaises(WorkspaceError):
                drive.reconcile(cipher)
            pending = json.loads((root/'state'/'workspace-pending.json').read_text())
            self.assertEqual(pending['credential_authority'], drive.credential_authority)
            self.assertEqual(pending['binding']['provider'], 'workspace-skill')
            obj = pending['record']['id']
            good = fake.data[obj]
            fake.data[obj] = good[:-1] + bytes([good[-1] ^ 1])
            with self.assertRaises(core.FinixError):
                drive.reconcile(cipher)
            self.assertTrue((root/'state'/'workspace-pending.json').exists())
            self.assertTrue(cipher.exists())
            fake.data[obj] = good
            row = drive.reconcile(cipher)
            self.assertTrue(row['verified'])
            self.assertEqual(row['credential_authority'], drive.credential_authority)
            self.assertEqual(sum(op == 'upload' for op, _ in fake.calls), 1)
            fake.home = core.private_dir(root/'different-home')
            before = len(fake.calls)
            with self.assertRaises(core.FinixError):
                drive.download(row)
            self.assertEqual(len(fake.calls), before)
            fake.home = home
            journal = json.loads((root/'state'/'workspace.json').read_text())
            journal['credential_authority'] = 'finix:google'
            core.atomic_json(root/'state'/'workspace.json', journal)
            before = len(fake.calls)
            with self.assertRaises(core.FinixError):
                drive.reconcile(cipher)
            self.assertEqual(len(fake.calls), before)

    def test_inventory_paginates_and_trash_is_reversible(self):
        worker = self.worker()
        class Files(GoogleDouble):
            def list(self, **kwargs):
                self.calls.append(('list', kwargs))
                return Request({'files': [{'id': 'item-2'}]} if kwargs.get('pageToken') else
                    {'files': [{'id': 'item-1'}], 'nextPageToken': 'next'})
            def update(self, **kwargs):
                self.updated = kwargs
                return Request({'id': kwargs['fileId'], 'trashed': True})
        service = Files()
        rows = worker.perform(service, {'operation': 'list', 'account_pin': pin(), 'args': ['folder-1']})
        self.assertEqual([r['id'] for r in rows['files']], ['item-1', 'item-2'])
        self.assertEqual([c[1].get('pageToken') for c in service.calls if c[0]=='list'], [None, 'next'])
        result = worker.perform(service, {'operation': 'delete', 'account_pin': pin(), 'args': ['item-1']})
        self.assertEqual(result, {'status': 'trashed', 'fileId': 'item-1', 'permanent': False})
        self.assertEqual(service.updated, {'fileId': 'item-1', 'body': {'trashed': True}, 'fields': 'id,trashed'})

    def test_child_malformed_response_is_sanitized_and_reaped(self):
        from unittest.mock import patch
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            entry = home/'bad_response.py'
            entry.write_text("import sys\nsys.stdin.readline()\nprint('PRIVATE_PROVIDER_SECRET')\n")
            with patch.object(module, 'WORKER', entry):
                runner = module.WorkspaceSkillRunner(home, enabled=True)
                with self.assertRaises(module.SetupNeeded) as error:
                    runner._child({'operation': 'identity'}, None)
                self.assertEqual(error.exception.code, 'INVALID_RESPONSE')
                self.assertNotIn('PRIVATE_PROVIDER_SECRET', str(error.exception))
                self.assertTrue(runner.last_reaped)

    def test_real_google_client_binary_upload_readback_offline(self):
        import json
        import re
        try:
            from googleapiclient.discovery import build_from_document
            from googleapiclient.discovery_cache import get_static_doc
            from httplib2 import Response
        except ImportError:
            self.skipTest('real Google client available in packaged Hermes kernel; no installation attempted')
        from urllib.parse import urlsplit, parse_qs
        data = bytes(range(256))*40001  # spans multiple production media chunks
        class Http:
            def __init__(self):
                self.metadata, self.content, self.ranges = None, bytearray(), []
            def request(self, uri, method='GET', body=None, headers=None, **kwargs):
                query = parse_qs(urlsplit(uri).query)
                if '/about' in uri:
                    return Response({'status': '200'}), b'{"user":{"permissionId":"requester-1"}}'
                if method == 'POST' and 'uploadType' in query:
                    self.metadata = json.loads(body)
                    return Response({'status':'200','location':'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=offline'}), b''
                if method == 'PUT':
                    chunk = body.read() if hasattr(body, 'read') else body
                    self.content.extend(chunk)
                    if len(self.content) < len(data):
                        return Response({'status':'308','range':f'bytes=0-{len(self.content)-1}'}), b''
                    return Response({'status':'200'}), json.dumps({'id':'binary-real', 'mimeType':'application/octet-stream'}).encode()
                if method == 'GET' and query.get('alt') == ['media']:
                    value = next(v for k,v in headers.items() if k.lower() == 'range')
                    start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)',value).groups())
                    end = min(end, len(self.content)-1)
                    self.ranges.append((start,end))
                    return Response({'status':'206','content-range':f'bytes {start}-{end}/{len(self.content)}'}), bytes(self.content[start:end+1])
                raise AssertionError((method, uri))
        http = Http()
        service = build_from_document(get_static_doc('drive','v3'), http=http)
        worker = self.worker()
        binding_checks = []
        service = worker.BoundService(service, lambda: binding_checks.append(True))
        with private_stage() as root:
            source, output = root/'in.age', root/'out.age'
            source.write_bytes(data)
            ack = worker.perform(service, {'operation':'upload','account_pin':pin(), 'args':[str(source),
                '--id','binary-real','--name','cipher.age','--parent','folder-real','--mime-type',worker.MIME]})
            self.assertEqual(ack['id'],'binary-real')
            self.assertEqual(http.metadata['id'],'binary-real')
            worker.perform(service, {'operation':'download','account_pin':pin(),'expected_size':len(data),
                'args':['binary-real','--output',str(output)]})
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), hashlib.sha256(data).hexdigest())
            self.assertGreater(len(http.ranges),1)
            self.assertGreater(len(binding_checks), 2*len(http.ranges))

    def test_runner_detects_profile_directory_replacement_before_discovery(self):
        from finix import core
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as root:
            home = core.private_dir(root/'profile')
            runner = module.WorkspaceSkillRunner(home, enabled=True)
            home.rename(root/'old-profile')
            home.mkdir(mode=0o700)
            with self.assertRaises(module.SetupNeeded) as error:
                runner('identity')
            self.assertEqual(error.exception.code, 'BINDING_CHANGED')

    def test_contract_metadata_uses_only_bounded_regular_reads(self):
        from unittest.mock import patch
        module = importlib.import_module('finix.workspace_skill_transport')
        installed = Path(os.environ['FINIX_INSTALLED_WORKSPACE_SKILL']) if os.environ.get('FINIX_INSTALLED_WORKSPACE_SKILL') else None
        if installed is None or not installed.exists():
            self.skipTest('requires installed skill source')
        with patch.object(Path, 'read_text', side_effect=AssertionError('unbounded metadata read')):
            self.assertEqual(len(module.contract_files(installed)), 3)

    def test_missing_dependency_is_actionable_setup_needed(self):
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            skill = home/'skills'/'google-workspace'
            skill.mkdir(parents=True)
            (skill/'SKILL.md').write_text('---\nname: google-workspace\nversion: 1.2.0\n---\n')
            with self.assertRaises(module.SetupNeeded) as error:
                module.discover_skill(home)
            self.assertEqual(error.exception.code, 'SKILL_DEPENDENCY_MISSING')

    def test_explicit_runner_class_is_the_optional_backend_selection(self):
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            runner = module.WorkspaceSkillRunner(home)
            self.assertIsNone(runner.account_pin)
            with self.assertRaises(module.SetupNeeded):
                module.WorkspaceSkillRunner(home, enabled=False)

    def test_child_honors_trusted_selected_runtime_prefix(self):
        import sys
        from unittest.mock import patch
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            entry = home/'runtime_probe.py'
            entry.write_text("import sys,json\nsys.stdin.readline()\nprint(json.dumps({'result': {'marker':sys._xoptions.get('finix_contract')}}))\n")
            runner = module.WorkspaceSkillRunner(home)
            with patch.object(module, 'WORKER', entry):
                result = runner._child({'operation':'identity'}, None,
                    runtime=[sys.executable, '-I', '-B', '-X', 'finix_contract=selected'])
            self.assertEqual(result['result']['marker'], 'selected')
            self.assertTrue(runner.last_reaped)

    def test_default_worker_survives_elapsed_clock_without_progress(self):
        import itertools
        import sys
        from unittest.mock import patch
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            entry = home/'slow_fixture.py'
            entry.write_text("import sys,time,json\nsys.stdin.readline()\ntime.sleep(.15)\nprint(json.dumps({'result':{'complete':True}}))\n")
            runner = module.WorkspaceSkillRunner(home)
            with patch.object(module, 'WORKER', entry), patch('time.monotonic', side_effect=itertools.count(0, 86400)):
                result = runner._child({'operation':'identity'}, None, runtime=[sys.executable, '-I', '-B'])
            self.assertTrue(result['result']['complete'])
            self.assertTrue(runner.last_reaped)

    def test_runtime_guard_failure_does_not_suggest_sdk_oauth(self):
        from unittest.mock import patch
        from finix.workspace_drive import WorkspaceError
        module = importlib.import_module('finix.workspace_skill_transport')
        installed = Path(os.environ['FINIX_INSTALLED_WORKSPACE_SKILL']) if os.environ.get('FINIX_INSTALLED_WORKSPACE_SKILL') else None
        if installed is None or not installed.exists():
            self.skipTest('requires installed skill source')
        with patch('finix.workspace_drive.trusted_runtime', side_effect=WorkspaceError('UNTRUSTED_HELPER')):
            with self.assertRaises(module.SetupNeeded) as error:
                with module.trusted_sources(installed):
                    self.fail('untrusted runtime admitted')
            self.assertEqual(error.exception.code, 'UNTRUSTED_RUNTIME')
            self.assertNotIn('finix:google', str(error.exception))

    def test_cancellation_and_deadline_reap_owned_child_tree(self):
        import sys
        import threading
        from unittest.mock import patch
        from finix import core
        module = importlib.import_module('finix.workspace_skill_transport')
        for mode in ('cancel', 'deadline'):
            with self.subTest(mode=mode), private_stage() as home:
                entry, pidfile = home/'wait.py', home/'owned.pid'
                entry.write_text("import sys,subprocess,time\nfrom pathlib import Path\nsys.stdin.readline()\n"
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                    f"Path({str(pidfile)!r}).write_text(str(p.pid))\ntime.sleep(60)\n")
                runner = module.WorkspaceSkillRunner(home, timeout=.6 if mode == 'deadline' else 10)
                event = threading.Event()
                timer = threading.Timer(.6, event.set)
                if mode == 'cancel':
                    timer.start()
                try:
                    with patch.object(module, 'WORKER', entry):
                        with self.assertRaises(core.FinixError):
                            runner._child({'operation':'identity'}, event)
                    self.assertTrue(runner.last_reaped)
                    self.assertTrue(pidfile.exists(), 'owned descendant was actually started')
                    if os.name == 'nt':
                        import ctypes as C
                        from ctypes import wintypes as W
                        from finix import windows_native as win
                        handle = win.api(win.K, 'OpenProcess', W.HANDLE, W.DWORD, W.BOOL, W.DWORD)(0x100000, False, int(pidfile.read_text()))
                        if handle:
                            try:
                                state = win.api(win.K, 'WaitForSingleObject', W.DWORD, W.HANDLE, W.DWORD)(handle, 5000)
                                self.assertEqual(state, 0, 'owned descendant must exit on job cleanup')
                            finally:
                                win.close(handle)
                finally:
                    timer.cancel()
                    if timer.ident is not None:
                        timer.join(5)

    def test_mutation_unknown_ack_is_never_retried_or_exposed(self):
        worker = self.worker()
        class Files(GoogleDouble):
            def create(self, **kwargs):
                self.calls.append(('create', kwargs))
                class Lost:
                    def execute(self, **kw):
                        raise ConnectionError('PRIVATE_PROVIDER_SECRET owner@example.invalid')
                return Lost()
        service = Files()
        with self.assertRaises(worker.SkillError) as error:
            worker.perform(service, {'operation':'create-folder', 'account_pin':pin(),
                'args':['--id','folder-unknown','--name','Finix-private']})
        self.assertEqual(error.exception.code, 'UNCERTAIN_ACK')
        self.assertNotIn('PRIVATE_PROVIDER_SECRET', str(error.exception))
        self.assertEqual(sum(op=='create' for op,_ in service.calls), 1)

    def test_missing_google_runtime_dependency_is_closed_setup_error(self):
        from unittest.mock import patch
        module = importlib.import_module('finix.workspace_skill_transport')
        with private_stage() as home:
            entry = home/'missing_dep.py'
            entry.write_text("import sys\nsys.path.insert(0, REPO)\nfrom finix.workspace_skill_worker import main\n"
                "def missing(skill): raise ModuleNotFoundError('no google client')\n"
                "raise SystemExit(main(service_factory=missing))\n".replace('REPO',repr(str(Path(__file__).resolve().parents[1]))))
            runner = module.WorkspaceSkillRunner(home)
            with patch.object(module, 'WORKER', entry):
                response = runner._child({'operation':'identity', 'home':str(home), 'skill':str(home),
                    'binding': module.binding_snapshot(home)}, None)
            self.assertEqual(response, {'error':'SKILL_SETUP_REQUIRED'})
            self.assertTrue(runner.last_reaped)

    def test_incomplete_inventory_is_not_reported_as_complete(self):
        worker = self.worker()
        class Files(GoogleDouble):
            def list(self, **kwargs):
                return Request({'files': [], 'incompleteSearch': True})
        with self.assertRaises(worker.SkillError) as error:
            worker.perform(Files(), {'operation':'list','account_pin':pin(),'args':['folder-1']})
        self.assertEqual(error.exception.code, 'INVALID_RESPONSE')

    def test_preallocated_id_requires_same_service_account_pin(self):
        worker = self.worker()
        service = GoogleDouble()
        result = worker.perform(service, {'operation': 'generate-id', 'args': [], 'account_pin': pin()})
        self.assertEqual(result, {'id': 'allocated-1'})
        self.assertEqual([c[0] for c in service.calls], ['about', 'generate'])
        for selected in (None, pin('another-account')):
            service.calls.clear()
            with self.assertRaises(worker.SkillError) as error:
                worker.perform(service, {'operation': 'generate-id', 'args': [], 'account_pin': selected})
            self.assertEqual(error.exception.code, 'ACCOUNT_CHANGED')
            self.assertEqual([c[0] for c in service.calls], ['about'])


if __name__ == '__main__':
    unittest.main()
