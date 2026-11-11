"""Narrow no-network/no-child-process gate; not a full runtime test suite."""
import argparse
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scratch', type=Path, required=True,
                   help='Existing isolated absolute scratch directory; never a live profile')
    args = p.parse_args()
    if not args.scratch.is_absolute() or not args.scratch.is_dir():
        p.error('--scratch must be an existing absolute directory')
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='finix-offline-', dir=args.scratch) as tmp:
        private = Path(tmp)
        tools = private / 'fake-tools'
        tools.mkdir()
        suffix = '.exe' if os.name == 'nt' else ''
        for name in ('age', 'age-keygen', 'rclone'):
            tool = tools / (name + suffix)
            tool.write_bytes(b'not a real executable; process launch must be denied\n')
            tool.chmod(0o700)
        saved = dict(os.environ)
        os.environ.clear()
        for key in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'):
            if key in saved:
                os.environ[key] = saved[key]
        os.environ.update(PATH=str(tools), HERMES_HOME=str(private / 'profile'),
                          HOME=str(private), USERPROFILE=str(private),
                          TMPDIR=str(private), TMP=str(private), TEMP=str(private))
        tempfile.tempdir = str(private)
        for name in ('age', 'age-keygen', 'rclone'):
            actual = shutil.which(name)
            if actual is None or Path(actual).parent != tools:
                raise RuntimeError('Fake executable resolution failed')

        def deny(event, arguments):
            if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn')) or event in {'os.system', 'os.posix_spawn'}:
                raise PermissionError('OFFLINE_GATE_DENIED')
        sys.addaudithook(deny)
        # Prove the guard is active before importing any test or runtime module.
        for action in (lambda: socket.socket(),
                       lambda: __import__('subprocess').run([str(tools / ('rclone' + suffix))])):
            try:
                action()
            except PermissionError:
                pass
            else:
                raise RuntimeError('Offline guard failed')
        sys.path.insert(0, str(root))
        sys.path.insert(0, str(Path(__file__).parent))
        from finix import core  # standalone import, no Hermes or provider tools
        if not hasattr(core, 'backup'):
            raise RuntimeError('Core import contract failed')
        suite = unittest.defaultTestLoader.loadTestsFromName('test_release')
        if os.name == 'nt' and (root / 'tests/test_native_windows.py').is_file():
            sys.path.insert(0, str(root / 'tests'))
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
                'test_native_windows.NativeWindowsTests.test_missing_encryption_tool_retains_closed_dependency_code'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print('Network and child processes denied; all provider tools resolved to fake files.')
        return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    raise SystemExit(main())
