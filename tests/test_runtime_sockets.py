"""Producer-scoped runtime sockets: real local crypto, never provider access."""
from contextlib import ExitStack, closing
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tarfile
import tempfile
import unittest

from finix import core


RUNTIME = ('gateway.sock', 'state/gateway.loop-tick.10735.sock')
PERSISTENT = ('other.sock', 'state/arbitrary', 'state/other.sock',
              'nested/gateway.sock', 'nested/state/gateway.loop-tick.10735.sock',
              'state/gateway.loop-tick.0.sock', 'state/gateway.loop-tick.01.sock',
              'state/gateway.loop-tick.-1.sock', 'state/gateway.loop-tick.1x.sock',
              'state/gateway.loop-tick.١.sock', 'state/gateway.loop-tick.12.sock.bak',
              'config.yaml', 'auth.json', 'sessions/synthetic.json')


class RuntimeSocketPolicyTests(unittest.TestCase):
    def test_only_producer_runtime_paths_are_excluded(self):
        for name in RUNTIME + ('state/gateway.loop-tick.1.sock',):
            with self.subTest(name=name):
                self.assertTrue(core.excluded(name))
                self.assertFalse(core.allowed(name))
        for name in PERSISTENT:
            with self.subTest(name=name):
                self.assertFalse(core.excluded(name))
                self.assertTrue(core.allowed(name))
        # The frozen finix-1 admission contract must not gain new paths.
        for name in RUNTIME:
            self.assertFalse(core.legacy_allowed(name))

    def test_runtime_roundtrip_preserves_regular_user_files_and_wal(self):
        with tempfile.TemporaryDirectory(prefix='fxs-') as temp, ExitStack() as stack:
            base = Path(temp)
            source = core.private_dir(base / 'profile')
            for name in PERSISTENT:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic persistent bytes')
            if os.name == 'posix':
                for name in RUNTIME:
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    bound = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                    bound.bind(str(path))
                    bound.listen(1)
            else:
                # Native Windows exercises exact-name classification and native
                # crypto/filesystem primitives; no POSIX shim or socket emulation.
                for name in RUNTIME:
                    (source / name).write_bytes(b'synthetic stale runtime record')
            db = stack.enter_context(closing(sqlite3.connect(source / 'state.db')))
            db.execute('pragma journal_mode=WAL')
            db.execute('create table messages(value text)')
            db.execute("insert into messages values ('synthetic committed WAL')")
            db.commit()
            self.assertGreater((source / 'state.db-wal').stat().st_size, 0)
            identity = base / 'identity'
            recipient = core.keygen(identity)
            result = core.backup(source, base / 'copies', base / 'ledger', recipient,
                                 sqlite_temp_dir=core.private_dir(source / 'cache/finix/sqlite-temp'))
            self.assertIs(result['complete'], True)
            entries = core.validate_archive(core.decrypt(result['path'], identity))
            self.assertEqual(set(entries), set(PERSISTENT) | {'state.db'})
            target = base / 'restored'
            core.restore(result['path'], identity, target)
            for name in PERSISTENT:
                self.assertEqual((target / name).read_bytes(), b'synthetic persistent bytes')
            for name in RUNTIME:
                self.assertFalse((target / name).exists())
            with closing(sqlite3.connect(target / 'state.db')) as restored:
                self.assertEqual(restored.execute('pragma integrity_check').fetchone(), ('ok',))
                self.assertEqual(restored.execute('select value from messages').fetchall(),
                                 [('synthetic committed WAL',)])
    @unittest.skipUnless(os.name == 'posix', 'real Linux socket/symlink/FIFO guards')
    def test_unexpected_unsafe_entries_still_fail_with_runtime_sockets_present(self):
        with tempfile.TemporaryDirectory(prefix='fxs-') as temp, ExitStack() as stack:
            source = Path(temp) / 'profile'
            source.mkdir()
            (source / 'state').mkdir()
            for name in RUNTIME:
                bound = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                bound.bind(str(source / name))
            unexpected = source / 'state/gateway.loop-tick.not-a-pid.sock'
            bound = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            bound.bind(str(unexpected))
            for kind in ('socket', 'symlink', 'fifo'):
                if kind != 'socket':
                    unexpected.unlink()
                    if kind == 'symlink':
                        unexpected.symlink_to(source / 'config.yaml')
                    else:
                        os.mkfifo(unexpected)
                with self.subTest(kind=kind), core.locked(source) as anchor:
                    with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                        core.scan(anchor)
            unexpected.unlink()
            # The same guard must apply outside state/ and to a root .sock.
            other = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            other.bind(str(source / 'other.sock'))
            with core.locked(source) as anchor:
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    core.scan(anchor)

    def test_archive_admission_matches_capture_and_keeps_hash_and_v1_guards(self):
        def fixture(name, version='finix-2', wrong_hash=False):
            data = b'synthetic fixture'
            manifest = {'format': version, 'files': {name: {
                'size': len(data), 'sha256': '0' * 64 if wrong_hash else hashlib.sha256(data).hexdigest()}}}
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.GNU_FORMAT) as archive:
                for path, payload in (('manifest.json', json.dumps(manifest).encode()),
                                      (('profile/' if version == 'finix-2' else '') + name, data)):
                    member = tarfile.TarInfo(path)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
            return buffer.getvalue()
        for name in RUNTIME:
            for version in ('finix-1', 'finix-2'):
                with self.subTest(name=name, version=version), self.assertRaises(core.FinixError):
                    core.validate_archive(fixture(name, version))
        for name in PERSISTENT:
            self.assertEqual(core.validate_archive(fixture(name))[name], b'synthetic fixture')
        with self.assertRaises(core.FinixError):
            core.validate_archive(fixture('other.sock', wrong_hash=True))
        self.assertEqual(core.validate_archive(fixture('config.yaml', 'finix-1'))['config.yaml'],
                         b'synthetic fixture')
        with self.assertRaises(core.FinixError):
            core.validate_archive(fixture('other.sock', 'finix-1'))


if __name__ == '__main__':
    unittest.main()
