"""Real interpreter-launcher equivalence; no profile mutation or cloud calls."""
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

import test_sqlite_generation_descriptor as descriptor_tests
from finix import default_recovery as engine


@unittest.skipUnless(os.name == 'nt', 'Windows venv redirector regression')
class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        descriptor_tests.SQLiteDescriptorTests.setUp(self)

    def test_base_image_and_venv_launcher_verify_same_unchanged_generation(self):
        from ctypes import wintypes as W
        import hermes_constants
        import yaml
        api=ctypes.WinDLL('kernel32',use_last_error=True).GetModuleFileNameW
        api.argtypes=[W.HMODULE,W.LPWSTR,W.DWORD];api.restype=W.DWORD
        buf=ctypes.create_unicode_buffer(32768);count=api(None,buf,len(buf))
        self.assertTrue(0<count<len(buf))
        base=Path(buf.value);launcher=Path(sys.executable)
        self.assertFalse(base.samefile(launcher),'Run this regression with the actual managed venv launcher')
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE fixture(value TEXT)')
            db.execute("INSERT INTO fixture VALUES ('preserved user content')")
        boot="""
import os,sys,json,pathlib
sys.path[:0]=json.loads(sys.argv[1])
def guard(event,args):
 if event in ('socket.connect','socket.bind','socket.getaddrinfo'):raise AssertionError('NETWORK_FORBIDDEN')
sys.addaudithook(guard)
from hermes_constants import get_default_hermes_root,get_hermes_home
assert get_default_hermes_root()==get_hermes_home()==pathlib.Path(os.environ['HERMES_HOME'])
from finix import default_recovery as e
p=pathlib.Path(sys.argv[2]);temp=pathlib.Path(sys.argv[3])
if sys.argv[4]=='capture':result=e._sqlite_generation(p,temp)
else:
 sealed=json.loads(pathlib.Path(sys.argv[4]).read_text());e._verify_sqlite_generation(p,temp,sealed);result={'verified':True}
print(json.dumps(result))
"""
        paths=[str(Path(engine.__file__).parent.parent),str(Path(hermes_constants.__file__).parent),str(Path(yaml.__file__).parent.parent)]
        def invoke(executable,mode):
            p=subprocess.run([str(executable),'-I','-B','-c',boot,json.dumps(paths),str(self.path),str(self.temp),mode],capture_output=True,text=True,timeout=20)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)
        sealed=invoke(base,'capture')
        descriptor=self.directory/'sealed-runtime.json';descriptor.write_text(json.dumps(sealed))
        self.assertEqual(invoke(launcher,str(descriptor)),{'verified':True})
        current=invoke(launcher,'capture');self.assertEqual(current,sealed)
        # Normalization must not make a different executable identity/hash acceptable.
        sealed['producer_pins']['runtime']['executable_leaf']['sha256']='0'*64
        descriptor.write_text(json.dumps(sealed))
        with self.assertRaises(AssertionError) as rejected:
            invoke(launcher,str(descriptor))
        self.assertIn('TARGET_CHANGED_REPREPARE_REQUIRED',str(rejected.exception))
