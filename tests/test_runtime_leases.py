"""Exact SDK PM lease policy; fixtures contain only synthetic data.

Producer: pm.environments.install_state_dir (SHA256[:16]),
pm.packages and pm.runtime (uuid4().hex generations), and
hermes_cli.runtime_state.lease_directory (empty uuid4().hex lease, kernel
lock until release/exit; unlocked leftovers are pruned, not restored).
"""
from contextlib import contextmanager, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid

from finix import core


INSTALL = '0123456789abcdef'
GENERATION = '0123456789abcdef0123456789abcdef'
LEASE = 'abcdef0123456789abcdef0123456789'
GENERATIONS = (
    f'installs/{INSTALL}/environments/{GENERATION}',
    f'installs/{INSTALL}/pm-runtime/generations/{GENERATION}',
)
RUNTIME = tuple(f'{generation}/.leases/{LEASE}' for generation in GENERATIONS)
PERSISTENT = (
    f'installs/{INSTALL}/facts.json', f'installs/{INSTALL}/.install.lock',
    f'installs/{INSTALL}/pm-runtime/selected.json',
    f'installs/{INSTALL}/pm-runtime/.prepare.lock',
    'tools/synthetic-tool', 'skills/.usage.json', 'sessions/synthetic.json',
) + tuple(f'{generation}/{leaf}' for generation in GENERATIONS
          for leaf in ('.lease-managed', 'pm-runtime.json', '.leases/notes.txt',
                       '.leases/README', '.leases/' + LEASE + '.bak',
                       'payload', LEASE))
NEAR_MISSES = tuple(dict.fromkeys(
    near for name in RUNTIME for near in (
        'nested/' + name, name.replace('installs/', 'tools/', 1),
        name.replace(INSTALL, INSTALL[:-1], 1),
        name.replace(INSTALL, INSTALL + '0', 1),
        name.replace(INSTALL, 'g' * 16, 1),
        name.replace(GENERATION, GENERATION[:-1], 1),
        name.replace(GENERATION, GENERATION + '0', 1),
        name.replace(GENERATION, 'g' * 32, 1),
        name.replace('/.leases/', '/leases/'),
        name + '0', name[:-1], name + '/child', name + '.bak',
        name.rsplit('/', 1)[0] + '/' + 'g' * 32,
        name.rsplit('/', 1)[0] + '/' + str(uuid.UUID(hex=LEASE)),
    )
))


@contextmanager
def private_fixture():
    # Do not fix an existing root/ancestor ACL. Every directory we create is new.
    root = Path(tempfile.gettempdir()) / ('fx-lease-' + uuid.uuid4().hex)
    core.private_dir(root)
    try:
        yield root
    finally:
        import shutil
        shutil.rmtree(root)


def put(source, name, data=b'synthetic persistent bytes'):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def archive_fixture(name, version='finix-2', wrong_hash=False):
    payload = b'synthetic archive bytes'
    manifest = {'format': version, 'files': {name: {
        'size': len(payload),
        'sha256': '0' * 64 if wrong_hash else hashlib.sha256(payload).hexdigest(),
    }}}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.GNU_FORMAT) as archive:
        for path, data in (('manifest.json', json.dumps(manifest).encode()),
                           (('profile/' if version == 'finix-2' else '') + name, payload)):
            member = tarfile.TarInfo(path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


class RuntimeLeasePolicyTests(unittest.TestCase):
    def test_exact_producer_leaves_only_and_archive_admission_parity(self):
        for name in RUNTIME:
            with self.subTest(runtime=name):
                self.assertTrue(core.excluded(name))
                self.assertFalse(core.allowed(name))
                self.assertFalse(core.legacy_allowed(name))
                for version in ('finix-1', 'finix-2'):
                    with self.assertRaises(core.FinixError):
                        core.validate_archive(archive_fixture(name, version))
        for name in PERSISTENT + NEAR_MISSES:
            with self.subTest(persistent=name):
                self.assertFalse(core.excluded(name))
                self.assertTrue(core.allowed(name))
                self.assertEqual(core.validate_archive(archive_fixture(name))[name],
                                 b'synthetic archive bytes')
        for generation in GENERATIONS:
            self.assertFalse(core.excluded(generation + '/.leases'))
        with self.assertRaises(core.FinixError):
            core.validate_archive(archive_fixture(PERSISTENT[0], wrong_hash=True))
        with self.assertRaises(core.FinixError):
            core.validate_archive(archive_fixture(PERSISTENT[0], 'finix-1'))
        self.assertEqual(core.validate_archive(archive_fixture('config.yaml', 'finix-1')),
                         {'config.yaml': b'synthetic archive bytes'})

    def test_platform_case_policy(self):
        for name in RUNTIME:
            self.assertEqual(core.excluded(name.upper()), os.name == 'nt')
            if os.name != 'nt':
                self.assertTrue(core.allowed(name.upper()))

    @unittest.skipUnless(os.name == 'nt', 'native Windows byte-range lock')
    def test_live_locked_sdk_shaped_leases_scan_without_reading_or_releasing(self):
        # Use the actual unchanged SDK producer when explicitly supplied by the
        # integration runner; otherwise reproduce its stdlib-only Windows lock.
        child_code = r'''
import os, pathlib, sys
source = pathlib.Path(sys.argv[1])
sdk = sys.argv[2]
releases = []
fds = []
if sdk:
    sys.path.insert(0, sdk)
    from hermes_cli.runtime_state import lease_directory
    for generation in sys.argv[3:]:
        releases.append(lease_directory(source / generation))
else:
    import msvcrt, uuid
    for generation in sys.argv[3:]:
        leases = source / generation / '.leases'
        leases.mkdir(exist_ok=True)
        fd = os.open(leases / uuid.uuid4().hex, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        fds.append(fd)
print('READY', flush=True)
try:
    for line in sys.stdin:
        if line.strip() != 'near-miss':
            break
        # Same live lock at a non-producer name must remain a hard failure.
        import msvcrt
        near = source / sys.argv[3] / '.leases' / 'locked-near-miss.bak'
        fd = os.open(near, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        fds.append(fd)
        print('NEAR-MISS-LOCKED', flush=True)
finally:
    for release in releases:
        release()
    for fd in fds:
        os.close(fd)
'''
        with private_fixture() as root:
            source = core.private_dir(root / 'source')
            # The SDK prunes *all* unlocked files already in .leases on entry.
            # Add user-data near misses only after the producer has its pins.
            for generation in GENERATIONS:
                put(source, generation + '/.lease-managed')
            child = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', child_code, str(source),
                 os.environ.get('FINIX_TEST_SDK_ROOT', ''), *GENERATIONS],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(child.stdout.readline().strip(), 'READY')
                for name in PERSISTENT:
                    put(source, name)
                leases = [path for generation in GENERATIONS
                          for path in (source / generation / '.leases').iterdir()
                          if len(path.name) == 32]
                self.assertEqual(len(leases), len(GENERATIONS))
                # Positive control: reading each actual live lease fails at the
                # same core.peek boundary as inventory before the policy fix.
                for path in leases:
                    with core.regular_input(path) as fd:
                        with self.assertRaises(PermissionError):
                            core.peek(fd, 16)
                with core.locked(source) as anchor:
                    inventory = core.scan(anchor)
                self.assertEqual(set(inventory), set(PERSISTENT))
                self.assertIsNone(child.poll())
                for path in leases:
                    self.assertTrue(path.exists())
                    with core.regular_input(path) as fd:
                        with self.assertRaises(PermissionError):
                            core.peek(fd, 16)
                child.stdin.write('near-miss\n')
                child.stdin.flush()
                self.assertEqual(child.stdout.readline().strip(), 'NEAR-MISS-LOCKED')
                with core.locked(source) as anchor:
                    with self.assertRaises(PermissionError):
                        core.scan(anchor)
            finally:
                child.communicate('\n', timeout=15)
            self.assertEqual(child.returncode, 0)

    @unittest.skipUnless(os.name == 'nt', 'native Windows no-reparse control')
    def test_windows_lease_parent_junction_is_not_hidden(self):
        with private_fixture() as root:
            source = core.private_dir(root / 'source')
            target = core.private_dir(root / 'target')
            (target / LEASE).write_bytes(b'synthetic outside data')
            for generation in GENERATIONS:
                parent = core.private_dir(source / generation)
                junction = parent / '.leases'
                result = subprocess.run(
                    ['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                    capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                try:
                    with core.locked(source) as anchor:
                        with self.assertRaisesRegex(core.FinixError, 'reparse'):
                            core.scan(anchor)
                    self.assertEqual((target / LEASE).read_bytes(), b'synthetic outside data')
                finally:
                    junction.rmdir()

    @unittest.skipUnless(os.name == 'posix', 'Linux no-follow controls')
    def test_linux_scan_preserves_near_misses_and_rejects_unsafe_lease_parents(self):
        import fcntl
        with private_fixture() as root, ExitStack() as stack:
            source = core.private_dir(root / 'source')
            # A regular lease cannot also be the directory for /child. That
            # near miss is covered independently by archive/policy admission.
            expected = set(PERSISTENT + NEAR_MISSES) - {name + '/child' for name in RUNTIME}
            for name in expected:
                put(source, name)
            for name in RUNTIME:
                # Linux flock is advisory; stale process pins still must not
                # enter archives even though content reads would succeed.
                held = stack.enter_context(put(source, name, b'').open('r+b'))
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with core.locked(source) as anchor:
                self.assertEqual(set(core.scan(anchor)), expected)
            stack.close()  # End the live-lock check before parent-substitution controls.
            for generation in GENERATIONS:
                leases = source / generation / '.leases'
                saved = leases.with_name('saved-leases')
                leases.rename(saved)
                leases.symlink_to(saved, target_is_directory=True)
                try:
                    with core.locked(source) as anchor:
                        with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                            core.scan(anchor)
                finally:
                    leases.unlink()
                    saved.rename(leases)
            unexpected = source / GENERATIONS[0] / '.leases/not-a-runtime-lease'
            unexpected.symlink_to(source / 'tools/synthetic-tool')
            with core.locked(source) as anchor:
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    core.scan(anchor)


if __name__ == '__main__':
    unittest.main()
