"""Offline real-SDK PM child regression. All credentials/providers are synthetic."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from test_workspace_portability import NativeFixture
from finix.workspace_drive import WorkspaceRunner, WorkspaceError

SDK = os.environ.get('FINIX_RUNTIME_SDK_ROOT')

@unittest.skipUnless(SDK, 'requires explicitly selected real SDK fixture runner')
class PMChildRuntimeTests(NativeFixture):
    def setUp(self):
        super().setUp()
        self.sdk = Path(SDK)
        self.assertEqual(Path(sys.executable).resolve(), Path(sys._base_executable).resolve())
        from pm.environments import selected_venv, site_packages
        self.venv = selected_venv(self.sdk)
        self.assertIn(str(site_packages(self.venv)), sys.path)
        real = self.sdk / 'skills/productivity/google-workspace/scripts/google_api.py'
        (self.root / 'google_token.json').write_text(json.dumps(dict(token='synthetic-only', refresh_token='synthetic-only', client_id='synthetic-only', client_secret='synthetic-only', expiry='2099-01-01T00:00:00Z', scopes=['https://www.googleapis.com/auth/drive'])))
        self.script.write_text('''import sys,importlib.util,json
from pathlib import Path
def guard(event,args):
 if event.startswith('socket.'): raise RuntimeError('NETWORK_DENIED')
sys.addaudithook(guard)
spec=importlib.util.spec_from_file_location('real_sdk_google',REAL)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.TOKEN_PATH=Path(__file__).parent/'google_token.json'
class Service:
 def files(self): return self
 def about(self): return self
 def get(self,**kwargs): return self
 def execute(self): return {'id':'offline-proof','credentials_loaded':True,'isolated':sys.flags.isolated}
def build_service(*args):
 from googleapiclient.discovery import build
 assert m.get_credentials().valid
 return Service()
'''.replace('REAL', repr(str(real))))

    def test_child_invalid_json_and_private_stderr_stay_closed(self):
        self.script.write_text('import sys\ndef main():\n print("synthetic-private-token",file=sys.stderr)\n print("not-json")\n')
        with self.assertRaises(WorkspaceError) as caught:
            WorkspaceRunner(self.root,script=self.script)('probe')
        self.assertEqual(caught.exception.code,'INVALID_RESPONSE')
        self.assertNotIn('synthetic-private-token',str(caught.exception))

    def test_standalone_fresh_venv_without_sdk_keeps_own_runtime(self):
        from finix.workspace_drive import child_env
        venv = self.root/'standalone'
        subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True,capture_output=True)
        python = venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        provider = self.root/'standalone_provider.py'
        provider.write_text('import json\\ndef main(): print(json.dumps({"standalone":True}))\\n'.replace('\\n','\n'))
        source = str(Path(__file__).parents[1])
        code = "import sys;sys.path.insert(0,sys.argv[1]);sys.addaudithook(lambda e,a: (_ for _ in ()).throw(RuntimeError('NETWORK_DENIED')) if e.startswith('socket.') else None);from finix.workspace_drive import WorkspaceRunner;assert WorkspaceRunner(sys.argv[2],script=sys.argv[3])('probe')=={'standalone':True}"
        result = subprocess.run([str(python),'-I','-B','-c',code,source,str(self.root),str(provider)],env=child_env(self.root),capture_output=True,timeout=20)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,b'',b''))

    def test_baseline_base_child_loses_managed_google_dependencies(self):
        from finix.workspace_drive import child_env
        code = "import json;\ntry: import google.auth\nexcept ModuleNotFoundError as e: print(json.dumps({'type':type(e).__name__,'module':e.name}))\nelse: raise AssertionError('base unexpectedly has Google dependencies')"
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code], env=child_env(self.root), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(result.stdout), {'type':'ModuleNotFoundError','module':'google'})

    def test_generation_changed_during_pinning_fails_closed(self):
        from pm import environments
        with patch.object(environments, 'selected_venv', side_effect=[self.venv,self.venv,self.root]):
            with self.assertRaises(WorkspaceError):
                WorkspaceRunner(self.root, script=self.script)('metadata','offline-proof')

    def test_connections_start_runs_real_worker_without_ambient_pythonpath(self):
        import time
        from types import SimpleNamespace
        from finix.connections import Connections
        profile = SimpleNamespace(home=self.root, root=self.root/'state', profile_id='synthetic', config=lambda: {})
        manager = Connections()
        with patch('finix.connections.workspace_prerequisites', return_value=self.script):
            manager._start(profile, 'workspace', 'Finix', True, True)
        session = manager.sessions['synthetic']
        deadline = time.monotonic()+20
        while session['public']['status'] == 'working' and time.monotonic()<deadline:
            time.sleep(.02)
        try:
            self.assertEqual(session['public']['status'], 'verified', session['public'])
        finally:
            manager._stop(session, 'cancelled')

    def test_workspace_metadata_imports_real_sdk_credentials_in_pm_child(self):
        self.assertEqual(WorkspaceRunner(self.root, script=self.script)('metadata','offline-proof'),
                         {'id':'offline-proof','credentials_loaded':True,'isolated':1})

    def test_connection_worker_uses_same_pinned_pm_runtime(self):
        from finix.workspace_drive import trusted_runtime, child_env
        worker = Path(__file__).parents[1] / 'finix/connection_worker.py'
        with trusted_runtime(self.script, entry=worker) as (prefix, wrapper):
            argv = [*prefix, wrapper, 'workspace-reuse', str(self.root), str(self.root), str(self.script)]
            if os.name == 'nt':
                from finix.windows_native import popen, cleanup
                p = popen(argv, env=child_env(self.root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            else:
                p = subprocess.Popen(argv, env=child_env(self.root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                out, err = p.communicate(timeout=20)
                self.assertEqual(err, b'')
                self.assertEqual(p.returncode, 0, out)
                self.assertEqual(json.loads(out), {'status':'verified'})
            finally:
                if os.name == 'nt': cleanup(p)

    def test_changed_generation_fails_closed(self):
        from pm import environments
        with patch.object(environments, 'running_from_selected_environment', return_value=False):
            with self.assertRaises(WorkspaceError) as caught:
                WorkspaceRunner(self.root, script=self.script)('metadata','offline-proof')
        self.assertEqual(caught.exception.code, 'UNTRUSTED_HELPER')

    def test_foreign_sdk_module_fails_closed(self):
        from pm import environments
        with patch.object(environments, '__file__', str(self.root/'environments.py')):
            with self.assertRaises(WorkspaceError):
                WorkspaceRunner(self.root, script=self.script)('metadata','offline-proof')
