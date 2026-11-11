"""Native POSIX effective privacy; no SDK or existing fixture permission repair."""
import os
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from finix import default_recovery as engine, recovery

@unittest.skipIf(os.name=='nt','POSIX DAC only; Windows per-component DACL unchanged')
class EffectivePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'root'; self.root.mkdir(mode=0o700)
        self.child=self.root/'skills'/'apple'/'apple-notes'
        self.child.mkdir(parents=True,mode=0o755)
    def test_sdk_style_0755_below_private_retained_root(self):
        with ExitStack() as stack:
            anchor=engine._retain(stack,self.child,self.root)
            anchor.check()
            self.assertEqual(os.fstat(anchor.fd).st_ino,self.child.stat().st_ino)
    def test_double_slash_sticky_current_owner_refused(self):
        # Actual filesystem metadata; no chown or mocked ownership.
        self.child.chmod(0o1777)
        self.assertEqual(self.child.stat().st_uid,os.geteuid())
        from finix import core
        root=core.checked_path('//'+str(self.root).lstrip('/'))
        child=core.checked_path(str(root.joinpath(*self.child.relative_to(self.root).parts)))
        self.assertEqual(root.anchor,'//')
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):
            engine._retain(stack,child,root)

    def test_unsupported_anchors_refused_before_chain(self):
        from finix import core
        alias=core.checked_path('//'+str(self.root).lstrip('/'))
        pairs=[(alias,alias),(self.root,alias),(alias,self.root),
               (Path('relative'),Path('relative'))]
        with patch.object(recovery,'_PosixAnchor',side_effect=AssertionError('chain constructed')) as chain:
            for child,root in pairs:
                with self.subTest(child=child,root=root),ExitStack() as stack:
                    with self.assertRaises(engine.RecoveryError):engine._retain(stack,child,root)
            chain.assert_not_called()

    def test_single_slash_sticky_current_owner_refused(self):
        self.child.chmod(0o1777)
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):
            engine._retain(stack,self.child,self.root)

    def test_root0711_refused(self):
        self.root.chmod(0o711)
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_writable_descendant_refused(self):
        self.child.chmod(0o775)
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_foreign_owner_refused(self):
        if os.geteuid()!=0:
            # Native owner change requires privilege; root runner executes this control separately.
            self.skipTest('requires native chown privilege')
        os.chown(self.child,12345,-1)
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_symlink_refused(self):
        leaf=self.child; leaf.rename(leaf.with_name('real'));leaf.symlink_to('real',target_is_directory=True)
        with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_replaced_ancestor_during_chain_refused(self):
        original=recovery._PosixAnchor.check
        def replace(anchor):
            self.child.rename(self.child.with_name('old'));self.child.mkdir(mode=0o755)
            return original(anchor)
        with patch.object(recovery._PosixAnchor,'check',replace):
            with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_root_privacy_changed_after_chain_refused(self):
        original=recovery._PosixAnchor.check
        def change(anchor):
            original(anchor);self.root.chmod(0o711)
        with patch.object(recovery._PosixAnchor,'check',change):
            with ExitStack() as stack,self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
