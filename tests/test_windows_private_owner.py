"""Native owner and DACL contracts; never fake the host OS."""
import ctypes as C
from ctypes import wintypes as W
import os
import importlib
from typing import Any
from pathlib import Path
import subprocess
import unittest


def native() -> Any:
    """The real backend intentionally cannot import on a non-Windows host."""
    return importlib.import_module('finix.windows_native')


def security(handle):
    win = native()
    owner, dacl, descriptor = C.c_void_p(), C.c_void_p(), C.c_void_p()
    get = win.api(win.A, 'GetSecurityInfo', W.DWORD, W.HANDLE, C.c_int, W.DWORD,
                  C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p),
                  C.c_void_p, C.POINTER(C.c_void_p))
    if get(handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor)):
        raise OSError('Cannot inspect synthetic security descriptor')
    try:
        control, revision = W.WORD(), W.DWORD()
        get_control = win.api(win.A, 'GetSecurityDescriptorControl', W.BOOL,
                              C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD))
        if not get_control(descriptor, C.byref(control), C.byref(revision)):
            raise OSError('Cannot inspect synthetic DACL control')
        class ACL(C.Structure):
            _fields_ = [('revision', W.BYTE), ('reserved', W.BYTE), ('size', W.WORD),
                        ('count', W.WORD), ('reserved2', W.WORD)]
        acl = C.cast(dacl, C.POINTER(ACL)).contents
        get_ace = win.api(win.A, 'GetAce', W.BOOL, C.c_void_p, W.DWORD, C.POINTER(C.c_void_p))
        entries = []
        for index in range(acl.count):
            ace = C.c_void_p()
            if not get_ace(dacl, index, C.byref(ace)):
                raise OSError('Cannot inspect synthetic ACE')
            assert ace.value is not None
            kind, flags = C.string_at(ace, 2)
            mask = C.cast(ace.value + 4, C.POINTER(W.DWORD)).contents.value
            entries.append((kind, flags, mask, win.sid_text(ace.value + 8)))
        return {'owner': win.sid_text(owner), 'protected': bool(control.value & 0x1000),
                'entries': entries}
    finally:
        win.free(descriptor)


@unittest.skipUnless(os.name == 'nt', 'Native Windows owner/DACL contract')
class NativePrivateOwnerTests(unittest.TestCase):
    def private_snapshot(self, path):
        win = native()
        with win.directory(path) as directory:
            result = security(directory.handle)
            self.assertTrue(win.trusted_handle(directory.handle, private=True))
        self.assertEqual(result['owner'], win.current_sid())
        self.assertTrue(result['protected'])
        self.assertEqual(len(result['entries']), 2)
        self.assertEqual({ace[3] for ace in result['entries']}, {win.current_sid(), 'S-1-5-18'})
        return result

    def test_new_recursive_directories_are_owned_by_token_user(self):
        import tempfile
        from finix import core
        win = native()
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            base = Path(tmp)
            ordinary = base / 'ordinary'
            ordinary.mkdir()
            with win.directory(ordinary) as directory:
                observed = security(directory.handle)
            print('NATIVE_DEFAULT_OWNER_EQUALS_TOKEN_USER', observed['owner'] == win.current_sid())
            target = core.private_dir(base / 'private' / 'nested' / 'leaf')
            for path in (base / 'private', base / 'private/nested', target):
                self.private_snapshot(path)

    def test_new_private_file_has_current_owner_and_private_dacl(self):
        import tempfile
        msvcrt: Any = importlib.import_module('msvcrt')
        from finix import core
        win = native()
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            root = core.private_dir(Path(tmp) / 'private')
            path = root / 'synthetic.txt'
            with core.regular_output(path) as fd:
                os.write(fd, b'synthetic')
                handle = msvcrt.get_osfhandle(fd)
                value = security(handle)
                self.assertEqual(value['owner'], win.current_sid())
                self.assertTrue(value['protected'])
                self.assertTrue(win.trusted_handle(handle, private=True))
            self.assertEqual(path.read_bytes(), b'synthetic')

    def test_existing_broad_directory_is_refused_without_adoption(self):
        import tempfile
        from finix import core
        win = native()
        with tempfile.TemporaryDirectory(dir=os.environ.get('RUNNER_TEMP')) as tmp:
            path = Path(tmp) / 'existing'
            path.mkdir()
            result = subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:(OI)(CI)(R)'],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0)
            with win.directory(path) as directory:
                before = security(directory.handle)
            with self.assertRaises(OSError):
                core.private_dir(path)
            with win.directory(path) as directory:
                after = security(directory.handle)
            self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
