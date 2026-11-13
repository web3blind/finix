"""Real CPython isolated-import cache admission; no host or restore launch.

Run with FINIX_BYTECODE_TEST_SCOPE pointing to a fresh private fixture directory.
The optional FINIX_BYTECODE_TEST_DEPS adds an already installed PyYAML location
only to the isolated diagnostic child, not to any product launcher.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid

from finix import core, default_recovery_lifecycle as life


class CurrentBytecodeTests(unittest.TestCase):
    def setUp(self):
        scope = Path(os.environ['FINIX_BYTECODE_TEST_SCOPE'])
        self.root = core.private_dir(scope / uuid.uuid4().hex)
        self.installed = core.private_dir(self.root / 'plugins/finix')
        package = Path(life.__file__).parent.parent
        names = ['__init__.py', 'plugin.yaml', 'dashboard/plugin_api.py']
        names += ['finix/' + p.name for p in (package / 'finix').glob('*.py')]
        for name in names:
            target = self.installed / name
            core.private_dir(target.parent)
            shutil.copyfile(package / name, target)
        core.atomic_json(self.root / 'config.yaml', {'plugins': {'enabled': ['finix']}})
        self.pins = life._current_finix(self.root)
        paths = [str(self.installed)]
        if os.environ.get('FINIX_BYTECODE_TEST_DEPS'):
            paths.append(os.environ['FINIX_BYTECODE_TEST_DEPS'])
        script = ('import sys; sys.path[:0]=' + repr(paths) + '; '
                  'assert sys.flags.isolated == 1; assert not sys.dont_write_bytecode; '
                  'import finix.default_recovery_api; print("ISOLATED_IMPORT_COMPLETE")')
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-I', '-c', script], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ISOLATED_IMPORT_COMPLETE')
        self.source = self.installed / 'finix/default_recovery_api.py'
        self.cache = Path(importlib.util.cache_from_source(str(self.source)))
        self.assertTrue(self.cache.is_file())
        # Keep exactly one real cache to target each negative independently.
        for path in self.installed.rglob('*.pyc'):
            if path != self.cache and self._testMethodName != 'test_real_isolated_import_cache_remains_admitted':
                path.unlink()

    def refused(self):
        with self.assertRaises((life.RecoveryError, core.FinixError, OSError)):
            life._current_finix(self.root)

    def test_real_isolated_import_cache_remains_admitted(self):
        try:
            admitted = life._current_finix(self.root)
        except life.RecoveryError as error:
            self.fail('Unchanged pinned source with real -I cache refused: ' + error.code)
        self.assertEqual(admitted, self.pins)

    def test_trusted_compile_serialization_variants_compare_without_loading(self):
        import marshal
        retained = compile(self.source.read_bytes(), str(self.source), 'exec', dont_inherit=True)
        compiled = compile(self.source.read_bytes(), str(self.source), 'exec', dont_inherit=True)
        self.assertTrue(life._cache_code_matches(self.cache.read_bytes()[16:], marshal.dumps(compiled)))
        self.assertIsNotNone(retained)

    def test_timestamp_matching_forgery_refused(self):
        import marshal
        original = self.cache.read_bytes()
        forged = compile('raise RuntimeError("must not execute")', str(self.source),
                         'exec', dont_inherit=True)
        self.cache.write_bytes(original[:16] + marshal.dumps(forged))
        self.refused()

    def test_trailing_bytes_refused(self):
        self.cache.write_bytes(self.cache.read_bytes() + b'trailing')
        self.refused()

    def test_hardlink_refused(self):
        os.link(self.cache, self.root / 'second-link')
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX symlink; Windows uses junction case')
    def test_cache_symlink_refused(self):
        retained = self.root / 'retained.pyc'
        self.cache.rename(retained)
        self.cache.symlink_to(retained)
        self.refused()

    @unittest.skipIf(os.name == 'nt', 'POSIX cache-directory symlink')
    def test_cache_directory_symlink_refused(self):
        retained = self.root / 'retained-cache'
        self.cache.parent.rename(retained)
        self.cache.parent.symlink_to(retained, target_is_directory=True)
        try:
            self.refused()
        finally:
            self.cache.parent.unlink()

    @unittest.skipUnless(os.name == 'nt', 'native Windows reparse point')
    def test_cache_directory_junction_refused(self):
        retained = self.root / 'retained-cache'
        self.cache.parent.rename(retained)
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.cache.parent),
                                 str(retained)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            self.refused()
        finally:
            self.cache.parent.rmdir()

    def test_untrusted_cache_ancestor_refused(self):
        if os.name == 'nt':
            result = subprocess.run(['icacls', str(self.cache.parent), '/grant',
                                     '*S-1-1-0:(WDAC)'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.cache.parent.chmod(0o775)
        self.refused()

    def test_unsupported_flags_and_malformed_cache_refused(self):
        original = self.cache.read_bytes()
        for replacement in (original[:4] + b'\x03\0\0\0' + original[8:],
                            original[:16] + b'r\xff\xff\xff\x7f',
                            original[:16] + b's\xff\xff\xff\x7f'):
            with self.subTest(prefix=replacement[16:21].hex()):
                self.cache.write_bytes(replacement)
                self.refused()
        self.cache.write_bytes(original)
        self.assertEqual(life._current_finix(self.root), self.pins)

    @unittest.skipUnless(os.name == 'nt', 'Windows loader filename spelling')
    def test_windows_loader_case_spelling_requires_same_native_source(self):
        import marshal
        from unittest.mock import patch
        filename = os.path.normcase(str(self.source))
        self.assertNotEqual(filename, str(self.source))
        original = self.cache.read_bytes()
        code = compile(self.source.read_bytes(), filename, 'exec', dont_inherit=True,
                       optimize=sys.flags.optimize)
        self.cache.write_bytes(original[:16] + marshal.dumps(code))
        self.assertEqual(life._current_finix(self.root), self.pins)
        with patch('os.path.samefile', return_value=False):
            self.refused()

    def test_cache_byte_bound_refused(self):
        with self.cache.open('r+b') as stream:
            stream.truncate(16*1024*1024+1)
        self.refused()

    def test_changed_source_refused(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n# changed source\n')
        self.refused()

    def test_unknown_cache_refused(self):
        (self.installed / 'unknown.pyc').write_bytes(self.cache.read_bytes())
        self.refused()


class CacheRepresentationTests(unittest.TestCase):
    def setUp(self):
        import marshal
        self.code = compile('def f(x):\n return x in {"ready", "done"}\n', '<pinned>', 'exec', dont_inherit=True)
        self.expected = marshal.dumps(self.code)

    def test_comparator_never_uses_marshal_loading(self):
        from unittest.mock import patch
        with patch('marshal.load', side_effect=AssertionError('forbidden')), \
                patch('marshal.loads', side_effect=AssertionError('forbidden')):
            self.assertTrue(life._cache_code_matches(self.expected, self.expected))

    def test_malformed_forms_and_resource_attacks_refused(self):
        attacks = [b'', b'?', b'r\0\0\0\0', b'r\xff\xff\xff\xff',
                   b's\xff\xff\xff\x7f', b'(\xff\xff\xff\x7f',
                   b'\xa9\x01r\0\0\0\0', b')\x01'*129 + b'N',
                   self.expected + b'N', self.expected[:-1]]
        for attack in attacks:
            with self.subTest(prefix=attack[:12].hex()):
                self.assertFalse(life._cache_code_matches(attack, self.expected))

    def test_code_filename_and_constant_bits_are_pinned(self):
        import marshal
        self.assertFalse(life._cache_code_matches(marshal.dumps(self.code.replace(co_filename='<foreign>')), self.expected))
        for first, second in ((0.0, -0.0), (1, True), (b'a', 'a'), ((1,), frozenset({1}))):
            with self.subTest(kind=type(first).__name__):
                self.assertFalse(life._cache_code_matches(marshal.dumps(first), marshal.dumps(second)))

    def test_frozenset_serialization_order_is_irrelevant(self):
        a = b'>\x02\0\0\0i\x01\0\0\0i\x02\0\0\0'
        b = b'>\x02\0\0\0i\x02\0\0\0i\x01\0\0\0'
        self.assertTrue(life._cache_code_matches(a, b))

    def test_singleton_reference_flags_are_refused(self):
        # CPython's singleton branches do not register FLAG_REF. Treating these
        # as reference entries would make our table diverge from the importer.
        for tag in b'NFT.':
            self.assertFalse(life._cache_code_matches(bytes([tag | 128]), bytes([tag])))

    def test_surrogate_string_constants_remain_admitted(self):
        import marshal
        serialized = marshal.dumps('\ud800')
        self.assertTrue(life._cache_code_matches(serialized, serialized))

    def test_repeated_reference_dag_is_bounded(self):
        # A root tuple contains successively shared subtuples, not trailing data.
        attack = b'(' + (121).to_bytes(4, 'little') + b'\xa9\x00'
        for index in range(120):
            attack += b'\xa9\x02r' + index.to_bytes(4, 'little') + b'r' + index.to_bytes(4, 'little')
        self.assertFalse(life._cache_code_matches(attack, self.expected))


if __name__ == '__main__':
    unittest.main()
