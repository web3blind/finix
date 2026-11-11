"""Independent review's synthetic ACL proof, asserted fail-closed."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from finix import core
from finix.integration import Profile


@unittest.skipUnless(os.name == 'nt', 'native Windows ACL proof')
class AncestryTests(unittest.TestCase):
    def test_untrusted_ancestor_rejected_before_routing_creation(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            base = Path(tmp)
            base.rmdir()
            core.private_dir(base)
            ancestor = core.private_dir(base / 'UntrustedParent')
            home = core.private_dir(ancestor / 'SyntheticHome')
            subprocess.run(['icacls.exe', str(ancestor), '/grant', '*S-1-1-0:(F)'], capture_output=True, check=True)
            try:
                with win.directory(home) as anchor:
                    self.assertFalse(all(win.trusted_handle(h, ancestor=True) for h in anchor.handles[:-1]))
                    self.assertTrue(win.trusted_handle(anchor.handle, private=True))
                with self.assertRaises((OSError, core.FinixError)):
                    Profile(home, default_root=home)
                self.assertFalse((ancestor / 'finix-data').exists())
                self.assertEqual(list(home.iterdir()), [])
            finally:
                subprocess.run(['icacls.exe', str(ancestor), '/remove:g', '*S-1-1-0'], capture_output=True, check=True)

    def test_reopening_rejects_new_untrusted_ancestor_without_state_change(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            base = Path(tmp)
            base.rmdir()
            core.private_dir(base)
            ancestor = core.private_dir(base / 'InitiallyTrusted')
            home = core.private_dir(ancestor / 'SyntheticHome')
            original = Profile(home, default_root=home)
            core.atomic_json(original.root / 'settings.json', {'recipient': 'synthetic-public'})
            state = original.root.parent
            before = {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()}
            subprocess.run(['icacls.exe', str(ancestor), '/grant', '*S-1-1-0:(F)'], capture_output=True, check=True)
            try:
                with self.assertRaises((OSError, core.FinixError)):
                    Profile(home, default_root=home)
                self.assertEqual(before, {p.relative_to(state): p.read_bytes() for p in state.rglob('*') if p.is_file()})
            finally:
                subprocess.run(['icacls.exe', str(ancestor), '/remove:g', '*S-1-1-0'], capture_output=True, check=True)
            reopened = Profile(home, default_root=home)
            self.assertEqual(original.profile_id, reopened.profile_id)
            self.assertEqual(reopened.config()['recipient'], 'synthetic-public')

    def test_home_and_routing_anchors_remain_held_through_atomic_record(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as tmp:
            base = Path(tmp)
            base.rmdir()
            core.private_dir(base)
            home = core.private_dir(base / 'SyntheticHome')
            atomic = core.atomic_json
            observed = []
            def checked(path, value):
                if Path(path).name.startswith('home-binding-'):
                    for target in (home, home.parent, Path(path).parent):
                        with self.assertRaises(OSError):
                            target.rename(target.with_name(target.name + '-moved'))
                    observed.append(True)
                return atomic(path, value)
            with patch.object(core, 'atomic_json', checked):
                Profile(home, default_root=home)
            self.assertEqual(observed, [True])
