"""Default journal creation must not depend on a fixture's restrictive umask."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == 'posix', 'POSIX mkdir parent-mode semantics')
class JournalParentCreationTests(unittest.TestCase):
    def run_case(self, existing=False):
        repo = Path(__file__).resolve().parents[1]
        sdk = os.environ.get('FINIX_TEST_SDK_SOURCE')
        self.assertTrue(sdk)
        code = '''
import json,os,sys
from pathlib import Path
sys.path[:0]=[REPO, str(Path(REPO)/'tests'), SDK]
from test_current_workspace_skill_restore_closure import CurrentWorkspaceClosureTests
from finix import core,default_recovery as cold
case=CurrentWorkspaceClosureTests('runTest')
case.setUp()
root=case.root
journal=root/'cache/finix/default-recovery'
try:
 if EXISTING:
  core.private_dir(root/'cache')
  core.private_dir(root/'cache/finix')
  journal.mkdir(mode=0o755)
 try:
  engine=case.prepare(archived=False)
  result={'outcome':engine.status()['state']}
 except cold.RecoveryError as e:
  import traceback
  sys.stderr.write(json.dumps([{'module':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(e.__traceback__)]))
  result={'outcome':'refused','code':e.code}
 result['journal_mode']=oct(journal.stat().st_mode & 0o777)
 result['root_mode']=oct(root.stat().st_mode & 0o777)
 print(json.dumps(result))
finally:
 case.doCleanups()
'''
        setup = 'REPO=' + repr(str(repo)) + ';SDK=' + repr(sdk) + ';EXISTING=' + repr(existing) + '\n'
        # Native child-only umask; never mutate the runner/agent's process mask.
        child = subprocess.run([sys.executable, '-B', '-c', setup + code],
            capture_output=True, text=True, umask=0o022, timeout=35)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.last_child_details = child.stderr
        return json.loads(child.stdout)

    def test_fresh_journal_parent_is_private_under_normal_umask(self):
        result = self.run_case()
        self.assertEqual(result, {'outcome': 'prepared', 'journal_mode': '0o700', 'root_mode': '0o700'}, self.last_child_details)

    def inventory_fixture(self):
        import uuid
        from finix import core
        root = core.private_dir(Path(os.environ['FINIX_CURRENT_CLOSURE_TEST_ROOT']) / uuid.uuid4().hex)
        owned = root / 'memories'
        owned.mkdir(mode=0o755)
        owned.chmod(0o755)  # Deliberate SDK-shaped mode on this new test object.
        (owned / 'note.md').write_bytes(b'synthetic note')
        return root, owned

    def test_owned_context_accepts_sdk_readonly_descendant_of_private_root(self):
        from contextlib import ExitStack
        from finix import default_recovery as cold
        root, owned = self.inventory_fixture()
        with ExitStack() as stack:
            self.assertEqual(set(cold._owned_inventory(root, stack)), {'memories/note.md'})
        self.assertEqual(owned.stat().st_mode & 0o777, 0o755)

    def test_owned_context_refuses_writable_descendant_without_repair(self):
        from contextlib import ExitStack
        from finix import default_recovery as cold
        root, owned = self.inventory_fixture()
        owned.chmod(0o777)
        with ExitStack() as stack, self.assertRaises(cold.RecoveryError):
            cold._owned_inventory(root, stack)
        self.assertEqual(owned.stat().st_mode & 0o777, 0o777)

    def test_owned_context_refuses_nonprivate_root_without_repair(self):
        from contextlib import ExitStack
        from finix import default_recovery as cold
        root, _ = self.inventory_fixture()
        root.chmod(0o755)
        with ExitStack() as stack, self.assertRaises(cold.RecoveryError):
            cold._owned_inventory(root, stack)
        self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_inventory_cannot_borrow_an_unrelated_private_boundary(self):
        from contextlib import ExitStack
        from finix import default_recovery as cold
        root, owned = self.inventory_fixture()
        with ExitStack() as stack, self.assertRaises(cold.RecoveryError):
            cold._inventory(owned, stack, private_from=root / 'unrelated')

    def test_existing_nonprivate_journal_is_refused_without_repair(self):
        result = self.run_case(existing=True)
        self.assertEqual(result, {'outcome': 'refused', 'code': 'UNSAFE_ACL', 'journal_mode': '0o755', 'root_mode': '0o700'})
