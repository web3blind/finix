"""Native handle ownership for a Windows venv redirector (no gateway mocks)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from finix.recovery_activation import _WindowsLaunch

class ReadyIdentity(unittest.TestCase):
    def test_exact_ready_roster_only(self):
        from finix.recovery_activation import _ready_identity
        target = Path('C:/fixture/profiles/recovered')
        good = dict(pid=42, state='ready', hermes_home=str(target),
                    served_profiles=[dict(home=str(target), profile_id=str(target))])
        self.assertTrue(_ready_identity(good, target))
        for delta in ({'hermes_home': 'OTHER'}, {'state': 'starting'},
                      {'pending': True}, {'error': 'refused'},
                      {'served_profiles': []},
                      {'served_profiles': good['served_profiles'] * 2}):
            self.assertFalse(_ready_identity({**good, **delta}, target))

@unittest.skipUnless(os.name == 'nt', 'native Windows only')
class RedirectorOwnership(unittest.TestCase):
    def test_foreign_principal_and_prelaunch_creation_refused(self):
        import time
        from unittest.mock import patch
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        owner = _WindowsLaunch(p)
        try:
            time.sleep(.1)
            with patch.object(owner, '_sid', return_value='S-1-0-0'):
                owner.capture()
                self.assertEqual(owner.runtime_pids, set())
            created = owner.launch_time
            owner.launch_time = 2**64 - 1
            owner.capture()
            self.assertEqual(owner.runtime_pids, set())
            owner.launch_time = created
            with patch('os.path.samefile', return_value=False):
                owner.capture()
                self.assertEqual(owner.runtime_pids, set())
            owner.capture()
            self.assertTrue(owner.runtime_pids)
        finally:
            owner.stop()
            if p.poll() is None: p.terminate()
            p.wait(timeout=10)
            owner.close()

    def test_native_child_bound_to_retained_launcher(self):
        import psutil
        import time
        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        try:
            owner=_WindowsLaunch(p)
            for _ in range(50):
                owner.capture()
                if owner.runtime_pids: break
                time.sleep(.02)
            children=psutil.Process(p.pid).children()
            self.assertTrue(children)
            self.assertTrue(owner.matches(children[0].pid))
            self.assertFalse(owner.matches(other.pid))
            self.assertFalse(owner.matches(True))
            self.assertFalse(owner.matches(str(children[0].pid)))
            # A stale/reused creation identity cannot authorize the live handle.
            handle, created = owner.handles[children[0].pid]
            owner.handles[children[0].pid] = (handle, created + 1)
            self.assertFalse(owner.matches(children[0].pid))
            owner.handles[children[0].pid] = (handle, created)
            owner.stop()
            self.assertFalse(psutil.pid_exists(children[0].pid))
        finally:
            for child in (p,other):
                if child.poll() is None: child.terminate(); child.wait()
            if 'owner' in locals(): owner.close()
