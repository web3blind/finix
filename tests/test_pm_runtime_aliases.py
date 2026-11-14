"""PM's direct-root virtualenv aliases are not profile payload or authority.

Layout contracts: SDK pm.runtime.prepare_runtime, pm.runtime_stage.stage_runtime,
pm.environment.PythonEnvironment.create (uv venv --relocatable).
"""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
import uuid

from finix import core, streaming


INSTALL = '0123456789abcdef'
GENERATION = '0123456789abcdef0123456789abcdef'
PREFIX = f'installs/{INSTALL}/pm-runtime/generations/{GENERATION}'
INPUTS = 'a' * 64
HOME = '/synthetic-owner/tools/python-3.14.9+20261021-linux-x64/bin'
ALIASES = {'lib64': 'lib', 'bin/python': HOME + '/python3',
           'bin/python3': 'python', 'bin/python3.14': 'python'}


def put(root, name, data=b'synthetic user data'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@contextmanager
def fixture():
    base = core.private_dir(Path(tempfile.gettempdir()) / ('fx-pm-' + uuid.uuid4().hex))
    try:
        yield base, core.private_dir(base / 'source')
    finally:
        shutil.rmtree(base)


def runtime(source, prefix=PREFIX):
    put(source, prefix + '/pm-runtime.json', json.dumps({'inputs': INPUTS}).encode())
    put(source, prefix + '/.lease-managed', b'')
    put(source, prefix + '/pyvenv.cfg', (
        f'home = {HOME}\nimplementation = CPython\nuv = 0.13.5\n'
        'version_info = 3.14.9\ninclude-system-site-packages = false\nrelocatable = true\n'
    ).encode())
    put(source, prefix + '/lib/python3.14/site-packages/synthetic.py', b'# inert bytes\n')
    (source / prefix / 'bin').mkdir(exist_ok=True)
    for name, target in ALIASES.items():
        (source / prefix / name).symlink_to(target, target_is_directory=name == 'lib64')


def scan(source):
    with core.locked(source) as anchor:
        return core.scan(anchor)


class PMRuntimeRegularPayloadTests(unittest.TestCase):
    def test_alias_names_are_not_path_exclusions_or_regular_file_omissions(self):
        with fixture() as (base, source):
            names = [PREFIX + '/' + name for name in ALIASES]
            names += ['installs/user-data', 'tools/user-data', PREFIX + '/notes.txt',
                      PREFIX.replace('pm-runtime/', 'pm-runtime-user/') + '/bin/python',
                      PREFIX.replace('/generations/', '/generations-user/') + '/lib64',
                      PREFIX.replace(INSTALL, 'g' * 16) + '/bin/python',
                      'tools/' + PREFIX + '/bin/python', 'nested/' + PREFIX + '/lib64']
            for name in names:
                self.assertFalse(core.excluded(name))
                self.assertTrue(core.allowed(name))
                put(source, name)
            output = io.BytesIO()
            with core.locked(source) as anchor:
                self.assertEqual(set(core.scan(anchor)), set(names))
                streaming.write_archive(anchor, output, core.private_dir(base / 'staging'))
            self.assertEqual(core.validate_archive(output.getvalue()),
                             {name: b'synthetic user data' for name in names})

    @unittest.skipUnless(os.name == 'nt', 'native Windows reparse rejection')
    def test_windows_canonical_alias_name_junction_is_still_rejected(self):
        import subprocess
        with fixture() as (base, source):
            target = core.private_dir(base / 'target')
            put(target, 'user-data')
            parent = core.private_dir(source / PREFIX)
            junction = parent / 'lib64'
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaisesRegex(core.FinixError, 'reparse'):
                    scan(source)
                self.assertEqual((target / 'user-data').read_bytes(), b'synthetic user data')
            finally:
                junction.rmdir()


@unittest.skipUnless(os.name == 'posix', 'native Linux symlink and descriptor contract')
class PMRuntimeAliasTests(unittest.TestCase):
    def test_generation_and_bin_replacement_cannot_supply_alias_metadata(self):
        from unittest.mock import patch
        for member in ('', '/bin'):
            with self.subTest(member=member), fixture() as (base, source):
                runtime(source)
                generation = source / PREFIX
                original = core._pm_runtime_alias
                fired = []
                def substitute(*args, **kwargs):
                    if not fired:
                        fired.append(True)
                        path = source / (PREFIX + member)
                        path.rename(base / 'detached')
                        if member:
                            path.mkdir()
                            for name, target in ALIASES.items():
                                if name.startswith('bin/'):
                                    (path / name[4:]).symlink_to(target)
                        else:
                            runtime(source)
                    return original(*args, **kwargs)
                with patch.object(core, '_pm_runtime_alias', substitute):
                    with self.assertRaises(core.FinixError):
                        scan(source)
                self.assertTrue(fired)

    def test_malformed_generation_rename_restore_schedule_rejects_at_safety_boundary(self):
        # Parent's independent oracle, including its permissive wrapper signature.
        from unittest.mock import patch
        with fixture() as (base, source):
            runtime(source)
            generation = source / PREFIX
            (generation / 'pyvenv.cfg').write_bytes(b'malformed metadata\n')
            with self.assertRaises(core.FinixError):
                scan(source)
            original_open, original_classifier = os.open, core._pm_runtime_alias
            moved, restored = [], []
            detached, replacement = base / 'detached', base / 'replacement'
            def replaced(path, flags, mode=0o777, *, dir_fd=None):
                if path == 'installs' and dir_fd is not None and not moved:
                    moved.append(True)
                    generation.rename(detached)
                    runtime(source)
                return original_open(path, flags, mode, dir_fd=dir_fd)
            def classified(*args, **kwargs):
                accepted = original_classifier(*args, **kwargs)
                if accepted and args[2] == PREFIX + '/lib64':
                    generation.rename(replacement)
                    detached.rename(generation)
                    restored.append(True)
                return accepted
            with core.locked(source) as anchor, patch.object(core.os, 'open', replaced), \
                    patch.object(core, '_pm_runtime_alias', classified):
                with self.assertRaises(core.FinixError):
                    core.make_archive(anchor)
            self.assertTrue(moved)
            self.assertTrue(restored)

    def test_noncanonical_alias_targets_and_special_files_still_fail(self):
        for leaf in ALIASES:
            for target in ('/etc/passwd', '../python', 'python.bak'):
                with self.subTest(leaf=leaf, target=target), fixture() as (_, source):
                    runtime(source)
                    path = source / PREFIX / leaf
                    path.unlink()
                    path.symlink_to(target)
                    with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                        scan(source)
            for kind in ('fifo', 'socket', 'directory-with-link'):
                with self.subTest(leaf=leaf, kind=kind), fixture() as (_, source):
                    runtime(source)
                    path = source / PREFIX / leaf
                    path.unlink()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound:
                        if kind == 'fifo':
                            os.mkfifo(path)
                        elif kind == 'socket':
                            # Bind through a short relative name: AF_UNIX has a
                            # small pathname limit unrelated to capture safety.
                            previous = os.getcwd()
                            try:
                                os.chdir(path.parent)
                                bound.bind(path.name)
                            finally:
                                os.chdir(previous)
                        else:
                            path.mkdir()
                            (path / 'unsafe').symlink_to('/etc/passwd')
                        with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                            scan(source)

    def test_sibling_and_lookalike_runtime_directories_do_not_gain_exclusions(self):
        prefixes = ['nested/' + PREFIX, PREFIX.replace('installs/', 'tools/', 1),
                    PREFIX.replace(INSTALL, 'g' * 16), PREFIX.replace(INSTALL, INSTALL + '0'),
                    PREFIX.replace(GENERATION, GENERATION[:-1]),
                    PREFIX.replace('pm-runtime/', 'pm-runtime-user/'),
                    PREFIX.replace('/generations/', '/generations-user/'),
                    PREFIX.upper()]
        for prefix in prefixes:
            with self.subTest(prefix=prefix), fixture() as (_, source):
                runtime(source, prefix)
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)
        for leaf in ('bin/python3.13', 'bin/python3.14.bak', 'bin/user-link',
                     'lib64.bak', 'lib/user-link', 'user-link'):
            with self.subTest(leaf=leaf), fixture() as (_, source):
                runtime(source)
                path = source / PREFIX / leaf
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to('python')
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)
        for name in ('ordinary-link', 'installs/user-link', 'tools/user-link'):
            with self.subTest(name=name), fixture() as (_, source):
                runtime(source)
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to('/etc/passwd')
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)

    def test_missing_malformed_or_linked_metadata_does_not_launder_aliases(self):
        for name in ('pm-runtime.json', 'pyvenv.cfg', '.lease-managed'):
            for change in ('missing', 'symlink', 'fifo', 'malformed', 'oversize'):
                with self.subTest(name=name, change=change), fixture() as (base, source):
                    runtime(source)
                    path = source / PREFIX / name
                    data = path.read_bytes()
                    path.unlink()
                    if change == 'symlink':
                        outside = put(base, 'outside-metadata', data)
                        path.symlink_to(outside)
                    elif change == 'fifo':
                        os.mkfifo(path)
                    elif change == 'malformed':
                        path.write_bytes(b'{not valid metadata')
                    elif change == 'oversize':
                        path.write_bytes(b'x' * 16385)
                    with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                        scan(source)
        for old, new in ((HOME, '/arbitrary/interpreter/bin'),
                         ('version_info = 3.14.9', 'version_info = 3.13.9'),
                         ('relocatable = true', 'relocatable = false'),
                         ('include-system-site-packages = false', 'include-system-site-packages = true'),
                         ('implementation = CPython', 'implementation = Unknown'),
                         ('home = ' + HOME, 'home = /evil\nhome = ' + HOME)):
            with self.subTest(change=(old, new)), fixture() as (_, source):
                runtime(source)
                config = source / PREFIX / 'pyvenv.cfg'
                config.write_text(config.read_text().replace(old, new))
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)

        # Even mutually consistent forged config + link cannot select an
        # arbitrary external interpreter; metadata never widens the layout.
        with fixture() as (_, source):
            runtime(source)
            config = source / PREFIX / 'pyvenv.cfg'
            config.write_text(config.read_text().replace(HOME, '/arbitrary/interpreter/bin'))
            alias = source / PREFIX / 'bin/python'
            alias.unlink()
            alias.symlink_to('/arbitrary/interpreter/bin/python3')
            with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                scan(source)

    def test_symlinked_generation_ancestors_are_still_rejected(self):
        for name in ('installs', 'installs/' + INSTALL, PREFIX.rsplit('/', 1)[0], PREFIX,
                     PREFIX + '/bin'):
            with self.subTest(name=name), fixture() as (base, source):
                runtime(source)
                original = source / name
                saved = base / 'outside-tree'
                original.rename(saved)
                original.symlink_to(saved, target_is_directory=True)
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)

    def test_alias_link_is_rechecked_after_classification(self):
        from unittest.mock import patch
        with fixture() as (_, source):
            runtime(source)
            path = source / PREFIX / 'bin/python'
            original = os.readlink
            def changed(name, *, dir_fd=None):
                target = original(name, dir_fd=dir_fd)
                if name == 'python':
                    path.unlink()
                    path.symlink_to('/etc/passwd')
                return target
            with patch.object(core.os, 'readlink', changed):
                with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                    scan(source)

    @unittest.skipUnless(os.environ.get('FINIX_TEST_SDK_ROOT')
                         and os.environ.get('FINIX_TEST_UV')
                         and os.environ.get('FINIX_TEST_STORE_PYTHON'),
                         'explicit read-only SDK and pinned offline tooling required')
    def test_actual_sdk_uv_created_aliases_capture_without_target_reads(self):
        import sys
        from unittest.mock import patch
        sdk = Path(os.environ['FINIX_TEST_SDK_ROOT'])
        with fixture() as (base, source):
            with patch.dict(os.environ, HERMES_HOME=str(source),
                            HERMES_DISABLE_LAZY_INSTALLS='1', TMPDIR=str(base)):
                sys.path.insert(0, str(sdk))
                try:
                    from pm.environment import PythonEnvironment
                    from pm.environments import install_key
                finally:
                    sys.path.remove(str(sdk))
            prefix = PREFIX.replace(INSTALL, install_key(sdk))
            generation = source / prefix
            env = dict(os.environ, HERMES_HOME=str(source),
                       HERMES_DISABLE_LAZY_INSTALLS='1', UV_PYTHON_DOWNLOADS='never',
                       TMPDIR=str(base))
            PythonEnvironment(uv=Path(os.environ['FINIX_TEST_UV']),
                              python=Path(os.environ['FINIX_TEST_STORE_PYTHON']),
                              destination=generation, cache=base / 'uv-cache',
                              env=env, offline=True, no_config=True).create()
            # Publication records have precisely the producer's schema. This
            # fixture creates a venv, not a prepared dependency graph or SDK install.
            put(source, prefix + '/pm-runtime.json', json.dumps({'inputs': INPUTS}).encode())
            put(source, prefix + '/.lease-managed', b'')
            put(source, prefix + '/lib/synthetic-user-note', b'inert user data')
            aliases = {str(p.relative_to(generation)): os.readlink(p)
                       for p in generation.rglob('*') if p.is_symlink()}
            self.assertEqual(set(aliases), set(ALIASES))
            baseline = os.environ.get('FINIX_TEST_BASELINE_CORE')
            if baseline:
                import runpy
                old = runpy.run_path(baseline, run_name='finix.core')
                with core.locked(source) as anchor:
                    with self.assertRaisesRegex(old['FinixError'], 'symlink or special file'):
                        old['scan'](anchor)
            expected = {str(p.relative_to(source)) for p in source.rglob('*')
                        if p.is_file() and not p.is_symlink()}
            # No target open is needed, even when bin/python names a real SDK
            # interpreter. A wrapper permits only descriptor-relative source opens.
            original = os.open
            def source_only(path, flags, mode=0o777, *, dir_fd=None):
                if dir_fd is None and str(path) != str(source):
                    raise AssertionError('alias target opened: ' + str(path))
                return original(path, flags, mode, dir_fd=dir_fd)
            with core.locked(source) as anchor, patch.object(core.os, 'open', source_only):
                self.assertEqual(set(core.scan(anchor)), expected)
            output = io.BytesIO()
            with core.locked(source) as anchor:
                streaming.write_archive(anchor, output, core.private_dir(base / 'staging'))
            entries = core.validate_archive(output.getvalue())
            self.assertEqual(set(entries), expected)
            for name, data in entries.items():
                self.assertEqual(data, (source / name).read_bytes())
            self.assertEqual({str(p.relative_to(generation)): os.readlink(p)
                              for p in generation.rglob('*') if p.is_symlink()}, aliases)

    def test_duplicate_runtime_marker_is_not_an_alias_authority(self):
        with fixture() as (_, source):
            runtime(source)
            put(source, PREFIX + '/pm-runtime.json',
                ('{"inputs":"' + 'b' * 64 + '","inputs":"' + INPUTS + '"}').encode())
            with self.assertRaisesRegex(core.FinixError, 'symlink or special file'):
                scan(source)

    def test_canonical_runtime_capture_keeps_regular_payload_and_user_data(self):
        with fixture() as (base, source):
            runtime(source)
            users = ['installs/notes.txt', 'tools/user-note.txt', PREFIX + '/notes.txt',
                     PREFIX + '/bin/my-script', PREFIX + '/lib/user-note.txt',
                     PREFIX + '/.leases/notes.txt', PREFIX + '/bin/python3.14.bak']
            for name in users:
                put(source, name)
            expected = {str(p.relative_to(source)) for p in source.rglob('*')
                        if p.is_file() and not p.is_symlink()}
            output = io.BytesIO()
            with core.locked(source) as anchor:
                self.assertEqual(set(core.scan(anchor)), expected)
                streaming.write_archive(anchor, output, core.private_dir(base / 'staging'))
            output.seek(0)
            payload, count, _, completion = streaming.restore_stream(output, core.private_dir(base / 'readback'))
            self.assertTrue(completion['complete'])
            self.assertEqual(count, len(expected))
            self.assertEqual({str(p.relative_to(payload)) for p in payload.rglob('*') if p.is_file()}, expected)
            for name in expected:
                self.assertEqual((payload / name).read_bytes(), (source / name).read_bytes())
            for name, target in ALIASES.items():
                self.assertEqual(os.readlink(source / PREFIX / name), target)


if __name__ == '__main__':
    unittest.main()
