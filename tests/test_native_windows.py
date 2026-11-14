"""Real native Windows contracts; fixtures stay under the task temp root."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'native Windows contract')
class NativeWindowsTests(unittest.TestCase):
    def test_missing_encryption_tool_retains_closed_dependency_code(self):
        from finix import core, streaming, windows_native as win
        from unittest.mock import patch
        import io
        with patch.object(win, 'popen', side_effect=FileNotFoundError('private executable path')):
            with self.assertRaisesRegex(core.FinixError, '^DEPENDENCY_UNAVAILABLE$'):
                streaming.write_encrypted_snapshot(None, 'synthetic-recipient', io.BytesIO())

    def test_disappearing_sqlite_shm_discovery_preserves_wal_backup_restore(self):
        from finix import core
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = core.private_dir(base / 'source')
            db = source / 'live.db'
            connection = sqlite3.connect(db)
            try:
                connection.execute('pragma journal_mode=wal')
                connection.execute('pragma wal_autocheckpoint=0')
                connection.execute('create table messages(value text)')
                connection.execute("insert into messages values ('committed only in WAL')")
                connection.commit()
                self.assertTrue(db.with_name('live.db-wal').exists())
                original = Path.lstat
                races = []
                def racing(path, *args, **kwargs):
                    if path == source / 'live.db-shm':
                        races.append(True)
                        raise FileNotFoundError('synthetic disappearing SHM')
                    return original(path, *args, **kwargs)
                identity = base / 'identity'
                recipient = core.keygen(identity)
                with patch.object(Path, 'lstat', racing):
                    result = core.backup(source, base / 'output', base / 'state', recipient,
                        sqlite_temp_dir=core.private_dir(base / 'sqlite-temp'))
                self.assertTrue(races)
                restored = base / 'restored'
                core.restore(Path(result['path']), identity, restored)
                self.assertFalse((restored / 'live.db-wal').exists())
                self.assertFalse((restored / 'live.db-shm').exists())
                from contextlib import closing
                with closing(sqlite3.connect(restored / 'live.db')) as recovered:
                    self.assertEqual(recovered.execute('select * from messages').fetchall(),
                                     [('committed only in WAL',)])
                    self.assertEqual(recovered.execute('pragma integrity_check').fetchone(), ('ok',))
            finally:
                connection.close()

    def test_sqlite_companions_actually_removed_after_enumeration_are_safe(self):
        from finix import core
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            source = core.private_dir(Path(temp) / 'source')
            db = source / 'live.db'
            connection = sqlite3.connect(db)
            try:
                connection.execute('pragma journal_mode=wal')
                connection.execute('create table data(value)')
                connection.commit()
                self.assertTrue((source / 'live.db-shm').exists())
                original = Path.lstat
                closed = []
                def racing(path, *args, **kwargs):
                    if path == db and not closed:
                        connection.close()  # SQLite removes WAL and SHM after listdir.
                        closed.append(True)
                        self.assertFalse((source / 'live.db-shm').exists())
                    return original(path, *args, **kwargs)
                with core.locked(source) as anchor, patch.object(Path, 'lstat', racing):
                    inventory = core.scan(anchor)
                self.assertIn('live.db', inventory)
                # The validating read-only SQLite connection may recreate its
                # own empty WAL/SHM. They are validated, not archive payloads.
                self.assertTrue(set(inventory) <= {'live.db', 'live.db-wal', 'live.db-shm'})
            finally:
                connection.close()

    def test_missing_non_sqlite_files_and_fake_companions_fail_closed(self):
        from finix import core
        from unittest.mock import patch
        for name, data in [('plain.txt', b'not sqlite'), ('fake.db-shm', b'not sqlite'),
                           ('fake.db-shm', b'SQLite format 3\x00' + b'garbage' * 100)]:
            with self.subTest(name=name, header=data[:16]), tempfile.TemporaryDirectory() as temp:
                source = core.private_dir(Path(temp) / 'source')
                (source / 'fake.db').write_bytes(data)
                (source / name).write_bytes(b'vanished')
                original = Path.lstat
                def racing(path, *args, **kwargs):
                    if path == source / name:
                        raise FileNotFoundError('synthetic disappearance')
                    return original(path, *args, **kwargs)
                with core.locked(source) as anchor, patch.object(Path, 'lstat', racing):
                    with self.assertRaises((core.FinixError, __import__('sqlite3').DatabaseError)):
                        core.scan(anchor)

    def test_disappeared_directory_with_sqlite_suffix_is_not_a_companion(self):
        from finix import core
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            source = core.private_dir(Path(temp) / 'source')
            db = source / 'live.db'
            connection = sqlite3.connect(db)
            connection.execute('create table data(value)')
            connection.commit()
            connection.close()
            sibling = source / 'live.db-shm'
            sibling.mkdir()
            original = Path.lstat
            def racing(path, *args, **kwargs):
                if path == sibling:
                    sibling.rmdir()
                return original(path, *args, **kwargs)
            with core.locked(source) as anchor, patch.object(Path, 'lstat', racing):
                with self.assertRaises(core.FinixError):
                    core.scan(anchor)

    def test_missing_companion_does_not_hide_invalid_sqlite_owner(self):
        from finix import core
        from unittest.mock import patch
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            source = core.private_dir(Path(temp) / 'source')
            db = source / 'live.db'
            connection = sqlite3.connect(db)
            connection.execute('pragma journal_mode=wal')
            connection.execute('create table data(value)')
            connection.commit()
            original = Path.lstat
            def racing(path, *args, **kwargs):
                if path == source / 'live.db-shm':
                    # Live SQLite content/size changes are now supported. A
                    # vanished companion must still never hide a corrupt owner.
                    db.write_bytes(b'not a SQLite database')
                    raise FileNotFoundError('synthetic missing companion and changed owner')
                return original(path, *args, **kwargs)
            try:
                with core.locked(source) as anchor, patch.object(Path, 'lstat', racing):
                    with self.assertRaisesRegex(core.FinixError, 'Source changed before read'):
                        core.scan(anchor)
            finally:
                connection.close()

    def test_core_imports_without_posix_modules(self):
        result = subprocess.run([sys.executable, '-c', 'from finix import core'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_anchored_directory_rejects_junction(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / 'real').mkdir()
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(base / 'link'), str(base / 'real')], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaises(OSError):
                with win.directory(base / 'link'):
                    self.fail('junction accepted')
            (base / 'link').rmdir()

    def test_relative_handle_open_does_not_follow_mutated_root_reparse(self):
        from finix import windows_native as win
        import struct
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root, outside = base / 'root', base / 'outside'
            root.mkdir()
            outside.mkdir()
            (outside / 'sentinel').write_text('outside')
            with win.directory(root) as anchor:
                handle = win.create(str(root), 0x40000000, 3, None, 3, 0x02200000, None)
                self.assertNotEqual(handle, win.INVALID)
                try:
                    substitute = ('\\??\\' + str(outside)).encode('utf-16-le')
                    display = str(outside).encode('utf-16-le')
                    paths = substitute + b'\0\0' + display + b'\0\0'
                    data = struct.pack('<IHHHHHH', 0xA0000003, len(paths) + 8, 0, 0, len(substitute), len(substitute) + 2, len(display)) + paths
                    returned = win.W.DWORD()
                    ioctl = win.api(win.K, 'DeviceIoControl', win.W.BOOL, win.W.HANDLE, win.W.DWORD, win.C.c_void_p, win.W.DWORD, win.C.c_void_p, win.W.DWORD, win.C.POINTER(win.W.DWORD), win.C.c_void_p)
                    buffer = win.C.create_string_buffer(data)
                    self.assertTrue(ioctl(handle, 0x900A4, buffer, len(data), None, 0, win.C.byref(returned), None))
                finally:
                    win.close(handle)
                try:
                    with self.assertRaises(OSError):
                        child = win.relative_open(anchor.handle, 'sentinel', directory=False)
                        win.close(child)
                finally:
                    pass
            root.rmdir()
            self.assertEqual((outside / 'sentinel').read_text(), 'outside')

    def test_pinned_directory_cannot_be_renamed(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'source'
            source.mkdir()
            with win.directory(source):
                with self.assertRaises(OSError):
                    source.rename(source.with_name('moved'))

    def test_directory_lock_excludes_other_process(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            with win.locked(Path(temp)):
                script = 'from pathlib import Path; from finix.windows_native import locked; import sys;\nwith locked(Path(sys.argv[1])): pass'
                result = subprocess.run([sys.executable, '-c', script, temp], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            with win.locked(Path(temp)):
                pass

    def test_publication_does_not_replace_existing_destination(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            stage, target = Path(temp) / 'stage', Path(temp) / 'target'
            stage.mkdir()
            target.mkdir()
            (target / 'sentinel').write_text('untouched')
            with self.assertRaises(OSError):
                win.publish_directory(stage, target)
            self.assertEqual((target / 'sentinel').read_text(), 'untouched')
            target.joinpath('sentinel').unlink()
            target.rmdir()
            win.publish_directory(stage, target)
            self.assertTrue(target.is_dir())

    def test_parent_death_kills_job_descendants(self):
        from finix import windows_native as win
        import json
        child = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
        launcher = ('from finix import windows_native as w; import sys,subprocess,os; '
                    'p=w.popen([sys.executable,"-c",sys.argv[1]],stdout=subprocess.PIPE); '
                    'print(p.stdout.readline().decode().strip(),flush=True); sys.stdin.readline(); os._exit(0)')
        parent = subprocess.Popen([sys.executable, '-c', launcher, child], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        process = None
        try:
            pid = int(parent.stdout.readline())
            process = win.api(win.K, 'OpenProcess', win.W.HANDLE, win.W.DWORD, win.W.BOOL, win.W.DWORD)(0x100000, False, pid)
            self.assertTrue(process)
            parent.stdin.write(b'exit\n')
            parent.stdin.flush()
            parent.wait(timeout=5)
            self.assertEqual(win.wait(process, 5000), 0, 'descendant survived abrupt supervisor death')
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            parent.stdin.close()
            parent.stdout.close()
            if process:
                win.close(process)

    def test_windows_pipes_drain_both_streams_with_bounded_queue(self):
        from finix import windows_native as win
        import time
        p = win.popen([sys.executable, '-c', 'import os; [(os.write(1,b"x"*4096), os.write(2,b"y"*4096)) for _ in range(128)]'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sizes = {True: 0, False: 0}
        try:
            for stdout, chunk in win.output_events(p, time.monotonic() + 10):
                sizes[stdout] += len(chunk)
            self.assertEqual(p.wait(timeout=5), 0)
            self.assertEqual(sizes, {True: 524288, False: 524288})
        finally:
            win.cleanup(p)
            p.stdout.close()
            p.stderr.close()

    def test_blocked_pipe_cancellation_unwinds_and_kills_child(self):
        from finix import windows_native as win
        from finix import core
        import threading
        p = win.popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.PIPE)
        cancel = threading.Event()
        timer = threading.Timer(.2, cancel.set)
        timer.start()
        try:
            with self.assertRaises(core.FinixError):
                win.Pipe(p.stdout, cancel).read(1024)
        finally:
            win.cleanup(p)
            p.stdout.close()
            timer.join()
        self.assertIsNotNone(p.poll())

    def test_wrong_key_and_tamper_never_publish(self):
        from finix import core
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = core.private_dir(base / 'source')
            (source / 'data').write_bytes(os.urandom(2 * 1024 * 1024))
            identity, wrong = base / 'identity', base / 'wrong'
            recipient = core.keygen(identity)
            core.keygen(wrong)
            result = core.backup(source, base / 'output', base / 'state', recipient,
                                 sqlite_temp_dir=core.private_dir(base / 'sqlite-temp'))
            cipher, target = Path(result['path']), base / 'restored'
            with self.assertRaises(core.FinixError):
                core.restore(cipher, wrong, target)
            self.assertFalse(target.exists())
            data = bytearray(cipher.read_bytes())
            data[-1] ^= 1
            cipher.write_bytes(data)
            with self.assertRaises(core.FinixError):
                core.restore(cipher, identity, target)
            self.assertFalse(target.exists())
            self.assertEqual(list(base.glob('.finix-restore-*')), [])

    def test_native_multi_root_bundle_roundtrip(self):
        from finix import core, streaming
        import io
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, extra = core.private_dir(base / 'profile'), core.private_dir(base / 'external')
            (source / 'config.yaml').write_text('synthetic: true\n')
            (extra / 'external.txt').write_text('external synthetic')
            record = {'name': 'documents', 'path': str(extra)}
            buffer = io.BytesIO()
            with core.locked(source) as sourcefd, core.locked(extra) as extrafd:
                streaming.write_archive(sourcefd, buffer, extra_fds=[(record, extrafd)])
            buffer.seek(0)
            stage = core.private_dir(base / 'stage')
            payload, count, layout, completion = streaming.restore_stream(buffer, stage)
            self.assertEqual(layout, 'bundle')
            self.assertEqual(count, 2)
            self.assertEqual((payload / 'external/documents/external.txt').read_text(), 'external synthetic')

    def test_profile_setup_receipt_native_roundtrip(self):
        from finix import core
        from finix.integration import Profile
        from finix.setup_flow import SetupFlow
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = core.private_dir(base / 'profile')
            (home / 'config.yaml').write_text('model: synthetic\n')
            profile = Profile(home, default_root=home)
            flow = SetupFlow(profile)
            self.assertFalse(flow.view()['verified'])
            recipient = core.keygen(base / 'identity')
            profile.recipient(profile.profile_id, recipient, None)
            flow.check_and_run()
            self.assertTrue(flow.view()['verified'])
            flow.schedule('24h')
            self.assertEqual(profile.config()['interval_hours'], 24)

    def test_rclone_native_existing_config_local_inventory(self):
        from finix import core
        from finix import windows_native as win
        from finix.rclone_storage import RcloneRunner
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = core.private_dir(base / 'config')
            config = folder / 'rclone.conf'
            with win.regular(config, create_new=True) as fd:
                os.write(fd, b'[synthetic]\ntype = yandex\n')
            result = RcloneRunner(base, config)('listremotes', '--long')
            self.assertIn(b'synthetic:', result)
            self.assertIn(b'yandex', result)
            self.assertEqual(config.read_bytes(), b'[synthetic]\ntype = yandex\n')

    def test_native_rclone_storage_synthetic_transport_retention(self):
        # Explicit in-memory provider fixture, not a cloud acceptance claim.
        from test_rclone_storage import MemoryRunner
        from finix import core
        from finix.rclone_storage import RcloneStorage
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = core.private_dir(base / 'config')
            config = folder / 'rclone.conf'
            with core.regular_output(config) as fd:
                os.write(fd, b'[cloud]\ntype = yandex\n')
            transport = MemoryRunner()
            adapter = RcloneStorage(base, 'a' * 64, base / 'state', str(config), 'cloud', 'backups', runner=transport)
            cipher = base / 'synthetic.age'
            cipher.write_bytes(b'age-encryption.org/v1\nsynthetic-ciphertext-fixture')
            completion = dict(sha256=core.digest_file(cipher), complete=True, omitted_count=0)
            first = adapter.backup_cipher(cipher, keep=1, completion=completion)
            second = adapter.backup_cipher(cipher, keep=1, completion=completion)
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(len(adapter.ledger()['records']), 1)
            output = base / 'download.age'
            adapter.download(second, output)
            self.assertEqual(output.read_bytes(), cipher.read_bytes())

    def test_native_ciphertext_copy_is_exact_and_private(self):
        from finix import core, cloud_files
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, target = base / 'source.age', base / 'copy.age'
            data = b'age-encryption.org/v1\n' + os.urandom(2 * 1024 * 1024)
            source.write_bytes(data)
            expected = cloud_files.describe(source, native=True)
            cloud_files.copy(source, target, expected)
            self.assertEqual(target.read_bytes(), data)
            with win.regular(target) as fd:
                import msvcrt
                self.assertTrue(win.trusted_handle(msvcrt.get_osfhandle(fd), private=True))

    def test_profile_bound_native_local_backup(self):
        from finix import core
        from finix.integration import Profile
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            home = core.private_dir(base / 'synthetic-profile')
            (home / 'config.yaml').write_text('model: synthetic\n')
            profile = Profile(home, default_root=home)
            identity = base / 'identity'
            recipient = core.keygen(identity)
            status = profile.recipient(profile.profile_id, recipient, None)
            self.assertEqual(status['recipient'], recipient)
            profile.configure('sqlite_temp_dir', str(core.private_dir(base / 'sqlite-temp')))
            result = profile.backup()
            core.restore(result['path'], identity, base / 'restore')
            self.assertEqual((base / 'restore/config.yaml').read_text(), 'model: synthetic\n')

    def test_yandex_preflight_real_rclone_is_local_and_creates_no_config(self):
        import shutil
        import json
        binary = shutil.which('rclone')
        self.assertIsNotNone(binary, 'real rclone binary required on test PATH')
        from finix import windows_native as win
        self.assertTrue(win.trusted_binary(binary), 'rclone ACL trust')
        worker = Path(__file__).parents[1] / 'finix/connection_worker.py'
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(worker), 'preflight-yandex', temp, temp, binary], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'status': 'ready', 'provider': 'yandex'})
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_supervised_process_kills_descendants_on_close(self):
        from finix import windows_native as win
        import json
        child = "import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(json.dumps(p.pid),flush=True); time.sleep(60)"
        p = win.popen([sys.executable, '-c', child], stdout=subprocess.PIPE)
        descendant = json.loads(p.stdout.readline())
        process = win.api(win.K, 'OpenProcess', win.W.HANDLE, win.W.DWORD, win.W.BOOL, win.W.DWORD)(0x100000, False, descendant)
        self.assertTrue(process)
        try:
            win.cleanup(p)
            self.assertEqual(win.wait(process, 5000), 0, 'descendant survived job close')
        finally:
            win.close(process)
            p.stdout.close()

    def test_completed_connection_worker_releases_job_handle(self):
        from finix import windows_native as win
        from finix.connections import Connections
        with tempfile.TemporaryDirectory() as temp:
            p = win.popen([sys.executable, '-c', 'print(\'{"status":"verified"}\',flush=True)'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            handle = p._finix_job
            session = {'process': p, 'stage': Path(temp), 'public': {'status': 'working', 'provider': 'yandex'}}
            try:
                Connections()._read(session)
                self.assertEqual(session['public']['status'], 'verified')
                flags = win.W.DWORD()
                get = win.api(win.K, 'GetHandleInformation', win.W.BOOL, win.W.HANDLE, win.C.POINTER(win.W.DWORD))
                self.assertFalse(get(handle, win.C.byref(flags)), 'completed worker leaked containment handle')
            finally:
                win.cleanup(p)

    def test_worker_missing_binary_emits_closed_failure(self):
        import json
        worker = Path(__file__).parents[1] / 'finix/connection_worker.py'
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(worker), 'yandex', temp, temp, 'nonexistent-finix-test-rclone.exe'], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(result.stdout), {'status': 'failed', 'code': 'CONNECTION_FAILED'})

    def test_restore_rejects_case_aliased_directories(self):
        from finix import core, streaming
        import hashlib
        import io
        import json
        import tarfile
        names = ['profile/A/one', 'profile/a/two']
        data = b'x'
        manifest = {'format': 'finix-2', 'files': {n[8:]: {'size': 1, 'sha256': hashlib.sha256(data).hexdigest()} for n in names}}
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w:gz') as tar:
            for name, payload in [(n, data) for n in names] + [('manifest.json', json.dumps(manifest).encode())]:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        archive.seek(0)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(core.FinixError):
                streaming.restore_stream(archive, Path(temp))

    def test_native_age_encrypted_roundtrip(self):
        import shutil
        from finix import core
        self.assertIsNotNone(shutil.which('age'), 'real age binary required on test PATH')
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = core.private_dir(base / 'source')
            (source / 'config.yaml').write_bytes(b'only synthetic data\n')
            identity = base / 'identity.txt'
            recipient = core.keygen(identity)
            result = core.backup(source, base / 'output', base / 'state', recipient,
                                 sqlite_temp_dir=core.private_dir(base / 'sqlite-temp'))
            cipher = Path(result['path'])
            self.assertTrue(cipher.read_bytes().startswith(b'age-encryption.org/v1'))
            self.assertIn('config.yaml', core.validate_archive(core.decrypt(cipher, identity)))
            target = base / 'restored'
            restored = core.restore(cipher, identity, target)
            self.assertEqual(restored['files'], 1)
            self.assertEqual((target / 'config.yaml').read_bytes(), b'only synthetic data\n')
            self.assertTrue(core.status(base / 'output', base / 'state')['backups'][0]['verified'])

    def test_native_archive_roundtrip_with_sqlite_wal_and_paused_cron(self):
        from finix import core, streaming
        import io
        import json
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source, stage = base / 'source', base / 'stage'
            core.private_dir(source)
            core.private_dir(stage)
            (source / 'cron').mkdir()
            (source / 'cron/jobs.json').write_text('{"jobs":[{"id":"synthetic","enabled":true}]}')
            (source / 'config.yaml').write_bytes(b'synthetic: true\n')
            db = sqlite3.connect(source / 'state %.db')
            try:
                db.execute('pragma journal_mode=WAL')
                db.execute('create table fixture(value text)')
                db.execute("insert into fixture values ('committed WAL')")
                db.commit()
                archive = io.BytesIO()
                with core.locked(source) as anchor:
                    streaming.write_archive(anchor, archive, core.private_dir(base / 'sqlite-temp'))
                archive.seek(0)
                payload, count, layout, completion = streaming.restore_stream(archive, stage)
                self.assertEqual(count, 3)
                self.assertEqual((payload / 'config.yaml').read_bytes(), b'synthetic: true\n')
                self.assertFalse(json.loads((payload / 'cron/jobs.json').read_text())['jobs'][0]['enabled'])
                restored = sqlite3.connect(payload / 'state %.db')
                try:
                    self.assertEqual(restored.execute('select value from fixture').fetchone()[0], 'committed WAL')
                finally:
                    restored.close()
            finally:
                db.close()

    def test_directory_delete_child_acl_is_untrusted(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            path = win.private_dir(Path(temp) / 'directory')
            result = subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:(DC)'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(win.trusted_directory(path))

    def test_private_directory_rejects_broad_acl(self):
        from finix import windows_native as win
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'private'
            win.private_dir(path)
            self.assertTrue(win.trusted_directory(path, private=True))
            result = subprocess.run(['icacls', str(path), '/grant', '*S-1-1-0:(OI)(CI)F'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(win.trusted_directory(path, private=True))
            with self.assertRaises(OSError):
                win.private_dir(path)

    def test_windows_archive_names_reject_device_ads_aliases(self):
        from finix import windows_native as win
        for name in ['C:/escape', 'a:stream', 'NUL', 'con.txt', 'a./b', 'x /b', 'aux', 'COM1', 'LPT9.txt']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                win.admit_name(name)
        win.admit_name('memories/данные.txt')


if __name__ == '__main__':
    unittest.main()
