"""Packaging contracts; no provider executable or account is used."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('assemble', Path(__file__).with_name('assemble.py'))
a = importlib.util.module_from_spec(spec)
if spec.loader and Path(spec.origin).exists():
    spec.loader.exec_module(a)

class ReleaseTests(unittest.TestCase):
    def test_approved_mit_license_and_metadata_are_required(self):
        import json
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        a.check_license(entries)
        for name in ('LICENSE', 'pyproject.toml', 'package.json', 'package-lock.json'):
            invalid = dict(entries)
            invalid[name] = b'{}' if name.endswith('.json') else b'invalid'
            with self.subTest(name=name), self.assertRaises(ValueError):
                a.check_license(invalid)

    def test_approved_license_line_endings_are_explicit_and_not_rewritten(self):
        import json
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        canonical = entries['LICENSE'].replace(b'\r\n', b'\n')
        for license_bytes in (canonical, canonical.replace(b'\n', b'\r\n')):
            candidate = dict(entries, LICENSE=license_bytes)
            before = dict(candidate)
            a.check_license(candidate)
            self.assertEqual(candidate, before)
        with self.assertRaises(ValueError):
            a.check_license(dict(entries, LICENSE=canonical + b' '))

    def test_build_materializes_without_modifying_reviewed_inputs(self):
        import importlib.util
        from unittest.mock import patch
        import sys
        with patch.dict(sys.modules, {'assemble': a}):
            spec = importlib.util.spec_from_file_location('release_build', Path(__file__).with_name('build.py'))
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
        entries = {'desktop/plugin.js': b'old bundle', 'README.md': b'reviewed'}
        original = dict(entries)
        with tempfile.TemporaryDirectory() as d:
            result = build.materialize(entries, {'desktop/plugin.js': b'rebuilt'}, b'notices', Path(d))
            self.assertEqual(entries, original)
            self.assertEqual((Path(d) / 'desktop/plugin.js').read_bytes(), b'rebuilt')
            self.assertEqual(result['THIRD-PARTY-NOTICES.md'], b'notices')
            self.assertEqual(result['README.md'], b'reviewed')

    def test_native_payload_matches_explicit_wheel_desktop_sources(self):
        import json
        import tomllib
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        native = a.native_payload(entries)
        metadata = tomllib.loads(entries['pyproject.toml'].decode())
        for files in metadata['tool']['setuptools']['data-files'].values():
            for name in files:
                if '*' not in name:
                    self.assertEqual(native[name], entries[name], name)
        self.assertNotIn('scripts/release/build.py', native)
        self.assertNotIn('package-lock.json', native)

    def test_archived_native_payload_contains_every_declared_data_file(self):
        import json
        import tomllib
        import zipfile
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        native = a.native_payload(entries)
        metadata = tomllib.loads(entries['pyproject.toml'].decode())
        required = {name for files in metadata['tool']['setuptools']['data-files'].values()
                    for name in files if '*' not in name}
        self.assertFalse(required - set(native), 'Release selection dropped declared wheel inputs')
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'native.zip'
            a.archive(archive, native)
            a.inspect_archive(archive)
            with zipfile.ZipFile(archive) as package:
                self.assertFalse(required - set(package.namelist()))
                for name in required:
                    self.assertEqual(package.read(name), entries[name], name)

    def test_fresh_native_payload_declares_wheel_runtime_requirements(self):
        import json
        import tomllib
        import yaml
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        with tempfile.TemporaryDirectory() as d:
            package = Path(d) / 'native.zip'
            a.archive(package, a.native_payload(entries))
            import zipfile
            with zipfile.ZipFile(package) as z:
                z.extractall(Path(d) / 'fresh')
            fresh = Path(d) / 'fresh'
            # Native installs have no old pyproject left over to mask omissions.
            self.assertFalse((fresh / 'pyproject.toml').exists())
            declared = yaml.safe_load((fresh / 'plugin.yaml').read_text())
            expected = tomllib.loads(entries['pyproject.toml'].decode())['project']['dependencies']
            self.assertEqual(declared.get('python_dependencies', []), expected)

    def test_native_payload_is_admitted_by_installed_pm_reader(self):
        import json
        import tomllib
        import os
        import sys
        host = os.environ.get('FINIX_TEST_PM_SOURCE')
        if not host:
            self.skipTest('Set FINIX_TEST_PM_SOURCE to the supported installed Hermes source')
        sys.path.insert(0, host)
        from pm.plugin_declarations import read_python_declaration
        from pm.plugin_inputs import Members, encode, decode
        from pm.workspace import member_sources, _workspace_member
        root = Path(__file__).resolve().parents[2]
        entries = a.select(root, json.loads((root / 'scripts/release/allowlist.json').read_text()))
        expected = tuple(tomllib.loads(entries['pyproject.toml'].decode())['project']['dependencies'])
        with tempfile.TemporaryDirectory() as d:
            fresh = Path(d) / 'fresh'
            fresh.mkdir()
            for name, data in a.native_payload(entries).items():
                target = fresh / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            identity = Path(d) / 'plugins' / 'finix'
            # Map the installed identity to ONLY the unpacked candidate payload.
            sources = member_sources(decode(encode(Members({identity: fresh}))).dirs)
            self.assertEqual(sources, {identity.resolve(): fresh.resolve()})
            declaration = read_python_declaration(sources[identity.resolve()])
            self.assertEqual(declaration.install_requirements, expected)
            self.assertTrue(declaration.installs_packages)
            self.assertEqual(declaration.files, (fresh.resolve() / 'plugin.yaml',))
            build = Path(d) / 'workspace'
            build.mkdir()
            member = _workspace_member(fresh, build, identity=identity)
            document = tomllib.loads((member / 'pyproject.toml').read_text())
            self.assertEqual(tuple(document['project']['dependencies']), expected)
            self.assertFalse(document['tool']['uv']['package'])

    def test_all_first_party_runtime_modules_are_explicit_release_inputs(self):
        import json
        root = Path(__file__).resolve().parents[2]
        # No intentional runtime exclusions. New modules must be reviewed and
        # explicitly allowlisted, never silently discovered by the assembler.
        intentional_exclusions = set()
        runtime = {p.relative_to(root).as_posix() for p in (root / 'finix').rglob('*.py')}
        names = set(json.loads((root / 'scripts/release/allowlist.json').read_text()))
        self.assertFalse(intentional_exclusions - runtime, 'Stale intentional exclusions')
        self.assertEqual(runtime - intentional_exclusions, {n for n in names if n.startswith('finix/') and n.endswith('.py')})

    def test_allowlist_does_not_discover_new_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'README.md').write_text('public')
            (p / 'rclone.conf').write_text('private')
            self.assertEqual(a.select(p, ['README.md']), {'README.md': b'public'})

    def test_access_time_is_not_a_source_mutation(self):
        from unittest.mock import patch
        import os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            file = p / 'README.md'
            file.write_text('public')
            original = Path.lstat
            calls = []
            def changed_atime(path, *args, **kwargs):
                observed = original(path, *args, **kwargs)
                if path == file:
                    calls.append(True)
                    from types import SimpleNamespace
                    values = {n: getattr(observed, n) for n in dir(observed) if n.startswith('st_')}
                    values['st_atime'] += len(calls)
                    return SimpleNamespace(**values)
                return observed
            with patch.object(Path, 'lstat', changed_atime):
                self.assertEqual(a.select(p, ['README.md']), {'README.md': b'public'})

    def test_personal_path_and_secret_blocked(self):
        for payload in [b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'alice' + bytes([92]) + b'secret',
                        b'/home/' + b'alice/private',
                        b'AGE-SECRET-KEY-1' + b'A' * 58,
                        b'-----BEGIN ' + b'PRIVATE KEY-----', b'ghp_' + b'a' * 36,
                        b'"refresh_' + b'token": "real-secret-value"']:
            with self.subTest(payload=payload[:15]), self.assertRaises(ValueError):
                a.scan('public.txt', payload)

    def test_link_and_reparse_blocked(self):
        from types import SimpleNamespace
        self.assertTrue(a.unsafe_stat(SimpleNamespace(st_mode=0o120777, st_file_attributes=0)))
        self.assertTrue(a.unsafe_stat(SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400)))

    def test_actual_hardlink_is_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'README.md').write_text('public')
            os.link(p / 'README.md', p / 'other.md')
            with self.assertRaisesRegex(ValueError, 'single-link'):
                a.select(p, ['README.md'])

    def test_actual_symlink_is_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'actual.md').write_text('public')
            try:
                os.symlink(p / 'actual.md', p / 'README.md')
            except OSError:
                self.skipTest('OS does not permit symlink creation in this isolated fixture')
            with self.assertRaisesRegex(ValueError, 'Link/reparse'):
                a.select(p, ['README.md'])

    def test_archive_is_deterministic_and_inspected(self):
        with tempfile.TemporaryDirectory() as d:
            x, y = Path(d) / 'x.zip', Path(d) / 'y.zip'
            entries = {'README.md': b'public', 'finix/test.py': b'pass\n'}
            a.archive(x, entries)
            a.archive(y, dict(reversed(list(entries.items()))))
            self.assertEqual(x.read_bytes(), y.read_bytes())
            self.assertEqual(a.inspect_archive(x), 2)

    def test_publication_gate_is_closed(self):
        import contextlib
        import io
        from unittest.mock import patch
        text = io.StringIO()
        with patch('sys.argv', ['assemble.py', '--publish-check']), contextlib.redirect_stderr(text):
            with self.assertRaises(SystemExit) as caught:
                a.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('PUBLICATION BLOCKED', text.getvalue())

    def test_noncanonical_windows_archive_names_are_rejected(self):
        for name in ['', '.', 'a//b', './a', 'a/', 'a./b', 'a /b', 'CON', 'nul.txt', 'a/COM1.py', 'a\x00b']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                a.scan(name, b'public')

    def test_allowlist_case_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'README.md').write_text('public')
            with self.assertRaises(ValueError):
                a.select(p, ['README.md', 'README.md'])
            with self.assertRaises(ValueError):
                a.select(p, ['README.md', 'readme.md'])

    def test_wheel_mit_headers_accept_crlf_and_reject_body_spoof(self):
        self.assertTrue(callable(getattr(a, 'check_wheel_license', None)))
        for newline in (b'\n', b'\r\n'):
            a.check_wheel_license(newline.join([b'Metadata-Version: 2.4', b'License-Expression: MIT', b'License-File: LICENSE', b'', b'body']))
        with self.assertRaises(ValueError):
            a.check_wheel_license(b'Metadata-Version: 2.4\n\nLicense-Expression: MIT\nLicense-File: LICENSE\n')

    def test_windows_wheel_permissions_normalized_without_payload_changes(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'built.whl'
            info = zipfile.ZipInfo('module.py')
            info.create_system = 3
            info.external_attr = 0o100666 << 16
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr(info, b'pass\n')
            self.assertTrue(callable(getattr(a, 'normalize_wheel', None)))
            a.normalize_wheel(p)
            self.assertEqual(a.inspect_archive(p), 1)
            with zipfile.ZipFile(p) as z:
                self.assertEqual(z.read('module.py'), b'pass\n')
                self.assertEqual(z.getinfo('module.py').external_attr >> 16, 0o100644)

    def test_archive_special_file_mode_is_rejected(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'bad.zip'
            info = zipfile.ZipInfo('pipe')
            info.create_system = 3
            info.external_attr = 0o010644 << 16
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr(info, b'public')
            with self.assertRaises(ValueError):
                a.inspect_archive(p)

    def test_private_name_and_traversal_blocked(self):
        for name in ['../evil', 'rclone.conf', '.env', '.linux-test-bin/tool', 'cache/data', 'x.key']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                a.scan(name, b'public')

if __name__ == '__main__':
    unittest.main()
