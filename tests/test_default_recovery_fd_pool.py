"""Transaction-scoped retained-prefix lifetime and refusal regression tests."""
import errno
import os
import unittest
import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
from finix import default_recovery as engine,recovery

@unittest.skipIf(os.name=='nt','POSIX descriptors; native Windows unchanged')
class RetainedPrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'root';self.root.mkdir(mode=0o700)
        self.child=self.root/'skills';self.child.mkdir(mode=0o755)
    def test_resource_exhaustion_is_not_an_acl_refusal(self):
        with ExitStack() as stack,patch.object(recovery.os,'open',side_effect=OSError(errno.EMFILE,'fd exhaustion')):
            with self.assertRaises(OSError) as ex:engine._retain(stack,self.child,self.root)
            self.assertEqual(ex.exception.errno,errno.EMFILE)
    def test_shared_prefix_keeps_exact_native_identity_until_stack_close(self):
        with ExitStack() as stack:
            first=engine._retain(stack,self.child,self.root)
            second=engine._retain(stack,self.root,self.root)
            third=engine._retain(stack,self.child,self.root)
            self.assertEqual(first.fds,third.fds)
            self.assertEqual(first.fds[:-1],second.fds)
            fd=first.fd;first.check();third.check()
        with self.assertRaises(OSError):os.fstat(fd)
        self.assertFalse(hasattr(stack,'_finix_retained_prefixes'))
    def test_cached_replaced_prefix_cannot_be_adopted(self):
        with ExitStack() as stack:
            first=engine._retain(stack,self.child,self.root)
            self.child.rename(self.root/'old');self.child.mkdir(mode=0o755)
            with self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
            with self.assertRaises(recovery.RecoveryError):first.check()
    def test_cached_modes_are_rechecked(self):
        with ExitStack() as stack:
            engine._retain(stack,self.child,self.root)
            self.root.chmod(0o711)
            with self.assertRaises(engine.RecoveryError):engine._retain(stack,self.child,self.root)
    def test_stack_can_be_reused_after_close(self):
        stack=ExitStack()
        first=engine._retain(stack,self.child,self.root);first.check();stack.close()
        engine._retain(stack,self.child,self.root).check();stack.close()
