"""Offline scheduling regressions; native Windows is not Linux acceptance."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from finix import workspace_skill_transport as transport
from tests.test_workspace_skill_transport import private_stage


class BindingTests(unittest.TestCase):
    def fixture(self, root):
        installed = os.environ.get('FINIX_INSTALLED_WORKSPACE_SKILL')
        if not installed:
            self.skipTest('requires explicit installed skill code path')
        home = root/'profile'
        skill = home/'skills'/'productivity'/'google-workspace'
        (skill/'scripts').mkdir(parents=True)
        for relative in ('SKILL.md', 'scripts/google_api.py', 'scripts/_hermes_home.py'):
            (skill/relative).write_bytes((Path(installed)/relative).read_bytes())
        return home, skill

    def entry(self, root, source):
        path = root/'offline_entry.py'
        path.write_text('import sys\nsys.path.insert(0, '+repr(str(Path(__file__).resolve().parents[1]))+')\n'+'''import builtins, io, socket
_original_open=builtins.open
def _offline_open(file, *args, **kwargs):
 if any(s in str(file) for s in ('google_token', 'client_secret', 'auth.json')):
  raise AssertionError('offline test forbids credential access')
 return _original_open(file, *args, **kwargs)
builtins.open=io.open=_offline_open
def _offline_network(*args, **kwargs):raise AssertionError('offline test forbids network')
socket.socket.connect=socket.socket.connect_ex=socket.create_connection=_offline_network
'''+source)
        return path

    def test_parent_selected_identity_crosses_child_before_service_construction(self):
        with private_stage() as root:
            home, skill = self.fixture(root)
            marker = root/'service-construction'
            entry = self.entry(root, '''from pathlib import Path
from finix.workspace_skill_worker import main
class Reply:
 def execute(self, **kw):return {'user': {'permissionId': 'requester-1'}}
class Service:
 def about(self):return self
 def get(self, **kw):return Reply()
 def close(self):pass
def factory(skill):
 Path(MARKER).write_text('constructed')
 return Service()
raise SystemExit(main(service_factory=factory))
'''.replace('MARKER', repr(str(marker))))
            runner = transport.WorkspaceSkillRunner(home)
            @contextlib.contextmanager
            def modeled_posix_swap(_):
                # Test-only bypass of source admission models the POSIX rename
                # schedule. It does NOT claim Windows production admits it.
                home.rename(root/'original')
                shutil.copytree(root/'original', home)
                yield None
            with patch.object(transport, 'trusted_sources', modeled_posix_swap), patch.object(transport, 'WORKER', entry):
                with self.assertRaises(transport.SetupNeeded) as error:
                    runner('identity')
            self.assertEqual(error.exception.code, 'BINDING_CHANGED')
            self.assertFalse(marker.exists(), 'zero service/credential construction')
            self.assertTrue(runner.last_reaped)
    def test_unpinned_siblings_never_enter_import_closure(self):
        for sibling in ('hermes_constants', 'googleapiclient', 'shutil'):
            with self.subTest(sibling=sibling), private_stage() as root:
                home, skill = self.fixture(root)
                marker = root/'unpinned-executed'
                code = 'from pathlib import Path\nimport os\nPath('+repr(str(marker))+').write_text("executed")\n'
                if sibling == 'hermes_constants':
                    code += 'def get_hermes_home():return Path(os.environ["HERMES_HOME"])\ndef display_hermes_home():return str(get_hermes_home())\n'
                (skill/'scripts'/(sibling+'.py')).write_text(code)
                entry = self.entry(root, '''import json, importlib
from finix.workspace_skill_transport import load_skill
request=json.loads(sys.stdin.readline())
module=load_skill(request['skill'])
try:importlib.import_module(SIBLING)
except ModuleNotFoundError:pass
print(json.dumps({'result': {'loaded':True}}))
'''.replace('SIBLING', repr(sibling)))
                runner = transport.WorkspaceSkillRunner(home)
                with patch.object(transport, 'WORKER', entry):
                    response = runner._child({'skill': str(skill)}, None)
                self.assertEqual(response, {'result': {'loaded': True}})
                self.assertFalse(marker.exists(), 'unpinned sibling code must never execute')
                self.assertTrue(runner.last_reaped)
    def test_cached_home_helper_without_runtime_provenance_is_refused(self):
        with private_stage() as root:
            home, skill = self.fixture(root)
            entry = self.entry(root, '''import types, json, os
from pathlib import Path
from finix.workspace_skill_transport import load_skill, SetupNeeded
fake=types.ModuleType('hermes_constants')
fake.get_hermes_home=lambda:Path(os.environ['HERMES_HOME'])
fake.display_hermes_home=lambda:str(fake.get_hermes_home())
sys.modules['hermes_constants']=fake
request=json.loads(sys.stdin.readline())
try:
 load_skill(request['skill'])
 print(json.dumps({'result': {'refused':False}}))
except SetupNeeded as exc:print(json.dumps({'result': {'refused':exc.code=='SKILL_CONTRACT_CHANGED'}}))
''')
            runner = transport.WorkspaceSkillRunner(home)
            with patch.object(transport, 'WORKER', entry):
                response = runner._child({'skill': str(skill)}, None)
            self.assertEqual(response, {'result': {'refused': True}})
    def test_same_profile_unpinned_source_directory_replacement_is_refused(self):
        with private_stage() as root:
            home, skill = self.fixture(root)
            marker = root/'service-construction'
            entry = self.entry(root, '''from pathlib import Path
from finix.workspace_skill_worker import main
def factory(skill):
 Path(MARKER).write_text('constructed')
 raise AssertionError('no construction allowed')
raise SystemExit(main(service_factory=factory))
'''.replace('MARKER', repr(str(marker))))
            runner = transport.WorkspaceSkillRunner(home)
            selected = runner._home_identity
            @contextlib.contextmanager
            def source_swap(_):
                (skill/'scripts').rename(skill/'old-scripts')
                shutil.copytree(skill/'old-scripts', skill/'scripts')
                yield None
            with patch.object(transport, 'trusted_sources', source_swap), patch.object(transport, 'WORKER', entry):
                with self.assertRaises(transport.SetupNeeded) as error:
                    runner('identity')
            self.assertEqual(error.exception.code, 'BINDING_CHANGED')
            self.assertEqual(transport.WorkspaceDrive._directory_identity(home), selected)
            self.assertFalse(marker.exists())
            self.assertTrue(runner.last_reaped)

    @unittest.skipUnless(os.name == 'nt', 'native NTFS pin test')
    def test_child_native_pins_block_rename_before_load_and_during_service(self):
        with private_stage() as root:
            home, skill = self.fixture(root)
            marker = root/'pin-results.json'
            entry = self.entry(root, '''from pathlib import Path
import json, os
from finix.workspace_skill_worker import main
from finix.workspace_skill_transport import load_skill
checks=[]
def attempt(stage):
 for path in (Path(os.environ['HERMES_HOME']), Path(SKILL)/'scripts'):
  try:path.rename(path.with_name(path.name+'-replaced'))
  except OSError:checks.append(stage)
  else:raise AssertionError('native anchor failed to deny rename')
class Reply:
 def execute(self, **kw):
  attempt('execute')
  return {'user':{'permissionId':'requester-1'}}
class Service:
 def about(self):return self
 def get(self, **kw):return Reply()
 def close(self):
  attempt('close')
  Path(MARKER).write_text(json.dumps(checks))
def factory(skill):
 attempt('before-load')
 module=load_skill(skill)
 attempt('before-credentials')
 # No grant loader/network is invoked; this is an explicit offline double.
 return Service()
raise SystemExit(main(service_factory=factory))
'''.replace('MARKER', repr(str(marker))).replace('SKILL', repr(str(skill))))
            runner = transport.WorkspaceSkillRunner(home)
            with patch.object(transport, 'trusted_sources', lambda _: contextlib.nullcontext()), patch.object(transport, 'WORKER', entry):
                self.assertEqual(runner.identity()['digest'], hashlib.sha256(b'requester-1').hexdigest())
            self.assertEqual(json.loads(marker.read_text()), ['before-load']*2+['before-credentials']*2+['execute']*2+['close']*2)
            self.assertTrue(runner.last_reaped)

    @unittest.skipUnless(sys.platform == 'linux', 'requires genuine Linux proc-fd execution; not a Windows model')
    def test_linux_mid_credential_lookup_uses_original_fd_root(self):
        runtime_code = os.environ.get('FINIX_HERMES_RUNTIME_CODE')
        if not runtime_code:
            self.skipTest('requires explicit genuine Hermes runtime code path')
        with private_stage() as root:
            home, skill = self.fixture(root)
            (home/'fixture-marker').write_text('original')
            marker = root/'selected-marker'
            entry = self.entry(root, '''sys.path.insert(0, RUNTIME_CODE)
import types, os, shutil
from pathlib import Path
from finix.workspace_skill_worker import main
from finix.workspace_skill_transport import load_skill
class Service:
 def close(self):pass
def factory(skill):
 module=load_skill(skill)
 # Exercise the genuine public runtime getter, not a manufactured SDK module.
 from hermes_constants import get_hermes_home
 assert get_hermes_home()==module.HERMES_HOME
 assert str(module.HERMES_HOME).startswith('/proc/self/fd/')
 def offline_credentials():
  home=Path(PROFILE_PATH_VALUE)
  home.rename(home.with_name('original'))
  shutil.copytree(home.with_name('original'), home)
  (home/'fixture-marker').write_text('replacement')
  Path(MARKER).write_text((module.HERMES_HOME/'fixture-marker').read_text())
  return object()
 module.get_credentials=offline_credentials
 package=types.ModuleType('googleapiclient');package.__path__=[]
 discovery=types.ModuleType('googleapiclient.discovery')
 discovery.build=lambda *a, **kw:Service()
 sys.modules['googleapiclient']=package;sys.modules['googleapiclient.discovery']=discovery
 return module.build_service('drive','v3')
raise SystemExit(main(service_factory=factory))
'''.replace('PROFILE_PATH_VALUE', repr(str(home))).replace('MARKER', repr(str(marker))).replace('RUNTIME_CODE', repr(runtime_code)))
            runner = transport.WorkspaceSkillRunner(home)
            with patch.object(transport, 'trusted_sources', lambda _: contextlib.nullcontext()), patch.object(transport, 'WORKER', entry):
                with self.assertRaises(transport.SetupNeeded) as error:
                    runner('identity')
            self.assertEqual(error.exception.code, 'BINDING_CHANGED')
            self.assertEqual(marker.read_text(), 'original')
            self.assertTrue(runner.last_reaped)
    def test_download_chunk_change_is_refused_before_verified_ack(self):
        from finix import workspace_skill_worker as worker
        from tests.test_workspace_skill_transport import GoogleDouble, pin
        changed = False
        def check():
            if changed:
                raise worker.SkillError('BINDING_CHANGED')
        class Service(GoogleDouble):
            def get_media(self, **kw):return object()
        class Download:
            def __init__(self, sink, request, **kw):self.sink = sink
            def next_chunk(self, **kw):
                nonlocal changed
                self.sink.write(b'x')
                changed = True
                return None, True
        with private_stage() as root:
            with self.assertRaises(worker.SkillError) as error:
                worker.perform(worker.BoundService(Service(), check),
                    {'operation':'download', 'account_pin':pin(), 'expected_size':1,
                     'args':['item-1','--output',str(root/'partial.age')]}, download_factory=Download)
            self.assertEqual(error.exception.code, 'BINDING_CHANGED')
    def test_genuine_public_home_getter_matches_selected_native_profile(self):
        runtime_code = os.environ.get('FINIX_HERMES_RUNTIME_CODE')
        if not runtime_code:
            self.skipTest('requires explicit genuine Hermes runtime code path')
        with private_stage() as root:
            home, skill = self.fixture(root)
            entry = self.entry(root, '''sys.path.insert(0, RUNTIME_CODE)
import os
from pathlib import Path
from finix.workspace_skill_worker import main
from finix.workspace_skill_transport import load_skill
class Reply:
 def execute(self, **kw):return {'user':{'permissionId':'requester-1'}}
class Service:
 def about(self):return self
 def get(self, **kw):return Reply()
 def close(self):pass
def factory(skill):
 module=load_skill(skill)
 import hermes_constants
 assert Path(hermes_constants.__file__).resolve()==Path(RUNTIME_CODE).resolve()/'hermes_constants.py'
 assert module.get_hermes_home is hermes_constants.get_hermes_home
 assert module.HERMES_HOME==hermes_constants.get_hermes_home()==Path(os.environ['HERMES_HOME'])
 # Never invoke credentials or network: genuine getter, explicit offline API.
 return Service()
raise SystemExit(main(service_factory=factory))
'''.replace('RUNTIME_CODE', repr(runtime_code)))
            runner = transport.WorkspaceSkillRunner(home)
            with patch.object(transport, 'trusted_sources', lambda _: contextlib.nullcontext()), patch.object(transport, 'WORKER', entry):
                self.assertEqual(runner.identity()['digest'], hashlib.sha256(b'requester-1').hexdigest())
            self.assertTrue(runner.last_reaped)

    def test_verified_bytes_are_compiled_not_later_path_contents(self):
        with private_stage() as root:
            home, skill = self.fixture(root)
            marker = root/'unverified-source-executed'
            entry = self.entry(root, '''import json
from pathlib import Path
from finix import workspace_skill_transport as transport
request=json.loads(sys.stdin.readline())
original=transport.contract_files
calls=0
def swap(skill):
 global calls
 result=original(skill)
 calls+=1
 if calls==2:
  (Path(skill)/'scripts'/'google_api.py').write_text('from pathlib import Path\\nPath('+repr(MARKER)+').write_text("executed")\\n')
 return result
transport.contract_files=swap
try:
 transport.load_skill(request['skill'])
 print(json.dumps({'result':{'refused':False}}))
except transport.SetupNeeded as exc:
 print(json.dumps({'result':{'refused':exc.code=='SKILL_CONTRACT_CHANGED'}}))
'''.replace('MARKER', repr(str(marker))))
            runner = transport.WorkspaceSkillRunner(home)
            with patch.object(transport, 'WORKER', entry):
                response = runner._child({'skill':str(skill)}, None)
            self.assertEqual(response, {'result':{'refused':True}})
            self.assertFalse(marker.exists())
            self.assertTrue(runner.last_reaped)


if __name__ == '__main__':
    unittest.main()
